#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
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


if __name__ == "__main__":
    unit_state_tests(); fake_device_poll_test(); print("ALL PASS")

