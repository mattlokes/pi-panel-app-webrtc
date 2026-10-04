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
        "http://frigate.home.local:1984/api/webrtc?src=front_door"


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


# --- reload -------------------------------------------------------------------------

def cfg(**kw):
    base = {"server": "http://x", "camera": [{"name": "front", "src": "front_sub"},
                                               {"name": "back", "src": "back_sub"}]}
    return Config.from_dict(base | kw)


def test_reload_policy_change_keeps_the_connection():
    ctl, log, clock = make(cameras=("front", "back"))
    ctl.config = cfg()
    ctl.set_visible(True)
    ctl.set_visible(False)
    ctl.reconfigure(cfg(keep_connected=True, linger_seconds=5))
    clock.t += 1000
    ctl.tick()
    assert log == [("start", "front")]           # never dropped: keep_connected now


def test_reload_turning_keep_connected_off_disconnects_when_hidden():
    ctl, log, clock = make(keep=True, cameras=("front", "back"))
    ctl.reconfigure(cfg(keep_connected=False, linger_seconds=0))
    assert log == [("start", "front"), ("stop", "front")]


def test_reload_changed_source_or_server_reconnects():
    ctl, log, _ = make(cameras=("front", "back"))
    ctl.config = cfg()
    ctl.set_visible(True)
    ctl.reconfigure(cfg(camera=[{"name": "front", "src": "front_main"}]))
    assert log[-2:] == [("stop", "front"), ("start", "front")]
    ctl.reconfigure(cfg(server="http://y", camera=[{"name": "front", "src": "front_main"}]))
    assert log[-2:] == [("stop", "front"), ("start", "front")]
    n = len(log)
    ctl.reconfigure(cfg(server="http://y", camera=[{"name": "front", "src": "front_main"}]))
    assert len(log) == n                         # nothing changed: nothing restarted


def test_reload_keeps_the_selected_camera_or_falls_back():
    ctl, log, _ = make(cameras=("front", "back"))
    ctl.config = cfg()
    ctl.set_visible(True)
    ctl.select("back")
    ctl.reconfigure(cfg(camera=[{"name": "side", "src": "s"}, {"name": "back", "src": "back_sub"}]))
    assert ctl.camera.name == "back" and log[-1] == ("start", "back")   # same camera, kept
    ctl.reconfigure(cfg(camera=[{"name": "side", "src": "s"}]))          # "back" removed
    assert ctl.camera.name == "side" and log[-2:] == [("stop", "back"), ("start", "side")]


# --- WebSocket client and go2rtc's WebSocket API ------------------------------------

import base64 as _b64
import hashlib as _hashlib
import json as _json
import socketserver as _ss
import struct as _struct

from pi_panel_webrtc.signalling import Go2rtcSocket
from pi_panel_webrtc.wsclient import WebSocket, WebSocketClosed, WebSocketError


def _frame(opcode, payload=b"", fin=True):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    head += bytes([n]) if n < 126 else bytes([126]) + _struct.pack("!H", n)
    return head + payload


