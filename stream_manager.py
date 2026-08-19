import json
import os
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
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.data_dir / "streams.json"
        self.streams: Dict[str, dict] = {}
        self.processes: Dict[str, subprocess.Popen] = {}
        self.logs: Dict[str, deque] = {}
        self.lock = threading.RLock()
        self._load()

    def _load(self):
        if self.config_file.exists():
            try:
                raw = json.loads(self.config_file.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self.streams = {item["id"]: item for item in raw if isinstance(item, dict) and item.get("id")}
            except Exception:
                self.streams = {}

        for sid in self.streams:
            self.logs[sid] = deque(maxlen=600)

    def _save(self):
        tmp = self.config_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.streams.values()), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.config_file)

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

    def list_streams(self) -> List[dict]:
        with self.lock:
            result = []
            for stream in self.streams.values():
                item = json.loads(json.dumps(stream))
                proc = self.processes.get(stream["id"])
                item["running"] = bool(proc and proc.poll() is None)
                item["pid"] = proc.pid if item["running"] else None
                result.append(item)
            return result

    def get_stream(self, stream_id: str) -> Optional[dict]:
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                return None
            item = json.loads(json.dumps(stream))
            proc = self.processes.get(stream_id)
            item["running"] = bool(proc and proc.poll() is None)
            item["pid"] = proc.pid if item["running"] else None
            return item

    def create_stream(self, payload: dict) -> dict:
        with self.lock:
            stream = self.defaults()
            stream.update(payload or {})
            stream["id"] = uuid.uuid4().hex[:12]
            self._normalize(stream)
            self.streams[stream["id"]] = stream
            self.logs[stream["id"]] = deque(maxlen=600)
            self._save()
            return self.get_stream(stream["id"])

    def update_stream(self, stream_id: str, payload: dict) -> dict:
        with self.lock:
            if stream_id not in self.streams:
                raise KeyError("Stream not found")
            proc = self.processes.get(stream_id)
            if proc and proc.poll() is None:
                raise RuntimeError("Stop the stream before editing it")
            updated = dict(self.streams[stream_id])
            updated.update(payload or {})
            updated["id"] = stream_id
            self._normalize(updated)
            self.streams[stream_id] = updated
            self._save()
            return self.get_stream(stream_id)

    def delete_stream(self, stream_id: str):
        with self.lock:
            self.stop_stream(stream_id, silent=True)
            self.streams.pop(stream_id, None)
            self.logs.pop(stream_id, None)
            text_file = self.data_dir / f"text_{stream_id}.txt"
            try:
                text_file.unlink(missing_ok=True)
            except Exception:
                pass
            self._save()

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
            stream["fps"] = max(20, min(60, int(stream.get("fps", 50))))
        except Exception:
            stream["fps"] = 50
        if stream.get("preset") not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}:
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
        if stream.get("logo_position") not in {"top-left", "top-right", "bottom-left", "bottom-right", "center"}:
            stream["logo_position"] = "top-right"
        stream["text"] = str(stream.get("text") or "")[:300]
        try:
            stream["text_size"] = max(12, min(120, int(stream.get("text_size", 38))))
        except Exception:
            stream["text_size"] = 38
        if stream.get("text_position") not in {"top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right", "center"}:
            stream["text_position"] = "bottom-center"

        dests = []
        for d in stream.get("destinations") or []:
            if not isinstance(d, dict):
                continue
            dests.append({
                "id": str(d.get("id") or uuid.uuid4().hex[:8]),
                "name": str(d.get("name") or "Telegram")[:60],
                "rtmp_base": str(d.get("rtmp_base") or "").strip(),
                "stream_key": str(d.get("stream_key") or "").strip(),
                "enabled": bool(d.get("enabled", True)),
            })
        stream["destinations"] = dests

    @staticmethod
    def _overlay_xy(position: str):
        margin = 30
        mapping = {
            "top-left": (str(margin), str(margin)),
            "top-right": (f"W-w-{margin}", str(margin)),
            "bottom-left": (str(margin), f"H-h-{margin}"),
            "bottom-right": (f"W-w-{margin}", f"H-h-{margin}"),
            "center": ("(W-w)/2", "(H-h)/2"),
        }
        return mapping.get(position, mapping["top-right"])

    @staticmethod
    def _text_xy(position: str):
        margin = 32
        mapping = {
            "top-left": (str(margin), str(margin)),
            "top-center": ("(w-text_w)/2", str(margin)),
            "top-right": (f"w-text_w-{margin}", str(margin)),
            "bottom-left": (str(margin), f"h-text_h-{margin}"),
            "bottom-center": ("(w-text_w)/2", f"h-text_h-{margin}"),
            "bottom-right": (f"w-text_w-{margin}", f"h-text_h-{margin}"),
            "center": ("(w-text_w)/2", "(h-text_h)/2"),
        }
        return mapping.get(position, mapping["bottom-center"])

    @staticmethod
    def _quality_filter(quality: str) -> Optional[str]:
        dims = {
            "1080p": (1920, 1080),
            "720p": (1280, 720),
            "480p": (854, 480),
        }
        if quality == "original":
            return None
        w, h = dims[quality]
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"

    def _text_file(self, stream_id: str, text: str) -> Path:
        path = self.data_dir / f"text_{stream_id}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def build_command(self, stream: dict) -> List[str]:
        if not stream.get("source"):
            raise ValueError("Source URL is required")

        enabled = [d for d in stream.get("destinations", []) if d.get("enabled") and d.get("rtmp_base") and d.get("stream_key")]
        if not enabled:
            raise ValueError("At least one enabled destination is required")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "info",
            "-re", "-thread_queue_size", "4096",
            "-i", stream["source"],
        ]

        logo = stream.get("logo", "").strip()
        if logo:
            cmd += ["-thread_queue_size", "1024", "-i", logo]

        filters = []
        base_label = "0:v"
        current = "base"

        qf = self._quality_filter(stream.get("quality", "1080p"))
        if qf:
            filters.append(f"[{base_label}]{qf}[{current}]")
        else:
            filters.append(f"[{base_label}]null[{current}]")

        if logo:
            logo_label = "logo"
            after_logo = "withlogo"
            filters.append(f"[1:v]scale={int(stream.get('logo_width',335))}:-1[{logo_label}]")
            x, y = self._overlay_xy(stream.get("logo_position", "top-right"))
            filters.append(f"[{current}][{logo_label}]overlay=x={x}:y={y}:format=auto:shortest=0[{after_logo}]")
            current = after_logo

        text = stream.get("text", "")
        if text.strip():
            text_file = self._text_file(stream["id"], text)
            x, y = self._text_xy(stream.get("text_position", "bottom-center"))
            after_text = "withtext"
            # textfile avoids escaping Arabic, colons, quotes and percent signs.
            filters.append(
                f"[{current}]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
                f"textfile={text_file}:reload=1:fontcolor=white:fontsize={int(stream.get('text_size',38))}:"
                f"borderw=2:bordercolor=black@0.85:box=1:boxcolor=black@0.30:boxborderw=12:"
                f"x={x}:y={y}[{after_text}]"
            )
            current = after_text

        filter_complex = ";".join(filters)
        fps = int(stream.get("fps", 50))
        bitrate = int(stream.get("bitrate", 5000))
        maxrate = max(bitrate + 100, int(round(bitrate * 1.10)))
        bufsize = bitrate * 2
        gop = fps * 2

        cmd += [
            "-filter_complex", filter_complex,
            "-map", f"[{current}]",
            "-map", "0:a?",
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
            "-flags", "+global_header",
        ]

        slaves = []
        for d in enabled:
            url = f"{d['rtmp_base']}{d['stream_key']}"
            # Telegram keys do not contain tee separators in normal use. Reject them if they do.
            if "|" in url or "]" in url:
                raise ValueError(f"Destination '{d.get('name')}' contains unsupported tee characters")
            slaves.append(f"[f=flv:onfail=ignore]{url}")

        tee_target = "|".join(slaves)
        cmd += [
            "-f", "tee",
            "-use_fifo", "1",
            "-fifo_options",
            "attempt_recovery=1:recovery_wait_time=3:recover_any_error=1:restart_with_keyframe=1:drop_pkts_on_overflow=1:max_recovery_attempts=0",
            tee_target,
        ]
        return cmd

    def _secret_values(self, stream: dict) -> List[str]:
        secrets = []
        for d in stream.get("destinations", []):
            key = d.get("stream_key", "")
            if key:
                secrets.append(key)
                secrets.append(f"{d.get('rtmp_base','')}{key}")
        return sorted(set(secrets), key=len, reverse=True)

    def _redact(self, stream: dict, text: str) -> str:
        out = text
        for secret in self._secret_values(stream):
            if secret:
                if secret.startswith("rtmp"):
                    base = next((d.get("rtmp_base", "") for d in stream.get("destinations", []) if secret == f"{d.get('rtmp_base','')}{d.get('stream_key','')}"), "")
                    out = out.replace(secret, f"{base}***REDACTED***")
                else:
                    out = out.replace(secret, "***REDACTED***")
        return out

    def start_stream(self, stream_id: str):
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                raise KeyError("Stream not found")
            existing = self.processes.get(stream_id)
            if existing and existing.poll() is None:
                raise RuntimeError("Stream is already running")

            cmd = self.build_command(stream)
            log = self.logs.setdefault(stream_id, deque(maxlen=600))
            safe_cmd = self._redact(stream, " ".join(shlex.quote(x) for x in cmd))
            log.append(f"[{time.strftime('%H:%M:%S')}] START {safe_cmd}")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self.processes[stream_id] = proc
            threading.Thread(target=self._read_logs, args=(stream_id, proc), daemon=True).start()
            return {"ok": True, "pid": proc.pid}

    def _read_logs(self, stream_id: str, proc: subprocess.Popen):
        stream = self.streams.get(stream_id, {})
        if proc.stderr:
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                clean = self._redact(stream, line.rstrip())
                with self.lock:
                    self.logs.setdefault(stream_id, deque(maxlen=600)).append(clean)
        code = proc.wait()
        with self.lock:
            self.logs.setdefault(stream_id, deque(maxlen=600)).append(
                f"[{time.strftime('%H:%M:%S')}] FFmpeg exited with code {code}"
            )

    def stop_stream(self, stream_id: str, silent: bool = False):
        with self.lock:
            proc = self.processes.get(stream_id)
            if not proc or proc.poll() is not None:
                if not silent:
                    self.logs.setdefault(stream_id, deque(maxlen=600)).append(f"[{time.strftime('%H:%M:%S')}] Stream is not running")
                return {"ok": True}

            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=8)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
            if not silent:
                self.logs.setdefault(stream_id, deque(maxlen=600)).append(f"[{time.strftime('%H:%M:%S')}] STOP requested")
            return {"ok": True}

    def get_logs(self, stream_id: str) -> List[str]:
        with self.lock:
            return list(self.logs.get(stream_id, []))
