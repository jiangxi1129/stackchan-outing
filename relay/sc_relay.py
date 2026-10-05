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
import re
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
# Must be the same scheme://host as the firmware's REMOTE_POLL_URL (plain http://: the ESP32 client talks HTTP through
# the phone's CONNECT proxy). Audio URLs are built from it, and the firmware only sends its token to that base.
PUBLIC_BASE = os.environ.get("SC_PUBLIC_BASE", "http://robot.example.com").rstrip("/")
TOKEN = os.environ.get("SC_RELAY_TOKEN", "")
TOKEN_FILE = pathlib.Path(os.environ.get("SC_TOKEN_FILE", "~/.config/stackchan-outing/token")).expanduser()
RUNTIME = pathlib.Path(os.environ.get("SC_RUNTIME_DIR", "./runtime")).resolve()
AUDIO_DIR = pathlib.Path(os.environ.get("SC_AUDIO_DIR", str(RUNTIME / "audio"))).resolve()
UPLOAD_DIR = pathlib.Path(os.environ.get("SC_UPLOAD_DIR", str(RUNTIME / "uploads"))).resolve()
QUEUE_TTL = int(os.environ.get("SC_QUEUE_TTL", "120"))
QUEUE_MAX = int(os.environ.get("SC_QUEUE_MAX", "20"))
MAX_BODY = int(os.environ.get("SC_MAX_BODY", str(8 * 1024 * 1024)))
LEGACY_QUERY_TOKEN = os.environ.get("SC_LEGACY_QUERY_TOKEN", "0") == "1"   # old firmware: token only in ?t=
TOKEN_HEADER = "X-SC-Token"
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

    def poll(self, remote: bool = True) -> dict:
        # remote tells the robot whether it is away. The firmware polls at home too, so this must be
        # the real answer: at home it keeps live mic streaming instead of switching to record-and-upload.
        now = self.clock()
        with self.lock:
            self.last_poll_at = now
            self._purge_locked(now)
            if not self.queue: return {"remote": remote}
            item = self.queue.pop(0)
        allowed = ("path", "face", "voice_url", "motion", "x", "y", "speed")
        return {"remote": remote, **{key: value for key, value in item.items() if key in allowed}}


