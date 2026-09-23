"""Offline tests: config, signalling, and the connection policy.

No GStreamer needed; the GStreamer parts are exercised live on the Pi.
Run with:  uv run --no-project --with pytest pytest
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pi_panel_webrtc.config import Config, ConfigError
from pi_panel_webrtc.controller import Controller
from pi_panel_webrtc.signalling import SignallingError, post_offer

ROOT = Path(__file__).resolve().parent.parent


# --- config -------------------------------------------------------------------

def test_shipped_example_is_valid():
    cfg = Config.load(ROOT / "config.toml.example")
    assert cfg.server == "http://frigate.home.local:1984"
    assert [c.name for c in cfg.cameras] == ["front_door"]
    assert cfg.cameras[0].offer_url(cfg.server) == \
        "http://frigate.home.local:1984/api/webrtc?src=front_door_sub"


def test_whep_camera_needs_no_server():
    cfg = Config.from_dict({"camera": [{"name": "g", "whep_url": "http://m:8889/g/whep"}]})
    assert cfg.cameras[0].offer_url("") == "http://m:8889/g/whep"
    assert cfg.cameras[0].label == "g"


@pytest.mark.parametrize("data,msg", [
    ({"server": "http://x"}, "at least one"),
    ({"server": "http://x", "camera": [{"name": "Front Door", "src": "a"}]}, "lowercase"),
    ({"server": "http://x", "camera": [{"name": "a"}]}, "exactly one"),
    ({"server": "http://x", "camera": [{"name": "a", "src": "a", "whep_url": "u"}]}, "exactly one"),
    ({"camera": [{"name": "a", "src": "a"}]}, "server is required"),
    ({"server": "http://x", "camera": [{"name": "a", "src": "a"}, {"name": "a", "src": "b"}]}, "twice"),
    ({"server": "http://x", "camera": [{"name": "a", "src": "a"}], "linger_seconds": -1}, "linger"),
    ({"server": "http://x", "camera": [{"name": "a", "src": "a"}], "extra": 1}, "unknown"),
])
def test_config_rejects(data, msg):
    with pytest.raises(ConfigError, match=msg):
        Config.from_dict(data)


def test_manifest_parses():
    data = tomllib.loads((ROOT / "pi-panel.toml").read_text())
    assert data["package"]["name"] == "webrtc"
    assert data["app"] == {"varlink": True, "rotate": False}
    assert "python3-gst-1.0" in data["system"]["packages"]


def test_interface_description_is_valid_idl(tmp_path):
    varlinkctl = shutil.which("varlinkctl")
    if not varlinkctl:
        pytest.skip("varlinkctl not installed")
    idl = ROOT / "src" / "pi_panel_webrtc" / "io.pipanel.app.WebRTC.varlink"
    result = subprocess.run([varlinkctl, "validate-idl", str(idl)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- signalling -------------------------------------------------------------------

ANSWER = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"


@pytest.fixture
def fake_go2rtc():
    """A go2rtc /api/webrtc stand-in that records requests."""
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            seen.update(path=self.path, ctype=self.headers["Content-Type"], body=body)
            if "src=missing" in self.path:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"streams: stream not found")
                return
            reply = b"not sdp" if "src=junk" in self.path else ANSWER.encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/sdp")
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def test_post_offer(fake_go2rtc):
    base, seen = fake_go2rtc
    assert post_offer(f"{base}/api/webrtc?src=front_door", "v=0 offer") == ANSWER
    assert seen == {"path": "/api/webrtc?src=front_door", "ctype": "application/sdp",
                    "body": "v=0 offer"}


def test_post_offer_errors(fake_go2rtc):
    base, _ = fake_go2rtc
    with pytest.raises(SignallingError, match="HTTP 500.*stream not found"):
        post_offer(f"{base}/api/webrtc?src=missing", "v=0")
    with pytest.raises(SignallingError, match="did not answer with SDP"):
        post_offer(f"{base}/api/webrtc?src=junk", "v=0")
    with pytest.raises(SignallingError, match="cannot reach"):
        post_offer("http://127.0.0.1:9/api/webrtc?src=x", "v=0", timeout=2)


# --- connection policy ------------------------------------------------------------

class FakeSession:
    def __init__(self, camera, log):
        self.camera, self.log, self._state = camera, log, "idle"

    @property
    def state(self):
        return self._state

    def start(self):
        self._state = "connecting"
        self.log.append(("start", self.camera.name))

    def stop(self):
        self._state = "idle"
        self.log.append(("stop", self.camera.name))

    def status(self):
        return {"error": None, "width": 640, "height": 360, "frames": 5, "connected_for": None}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(keep=False, linger=30.0, cameras=("front", "back", "side")):
    cfg = Config.from_dict({"server": "http://x", "keep_connected": keep, "linger_seconds": linger,
                            "camera": [{"name": n, "src": n} for n in cameras]})
    log, clock = [], Clock()
    ctl = Controller(cfg, lambda cam: FakeSession(cam, log), clock)
    ctl.tick()
    return ctl, log, clock


def test_hidden_at_start_does_not_connect():
    ctl, log, _ = make()
    assert log == [] and ctl.status()["state"] == "idle"


def test_visible_connects_and_hidden_disconnects_after_linger():
    ctl, log, clock = make(linger=30)
    ctl.set_visible(True)
    assert log == [("start", "front")]
    ctl.set_visible(False)
    clock.t += 29
    ctl.tick()
    assert log == [("start", "front")]            # still lingering
    ctl.set_visible(True)                          # back within the linger: no reconnect
    ctl.set_visible(False)
    clock.t += 31
    ctl.tick()
    assert log == [("start", "front"), ("stop", "front")]
    assert ctl.status()["state"] == "idle"


def test_keep_connected_ignores_visibility():
    ctl, log, clock = make(keep=True)
    assert log == [("start", "front")]
    ctl.set_visible(True)
    ctl.set_visible(False)
    clock.t += 1000
    ctl.tick()
    assert log == [("start", "front")]


def test_switching_cameras_replaces_the_session_only_when_connected():
    ctl, log, _ = make()
    assert ctl.select("back") and log == []        # hidden: just remembered
    ctl.set_visible(True)
    assert log == [("start", "back")]
    ctl.step(+1)
    assert log[-2:] == [("stop", "back"), ("start", "side")]
    ctl.step(+1)                                   # wraps
    assert log[-1] == ("start", "front")
    ctl.step(-1)
    assert log[-1] == ("start", "side")
    assert not ctl.select("nope")
    ctl.select("side")                              # already showing: no churn
    assert log[-1] == ("start", "side")


def test_reconnect_and_status():
    ctl, log, _ = make()
    ctl.set_visible(True)
    ctl.reconnect()
    assert log == [("start", "front"), ("stop", "front"), ("start", "front")]
    s = ctl.status()
    assert s["camera"] == "front" and s["state"] == "connecting" and s["width"] == 640
    ctl.shutdown()
    assert log[-1] == ("stop", "front") and ctl.status()["state"] == "idle"
