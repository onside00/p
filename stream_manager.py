import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import psutil


class StreamManager:
    """Stable one-process streaming manager.

    One stream = one FFmpeg process:
        source -> filters -> H.264/AAC encode ONCE -> tee muxer -> FIFO per output

    There is no Python MPEG-TS fan-out and no extra FFmpeg copy process per
    destination. A slow/dead destination is isolated by FFmpeg's FIFO muxer.
    """

    ENCODER_RESTART_DELAY = 1.5
    FAST_STOP_GRACE = 1.0
    INPUT_RW_TIMEOUT_US = 15_000_000

    FIFO_OPTIONS = (
        "queue_size=2000:"
        "drop_pkts_on_overflow=1:"
        "attempt_recovery=1:"
        "recover_any_error=1:"
        "recovery_wait_time=1:"
        "max_recovery_attempts=0:"
        "restart_with_keyframe=1"
    )

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        self.config_file = self.data_dir / "streams.json"

        self.streams: Dict[str, dict] = {}
        self.runtimes: Dict[str, dict] = {}
        self.logs: Dict[str, deque] = {}
        self.lock = threading.RLock()

        self._net_lock = threading.Lock()
        self._last_net_at = time.monotonic()
        try:
            io = psutil.net_io_counters()
            self._last_net_sent = int(io.bytes_sent)
            self._last_net_recv = int(io.bytes_recv)
        except Exception:
            self._last_net_sent = 0
            self._last_net_recv = 0
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        self._load()

    # ------------------------------------------------------------------
    # persistence / normalization
    # ------------------------------------------------------------------
    @staticmethod
    def defaults() -> dict:
        return {
            "name": "New Stream",
            "source": "",
            "quality": "1080p",
            "bitrate": 5000,
            "fps": 50,
            "preset": "superfast",
            "audio_bitrate": 128,
            "logo": "",
            "logo_width": 335,
            "logo_position": "top-right",
            "text": "",
            "text_size": 38,
            "text_position": "bottom-center",
            "destinations": [],
        }

    def _load(self):
        if self.config_file.exists():
            try:
                raw = json.loads(self.config_file.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for item in raw:
                        if isinstance(item, dict) and item.get("id"):
                            self._normalize(item)
                            self.streams[item["id"]] = item
            except Exception:
                self.streams = {}

        for sid in self.streams:
            self.logs[sid] = deque(maxlen=1200)

    def _save(self):
        tmp = self.config_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(list(self.streams.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.config_file)

    def _normalize(self, stream: dict):
        stream["name"] = str(stream.get("name") or "Stream").strip()[:80]
        stream["source"] = str(stream.get("source") or "").strip()

        if stream.get("quality") not in {"original", "1080p", "720p", "480p"}:
            stream["quality"] = "1080p"

        try:
            stream["bitrate"] = max(300, min(30000, int(stream.get("bitrate", 5000))))
        except Exception:
            stream["bitrate"] = 5000

        try:
            stream["fps"] = max(20, min(60, int(round(float(stream.get("fps", 50))))))
        except Exception:
            stream["fps"] = 50

        if stream.get("preset") not in {
            "ultrafast", "superfast", "veryfast", "faster", "fast", "medium"
        }:
            stream["preset"] = "superfast"

        try:
            stream["audio_bitrate"] = max(64, min(320, int(stream.get("audio_bitrate", 128))))
        except Exception:
            stream["audio_bitrate"] = 128

        stream["logo"] = str(stream.get("logo") or "").strip()
        try:
            stream["logo_width"] = max(40, min(900, int(stream.get("logo_width", 335))))
        except Exception:
            stream["logo_width"] = 335

        if stream.get("logo_position") not in {
            "top-left", "top-right", "bottom-left", "bottom-right", "center"
        }:
            stream["logo_position"] = "top-right"

        stream["text"] = str(stream.get("text") or "")[:300]
        try:
            stream["text_size"] = max(12, min(120, int(stream.get("text_size", 38))))
        except Exception:
            stream["text_size"] = 38

        if stream.get("text_position") not in {
            "top-left", "top-center", "top-right", "bottom-left",
            "bottom-center", "bottom-right", "center",
        }:
            stream["text_position"] = "bottom-center"

        # Backward compatibility for the very old single-output config.
        incoming_dests = stream.get("destinations") or []
        if not incoming_dests and stream.get("rtmp_base") and stream.get("stream_key"):
            incoming_dests = [{
                "name": "Telegram",
                "rtmp_base": stream.get("rtmp_base"),
                "stream_key": stream.get("stream_key"),
                "enabled": True,
            }]

        dests = []
        used_ids = set()
        for d in incoming_dests:
            if not isinstance(d, dict):
                continue
            did = str(d.get("id") or uuid.uuid4().hex[:8]).strip()[:40]
            if not did or did in used_ids:
                did = uuid.uuid4().hex[:8]
            used_ids.add(did)
            dests.append({
                "id": did,
                "name": str(d.get("name") or "Telegram").strip()[:60],
                "rtmp_base": str(d.get("rtmp_base") or "").strip(),
                "stream_key": str(d.get("stream_key") or "").strip(),
                "enabled": bool(d.get("enabled", True)),
            })
        stream["destinations"] = dests

        # Do not keep duplicate legacy secrets in newly saved files.
        stream.pop("rtmp_base", None)
        stream.pop("stream_key", None)

    # ------------------------------------------------------------------
    # serialization / metrics
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return default

    def _process_metrics(self, runtime: Optional[dict], stream: dict) -> dict:
        progress = dict((runtime or {}).get("progress") or {})
        proc = (runtime or {}).get("proc")

        cpu = 0.0
        rss_mb = 0.0
        pid = None
        if proc and proc.poll() is None:
            pid = proc.pid
            try:
                p = runtime.get("psproc")
                if not p or p.pid != proc.pid:
                    p = psutil.Process(proc.pid)
                    p.cpu_percent(interval=None)
                    runtime["psproc"] = p
                cpu = max(0.0, float(p.cpu_percent(interval=None)))
                rss_mb = max(0.0, float(p.memory_info().rss) / 1024 / 1024)
            except Exception:
                pass

        enabled_count = sum(
            1 for d in stream.get("destinations", []) if self._destination_is_runnable(d)
        )
        configured_kbps = int(stream.get("bitrate", 0)) + int(stream.get("audio_bitrate", 0))
        egress_mbps = (configured_kbps * enabled_count) / 1000.0

        bitrate = str(progress.get("bitrate") or "").strip()
        if bitrate in {"", "N/A"}:
            bitrate = f"~{configured_kbps}k"

        return {
            "pid": pid,
            "cpu_percent": round(cpu, 1),
            "cpu_cores": round(cpu / 100.0, 2),
            "rss_mb": round(rss_mb, 1),
            "encode_fps": round(self._safe_float(progress.get("fps")), 1),
            "speed": str(progress.get("speed") or "0x"),
            "bitrate": bitrate,
            "frame": int(self._safe_float(progress.get("frame"))),
            "drop_frames": int(self._safe_float(progress.get("drop_frames"))),
            "dup_frames": int(self._safe_float(progress.get("dup_frames"))),
            "restarts": int((runtime or {}).get("restart_count") or 0),
            "estimated_egress_mbps": round(egress_mbps, 2),
        }

    def _serialize_stream(self, stream: dict) -> dict:
        item = json.loads(json.dumps(stream))
        sid = stream["id"]
        runtime = self.runtimes.get(sid)
        active = bool(runtime and not runtime.get("stop_event").is_set())
        proc = runtime.get("proc") if runtime else None
        proc_alive = bool(proc and proc.poll() is None)

        item["running"] = active
        item["runtime_status"] = (runtime or {}).get("status", "stopped") if active else "stopped"
        item["pid"] = proc.pid if proc_alive else None
        item["uptime"] = 0
        if active and runtime.get("started_at"):
            item["uptime"] = max(0, int(time.time() - runtime["started_at"]))
        item["last_error"] = str((runtime or {}).get("last_error") or "")
        item["metrics"] = self._process_metrics(runtime, stream)

        states = (runtime or {}).get("output_states") or {}
        for dest in item.get("destinations", []):
            state = states.get(dest.get("id"), {})
            if not dest.get("enabled"):
                status = "disabled"
            elif not active:
                status = "stopped"
            elif state.get("status"):
                status = state["status"]
            elif proc_alive:
                status = "running"
            else:
                status = "reconnecting"
            dest.update({
                "status": status,
                "retries": int(state.get("retries") or 0),
                "last_error": str(state.get("last_error") or ""),
                "uptime": item["uptime"],
            })
        return item

    def list_streams(self) -> List[dict]:
        with self.lock:
            return [self._serialize_stream(s) for s in self.streams.values()]

    def get_stream(self, stream_id: str) -> Optional[dict]:
        with self.lock:
            stream = self.streams.get(stream_id)
            return self._serialize_stream(stream) if stream else None

    def get_system_stats(self) -> dict:
        try:
            cpu = round(float(psutil.cpu_percent(interval=None)), 1)
        except Exception:
            cpu = 0.0

        try:
            vm = psutil.virtual_memory()
            ram_percent = round(float(vm.percent), 1)
            ram_used_gb = round(float(vm.used) / 1024**3, 2)
            ram_total_gb = round(float(vm.total) / 1024**3, 2)
        except Exception:
            ram_percent = ram_used_gb = ram_total_gb = 0.0

        try:
            disk = psutil.disk_usage(str(self.data_dir))
            disk_percent = round(float(disk.percent), 1)
            disk_free_gb = round(float(disk.free) / 1024**3, 1)
        except Exception:
            disk_percent = disk_free_gb = 0.0

        tx_mbps = rx_mbps = 0.0
        try:
            io = psutil.net_io_counters()
            now = time.monotonic()
            with self._net_lock:
                elapsed = max(0.001, now - self._last_net_at)
                tx_mbps = max(0.0, (io.bytes_sent - self._last_net_sent) * 8 / elapsed / 1_000_000)
                rx_mbps = max(0.0, (io.bytes_recv - self._last_net_recv) * 8 / elapsed / 1_000_000)
                self._last_net_at = now
                self._last_net_sent = int(io.bytes_sent)
                self._last_net_recv = int(io.bytes_recv)
        except Exception:
            pass

        with self.lock:
            active = sum(1 for r in self.runtimes.values() if not r.get("stop_event").is_set())
            ffmpeg_pids = [
                r["proc"].pid for r in self.runtimes.values()
                if r.get("proc") and r["proc"].poll() is None
            ]

        load = [0.0, 0.0, 0.0]
        try:
            load = [round(float(x), 2) for x in os.getloadavg()]
        except Exception:
            pass

        return {
            "cpu_percent": cpu,
            "cpu_count": psutil.cpu_count(logical=True) or 0,
            "ram_percent": ram_percent,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "disk_percent": disk_percent,
            "disk_free_gb": disk_free_gb,
            "network_tx_mbps": round(tx_mbps, 2),
            "network_rx_mbps": round(rx_mbps, 2),
            "active_streams": active,
            "ffmpeg_processes": len(ffmpeg_pids),
            "ffmpeg_pids": ffmpeg_pids,
            "load_average": load,
            "timestamp": int(time.time()),
        }

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def create_stream(self, payload: dict) -> dict:
        with self.lock:
            stream = self.defaults()
            stream.update(payload or {})
            stream["id"] = uuid.uuid4().hex[:12]
            self._normalize(stream)
            self.streams[stream["id"]] = stream
            self.logs[stream["id"]] = deque(maxlen=1200)
            self._save()
            return self._serialize_stream(stream)

    @staticmethod
    def _stream_signature(stream: dict):
        fields = (
            "source", "quality", "bitrate", "fps", "preset", "audio_bitrate",
            "logo", "logo_width", "logo_position", "text", "text_size",
            "text_position", "destinations",
        )
        return tuple(json.dumps(stream.get(k), sort_keys=True, ensure_ascii=False) for k in fields)

    def update_stream(self, stream_id: str, payload: dict) -> dict:
        with self.lock:
            current = self.streams.get(stream_id)
            if not current:
                raise KeyError("Stream not found")

            updated = json.loads(json.dumps(current))
            updated.update(payload or {})
            updated["id"] = stream_id
            self._normalize(updated)

            runtime = self.runtimes.get(stream_id)
            active = bool(runtime and not runtime.get("stop_event").is_set())
            if active and self._stream_signature(updated) != self._stream_signature(current):
                raise RuntimeError(
                    "أوقف البث أولاً قبل تعديل المصدر/المعالجة/المخارج. "
                    "هذا الإصدار يستخدم FFmpeg tee واحد للاستقرار وأقل استهلاك."
                )

            self.streams[stream_id] = updated
            self._save()
            return self._serialize_stream(updated)

    def delete_stream(self, stream_id: str):
        self.stop_stream(stream_id, silent=True)
        with self.lock:
            self.streams.pop(stream_id, None)
            self.logs.pop(stream_id, None)
            try:
                (self.data_dir / f"text_{stream_id}.txt").unlink(missing_ok=True)
            except Exception:
                pass
            self._save()

    # ------------------------------------------------------------------
    # source probe
    # ------------------------------------------------------------------
    @staticmethod
    def _ratio_to_float(value) -> Optional[float]:
        if value in (None, "", "0/0", "N/A"):
            return None
        try:
            text = str(value)
            if "/" in text:
                a, b = text.split("/", 1)
                b = float(b)
                return float(a) / b if b else None
            return float(text)
        except Exception:
            return None

    @staticmethod
    def _to_int(value) -> Optional[int]:
        try:
            if value in (None, "", "N/A"):
                return None
            return int(float(value))
        except Exception:
            return None

    def probe_source(self, source: str) -> dict:
        source = str(source or "").strip()
        if not source:
            raise ValueError("حط رابط المصدر أولاً")

        cmd = [
            "ffprobe", "-v", "error",
            "-rw_timeout", str(self.INPUT_RW_TIMEOUT_US),
            "-analyzeduration", "5000000",
            "-probesize", "5000000",
            "-show_streams", "-show_format",
            "-of", "json",
            source,
        ]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise RuntimeError("فحص المصدر أخذ وقت طويل. تأكد أن الرابط يعمل من الـVPS.")
        except FileNotFoundError:
            raise RuntimeError("ffprobe غير موجود")

        if cp.returncode != 0:
            err = (cp.stderr or cp.stdout or "ffprobe failed").strip()
            raise RuntimeError(err[-1200:])

        try:
            data = json.loads(cp.stdout or "{}")
        except Exception:
            raise RuntimeError("تعذر قراءة نتيجة ffprobe")

        all_streams = data.get("streams") or []
        video = next((s for s in all_streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in all_streams if s.get("codec_type") == "audio"), {})
        fmt = data.get("format") or {}

        fps = self._ratio_to_float(video.get("avg_frame_rate"))
        if not fps or fps < 1:
            fps = self._ratio_to_float(video.get("r_frame_rate"))

        width = self._to_int(video.get("width"))
        height = self._to_int(video.get("height"))
        video_bitrate = self._to_int(video.get("bit_rate"))
        total_bitrate = self._to_int(fmt.get("bit_rate"))
        audio_bitrate = self._to_int(audio.get("bit_rate"))
        bitrate_bps = video_bitrate or total_bitrate
        bitrate_kbps = int(round(bitrate_bps / 1000)) if bitrate_bps else None

        if height and width:
            if height >= 1000:
                quality = "1080p"
            elif height >= 700:
                quality = "720p"
            elif height >= 450:
                quality = "480p"
            else:
                quality = "original"
        else:
            quality = "original"

        recommended_fps = int(round(fps)) if fps else None
        if recommended_fps:
            recommended_fps = max(20, min(60, recommended_fps))

        return {
            "ok": True,
            "source": source,
            "video": {
                "codec": video.get("codec_name") or "unknown",
                "profile": video.get("profile") or "",
                "width": width,
                "height": height,
                "resolution": f"{width}x{height}" if width and height else "unknown",
                "fps": round(fps, 3) if fps else None,
                "pix_fmt": video.get("pix_fmt") or "",
                "bitrate_kbps": bitrate_kbps,
            },
            "audio": {
                "codec": audio.get("codec_name") or "none",
                "sample_rate": self._to_int(audio.get("sample_rate")),
                "channels": self._to_int(audio.get("channels")),
                "bitrate_kbps": int(round(audio_bitrate / 1000)) if audio_bitrate else None,
            },
            "format": {
                "name": fmt.get("format_name") or "",
                "total_bitrate_kbps": int(round(total_bitrate / 1000)) if total_bitrate else None,
            },
            "recommended": {
                "quality": quality,
                "fps": recommended_fps,
                "bitrate": bitrate_kbps,
            },
        }

    # ------------------------------------------------------------------
    # ffmpeg command
    # ------------------------------------------------------------------
    @staticmethod
    def _overlay_xy(position: str):
        m = 30
        mapping = {
            "top-left": (str(m), str(m)),
            "top-right": (f"W-w-{m}", str(m)),
            "bottom-left": (str(m), f"H-h-{m}"),
            "bottom-right": (f"W-w-{m}", f"H-h-{m}"),
            "center": ("(W-w)/2", "(H-h)/2"),
        }
        return mapping.get(position, mapping["top-right"])

    @staticmethod
    def _text_xy(position: str):
        m = 32
        mapping = {
            "top-left": (str(m), str(m)),
            "top-center": ("(w-text_w)/2", str(m)),
            "top-right": (f"w-text_w-{m}", str(m)),
            "bottom-left": (str(m), f"h-text_h-{m}"),
            "bottom-center": ("(w-text_w)/2", f"h-text_h-{m}"),
            "bottom-right": (f"w-text_w-{m}", f"h-text_h-{m}"),
            "center": ("(w-text_w)/2", "(h-text_h)/2"),
        }
        return mapping.get(position, mapping["bottom-center"])

    @staticmethod
    def _quality_filter(quality: str) -> Optional[str]:
        dims = {"1080p": (1920, 1080), "720p": (1280, 720), "480p": (854, 480)}
        if quality == "original":
            return None
        w, h = dims[quality]
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
        )

    def _text_file(self, stream_id: str, text: str) -> Path:
        path = self.data_dir / f"text_{stream_id}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def _destination_is_runnable(dest: dict) -> bool:
        return bool(
            dest.get("enabled")
            and str(dest.get("rtmp_base") or "").strip()
            and str(dest.get("stream_key") or "").strip()
        )

    @staticmethod
    def _validate_tee_url(url: str, name: str):
        # Tee uses | as slave separator and ] to close slave options.
        if any(ch in url for ch in ("|", "[", "]", "\n", "\r")):
            raise ValueError(f"Destination '{name}' contains unsupported tee characters")

    def build_encoder_command(self, stream: dict) -> List[str]:
        if not stream.get("source"):
            raise ValueError("Source URL is required")

        enabled = [d for d in stream.get("destinations", []) if self._destination_is_runnable(d)]
        if not enabled:
            raise ValueError("أضف مخرج واحد فعال على الأقل")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats",
            "-stats_period", "1",
            "-progress", "pipe:1",
            "-fflags", "+genpts+discardcorrupt",
            "-thread_queue_size", "8192",
        ]

        source = str(stream["source"])
        if source.lower().startswith(("http://", "https://")):
            cmd += [
                "-rw_timeout", str(self.INPUT_RW_TIMEOUT_US),
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "2",
            ]

        # User normally supplies live HLS/RTMP. -re keeps VOD sources from being
        # blasted faster than realtime while live inputs remain naturally paced.
        cmd += ["-re", "-i", source]

        logo = str(stream.get("logo") or "").strip()
        if logo:
            cmd += ["-thread_queue_size", "1024", "-i", logo]

        filters = []
        current = "base"
        qf = self._quality_filter(stream.get("quality", "1080p"))
        filters.append(f"[0:v]{qf if qf else 'null'}[{current}]")

        if logo:
            filters.append(f"[1:v]scale={int(stream.get('logo_width', 335))}:-1[logo]")
            x, y = self._overlay_xy(stream.get("logo_position", "top-right"))
            filters.append(
                f"[{current}][logo]overlay=x={x}:y={y}:format=auto:shortest=0[withlogo]"
            )
            current = "withlogo"

        text = str(stream.get("text") or "")
        if text.strip():
            text_file = self._text_file(stream["id"], text)
            x, y = self._text_xy(stream.get("text_position", "bottom-center"))
            filters.append(
                f"[{current}]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
                f"textfile={text_file}:reload=1:fontcolor=white:"
                f"fontsize={int(stream.get('text_size', 38))}:"
                f"borderw=2:bordercolor=black@0.85:box=1:boxcolor=black@0.30:boxborderw=12:"
                f"x={x}:y={y}[withtext]"
            )
            current = "withtext"

        fps = int(stream.get("fps", 50))
        bitrate = int(stream.get("bitrate", 5000))
        maxrate = max(bitrate + 100, int(round(bitrate * 1.10)))
        bufsize = bitrate * 2
        gop = fps * 2

        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", f"[{current}]",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", stream.get("preset", "superfast"),
            "-tune", "zerolatency",
            "-profile:v", "main",
            "-level:v", "4.2",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-g", str(gop),
            "-keyint_min", str(fps),
            "-sc_threshold", "0",
            "-b:v", f"{bitrate}k",
            "-maxrate", f"{maxrate}k",
            "-bufsize", f"{bufsize}k",
            "-c:a", "aac",
            "-b:a", f"{int(stream.get('audio_bitrate', 128))}k",
            "-ar", "48000",
            "-ac", "2",
            "-max_muxing_queue_size", "4096",
            "-flags", "+global_header",
        ]

        slaves = []
        for d in enabled:
            url = f"{d['rtmp_base']}{d['stream_key']}"
            self._validate_tee_url(url, d.get("name") or d.get("id") or "output")
            slaves.append(f"[f=flv:onfail=ignore:flvflags=no_duration_filesize]{url}")

        cmd += [
            "-f", "tee",
            "-use_fifo", "1",
            "-fifo_options", self.FIFO_OPTIONS,
            "|".join(slaves),
        ]
        return cmd

    def build_command(self, stream: dict) -> List[str]:
        return self.build_encoder_command(stream)

    # ------------------------------------------------------------------
    # logging / redaction
    # ------------------------------------------------------------------
    @staticmethod
    def _destination_secrets(destination: dict) -> List[str]:
        base = str(destination.get("rtmp_base") or "")
        key = str(destination.get("stream_key") or "")
        values = []
        if key:
            values.extend([key, f"{base}{key}"])
        return sorted(set(values), key=len, reverse=True)

    def _secret_values(self, stream: dict) -> List[str]:
        values = []
        for d in stream.get("destinations", []):
            values.extend(self._destination_secrets(d))
        return sorted(set(values), key=len, reverse=True)

    def _redact(self, stream: dict, text: str) -> str:
        out = str(text or "")
        for secret in self._secret_values(stream):
            if not secret:
                continue
            if secret.startswith(("rtmp://", "rtmps://")) and "/" in secret:
                base = secret.rsplit("/", 1)[0] + "/"
                out = out.replace(secret, base + "***REDACTED***")
            else:
                out = out.replace(secret, "***REDACTED***")
        return out

    def _log(self, stream_id: str, message: str):
        with self.lock:
            if stream_id not in self.streams and stream_id not in self.logs:
                return
            self.logs.setdefault(stream_id, deque(maxlen=1200)).append(str(message))

    # ------------------------------------------------------------------
    # runtime / supervision
    # ------------------------------------------------------------------
    def _initial_output_states(self, stream: dict) -> dict:
        states = {}
        for d in stream.get("destinations", []):
            if self._destination_is_runnable(d):
                states[d["id"]] = {
                    "status": "connecting",
                    "retries": 0,
                    "last_error": "",
                    "host": (urlsplit(str(d.get("rtmp_base") or "")).hostname or "").lower(),
                }
        return states

    def _mark_outputs_running(self, runtime: dict, include_reconnecting: bool = False):
        for state in runtime.get("output_states", {}).values():
            status = state.get("status")
            if status == "connecting" or (include_reconnecting and status == "reconnecting"):
                state["status"] = "running"

    def _note_output_error(self, stream: dict, runtime: dict, raw_line: str, clean_line: str):
        lower = raw_line.lower()
        states = runtime.get("output_states", {})
        enabled = [d for d in stream.get("destinations", []) if self._destination_is_runnable(d)]

        matched_ids = set()
        slave_match = re.search(r"slave muxer #\s*(\d+)", lower)
        if slave_match:
            idx = int(slave_match.group(1))
            if 0 <= idx < len(enabled):
                matched_ids.add(enabled[idx]["id"])

        # Prefer an exact destination URL match. This is important when several
        # Telegram outputs share the same dc*.rtmp.t.me host.
        if not matched_ids:
            for d in enabled:
                full_url = f"{d.get('rtmp_base') or ''}{d.get('stream_key') or ''}".lower()
                if full_url and full_url in lower:
                    matched_ids.add(d["id"])

        # If an error only names a host, map it only when that host belongs to
        # exactly one enabled destination. Never blame all outputs on a shared host.
        if not matched_ids:
            host_to_ids = {}
            for d in enabled:
                host = (urlsplit(str(d.get("rtmp_base") or "")).hostname or "").lower()
                if host:
                    host_to_ids.setdefault(host, []).append(d["id"])
            for host, ids in host_to_ids.items():
                if host in lower and len(ids) == 1:
                    matched_ids.add(ids[0])

        # Local/file outputs are only used for diagnostics, but matching the
        # configured base makes the status display accurate there too.
        if not matched_ids:
            for d in enabled:
                base = str(d.get("rtmp_base") or "").lower()
                if len(base) >= 6 and base in lower:
                    matched_ids.add(d["id"])

        if not matched_ids and len(enabled) == 1:
            matched_ids.add(enabled[0]["id"])

        for did in matched_ids:
            state = states.get(did)
            if not state:
                continue
            state["status"] = "reconnecting"
            state["retries"] = int(state.get("retries") or 0) + 1
            state["last_error"] = clean_line[-500:]

    def _spawn_encoder_locked(self, stream_id: str, runtime: dict):
        stream = self.streams.get(stream_id)
        if not stream:
            raise KeyError("Stream not found")

        cmd = self.build_encoder_command(stream)
        safe_cmd = self._redact(stream, " ".join(shlex.quote(x) for x in cmd))
        self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] ENCODER START {safe_cmd}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        runtime["proc"] = proc
        runtime["status"] = "running"
        runtime["last_error"] = ""
        runtime["progress"] = {}
        if not runtime.get("started_at"):
            runtime["started_at"] = time.time()
        try:
            runtime["psproc"] = psutil.Process(proc.pid)
            runtime["psproc"].cpu_percent(interval=None)
        except Exception:
            runtime["psproc"] = None

        # A new encoder generation starts all tee slaves from CONNECTING.
        runtime["output_states"] = self._initial_output_states(stream)

        threading.Thread(
            target=self._read_progress,
            args=(stream_id, runtime, proc),
            daemon=True,
            name=f"progress-{stream_id}-{proc.pid}",
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(stream_id, runtime, proc),
            daemon=True,
            name=f"stderr-{stream_id}-{proc.pid}",
        ).start()
        return proc

    def _read_progress(self, stream_id: str, runtime: dict, proc: subprocess.Popen):
        if not proc.stdout:
            return
        batch = {}
        try:
            for raw in iter(proc.stdout.readline, ""):
                if not raw:
                    break
                line = raw.strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                batch[key] = value
                if key == "progress":
                    with self.lock:
                        if runtime.get("proc") is proc:
                            runtime["progress"].update(batch)
                            if value == "continue":
                                self._mark_outputs_running(runtime)
                    batch = {}
        except Exception:
            pass

    def _read_stderr(self, stream_id: str, runtime: dict, proc: subprocess.Popen):
        if not proc.stderr:
            return

        error_markers = (
            "error", "failed", "broken pipe", "connection refused", "connection reset",
            "network is unreachable", "timed out", "i/o error", "server error",
            "invalid data", "end of file",
        )
        recovery_markers = ("recovery successful", "recovered")

        try:
            for raw in iter(proc.stderr.readline, ""):
                if not raw:
                    break
                line = raw.rstrip()
                with self.lock:
                    stream = self.streams.get(stream_id, {})
                    clean = self._redact(stream, line)
                    lower = line.lower()
                    if any(m in lower for m in error_markers):
                        runtime["last_error"] = clean[-700:]
                        self._note_output_error(stream, runtime, line, clean)
                    elif any(m in lower for m in recovery_markers):
                        self._mark_outputs_running(runtime, include_reconnecting=True)
                if clean:
                    self._log(stream_id, clean)
        except Exception as exc:
            self._log(stream_id, f"[LOG] stderr reader stopped: {exc}")

    def _monitor_runtime(self, stream_id: str, runtime: dict):
        while not runtime["stop_event"].is_set():
            proc = runtime.get("proc")
            if not proc:
                break
            try:
                code = proc.wait()
            except Exception:
                code = -1

            if runtime["stop_event"].is_set():
                break

            with self.lock:
                if self.runtimes.get(stream_id) is not runtime:
                    break
                runtime["status"] = "restarting"
                runtime["last_exit_code"] = code
                runtime["restart_count"] = int(runtime.get("restart_count") or 0) + 1
                for state in runtime.get("output_states", {}).values():
                    state["status"] = "reconnecting"
            self._log(
                stream_id,
                f"[{time.strftime('%H:%M:%S')}] Encoder exited with code {code}; "
                f"auto restart #{runtime['restart_count']} in {self.ENCODER_RESTART_DELAY:.1f}s",
            )

            if runtime["stop_event"].wait(self.ENCODER_RESTART_DELAY):
                break

            while not runtime["stop_event"].is_set():
                try:
                    with self.lock:
                        if self.runtimes.get(stream_id) is not runtime:
                            return
                        self._spawn_encoder_locked(stream_id, runtime)
                    break
                except Exception as exc:
                    with self.lock:
                        runtime["status"] = "restarting"
                        runtime["last_error"] = str(exc)
                        runtime["restart_count"] = int(runtime.get("restart_count") or 0) + 1
                    self._log(stream_id, f"[RESTART] failed: {exc}")
                    if runtime["stop_event"].wait(self.ENCODER_RESTART_DELAY):
                        break

        with self.lock:
            if self.runtimes.get(stream_id) is runtime and runtime["stop_event"].is_set():
                self.runtimes.pop(stream_id, None)

    def start_stream(self, stream_id: str):
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                raise KeyError("Stream not found")

            existing = self.runtimes.get(stream_id)
            if existing and not existing.get("stop_event").is_set():
                raise RuntimeError("Stream is already running")

            enabled = [d for d in stream.get("destinations", []) if self._destination_is_runnable(d)]
            if not enabled:
                raise ValueError("أضف مخرج واحد فعال على الأقل")

            runtime = {
                "stop_event": threading.Event(),
                "status": "starting",
                "proc": None,
                "psproc": None,
                "started_at": time.time(),
                "restart_count": 0,
                "last_exit_code": None,
                "last_error": "",
                "progress": {},
                "output_states": self._initial_output_states(stream),
            }
            self.runtimes[stream_id] = runtime

            try:
                proc = self._spawn_encoder_locked(stream_id, runtime)
            except Exception:
                self.runtimes.pop(stream_id, None)
                raise

            threading.Thread(
                target=self._monitor_runtime,
                args=(stream_id, runtime),
                daemon=True,
                name=f"supervisor-{stream_id}",
            ).start()

            return {"ok": True, "pid": proc.pid, "mode": "single-ffmpeg-tee"}

    @staticmethod
    def _signal_process_group(proc: Optional[subprocess.Popen], sig):
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except Exception:
            try:
                proc.send_signal(sig)
            except Exception:
                pass

    def _finish_stop_async(self, stream_id: str, proc: subprocess.Popen):
        try:
            proc.wait(timeout=self.FAST_STOP_GRACE)
        except Exception:
            self._signal_process_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] Encoder stopped")

    def stop_stream(self, stream_id: str, silent: bool = False):
        with self.lock:
            runtime = self.runtimes.pop(stream_id, None)
            if not runtime:
                if not silent:
                    self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] Stream is not running")
                return {"ok": True, "stopping": False}

            runtime["status"] = "stopping"
            runtime["stop_event"].set()
            proc = runtime.get("proc")

        # Return to the HTTP request immediately. The process gets 1s to exit,
        # then a daemon reaper sends SIGKILL if necessary.
        if proc and proc.poll() is None:
            self._signal_process_group(proc, signal.SIGTERM)
            threading.Thread(
                target=self._finish_stop_async,
                args=(stream_id, proc),
                daemon=True,
                name=f"stop-{stream_id}-{proc.pid}",
            ).start()

        if not silent:
            self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] STOP requested")
        return {"ok": True, "stopping": bool(proc and proc.poll() is None)}

    def restart_output(self, stream_id: str, destination_id: str):
        raise RuntimeError(
            "إعادة تشغيل Output منفرد غير متاحة في وضع single-FFmpeg tee. "
            "الـFIFO يعيد الاتصال تلقائياً بدون عمليات FFmpeg إضافية."
        )

    def get_logs(self, stream_id: str) -> List[str]:
        with self.lock:
            return list(self.logs.get(stream_id, []))
