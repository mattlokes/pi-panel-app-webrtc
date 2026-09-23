"""One WebRTC connection to one camera, with GStreamer's webrtcbin.

    webrtcbin (one recvonly H.264 transceiver)
      └─ pad-added ─▶ decodebin ─▶ videoconvert ─▶ intervideosink channel=<display>

The decoded frames go to the always-running display pipeline through an
intervideo channel, so the window stays mapped however often this session is
torn down and rebuilt.

Signalling is non-trickle, WHEP style: create the offer, set it locally,
wait for ICE gathering to complete, POST it (on a worker thread), then set
the answer.

Hard-won details:
- webrtcbin signals arrive on its own threads. Every handler here only
  schedules work onto the GLib main loop (`_on_main`), tagged with the
  session's generation, so a callback from a torn-down connection can never
  touch the next one.
- A session description handed to webrtcbin must stay referenced until
  webrtcbin has used it (it is applied asynchronously). Promise replies need
  gst-python's overrides (python3-gst-1.0): without them, reading the offer
  out of the reply frees memory the promise still owns.

The session retries by itself until stopped: after failure, a
`disconnected` that lasts 5s, no answer or no frames within 20s, or a stall
of 10s. The backoff goes 1, 2, 4 … 30s.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

from .config import Camera  # noqa: E402
from .signalling import SignallingError, post_offer  # noqa: E402

log = logging.getLogger("pi-panel-webrtc")

VIDEO_CAPS = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
CONNECT_TIMEOUT = 20.0     # offer → first frame
STALL_TIMEOUT = 10.0       # playing, but no frame for this long
DISCONNECT_GRACE = 5.0     # ICE "disconnected" often recovers on its own
BACKOFF_MIN, BACKOFF_MAX = 1.0, 30.0

CONNECTING, PLAYING, RETRYING, IDLE = "connecting", "playing", "retrying", "idle"


def _field(structure: Any, name: str) -> Any:
    """Read a Gst.Structure field. With gst-python's overrides (1.26),
    Caps.get_structure() returns a StructureWrapper that must be entered:
    `with caps.get_structure(0) as s`. A promise reply is a plain Structure."""
    if hasattr(structure, "__enter__"):
        with structure as s:
            return s.get_value(name)
    return structure.get_value(name)


class CameraSession:
    def __init__(self, camera: Camera, server: str, channel: str,
                 on_change: Callable[[], None]) -> None:
        self.camera = camera
        self.url = camera.offer_url(server)
        self.channel = channel
        self.on_change = on_change
        self._state = IDLE
        self._gen = 0
        self._pipeline: Gst.Pipeline | None = None
        self._webrtc: Gst.Element | None = None
        self._keep: list[Any] = []          # descriptions webrtcbin still needs
        self._timers: list[int] = []
        self._backoff = BACKOFF_MIN
        self._posted = False
        # Written from the streaming thread by the frame probe; plain ints/floats.
        self.frames = 0
        self._last_frame = 0.0
        self.width: int | None = None
        self.height: int | None = None
        self.error: str | None = None
        self._since: float | None = None
        self._retry_at: float | None = None

    # --- Session protocol --------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "error": self.error,
            "width": self.width,
            "height": self.height,
            "frames": self.frames,
            "connected_for": round(now - self._since, 1) if self._since and self._state == PLAYING else None,
            "retry_in": round(max(0.0, self._retry_at - now), 1) if self._retry_at and self._state == RETRYING else None,
        }

    def start(self) -> None:
        self._gen += 1
        self._connect()

    def stop(self) -> None:
        self._gen += 1          # orphan every pending callback
        self._teardown()
        self._set_state(IDLE)

    # --- plumbing ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != self._state:
            log.info("%s: %s%s", self.camera.name, state,
                     f" ({self.error})" if state == RETRYING and self.error else "")
            self._state = state
            self.on_change()

    def _on_main(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run fn on the GLib loop, unless the session has moved on by then."""
        gen = self._gen

        def run() -> bool:
            if gen == self._gen:
                fn(*args)
            return False
        GLib.idle_add(run)

    def _timer(self, seconds: float, fn: Callable[[], Any]) -> None:
        gen = self._gen
        source: list[int] = []

        def run() -> bool:
            # A fired source is gone: forget it, or teardown would remove a
            # stale id (GLib warns, and the id may even have been reused).
            if source and source[0] in self._timers:
                self._timers.remove(source[0])
            if gen == self._gen:
                fn()
            return False
        source.append(GLib.timeout_add(int(seconds * 1000), run))
        self._timers.append(source[0])

    def _teardown(self) -> None:
        for t in self._timers:
            GLib.source_remove(t)
        self._timers.clear()
        if self._pipeline is not None:
            bus = self._pipeline.get_bus()
            bus.remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._webrtc = None
        self._keep.clear()

    def _retry(self, reason: str) -> None:
        log.warning("%s: %s", self.camera.name, reason)
        self._gen += 1
        self._teardown()
        self.error = reason
        delay = self._backoff
        self._backoff = min(self._backoff * 2, BACKOFF_MAX)
        self._retry_at = time.monotonic() + delay
        self._set_state(RETRYING)
        self._timer(delay, self._connect)

    # --- connecting ----------------------------------------------------------

    def _connect(self) -> None:
        self._teardown()
        self._posted = False
        self.width = self.height = None
        self._set_state(CONNECTING)

        pipe = Gst.Pipeline.new(f"webrtc-{self.camera.name}")
        webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        if webrtc is None:
            self._retry("webrtcbin is missing (install gstreamer1.0-plugins-bad and gstreamer1.0-nice)")
            return
        webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        pipe.add(webrtc)
        webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        webrtc.connect("notify::ice-gathering-state", self._on_gathering)
        webrtc.connect("notify::connection-state", self._on_connection_state)
        webrtc.connect("pad-added", self._on_pad_added)
        webrtc.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY,
                    Gst.Caps.from_string(VIDEO_CAPS))

        bus = pipe.get_bus()
        bus.add_signal_watch()
        gen = self._gen
        bus.connect("message::error", lambda _b, m: gen == self._gen and self._retry(
            f"pipeline error: {m.parse_error()[0].message}"))

        self._pipeline, self._webrtc = pipe, webrtc
        pipe.set_state(Gst.State.PLAYING)
        self._timer(CONNECT_TIMEOUT, lambda: self._state == CONNECTING and self._retry(
            f"no video within {CONNECT_TIMEOUT:.0f}s"))

    def _on_negotiation_needed(self, webrtc: Gst.Element) -> None:
        webrtc.emit("create-offer", None, Gst.Promise.new_with_change_func(self._on_offer, None))

    def _on_offer(self, promise: Gst.Promise, _: Any) -> None:
        promise.wait()
        reply = promise.get_reply()
        offer = _field(reply, "offer") if reply is not None else None
        if offer is None:
            self._on_main(self._retry, "webrtcbin could not create an offer")
            return
        self._keep.append(offer)
        self._webrtc.emit("set-local-description", offer, None)

    def _on_gathering(self, webrtc: Gst.Element, _pspec: Any) -> None:
        if webrtc.get_property("ice-gathering-state") != GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            return
        self._on_main(self._post_offer)

    def _post_offer(self) -> None:
        if self._posted or self._webrtc is None:
            return
        self._posted = True
        sdp = self._webrtc.get_property("local-description").sdp.as_text()
        gen = self._gen

        def worker() -> None:
            try:
                answer = post_offer(self.url, sdp)
            except SignallingError as exc:
                err = str(exc)
                GLib.idle_add(lambda: gen == self._gen and self._retry(err) and False)
                return
            GLib.idle_add(lambda: gen == self._gen and self._apply_answer(answer) and False)

        threading.Thread(target=worker, name="webrtc-signalling", daemon=True).start()

    def _apply_answer(self, text: str) -> None:
        result, sdp = GstSdp.SDPMessage.new_from_text(text)
        if result != GstSdp.SDPResult.OK:
            self._retry("the server's SDP answer did not parse")
            return
        if "H264" not in text:
            self._retry("the stream is not H.264 (in go2rtc use e.g. "
                        "`ffmpeg:<stream>#video=h264`, or pick the _sub stream)")
            return
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
        self._keep.append(answer)
        self._webrtc.emit("set-remote-description", answer, None)

    def _on_connection_state(self, webrtc: Gst.Element, _pspec: Any) -> None:
        state = webrtc.get_property("connection-state")
        self._on_main(self._connection_changed, state)

    def _connection_changed(self, state: Any) -> None:
        S = GstWebRTC.WebRTCPeerConnectionState
        if state == S.FAILED:
            self._retry("WebRTC connection failed")
        elif state == S.DISCONNECTED:
            self._timer(DISCONNECT_GRACE, lambda: self._webrtc is not None and
                        self._webrtc.get_property("connection-state") == S.DISCONNECTED and
                        self._retry("WebRTC connection lost"))

    # --- media -------------------------------------------------------------

    def _on_pad_added(self, _webrtc: Gst.Element, pad: Gst.Pad) -> None:
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        pipe = self._pipeline
        if pipe is None:
            return
        decode = Gst.ElementFactory.make("decodebin")
        decode.connect("pad-added", self._on_decoded_pad)
        pipe.add(decode)
        decode.sync_state_with_parent()
        pad.link(decode.get_static_pad("sink"))

    def _on_decoded_pad(self, _decode: Gst.Element, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps.to_string().startswith("video/"):
            return
        pipe = self._pipeline
        convert = Gst.ElementFactory.make("videoconvert")
        sink = Gst.ElementFactory.make("intervideosink")
        sink.set_property("channel", self.channel)
        for element in (convert, sink):
            pipe.add(element)
            element.sync_state_with_parent()
        convert.link(sink)
        pad.link(convert.get_static_pad("sink"))
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_frame)

    def _on_frame(self, pad: Gst.Pad, _info: Any) -> Gst.PadProbeReturn:
        self.frames += 1
        self._last_frame = time.monotonic()
        if self._state != PLAYING:
            self._on_main(self._playing)
        if self.width is None:
            caps = pad.get_current_caps()
            if caps is not None:
                s = caps.get_structure(0)
                self.width, self.height = _field(s, "width"), _field(s, "height")
        return Gst.PadProbeReturn.OK

    def _playing(self) -> None:
        if self._state == PLAYING:
            return
        self._backoff = BACKOFF_MIN
        self.error = None
        self._since = time.monotonic()
        self._set_state(PLAYING)
        self._watch_stall()

    def _watch_stall(self) -> None:
        def check() -> None:
            if time.monotonic() - self._last_frame > STALL_TIMEOUT:
                self._retry(f"no video for {STALL_TIMEOUT:.0f}s")
            else:
                self._watch_stall()
        self._timer(2.0, check)
