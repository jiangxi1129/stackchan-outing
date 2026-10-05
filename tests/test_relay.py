#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
tmp = tempfile.TemporaryDirectory()
os.environ.update(SC_RUNTIME_DIR=tmp.name, SC_RELAY_TOKEN="test-token", SC_PUBLIC_BASE="https://example.invalid")
spec = importlib.util.spec_from_file_location("outing_relay", ROOT / "relay/sc_relay.py")
relay = importlib.util.module_from_spec(spec); spec.loader.exec_module(relay)


def request(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as response:
        return response.status, json.loads(response.read())


def unit_state_tests():
    now = [1000.0]
    state = relay.RelayState(ttl=10, limit=20, clock=lambda: now[0])
    assert state.enqueue("/face", {"expression": "happy"})["queued"]
    assert not state.enqueue("/move", {"x": 0, "y": 0, "speed": 10})["queued"]
    state.enqueue("/move", {"x": -45, "y": 20, "speed": 50})
    state.enqueue("/play", {"voice_url": ""})
    state.enqueue("/shake", {})
    # Latest motion wins without changing face/play relative order.
    assert [item["path"] for item in state.queue] == ["/face", "/play", "/shake"]
    assert state.poll()["face"] == "happy"
    assert state.poll()["path"] == "/play"
    assert state.poll()["motion"] == "shake"
    assert state.poll() == {"remote": True}
    state.enqueue("/nod", {})
    now[0] += 11
    assert state.poll() == {"remote": True}  # expired command is never returned


def fake_device_poll_test():
    relay.STATE = relay.RelayState()
    relay.device_reachable = lambda: False
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert request(base, "/face", {"expression": "happy"})[1]["queued"]
        assert request(base, "/move", {"x": 10, "y": 20, "speed": 40})[1]["queued"]
        assert request(base, "/move", {"x": 0, "y": 0, "speed": 10})[1]["queued"] is False
        first = request(base, "/q/poll?t=test-token")[1]
        second = request(base, "/q/poll?t=test-token")[1]
        empty = request(base, "/q/poll?t=test-token")[1]
        assert first["face"] == "happy"
        assert (second["motion"], second["x"], second["y"]) == ("move", 10.0, 20.0)
        assert empty == {"remote": True}
        try: request(base, "/q/poll?t=wrong")
        except urllib.error.HTTPError as error: assert error.code == 401
        else: raise AssertionError("bad token was accepted")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def raw_request(base, path, body=None, headers=None, method=None):
    req = urllib.request.Request(base + path, data=body, method=method or ("GET" if body is None else "POST"), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def serve():
    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def token_and_status_tests():
    relay.STATE = relay.RelayState()
    relay.device_reachable = lambda: False
    server, thread, base = serve()
    try:
        # Header token works; the query token still works for old firmware; wrong or missing is 401.
        assert raw_request(base, "/q/poll", headers={"X-SC-Token": "test-token"})[0] == 200
        assert raw_request(base, "/q/poll?t=test-token")[0] == 200
        assert raw_request(base, "/q/poll", headers={"X-SC-Token": "wrong"})[0] == 401
        assert raw_request(base, "/q/poll")[0] == 401
        # A non-ASCII token is a plain 401, not a crash.
        assert raw_request(base, "/q/poll", headers={"X-SC-Token": "t\u00e9st"})[0] == 401
        # /q/status is reachable from outside, so it needs the token; /status is local-only and does not.
        assert raw_request(base, "/q/status")[0] == 401
        code, status = raw_request(base, "/q/status", headers={"X-SC-Token": "test-token"})
        assert code == 200 and status["device_reachable"] is False
        assert raw_request(base, "/status")[0] == 200
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def remote_flag_tests():
    # The firmware polls at home too. At home the relay must say remote=False or the robot cuts its live mic.
    relay.STATE = relay.RelayState()
    server, thread, base = serve()
    try:
        relay.device_reachable = lambda: True
        assert raw_request(base, "/q/poll", headers={"X-SC-Token": "test-token"})[1] == {"remote": False}
        relay.device_reachable = lambda: False
        assert raw_request(base, "/q/poll", headers={"X-SC-Token": "test-token"})[1] == {"remote": True}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def audio_url_tests():
    audio = relay.AUDIO_DIR / "hello.wav"; audio.write_bytes(b"RIFF....")
    relay.LEGACY_QUERY_TOKEN = False
    url = relay.public_audio_url("http://192.168.1.5/audio/hello.wav")
    assert url == "https://example.invalid/q/audio/hello.wav" and "test-token" not in url
    relay.LEGACY_QUERY_TOKEN = True
    assert relay.public_audio_url("http://192.168.1.5/audio/hello.wav").endswith("/q/audio/hello.wav?t=test-token")
    relay.LEGACY_QUERY_TOKEN = False
    assert relay.public_audio_url("http://evil.example/other.wav") == ""   # not in the audio dir: never proxied


def upload_ledger_tests():
    relay.UPLOADS = relay.UploadLedger()
    logs = []; real_log = relay.log; relay.log = logs.append
    server, thread, base = serve()
    hdr = {"X-SC-Token": "test-token", "Content-Type": "audio/wav"}
    wav = b"RIFF" + b"\0" * 40
    try:
        def files(): return sorted(p.name for p in relay.UPLOAD_DIR.glob("*.wav"))
        before = len(files())
        code, body = raw_request(base, "/q/mic?seq=1&boot=ab", wav, hdr)
        assert code == 200 and body["seq"] == 1 and len(files()) == before + 1
        # Our 200 got lost, the robot retries the same seq: answered 200 again, saved once.
        code, body = raw_request(base, "/q/mic?seq=1&boot=ab", wav, hdr)
        assert code == 200 and body.get("duplicate") is True and len(files()) == before + 1
        # Robot dropped #2 and #3: the relay says so in its log.
        assert raw_request(base, "/q/mic?seq=4&boot=ab", wav, hdr)[0] == 200
        assert any("#2-#3 never arrived" in line for line in logs), logs
        # Robot rebooted: new boot id, numbering starts over without being taken for duplicates.
        assert raw_request(base, "/q/mic?seq=1&boot=cd", wav, hdr)[1].get("duplicate") is None
        # Old firmware without seq still uploads.
        assert raw_request(base, "/q/mic", wav, hdr)[0] == 200
        assert len(files()) == before + 4
        # Photos and recordings are counted separately.
        jpg = b"\xff\xd8" + b"\0" * 20
        assert raw_request(base, "/q/photo?seq=1&boot=ab", jpg, {"X-SC-Token": "test-token"})[1].get("duplicate") is None
        assert raw_request(base, "/q/mic?seq=2&boot=ab", wav, {"X-SC-Token": "wrong"})[0] == 401
    finally:
        relay.log = real_log
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def upload_failure_and_race_tests():
    relay.UPLOADS = relay.UploadLedger()
    real_dir, real_log = relay.UPLOAD_DIR, relay.log
    logs = []; relay.log = logs.append
    server, thread, base = serve()
    hdr = {"X-SC-Token": "test-token", "Content-Type": "audio/wav"}
    wav = b"RIFF" + b"\0" * 40
    try:
        # The disk write fails: 503, nothing committed. The robot retries the same number: saved for real.
        relay.UPLOAD_DIR = real_dir / "missing-dir-so-write-fails"
        code, body = raw_request(base, "/q/mic?seq=1&boot=ef", wav, hdr)
        assert code == 503, (code, body)
        relay.UPLOAD_DIR = real_dir
        code, body = raw_request(base, "/q/mic?seq=1&boot=ef", wav, hdr)
        assert code == 200 and body.get("duplicate") is None and body.get("saved"), body
        assert (real_dir / body["saved"]).exists()
        # Malformed numbering is refused, so the robot cannot be told "duplicate" for garbage.
        for q in ("seq=0&boot=ef", "seq=-1&boot=ef", "seq=2", "boot=ef", "seq=2&boot=XYZ", "seq=2&boot=123456789", "seq=x&boot=ef"):
            assert raw_request(base, "/q/mic?" + q, wav, hdr)[0] == 400, q
    finally:
        relay.UPLOAD_DIR, relay.log = real_dir, real_log
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    # A duplicate racing the first copy waits for it, and is a duplicate only after the first copy is on disk.
    ledger = relay.UploadLedger(); events = []
    def slow_write():
        events.append("write-start"); time.sleep(0.3); events.append("write-done")
    results = {}
    t1 = threading.Thread(target=lambda: results.__setitem__("a", ledger.save("recording", "ab", 7, slow_write)))
    t1.start(); time.sleep(0.05)
    results["b"] = ledger.save("recording", "ab", 7, lambda: events.append("second-write"))
    events.append("second-answered"); t1.join()
    assert results == {"a": "new", "b": "duplicate"}, results
    assert events == ["write-start", "write-done", "second-answered"], events
    # And if the first write fails, the racing retry saves it instead of being called a duplicate.
    ledger = relay.UploadLedger()
    def failing_write(): time.sleep(0.2); raise OSError("disk full")
    errors = []
    def first():
        try: ledger.save("recording", "ab", 8, failing_write)
        except OSError as e: errors.append(str(e))
    t1 = threading.Thread(target=first); t1.start(); time.sleep(0.05)
    assert ledger.save("recording", "ab", 8, lambda: None) == "new"
    t1.join(); assert errors == ["disk full"]


if __name__ == "__main__":
    unit_state_tests(); fake_device_poll_test(); token_and_status_tests(); remote_flag_tests()
    audio_url_tests(); upload_ledger_tests(); upload_failure_and_race_tests(); print("ALL PASS")

