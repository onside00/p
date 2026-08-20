import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional


class StreamManager:
    """One encode per stream + independent copy-only RTMP/RTMPS outputs.

    source -> ONE FFmpeg encoder -> MPEG-TS pipe -> Python fan-out
           -> FFmpeg -c copy per destination

    Destinations can be added/removed/re-keyed while the encoder is running.
    Every output reconnects independently if it drops.
    """

    OUTPUT_QUEUE_ITEMS = 256
    OUTPUT_RECONNECT_DELAY = 2.0
    OUTPUT_RW_TIMEOUT_US = 10_000_000
    BROADCAST_READ_SIZE = 188 * 256

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        self.config_file = self.data_dir / "streams.json"

        self.streams: Dict[str, dict] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.logs: Dict[str, deque] = {}
        self.output_runtimes: Dict[str, Dict[str, dict]] = {}
        self.lock = threading.RLock()
        self._load()

    # ---------------------------- persistence ----------------------------
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
            self.logs[sid] = deque(maxlen=1000)
            self.output_runtimes[sid] = {}

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

        dests = []
        used_ids = set()
        for d in stream.get("destinations") or []:
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

    # ---------------------------- serialization ----------------------------
    def _runtime_snapshot(self, stream_id: str, destination_id: str) -> dict:
        runtime = self.output_runtimes.get(stream_id, {}).get(destination_id)
        if not runtime:
            return {
                "status": "stopped",
                "pid": None,
                "retries": 0,
                "last_error": "",
                "uptime": 0,
            }

        proc = runtime.get("proc")
        running = bool(proc and proc.poll() is None)
        status = runtime.get("status", "stopped")
        if running and status not in {"reconnecting", "stopping"}:
            status = "running"

        started_at = float(runtime.get("started_at") or 0)
        uptime = max(0, int(time.time() - started_at)) if started_at else 0
        return {
            "status": status,
            "pid": proc.pid if running else None,
            "retries": int(runtime.get("retries") or 0),
            "last_error": str(runtime.get("last_error") or ""),
            "uptime": uptime,
        }

    def _serialize_stream(self, stream: dict) -> dict:
        item = json.loads(json.dumps(stream))
        sid = stream["id"]
        proc = self.processes.get(sid)
        item["running"] = bool(proc and proc.poll() is None)
        item["pid"] = proc.pid if item["running"] else None
        for dest in item.get("destinations", []):
            dest.update(self._runtime_snapshot(sid, dest.get("id", "")))
        return item

    def list_streams(self) -> List[dict]:
        with self.lock:
            return [self._serialize_stream(s) for s in self.streams.values()]

    def get_stream(self, stream_id: str) -> Optional[dict]:
        with self.lock:
            s = self.streams.get(stream_id)
            return self._serialize_stream(s) if s else None

    # ---------------------------- CRUD / hot update ----------------------------
    def create_stream(self, payload: dict) -> dict:
        with self.lock:
            stream = self.defaults()
            stream.update(payload or {})
            stream["id"] = uuid.uuid4().hex[:12]
            self._normalize(stream)
            self.streams[stream["id"]] = stream
            self.logs[stream["id"]] = deque(maxlen=1000)
            self.output_runtimes[stream["id"]] = {}
            self._save()
            return self.get_stream(stream["id"])

    @staticmethod
    def _processing_signature(stream: dict):
        # Everything that changes the encoded media must wait until encoder stop.
        fields = (
            "source", "quality", "bitrate", "fps", "preset", "audio_bitrate",
            "logo", "logo_width", "logo_position", "text", "text_size",
            "text_position",
        )
        return tuple(stream.get(k) for k in fields)

    def update_stream(self, stream_id: str, payload: dict) -> dict:
        with self.lock:
            current = self.streams.get(stream_id)
            if not current:
                raise KeyError("Stream not found")

            updated = json.loads(json.dumps(current))
            updated.update(payload or {})
            updated["id"] = stream_id
            self._normalize(updated)

            proc = self.processes.get(stream_id)
            running = bool(proc and proc.poll() is None)
            if running and self._processing_signature(updated) != self._processing_signature(current):
                raise RuntimeError(
                    "البث شغال. تقدر تعدل المخارج والمفاتيح مباشرة، "
                    "لكن المصدر/الجودة/FPS/Bitrate/الشعار/النص تحتاج إيقاف البث أولاً."
                )

            self.streams[stream_id] = updated
            self._save()

            if running:
                self._reconcile_outputs_locked(stream_id, updated)
                self._log(stream_id, "[HOT] Outputs applied without restarting encoder")

            return self.get_stream(stream_id)

    def delete_stream(self, stream_id: str):
        with self.lock:
            self.stop_stream(stream_id, silent=True)
            self.streams.pop(stream_id, None)
            self.logs.pop(stream_id, None)
            self.output_runtimes.pop(stream_id, None)
            try:
                (self.data_dir / f"text_{stream_id}.txt").unlink(missing_ok=True)
            except Exception:
                pass
            self._save()

    # ---------------------------- source probe ----------------------------
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
            "-rw_timeout", str(self.OUTPUT_RW_TIMEOUT_US),
            "-analyzeduration", "8000000",
            "-probesize", "8000000",
            "-show_streams", "-show_format",
            "-of", "json",
            source,
        ]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=18)
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

    # ---------------------------- encoder command ----------------------------
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
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"

    def _text_file(self, stream_id: str, text: str) -> Path:
        path = self.data_dir / f"text_{stream_id}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def build_encoder_command(self, stream: dict) -> List[str]:
        if not stream.get("source"):
            raise ValueError("Source URL is required")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "info", "-stats_period", "2",
            "-re", "-thread_queue_size", "4096", "-i", stream["source"],
        ]

        logo = stream.get("logo", "").strip()
        if logo:
            cmd += ["-thread_queue_size", "1024", "-i", logo]

        filters = []
        current = "base"
        qf = self._quality_filter(stream.get("quality", "1080p"))
        filters.append(f"[0:v]{qf if qf else 'null'}[{current}]")

        if logo:
            filters.append(f"[1:v]scale={int(stream.get('logo_width', 335))}:-1[logo]")
            x, y = self._overlay_xy(stream.get("logo_position", "top-right"))
            filters.append(f"[{current}][logo]overlay=x={x}:y={y}:format=auto:shortest=0[withlogo]")
            current = "withlogo"

        text = stream.get("text", "")
        if text.strip():
            text_file = self._text_file(stream["id"], text)
            x, y = self._text_xy(stream.get("text_position", "bottom-center"))
            filters.append(
                f"[{current}]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
                f"textfile={text_file}:reload=1:fontcolor=white:fontsize={int(stream.get('text_size',38))}:"
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
            "-map", f"[{current}]", "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", stream.get("preset", "superfast"),
            "-tune", "zerolatency",
            "-profile:v", "main",
            "-level:v", "4.2",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-b:v", f"{bitrate}k",
            "-maxrate", f"{maxrate}k",
            "-bufsize", f"{bufsize}k",
            "-c:a", "aac",
            "-b:a", f"{int(stream.get('audio_bitrate',128))}k",
            "-ar", "48000",
            "-ac", "2",
            "-max_muxing_queue_size", "2048",
            # Internal encoded bus used by all outputs.
            "-f", "mpegts",
            "-mpegts_flags", "+resend_headers",
            "-pat_period", "0.1",
            "-sdt_period", "0.5",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-flush_packets", "1",
            "pipe:1",
        ]
        return cmd

    def build_output_command(self, destination: dict) -> List[str]:
        base = str(destination.get("rtmp_base") or "").strip()
        key = str(destination.get("stream_key") or "").strip()
        if not base or not key:
            raise ValueError("Destination server and key are required")
        url = f"{base}{key}"
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats_period", "5",
            "-fflags", "+genpts+discardcorrupt",
            "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-muxdelay", "0",
            "-flvflags", "no_duration_filesize",
            "-rw_timeout", str(self.OUTPUT_RW_TIMEOUT_US),
            "-f", "flv", url,
        ]

    def build_command(self, stream: dict) -> List[str]:
        return self.build_encoder_command(stream)

    # ---------------------------- logging / secret redaction ----------------------------
    def _log(self, stream_id: str, message: str):
        with self.lock:
            self.logs.setdefault(stream_id, deque(maxlen=1000)).append(str(message))

    @staticmethod
    def _destination_secrets(destination: dict) -> List[str]:
        base = str(destination.get("rtmp_base") or "")
        key = str(destination.get("stream_key") or "")
        out = []
        if key:
            out += [f"{base}{key}", key]
        return sorted(set(out), key=len, reverse=True)

    @staticmethod
    def _redact_values(text: str, values: List[str]) -> str:
        out = str(text or "")
        for secret in sorted((x for x in values if x), key=len, reverse=True):
            if secret.startswith("rtmp") and "/" in secret:
                base = secret.rsplit("/", 1)[0] + "/"
                out = out.replace(secret, base + "***REDACTED***")
            else:
                out = out.replace(secret, "***REDACTED***")
        return out

    def _redact(self, stream: dict, text: str) -> str:
        values = []
        for d in stream.get("destinations", []):
            values.extend(self._destination_secrets(d))
        return self._redact_values(text, values)

    # ---------------------------- output runtime ----------------------------
    @staticmethod
    def _destination_fingerprint(dest: dict):
        return (
            bool(dest.get("enabled", True)),
            str(dest.get("name") or ""),
            str(dest.get("rtmp_base") or ""),
            str(dest.get("stream_key") or ""),
        )

    @staticmethod
    def _destination_is_runnable(dest: dict) -> bool:
        return bool(
            dest.get("enabled")
            and str(dest.get("rtmp_base") or "").strip()
            and str(dest.get("stream_key") or "").strip()
        )

    @staticmethod
    def _terminate_process(proc: Optional[subprocess.Popen], timeout=3):
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=timeout)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _start_output_locked(self, stream_id: str, destination: dict):
        did = destination["id"]
        current = self.output_runtimes.setdefault(stream_id, {}).get(did)
        if current and not current.get("stop_event").is_set():
            return

        runtime = {
            "destination": json.loads(json.dumps(destination)),
            "fingerprint": self._destination_fingerprint(destination),
            "queue": queue.Queue(maxsize=self.OUTPUT_QUEUE_ITEMS),
            "stop_event": threading.Event(),
            "failure_event": threading.Event(),
            "proc": None,
            "thread": None,
            "status": "connecting",
            "retries": 0,
            "last_error": "",
            "started_at": 0,
            "dropped_chunks": 0,
        }
        self.output_runtimes[stream_id][did] = runtime
        t = threading.Thread(
            target=self._output_loop,
            args=(stream_id, did, runtime),
            daemon=True,
            name=f"out-{stream_id}-{did}",
        )
        runtime["thread"] = t
        t.start()

    def _stop_output_locked(self, stream_id: str, destination_id: str, remove=True):
        runtimes = self.output_runtimes.setdefault(stream_id, {})
        runtime = runtimes.get(destination_id)
        if not runtime:
            return

        runtime["status"] = "stopping"
        runtime["stop_event"].set()
        try:
            runtime["queue"].put_nowait(None)
        except Exception:
            pass
        self._terminate_process(runtime.get("proc"), timeout=2)
        runtime["status"] = "stopped"
        if remove:
            runtimes.pop(destination_id, None)

    def _stop_all_outputs_locked(self, stream_id: str):
        for did in list(self.output_runtimes.setdefault(stream_id, {}).keys()):
            self._stop_output_locked(stream_id, did, remove=True)

    def _reconcile_outputs_locked(self, stream_id: str, stream: dict):
        desired = {d["id"]: d for d in stream.get("destinations", [])}
        runtimes = self.output_runtimes.setdefault(stream_id, {})

        # Remove disabled/deleted outputs and restart only changed outputs.
        for did in list(runtimes.keys()):
            runtime = runtimes.get(did)
            dest = desired.get(did)
            if not dest or not self._destination_is_runnable(dest):
                self._log(stream_id, f"[HOT] Stop output {runtime.get('destination',{}).get('name', did)}")
                self._stop_output_locked(stream_id, did, remove=True)
                continue
            if runtime.get("fingerprint") != self._destination_fingerprint(dest):
                self._log(stream_id, f"[HOT] Restart changed output {dest.get('name', did)}")
                self._stop_output_locked(stream_id, did, remove=True)

        # Start new/enabled outputs. Encoder continues untouched.
        for did, dest in desired.items():
            if self._destination_is_runnable(dest) and did not in runtimes:
                self._log(stream_id, f"[HOT] Start output {dest.get('name', did)}")
                self._start_output_locked(stream_id, dest)

    def restart_output(self, stream_id: str, destination_id: str):
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                raise KeyError("Stream not found")
            proc = self.processes.get(stream_id)
            if not proc or proc.poll() is not None:
                raise RuntimeError("Encoder is not running")
            dest = next((d for d in stream.get("destinations", []) if d.get("id") == destination_id), None)
            if not dest:
                raise KeyError("Output not found")
            if not self._destination_is_runnable(dest):
                raise RuntimeError("Output is disabled or incomplete")
            self._stop_output_locked(stream_id, destination_id, remove=True)
            self._start_output_locked(stream_id, dest)
            self._log(stream_id, f"[OUT:{dest.get('name', destination_id)}] manual reconnect")
            return {"ok": True}

    def _output_loop(self, stream_id: str, destination_id: str, runtime: dict):
        destination = runtime["destination"]
        name = destination.get("name") or destination_id
        stop_event = runtime["stop_event"]
        q = runtime["queue"]

        while not stop_event.is_set():
            proc = None
            try:
                cmd = self.build_output_command(destination)
                safe_cmd = self._redact_values(
                    " ".join(shlex.quote(x) for x in cmd),
                    self._destination_secrets(destination),
                )
                runtime["status"] = "connecting"
                self._log(stream_id, f"[OUT:{name}] CONNECT {safe_cmd}")

                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=False,
                    bufsize=0,
                    start_new_session=True,
                )
                runtime["proc"] = proc
                runtime["status"] = "running"
                runtime["started_at"] = time.time()
                runtime["last_error"] = ""
                runtime["failure_event"].clear()

                def stderr_reader():
                    if not proc.stderr:
                        return
                    secrets = self._destination_secrets(destination)
                    fatal_markers = (
                        "connection refused", "cannot open connection",
                        "error opening output", "broken pipe", "connection reset",
                        "network is unreachable", "i/o error", "timed out",
                        "error writing", "av_interleaved_write_frame", "server error",
                    )
                    for raw in iter(proc.stderr.readline, b""):
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace").rstrip()
                        clean = self._redact_values(line, secrets)
                        if clean:
                            runtime["last_error"] = clean[-500:]
                            self._log(stream_id, f"[OUT:{name}] {clean}")
                            if any(m in clean.lower() for m in fatal_markers):
                                runtime["failure_event"].set()
                                self._terminate_process(proc, timeout=0.2)

                threading.Thread(target=stderr_reader, daemon=True).start()
                self._log(stream_id, f"[OUT:{name}] connected (pid={proc.pid})")

                while not stop_event.is_set():
                    if runtime["failure_event"].is_set():
                        raise RuntimeError(runtime.get("last_error") or "output connection failed")
                    if proc.poll() is not None:
                        raise RuntimeError(f"output FFmpeg exited with code {proc.returncode}")
                    try:
                        chunk = q.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if chunk is None:
                        break
                    try:
                        if not proc.stdin:
                            raise BrokenPipeError("output stdin closed")
                        proc.stdin.write(chunk)
                    except (BrokenPipeError, OSError) as exc:
                        raise RuntimeError(str(exc))

                if stop_event.is_set():
                    break

            except Exception as exc:
                if not stop_event.is_set():
                    runtime["last_error"] = str(exc)
                    runtime["retries"] = int(runtime.get("retries") or 0) + 1
                    runtime["status"] = "reconnecting"
                    self._log(
                        stream_id,
                        f"[OUT:{name}] disconnected; reconnect in {self.OUTPUT_RECONNECT_DELAY:.0f}s "
                        f"(retry {runtime['retries']}): {exc}",
                    )
            finally:
                if proc and proc.stdin:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                self._terminate_process(proc, timeout=2)
                runtime["proc"] = None

            if not stop_event.is_set():
                # Drop stale queue so reconnect returns to LIVE, not an old backlog.
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                stop_event.wait(self.OUTPUT_RECONNECT_DELAY)

        runtime["status"] = "stopped"
        runtime["proc"] = None
        self._log(stream_id, f"[OUT:{name}] stopped")

    # ---------------------------- main encoder / fan-out ----------------------------
    def start_stream(self, stream_id: str):
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                raise KeyError("Stream not found")

            existing = self.processes.get(stream_id)
            if existing and existing.poll() is None:
                raise RuntimeError("Stream is already running")

            enabled = [d for d in stream.get("destinations", []) if self._destination_is_runnable(d)]
            if not enabled:
                raise ValueError("أضف مخرج واحد فعال على الأقل")

            cmd = self.build_encoder_command(stream)
            safe_cmd = self._redact(stream, " ".join(shlex.quote(x) for x in cmd))
            self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] ENCODER START {safe_cmd}")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=False,
                bufsize=0,
                start_new_session=True,
            )
            self.processes[stream_id] = proc
            self.output_runtimes[stream_id] = {}
            self._reconcile_outputs_locked(stream_id, stream)

            threading.Thread(
                target=self._read_encoder_stderr,
                args=(stream_id, proc),
                daemon=True,
                name=f"enc-log-{stream_id}",
            ).start()
            threading.Thread(
                target=self._broadcast_encoder_stdout,
                args=(stream_id, proc),
                daemon=True,
                name=f"enc-bus-{stream_id}",
            ).start()
            return {"ok": True, "pid": proc.pid}

    def _read_encoder_stderr(self, stream_id: str, proc: subprocess.Popen):
        if not proc.stderr:
            return
        for raw in iter(proc.stderr.readline, b""):
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            with self.lock:
                stream = self.streams.get(stream_id, {})
                clean = self._redact(stream, line)
            if clean:
                self._log(stream_id, clean)

    def _broadcast_encoder_stdout(self, stream_id: str, proc: subprocess.Popen):
        stdout = proc.stdout
        if not stdout:
            return
        try:
            while True:
                chunk = stdout.read(self.BROADCAST_READ_SIZE)
                if not chunk:
                    break

                with self.lock:
                    runtimes = list(self.output_runtimes.get(stream_id, {}).items())

                for did, runtime in runtimes:
                    if runtime.get("stop_event").is_set():
                        continue
                    q = runtime.get("queue")
                    if not q:
                        continue
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:
                        runtime["dropped_chunks"] = int(runtime.get("dropped_chunks") or 0) + 1
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            q.put_nowait(chunk)
                        except queue.Full:
                            pass
                        dropped = runtime["dropped_chunks"]
                        if dropped in {1, 25, 100} or dropped % 500 == 0:
                            name = runtime.get("destination", {}).get("name", did)
                            self._log(stream_id, f"[OUT:{name}] slow queue; dropped {dropped} chunks")
        finally:
            code = proc.wait()
            with self.lock:
                if self.processes.get(stream_id) is proc:
                    self.processes.pop(stream_id, None)
                self._stop_all_outputs_locked(stream_id)
            self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] Encoder exited with code {code}")

    def stop_stream(self, stream_id: str, silent: bool = False):
        with self.lock:
            self._stop_all_outputs_locked(stream_id)
            proc = self.processes.get(stream_id)
            if not proc or proc.poll() is not None:
                self.processes.pop(stream_id, None)
                if not silent:
                    self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] Stream is not running")
                return {"ok": True}

            self._terminate_process(proc, timeout=8)
            self.processes.pop(stream_id, None)
            if not silent:
                self._log(stream_id, f"[{time.strftime('%H:%M:%S')}] STOP requested")
            return {"ok": True}

    def get_logs(self, stream_id: str) -> List[str]:
        with self.lock:
            return list(self.logs.get(stream_id, []))
