#!/usr/bin/env python3
"""StackChan outing relay. Standard library only.

The AI keeps calling this local HTTP endpoint. At home requests are forwarded
to the robot. Away from home, supported commands wait for the robot's /q/poll.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import pathlib
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("SC_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SC_LISTEN_PORT", "18760"))
DEVICE_HOST = os.environ.get("SC_DEVICE_HOST", "stackchan.local")
DEVICE_PORT = int(os.environ.get("SC_DEVICE_PORT", "80"))
PUBLIC_BASE = os.environ.get("SC_PUBLIC_BASE", "https://robot.example.invalid").rstrip("/")
TOKEN = os.environ.get("SC_RELAY_TOKEN", "")
TOKEN_FILE = pathlib.Path(os.environ.get("SC_TOKEN_FILE", "~/.config/stackchan-outing/token")).expanduser()
RUNTIME = pathlib.Path(os.environ.get("SC_RUNTIME_DIR", "./runtime")).resolve()
AUDIO_DIR = pathlib.Path(os.environ.get("SC_AUDIO_DIR", str(RUNTIME / "audio"))).resolve()
UPLOAD_DIR = pathlib.Path(os.environ.get("SC_UPLOAD_DIR", str(RUNTIME / "uploads"))).resolve()
QUEUE_TTL = int(os.environ.get("SC_QUEUE_TTL", "120"))
QUEUE_MAX = int(os.environ.get("SC_QUEUE_MAX", "20"))
MAX_BODY = int(os.environ.get("SC_MAX_BODY", str(8 * 1024 * 1024)))
MOTION_PATHS = ("/move", "/nod", "/shake", "/home")
QUEUE_PATHS = ("/face", "/play", "/snapshot", *MOTION_PATHS)

if not TOKEN:
    try: TOKEN = TOKEN_FILE.read_text().strip()
    except OSError: TOKEN = ""
RUNTIME.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    print(time.strftime("[outing-relay] %m-%d %H:%M:%S"), message, flush=True)


def motion_args(body) -> tuple[float, float, int]:
    """Parse once for both breath suppression and queued movement."""
    b = body if isinstance(body, dict) else {}
    try:
        values = (float(b.get("x", 0)), float(b.get("y", 0)), float(b.get("speed", 50)))
        if not all(math.isfinite(v) for v in values): raise ValueError("non-finite")
        x, y, speed = values
    except (TypeError, ValueError):
        x, y, speed = 0.0, 0.0, 50.0
    return max(-128.0, min(128.0, x)), max(0.0, min(90.0, y)), int(max(1, min(100, speed)))


class RelayState:
    def __init__(self, *, ttl: int = QUEUE_TTL, limit: int = QUEUE_MAX, clock=time.time):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.lock = threading.Lock()
        self.queue: list[dict] = []
        self.last_poll_at = 0.0
        self.last_face = "calm"

    def _purge_locked(self, now: float) -> int:
        before = len(self.queue)
        self.queue[:] = [item for item in self.queue if now - item["at"] < self.ttl]
        return before - len(self.queue)

    def enqueue(self, path: str, body: dict | None = None) -> dict:
        body = body if isinstance(body, dict) else {}
        if path not in QUEUE_PATHS:
            return {"success": False, "queued": False, "error": "unsupported while away"}
        if path == "/move":
            x, y, speed = motion_args(body)
            if x == 0 and y == 0 and speed <= 10:
                return {"success": True, "queued": False, "note": "idle breathing suppressed"}
        now = self.clock()
        item = {"at": now, "path": path}
        with self.lock:
            self._purge_locked(now)
            if path in MOTION_PATHS:
                self.queue[:] = [old for old in self.queue if "motion" not in old]
                item["motion"] = path[1:]
                if path == "/move": item.update(x=x, y=y, speed=speed)
                elif path == "/home": item["speed"] = 50
            elif path == "/face":
                item["face"] = str(body.get("expression") or body.get("face") or "calm")[:31]
                self.last_face = item["face"]
            elif path == "/play":
                item["voice_url"] = public_audio_url(str(body.get("voice_url") or ""))
            self.queue.append(item)
            if len(self.queue) > self.limit: del self.queue[:len(self.queue) - self.limit]
            size = len(self.queue)
        return {"success": True, "queued": True, "queue": size}

    def poll(self) -> dict:
        now = self.clock()
        with self.lock:
            self.last_poll_at = now
            self._purge_locked(now)
            if not self.queue: return {"remote": True}
            item = self.queue.pop(0)
        allowed = ("path", "face", "voice_url", "motion", "x", "y", "speed")
        return {"remote": True, **{key: value for key, value in item.items() if key in allowed}}


STATE = RelayState()
PHOTO_CV = threading.Condition()
PHOTO = {"at": 0.0, "data": b""}


def device_url(path: str) -> str:
    return f"http://{DEVICE_HOST}:{DEVICE_PORT}{path}"


def device_reachable() -> bool:
    try:
        with urllib.request.urlopen(device_url("/face"), timeout=1.5) as response:
            return response.status < 500
    except Exception:
        return False


def public_audio_url(source: str) -> str:
    """Only publish files already inside SC_AUDIO_DIR; never proxy arbitrary URLs."""
    try:
        parsed = urllib.parse.urlparse(source)
        candidate = (AUDIO_DIR / pathlib.PurePosixPath(parsed.path).name).resolve()
        candidate.relative_to(AUDIO_DIR)
        if not candidate.is_file(): return ""
        name = urllib.parse.quote(candidate.name)
        return f"{PUBLIC_BASE}/q/audio/{name}?t={urllib.parse.quote(TOKEN)}"
    except (ValueError, OSError):
        return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *_): pass

    def _json(self, status: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(raw)

    def _token_ok(self) -> bool:
        supplied = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("t") or [""])[0]
        return bool(TOKEN) and hmac.compare_digest(supplied, TOKEN)

    def _body(self) -> bytes | None:
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: length = MAX_BODY + 1
        if length < 0 or length > MAX_BODY: self._json(413, {"error": "body too large"}); return None
        return self.rfile.read(length)

    def _forward(self, method: str, body: bytes = b"") -> None:
        parsed = urllib.parse.urlparse(self.path)
        request = urllib.request.Request(device_url(parsed.path), data=body if method == "POST" else None, method=method,
                                         headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = response.read(); self.send_response(response.status)
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/q/poll":
            if not self._token_ok(): self._json(401, {"error": "bad token"}); return
            self._json(200, STATE.poll()); return
        if path.startswith("/q/audio/"):
            if not self._token_ok(): self._json(401, {"error": "bad token"}); return
            name = pathlib.PurePosixPath(urllib.parse.unquote(path[len("/q/audio/"):])).name
            target = (AUDIO_DIR / name).resolve()
            try: target.relative_to(AUDIO_DIR)
            except ValueError: self._json(400, {"error": "bad name"}); return
            if not target.is_file(): self._json(404, {"error": "not found"}); return
            data = target.read_bytes(); self.send_response(200); self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if path == "/q/status":
            self._json(200, {"device_reachable": device_reachable(), "queue": len(STATE.queue),
                             "last_poll_ago_s": round(time.time() - STATE.last_poll_at, 1) if STATE.last_poll_at else None}); return
        if path == "/snapshot" and not device_reachable():
            requested = time.time(); STATE.enqueue("/snapshot", {})
            with PHOTO_CV:
                PHOTO_CV.wait_for(lambda: PHOTO["at"] >= requested, timeout=15)
                data = PHOTO["data"] if PHOTO["at"] >= requested else b""
            if not data: self._json(504, {"error": "robot did not return a photo within 15 seconds"}); return
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(data); return
        if device_reachable():
            try: self._forward("GET"); return
            except Exception as error: log(f"local forward failed: {error}")
        if path == "/face": self._json(200, {"face": STATE.last_face, "remote": True}); return
        self._json(503, {"error": f"{path} is unavailable while robot is away"})

    def do_POST(self):
        raw = self._body()
        if raw is None: return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/q/photo", "/q/mic"):
            if not self._token_ok(): self._json(401, {"error": "bad token"}); return
            good = (path == "/q/photo" and raw.startswith(b"\xff\xd8")) or (path == "/q/mic" and raw.startswith(b"RIFF"))
            if not good: self._json(400, {"error": "bad upload"}); return
            suffix = ".jpg" if path == "/q/photo" else ".wav"
            target = UPLOAD_DIR / f"{int(time.time() * 1000)}{suffix}"
            target.write_bytes(raw)
            if path == "/q/photo":
                with PHOTO_CV:
                    PHOTO.update(at=time.time(), data=raw); PHOTO_CV.notify_all()
            self._json(200, {"ok": True, "saved": target.name}); return
        try: body = json.loads(raw.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError): self._json(400, {"error": "bad json"}); return
        if device_reachable():
            try: self._forward("POST", raw); return
            except Exception as error: log(f"local forward failed, queueing if supported: {error}")
        result = STATE.enqueue(path, body)
        self._json(200 if result.get("success") else 503, result)


def main() -> None:
    if not TOKEN: raise SystemExit("Set SC_RELAY_TOKEN or SC_TOKEN_FILE before starting")
    log(f"listening on {LISTEN_HOST}:{LISTEN_PORT}; device={DEVICE_HOST}:{DEVICE_PORT}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__": main()
