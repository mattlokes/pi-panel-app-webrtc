"""One WebRTC connection to one camera, with GStreamer's webrtcbin.

    webrtcbin (one recvonly video transceiver: H.265 preferred, or H.264)
      └─ pad-added, by the RTP encoding the server chose:
         H265, v4l2:     rtph265depay ─▶ h265parse ─▶ v4l2slh265dec (DMABuf) ─▶ proxysink
         H265, software: rtph265depay ─▶ h265parse ─▶ avdec_h265 ─▶ videoconvert BGRx ─▶ proxysink
         H264:           decodebin ─▶ videoconvert BGRx ─▶ proxysink

The proxysink feeds the always-running display pipeline (display.py), so the
window stays mapped however often this session is torn down and rebuilt. On
the v4l2 path the frames stay DMABufs all the way to the compositor.

H.265 never goes through decodebin. v4l2slh265dec outranks avdec_h265, so
decodebin would always pick it, and the decoder is a choice
(`decoder = auto | v4l2 | software`, see decoders.py). With "auto", a v4l2
failure switches the process to software.

Codecs: for a go2rtc camera, first ask go2rtc (`/api/streams?src=`) which
video codec the stream has. If it knows, offer only that one: go2rtc answers
a codec it can't serve, and then sends nothing (see
signalling.offer_codecs). Otherwise, and for WHEP servers, offer H.265 then
H.264.

Signalling: create the offer, set it locally, wait for our ICE gathering to
complete (about 0.35s), then, on a worker thread, per `camera.signalling`:

- "websocket": open go2rtc's /api/ws, send the offer, and apply the answer and
  the candidates go2rtc trickles after it. The socket stays open for the whole
  session, because go2rtc stops the stream when it closes.
- "http": POST the offer (go2rtc's /api/webrtc, or any WHEP endpoint) and set
  the answer. The server answers only after its own ICE gathering; on a
  LAN-only go2rtc with the default STUN server that is 7.8s.

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
from .decoders import DecoderLadder  # noqa: E402
from .signalling import (CODECS, Go2rtcSocket, SignallingError, answer_codec,  # noqa: E402
                         offer_codecs, post_offer, stream_codecs)

log = logging.getLogger("pi-panel-webrtc")

# The transceiver's codecs, offered in the order given (see offer_codecs).
RTP_CAPS = {
    "h265": "application/x-rtp,media=video,encoding-name=H265,payload=97,clock-rate=90000",
    "h264": "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000",
}
H265_DECODERS = {"v4l2": "v4l2slh265dec", "software": "avdec_h265"}
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
    def __init__(self, camera: Camera, server: str, display: Any, ladder: DecoderLadder,
                 on_change: Callable[[], None]) -> None:
        self.camera = camera
        self.server = server
        self.url = camera.offer_url(server)
        self.display = display          # display.Display: attach()/detach() our proxysink
        self.ladder = ladder
        self.on_change = on_change
        self._state = IDLE
        self._gen = 0
        self._pipeline: Gst.Pipeline | None = None
        self._webrtc: Gst.Element | None = None
        self._out: Gst.Element | None = None   # proxysink, read by the display
        self.codec: str | None = None           # "h264" | "h265", from the answer
        self.decoder: str | None = None         # "v4l2" | "software" | "libav"
        self._v4l2_branch: set[str] = set()     # element names, to blame bus errors
        self._coded = 0                         # buffers into the H.265 decoder
        self._keep: list[Any] = []          # descriptions webrtcbin still needs
        self._socket: Go2rtcSocket | None = None
        self._socket_lock = threading.Lock()   # the worker hands the socket over under it
        self._remote_set = False
        self._early_candidates: list[str] = []  # trickled before the answer was applied
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
            "codec": self.codec,
            "decoder": self.decoder,
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
        if self._out is not None:
            # Off screen first: waylandsink must let go of our buffers (the
            # decoder's DMABufs) before this pipeline goes away.
            self.display.detach(self._out)
            self._out = None
        if self._pipeline is not None:
            bus = self._pipeline.get_bus()
            bus.remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._webrtc = None
        self._keep.clear()
        with self._socket_lock:
            if self._socket is not None:
                # Also ends the worker thread blocked in recv(); its generation
                # is stale by now, so it reports nothing.
                self._socket.close()
                self._socket = None
        self._remote_set = False
        self._early_candidates.clear()

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
        self.codec = self.decoder = None
        self._v4l2_branch = set()
        self._coded = self.frames = 0
        self._set_state(CONNECTING)
        self._timer(CONNECT_TIMEOUT, self._connect_timeout)
        if not self.camera.src:
            self._build(list(CODECS))       # a WHEP server: offer both, H.265 first
            return
        # go2rtc: offer only the codec the stream is known to have, if any
        # (see offer_codecs). One quick HTTP request, off the main loop.
        gen = self._gen

        def probe() -> None:
            known = stream_codecs(self.server, self.camera.src or "")
            self._from_thread(gen, self._build, offer_codecs(known))
        threading.Thread(target=probe, name="webrtc-probe", daemon=True).start()

    def _build(self, codecs: list[str]) -> None:
        log.debug("%s: offering %s", self.camera.name, ", ".join(codecs))
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
                    Gst.Caps.from_string(";".join(RTP_CAPS[c] for c in codecs)))
        # Where the decoded video leaves: the display attaches to it once the
        # answer is in, and the media branch links to it on pad-added.
        out = Gst.ElementFactory.make("proxysink", "out")
        if out is None:
            self._retry("proxysink is missing (install gstreamer1.0-plugins-bad)")
            return
        pipe.add(out)
        out.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_frame)

        bus = pipe.get_bus()
        bus.add_signal_watch()
        gen = self._gen
        bus.connect("message::error", lambda _b, m: gen == self._gen and self._on_error(m))

        self._pipeline, self._webrtc, self._out = pipe, webrtc, out
        pipe.set_state(Gst.State.PLAYING)

    def _connect_timeout(self) -> None:
        if self._state != CONNECTING:
            return
        if self.decoder == "v4l2" and self._coded and not self.frames:
            # Video reached the decoder, and nothing came out.
            self._decoder_failed("no frames from the decoder")
            return
        self._retry(f"no video within {CONNECT_TIMEOUT:.0f}s")

    def _on_error(self, message: Gst.Message) -> None:
        reason = message.parse_error()[0].message
        if message.src is not None and message.src.get_name() in self._v4l2_branch:
            self._decoder_failed(reason)
        else:
            self._retry(f"pipeline error: {reason}")

    def _decoder_failed(self, reason: str) -> None:
        if self.ladder.failed("v4l2"):
            log.warning("%s: the H.265 hardware decoder failed (%s); using software from now on",
                        self.camera.name, reason)
        self._retry(f"H.265 hardware decoder: {reason}")

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

    def _from_thread(self, gen: int, fn: Callable[..., Any], *args: Any) -> None:
        """From a worker thread: run fn on the GLib loop if |gen| is still current."""
        def run() -> bool:
            if gen == self._gen:
                fn(*args)
            return False
        GLib.idle_add(run)

    def _post_offer(self) -> None:
        if self._posted or self._webrtc is None:
            return
        self._posted = True
        sdp = self._webrtc.get_property("local-description").sdp.as_text()
        log.debug("%s: offer:\n%s", self.camera.name, sdp)
        gen = self._gen

        if self.camera.signalling == "websocket":
            threading.Thread(target=self._websocket_worker, args=(gen, sdp),
                             name="webrtc-go2rtc-ws", daemon=True).start()
            return

        def worker() -> None:
            try:
                answer = post_offer(self.url, sdp)
            except SignallingError as exc:
                err = str(exc)
                GLib.idle_add(lambda: gen == self._gen and self._retry(err) and False)
                return
            GLib.idle_add(lambda: gen == self._gen and self._apply_answer(answer) and False)

        threading.Thread(target=worker, name="webrtc-signalling", daemon=True).start()

    def _websocket_worker(self, gen: int, sdp: str) -> None:
        try:
            sock = Go2rtcSocket.open(self.server, self.camera.src or "")
        except SignallingError as exc:
            self._from_thread(gen, self._retry, str(exc))
            return
        # Hand the socket over synchronously: if the session has already moved
        # on, close it here, or nothing would, and go2rtc would keep streaming
        # to a thread blocked in recv() forever.
        with self._socket_lock:
            if gen != self._gen:
                sock.close()
                return
            self._socket = sock
        try:
            sock.send_offer(sdp)
            for kind, value in sock.messages():
                if gen != self._gen:
                    break
                if kind == "answer":
                    self._from_thread(gen, self._apply_answer, value)
                elif kind == "candidate":
                    self._from_thread(gen, self._add_candidate, value)
                elif kind == "error":
                    self._from_thread(gen, self._retry, f"go2rtc: {value}")
                    break
        except SignallingError as exc:
            self._from_thread(gen, self._retry, str(exc))

    def _add_candidate(self, candidate: str) -> None:
        if self._webrtc is None:
            return
        if not self._remote_set:
            self._early_candidates.append(candidate)
            return
        self._webrtc.emit("add-ice-candidate", 0, candidate)

    def _apply_answer(self, text: str) -> None:
        log.debug("%s: answer:\n%s", self.camera.name, text)
        result, sdp = GstSdp.SDPMessage.new_from_text(text)
        if result != GstSdp.SDPResult.OK:
            self._retry("the server's SDP answer did not parse")
            return
        self.codec = answer_codec(text)
        if self.codec is None:
            self._retry("the stream is neither H.265 nor H.264 (in go2rtc use e.g. "
                        "`ffmpeg:<stream>#video=h264`)")
            return
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
        self._keep.append(answer)
        self._webrtc.emit("set-remote-description", answer, None)
        self._remote_set = True
        for candidate in self._early_candidates:
            self._webrtc.emit("add-ice-candidate", 0, candidate)
        self._early_candidates.clear()
        # Before any media: the decoder negotiates (DMABuf or not) with
        # waylandsink through the display's selector, which answers only on
        # its active pad. The last status frame stays up until ours arrive.
        self.display.attach(self._out)

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

    # Runs on a webrtcbin streaming thread. Building and linking elements is
    # fine there; anything touching session state goes through _on_main.

    def _on_pad_added(self, _webrtc: Gst.Element, pad: Gst.Pad) -> None:
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        pipe = self._pipeline
        if pipe is None:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        encoding = str(_field(caps.get_structure(0), "encoding-name") or "").upper()
        if encoding in ("H265", "HEVC"):
            self._add_h265(pipe, pad)
            return
        self.decoder = "libav"      # the Pi 5 has no H.264 hardware: avdec_h264
        decode = Gst.ElementFactory.make("decodebin")
        decode.connect("pad-added", self._on_decoded_pad)
        pipe.add(decode)
        decode.sync_state_with_parent()
        pad.link(decode.get_static_pad("sink"))

    def _add_h265(self, pipe: Gst.Pipeline, pad: Gst.Pad) -> None:
        decoder = self.ladder.pick(self.camera.decoder)
        names = ["rtph265depay", "h265parse", H265_DECODERS[decoder]]
        elements = [Gst.ElementFactory.make(n) for n in names]
        missing = [n for n, e in zip(names, elements) if e is None]
        if missing:
            reason = f"{', '.join(missing)} missing"
            self._on_main(self._decoder_failed if decoder == "v4l2" else self._retry, reason)
            return
        self.decoder = decoder
        if decoder == "v4l2":
            # Keep the frames DMABufs: waylandsink hands them to the compositor,
            # whose GPU reads the SAND tiles. Detiling on the CPU (videoconvert)
            # costs more than decoding in software.
            only_dmabuf = Gst.ElementFactory.make("capsfilter")
            only_dmabuf.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:DMABuf)"))
            elements.append(only_dmabuf)
            self._v4l2_branch = {e.get_name() for e in elements} | {self._out.get_name()}
            elements[2].get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_coded)
        else:
            elements += self._to_bgrx()
        for element in elements:
            pipe.add(element)
            element.sync_state_with_parent()
        for a, b in zip(elements, elements[1:] + [self._out]):
            a.link(b)
        pad.link(elements[0].get_static_pad("sink"))
        log.info("%s: H.265, %s decoder", self.camera.name, decoder)

    def _on_decoded_pad(self, _decode: Gst.Element, pad: Gst.Pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps.to_string().startswith("video/"):
            return
        pipe = self._pipeline
        elements = self._to_bgrx()
        for element in elements:
            pipe.add(element)
            element.sync_state_with_parent()
        for a, b in zip(elements, elements[1:] + [self._out]):
            a.link(b)
        pad.link(elements[0].get_static_pad("sink"))

    @staticmethod
    def _to_bgrx() -> list[Gst.Element]:
        """Software-decoded frames to what waylandsink's shm path takes."""
        convert = Gst.ElementFactory.make("videoconvert")
        bgrx = Gst.ElementFactory.make("capsfilter")
        bgrx.set_property("caps", Gst.Caps.from_string("video/x-raw,format=BGRx"))
        return [convert, bgrx]

    def _on_coded(self, _pad: Gst.Pad, _info: Any) -> Gst.PadProbeReturn:
        self._coded += 1
        return Gst.PadProbeReturn.OK

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