class _WSHandler(_ss.StreamRequestHandler):
    """A tiny WebSocket server: behaviour picked by the request path."""

    def read_frame(self):
        b1, b2 = self.rfile.read(2)
        n = b2 & 0x7F
        if n == 126:
            n = _struct.unpack("!H", self.rfile.read(2))[0]
        mask = self.rfile.read(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(n)))
        return b1 & 0x0F, data

    def handle(self):
        request = self.rfile.readline().decode()
        path = request.split()[1]
        headers = {}
        while (line := self.rfile.readline().decode().strip()):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        accept = _b64.b64encode(_hashlib.sha1(
            (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if path == "/badaccept":
            accept = "nonsense"
        if path == "/refuse":
            self.wfile.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            return
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        w = self.wfile.write
        if path == "/echo":
            op, data = self.read_frame()
            w(_frame(0x1, data))
            self.read_frame()                                    # the client's close
        elif path == "/frag":
            w(_frame(0x1, b"hel", fin=False) + _frame(0x0, b"lo", fin=True))
        elif path == "/ping":
            w(_frame(0x9, b"hb"))
            op, data = self.read_frame()
            w(_frame(0x1, f"got {op:x} {data.decode()}".encode()))
        elif path == "/close":
            w(_frame(0x8, _struct.pack("!H", 1001)))
        elif path.startswith("/api/ws?src="):
            src = path.split("=", 1)[1]
            op, data = self.read_frame()
            offer = _json.loads(data)
            assert offer["type"] == "webrtc/offer" and offer["value"].startswith("v=0")
            if src == "missing":
                w(_frame(0x1, _json.dumps({"type": "error", "value": "streams: stream not found"}).encode()))
            else:
                for msg in ({"type": "webrtc/answer", "value": "v=0 answer"},
                            {"type": "webrtc/candidate", "value": "candidate:1 1 udp 1 192.168.4.97 8555 typ host"},
                            {"type": "webrtc/candidate", "value": ""},       # end-of-candidates: ignored
                            {"type": "stats", "value": "?"}):                # not for us: ignored
                    w(_frame(0x1, _json.dumps(msg).encode()))
            w(_frame(0x8, _struct.pack("!H", 1000)))


@pytest.fixture
def ws_server():
    server = _ss.ThreadingTCPServer(("127.0.0.1", 0), _WSHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_ws_echo(ws_server):
    ws = WebSocket.connect(f"ws://{ws_server}/echo")
    ws.send_text("hello " * 30)          # > 125 bytes: 16-bit length
    assert ws.recv() == "hello " * 30
    ws.close()


def test_ws_fragments_and_close(ws_server):
    ws = WebSocket.connect(f"ws://{ws_server}/frag")
    assert ws.recv() == "hello"
    ws2 = WebSocket.connect(f"ws://{ws_server}/close")
    with pytest.raises(WebSocketClosed, match="1001"):
        ws2.recv()


def test_ws_answers_pings(ws_server):
    ws = WebSocket.connect(f"ws://{ws_server}/ping")
    assert ws.recv() == "got a hb"       # the server received our pong (0xA) with its payload


def test_ws_handshake_failures(ws_server):
    with pytest.raises(WebSocketError, match="Sec-WebSocket-Accept"):
        WebSocket.connect(f"ws://{ws_server}/badaccept")
    with pytest.raises(WebSocketError, match="refused"):
        WebSocket.connect(f"ws://{ws_server}/refuse")
    with pytest.raises(WebSocketError, match="cannot connect"):
        WebSocket.connect("ws://127.0.0.1:9/x", timeout=2)
    with pytest.raises(WebSocketError, match="not a ws"):
        WebSocket.connect("http://example.invalid/")


def test_go2rtc_socket_url():
    assert Go2rtcSocket.url("http://frigate.home.local:1984", "front_door") == \
        "ws://frigate.home.local:1984/api/ws?src=front_door"
    assert Go2rtcSocket.url("https://h/go2rtc/", "a b") == "wss://h/go2rtc/api/ws?src=a%20b"
    with pytest.raises(SignallingError):
        Go2rtcSocket.url("ftp://h", "x")


def test_go2rtc_socket_messages(ws_server):
    sock = Go2rtcSocket.open(f"http://{ws_server}", "front_door")
    sock.send_offer("v=0 offer")
    got = []
    with pytest.raises(SignallingError, match="connection lost"):
        for kind, value in sock.messages():
            got.append((kind, value))
    assert got == [("answer", "v=0 answer"),
                   ("candidate", "candidate:1 1 udp 1 192.168.4.97 8555 typ host")]


def test_go2rtc_socket_error_message(ws_server):
    sock = Go2rtcSocket.open(f"http://{ws_server}", "missing")
    sock.send_offer("v=0 offer")
    assert next(iter(sock.messages())) == ("error", "streams: stream not found")
    sock.close()


# --- the signalling option -------------------------------------------------------------

def test_signalling_defaults_and_overrides():
    c = Config.from_dict({"server": "http://x", "camera": [
        {"name": "a", "src": "a"},
        {"name": "b", "src": "b", "signalling": "http"},
        {"name": "c", "whep_url": "http://m/c/whep"}]})
    assert [x.signalling for x in c.cameras] == ["websocket", "http", "http"]
    c = Config.from_dict({"server": "http://x", "signalling": "http", "camera": [
        {"name": "a", "src": "a"}, {"name": "b", "src": "b", "signalling": "websocket"}]})
    assert [x.signalling for x in c.cameras] == ["http", "websocket"]


@pytest.mark.parametrize("data,msg", [
    ({"server": "http://x", "signalling": "carrier-pigeon", "camera": [{"name": "a", "src": "a"}]}, "signalling"),
    ({"server": "http://x", "camera": [{"name": "a", "src": "a", "signalling": "grpc"}]}, "signalling"),
    ({"camera": [{"name": "a", "whep_url": "http://m/whep", "signalling": "websocket"}]}, "whep_url"),
])
def test_signalling_rejects(data, msg):
    with pytest.raises(ConfigError, match=msg):
        Config.from_dict(data)


def test_reload_changing_signalling_reconnects():
    ctl, log, _ = make(cameras=("front", "back"))
    ctl.config = cfg()
    ctl.set_visible(True)
    ctl.reconfigure(cfg(camera=[{"name": "front", "src": "front_sub", "signalling": "http"}]))
    assert log[-2:] == [("stop", "front"), ("start", "front")]


# --- H.265: the codec in the answer, and the decoder -------------------------------------

from pi_panel_webrtc.decoders import DecoderLadder
from pi_panel_webrtc.signalling import answer_codec


def _sdp(*media):
    return "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n" + "".join(media)


H265 = "m=video 9 UDP/TLS/RTP/SAVPF 97\r\na=rtpmap:97 H265/90000\r\n"
H264 = "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H264/90000\r\n"


@pytest.mark.parametrize("sdp,codec", [
    (_sdp(H265), "h265"),
    (_sdp(H264), "h264"),
    # both answered: the server's order wins
    (_sdp("m=video 9 UDP/TLS/RTP/SAVPF 97 96\r\na=rtpmap:96 H264/90000\r\na=rtpmap:97 H265/90000\r\n"), "h265"),
    (_sdp("m=video 9 UDP/TLS/RTP/SAVPF 96 97\r\na=rtpmap:96 H264/90000\r\na=rtpmap:97 H265/90000\r\n"), "h264"),
    # rtx first, and an audio section's H265-looking rtpmap is not ours
    (_sdp("m=audio 9 UDP/TLS/RTP/SAVPF 97\r\na=rtpmap:97 H264/90000\r\n",
          "m=video 9 UDP/TLS/RTP/SAVPF 99 97\r\na=rtpmap:99 rtx/90000\r\na=rtpmap:97 h265/90000\r\n"), "h265"),
    (_sdp("m=video 9 UDP/TLS/RTP/SAVPF 100\r\na=rtpmap:100 VP8/90000\r\n"), None),
    (_sdp("m=video 0 UDP/TLS/RTP/SAVPF 97\r\na=rtpmap:97 H265/90000\r\n"), None),   # rejected
    (_sdp(), None),
])
def test_answer_codec(sdp, codec):
    assert answer_codec(sdp) == codec


def test_decoder_ladder():
    ladder = DecoderLadder()
    assert ladder.pick("auto") == "v4l2"
    assert ladder.pick("software") == "software"
    assert ladder.failed("v4l2") is True            # auto changes its mind
    assert ladder.failed("v4l2") is False           # once
    assert ladder.pick("auto") == "software"
    assert ladder.pick("v4l2") == "v4l2"            # explicit: no fallback
    ladder.reset()
    assert ladder.pick("auto") == "v4l2"


def test_decoder_defaults_and_overrides():
    c = Config.from_dict({"server": "http://x", "camera": [
        {"name": "a", "src": "a"}, {"name": "b", "src": "b", "decoder": "software"}]})
    assert [x.decoder for x in c.cameras] == ["auto", "software"]
    c = Config.from_dict({"server": "http://x", "decoder": "v4l2", "camera": [
        {"name": "a", "src": "a"}, {"name": "b", "whep_url": "http://m/b/whep", "decoder": "auto"}]})
    assert [x.decoder for x in c.cameras] == ["v4l2", "auto"]


@pytest.mark.parametrize("data,msg", [
    ({"server": "http://x", "decoder": "gpu", "camera": [{"name": "a", "src": "a"}]}, "decoder"),
    ({"server": "http://x", "camera": [{"name": "a", "src": "a", "decoder": "ffmpeg"}]}, "decoder"),
])
def test_decoder_rejects(data, msg):
    with pytest.raises(ConfigError, match=msg):
        Config.from_dict(data)


def test_reload_changing_decoder_reconnects():
    ctl, log, _ = make(cameras=("front", "back"))
    ctl.config = cfg()
    ctl.set_visible(True)
    ctl.reconfigure(cfg(camera=[{"name": "front", "src": "front_sub", "decoder": "software"}]))
    assert log[-2:] == [("stop", "front"), ("start", "front")]


# --- which codecs to offer go2rtc ----------------------------------------------------

from pi_panel_webrtc.signalling import offer_codecs, stream_codecs, video_codecs


def test_video_codecs_from_go2rtc_stream():
    # front_door on go2rtc 1.9.14: RTSP producer running, ffmpeg producer not started
    stream = {"producers": [
        {"medias": ["video, recvonly, H264", "audio, recvonly, MPEG4-GENERIC/16000",
                    "audio, sendonly, PCMU/8000"]},
        {"url": "ffmpeg:front_door#audio=opus"}]}
    assert video_codecs(stream) == {"h264"}
    assert video_codecs({"producers": [{"medias": ["video, recvonly, H265"]}]}) == {"h265"}
    assert video_codecs({"producers": [{"medias": None}]}) == set()
    assert video_codecs({}) == set()


def test_offer_codecs():
    assert offer_codecs({"h264"}) == ["h264"]
    assert offer_codecs({"h265"}) == ["h265"]
    assert offer_codecs({"h264", "h265"}) == ["h265", "h264"]
    assert offer_codecs(set()) == ["h265", "h264"]          # not running yet: H.265 first
    assert offer_codecs({"mjpeg"}) == ["h265", "h264"]


def test_stream_codecs_over_http():
    import json as _j

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if "src=front_door" in self.path:
                body = _j.dumps({"producers": [{"medias": ["video, recvonly, H264"]}]}).encode()
                self.send_response(200)
            else:
                body = b"stream not found"
                self.send_response(404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    try:
        assert stream_codecs(base, "front_door") == {"h264"}
        assert stream_codecs(base, "missing") == set()
        assert stream_codecs("http://127.0.0.1:9", "x", timeout=1) == set()
    finally:
        server.shutdown()