class UploadLedger:
    """Photos and recordings carry ?seq=N&boot=B. The robot keeps each one until it gets a 200, so a lost
    response means the same upload arrives twice; a skipped number means the robot dropped one.

    The number is committed only after the file is on disk, and check-write-commit runs under one lock:
    a failed write answers 5xx and the robot retries the same number; a duplicate that races the first copy
    waits for it and is only called a duplicate once that copy is really saved."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last: dict[tuple[str, str], int] = {}
        self.legacy_warned = False

    def save(self, kind: str, boot: str, seq: int | None, write) -> str:
        """write() puts the file on disk (and may raise). Returns "new", "duplicate" or "untracked"."""
        if seq is None:   # old firmware: no numbering, nothing to dedupe
            write(); return "untracked"
        with self.lock:
            key = (kind, boot)
            last = self.last.get(key)
            if last is not None and seq <= last: return "duplicate"
            write()   # raises → nothing committed, the caller answers 5xx, the robot keeps the copy
            if last is None and seq > 1:
                log(f"{kind} from boot {boot} starts at #{seq}: #1-#{seq - 1} never arrived (relay restarted, or the robot dropped them)")
            elif last is not None and seq > last + 1:
                log(f"{kind} #{last + 1}-#{seq - 1} never arrived: the robot dropped them (queue full or gave up retrying)")
            self.last[key] = seq
            return "new"


UPLOADS = UploadLedger()


STATE = RelayState()
BAD_TOKEN = {"count": 0}
BAD_TOKEN_LOCK = threading.Lock()
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
        # New firmware sends the token in the X-SC-Token header, so it stays out of URLs and proxy logs.
        # Firmware older than that can only send ?t=; set SC_LEGACY_QUERY_TOKEN=1 until it is reflashed.
        if LEGACY_QUERY_TOKEN: return f"{PUBLIC_BASE}/q/audio/{name}?t={urllib.parse.quote(TOKEN)}"
        return f"{PUBLIC_BASE}/q/audio/{name}"
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
        # Header first; ?t= is still accepted so firmware flashed before this change keeps working.
        supplied = self.headers.get(TOKEN_HEADER) or ""
        if not supplied:
            supplied = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("t") or [""])[0]
            if supplied and not UPLOADS.legacy_warned:
                UPLOADS.legacy_warned = True
                log("token arrived in ?t= (old firmware); it may end up in proxy logs. Reflash to send X-SC-Token")
        return bool(TOKEN) and hmac.compare_digest(supplied.encode("utf-8", "surrogateescape"), TOKEN.encode())

    def _deny(self) -> None:
        # Count refusals so a scan shows up in the log instead of passing silently.
        with BAD_TOKEN_LOCK:
            BAD_TOKEN["count"] += 1; count = BAD_TOKEN["count"]
        if count == 1 or count % 20 == 0:
            path = urllib.parse.urlparse(self.path).path
            log(f"refused {count} request(s) with a missing or wrong token so far (latest: {self.command} {path[:60]})")
        self._json(401, {"error": "bad token"})

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
            if not self._token_ok(): self._deny(); return
            self._json(200, STATE.poll(remote=not device_reachable())); return
        if path.startswith("/q/audio/"):
            if not self._token_ok(): self._deny(); return
            name = pathlib.PurePosixPath(urllib.parse.unquote(path[len("/q/audio/"):])).name
            target = (AUDIO_DIR / name).resolve()
            try: target.relative_to(AUDIO_DIR)
            except ValueError: self._json(400, {"error": "bad name"}); return
            if not target.is_file(): self._json(404, {"error": "not found"}); return
            data = target.read_bytes(); self.send_response(200); self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if path in ("/status", "/q/status"):
            # /status is for the AI/MCP on this machine: the reverse proxy only exposes /q/*, so it never leaves home.
            # /q/status is the same answer from outside and needs the token like every other /q/* path.
            if path == "/q/status" and not self._token_ok(): self._deny(); return
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
            if not self._token_ok(): self._deny(); return
            good = (path == "/q/photo" and raw.startswith(b"\xff\xd8")) or (path == "/q/mic" and raw.startswith(b"RIFF"))
            if not good: self._json(400, {"error": "bad upload"}); return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            seq_raw, boot = (query.get("seq") or [""])[0], (query.get("boot") or [""])[0]
            if seq_raw or boot:   # numbered upload: both must be well-formed (seq 1.., boot = esp_random() in hex)
                if not (seq_raw.isdigit() and int(seq_raw) >= 1 and re.fullmatch(r"[0-9a-f]{1,8}", boot)):
                    self._json(400, {"error": "bad seq/boot"}); return
                seq = int(seq_raw)
            else:
                seq = None
            kind = "photo" if path == "/q/photo" else "recording"
            suffix = ".jpg" if path == "/q/photo" else ".wav"
            target = UPLOAD_DIR / (f"{int(time.time() * 1000)}" + (f"-{boot}-{seq}" if seq is not None else "") + suffix)
            try: verdict = UPLOADS.save(kind, boot, seq, lambda: target.write_bytes(raw))
            except OSError as error:
                log(f"could not save {kind} #{seq if seq is not None else '?'}: {error}; the robot will retry")
                self._json(503, {"error": "could not save upload"}); return
            if verdict == "duplicate":   # our last 200 got lost on the way back; the robot retried. Already saved.
                self._json(200, {"ok": True, "seq": seq, "duplicate": True}); return
            if path == "/q/photo":
                with PHOTO_CV:
                    PHOTO.update(at=time.time(), data=raw); PHOTO_CV.notify_all()
            self._json(200, {"ok": True, "saved": target.name, "seq": seq}); return
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
