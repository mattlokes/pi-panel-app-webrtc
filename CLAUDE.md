# pi-panel-app-webrtc

A pi-panel app that shows a full-screen WebRTC camera feed from go2rtc or a
WHEP server. It uses GStreamer webrtcbin through PyGObject. The README covers
usage.

```bash
uv run --no-project --with pytest pytest     # offline; GStreamer isn't needed for these
```

The go2rtc server is `http://frigate.home.local:1984` (1.9.10), with streams
`front_door` and `front_door_sub`.

## Design points worth preserving

- **System Python, no venv.** PyGObject and gst-python come from apt and must match the system GStreamer. The manifest runs `/usr/bin/python3` and sets `PYTHONPATH` to `src/` plus pi-panel-varlink's source, using a relative layout path. Don't add pip dependencies.
- **Two pipelines.** The display pipeline (intervideosrc → waylandsink) runs for the life of the process, so the window stays mapped: pi-panel-ctl can only switch to a mapped window. Sessions feed it through an intervideo channel.
- **`controller.py` has no GStreamer.** It holds the connection policy (visible, linger, keep_connected) and camera switching, and is unit-tested with a fake session and clock. Keep policy there.
- **Every webrtcbin callback marshals to the GLib loop,** tagged with a generation (`CameraSession._on_main` / `_timer`). A late callback from a torn-down connection must never touch the next one.
- **Promise replies need `python3-gst-1.0`,** the gst-python overrides. Without them, `promise.get_reply().get_value("offer")` hands back memory the promise still owns, and the process segfaults or silently passes a NULL description. Also keep every session description referenced (`self._keep`) until webrtcbin has applied it; it is applied asynchronously.
- **Non-trickle signalling.** Wait for `ice-gathering-state == complete`, then POST. go2rtc doesn't accept trickled candidates.
- **H.264 only.** The Pi 5 has no H.264 hardware decoder, so decoding uses `avdec_h264` on the CPU. Prefer `_sub` streams.
- **waylandsink scales through `wp_viewporter`,** which the pi-panel compositor provides for this reason.
