# pi-panel-app-webrtc

A pi-panel app that shows a full-screen WebRTC camera feed from go2rtc or a
WHEP server. It uses GStreamer webrtcbin through PyGObject. The README covers
usage.

```bash
uv run --no-project --with pytest pytest     # offline; GStreamer isn't needed for these
```

The go2rtc server is `http://frigate.home.local:1984` (1.9.10, running inside
Frigate with its default config). Its streams, both from a Reolink doorbell
over RTSP and both H.264:
- `front_door`: 2560×1920 at about 10 fps. The app costs about 80% of one core on it. Frigate pulls this stream constantly.
- `front_door_sub`: 640×480. The app costs about 6% of one core on it.

## Design points worth preserving

- **System Python, no venv.** PyGObject and gst-python come from apt and must match the system GStreamer. The manifest runs `/usr/bin/python3` and sets `PYTHONPATH` to `src/` plus pi-panel-varlink's source, using a relative layout path. Don't add pip dependencies.
- **Two pipelines.** The display pipeline (intervideosrc → waylandsink) runs for the life of the process, so the window stays mapped: pi-panel-ctl can only switch to a mapped window. Sessions feed it through an intervideo channel.
- **`controller.py` has no GStreamer.** It holds the connection policy (visible, linger, keep_connected) and camera switching, and is unit-tested with a fake session and clock. Keep policy there.
- **Every webrtcbin callback marshals to the GLib loop,** tagged with a generation (`CameraSession._on_main` / `_timer`). A late callback from a torn-down connection must never touch the next one.
- **Promise replies need `python3-gst-1.0`,** the gst-python overrides. Without them, `promise.get_reply().get_value("offer")` hands back memory the promise still owns, and the process segfaults or silently passes a NULL description. Also keep every session description referenced (`self._keep`) until webrtcbin has applied it; it is applied asynchronously.
- **Use go2rtc's WebSocket API, not HTTP** (`signalling = "websocket"`, the default). Measured on jazz:
  - Over HTTP, `POST /api/webrtc` holds its answer until go2rtc's own ICE gathering finishes, including its default STUN server. That server is unreachable from the LAN-only Frigate, so every answer took 7.8s, even with the camera already streaming.
  - Over `/api/ws`, the answer arrives in about 10ms, and candidates are trickled afterwards.
  - The WebSocket must stay open for the whole session: go2rtc ties the consumer to it. The worker hands the socket to the session under `_socket_lock`, so a teardown that happens mid-handshake still closes it. Otherwise the thread would block in `recv()` forever and go2rtc would keep streaming.
  - A dropped consumer disappears from go2rtc's `/api/streams` within about 3–6s of a reconnect; there is no leak.
- **We always send a complete offer.** Wait for `ice-gathering-state == complete` (about 0.35s) before sending it, on either path.
- **H.264 only.** The Pi 5 has no H.264 hardware decoder, so decoding uses `avdec_h264` on the CPU. The user chose the main stream for its picture quality, knowing it costs about 80% of one core; `_sub` costs about 6%.
- **waylandsink scales through `wp_viewporter`,** which the pi-panel compositor provides for this reason.
