# pi-panel-app-webrtc

A pi-panel app that shows a full-screen WebRTC camera feed from go2rtc or a
WHEP server. It uses GStreamer webrtcbin through PyGObject. The README covers
usage.

```bash
uv run --no-project --with pytest pytest     # offline; GStreamer isn't needed for these
```

The go2rtc server is `http://frigate.home.local:1984` (192.168.4.145; go2rtc
1.9.14 inside Frigate 0.18, whose UI and API are on :5000). The streams:
- `front_door`: H.264 from the Reolink doorbell over RTSP, 2560×1920 at about 10 fps. The app costs about 45% of one core on it. Frigate pulls this stream constantly.
- `front_door_sub`: H.264, 640×480.
- `front_door_hevc`: an on-demand `exec:` transcode on the Frigate host's AMD GPU (VAAPI `hevc_vaapi`), H.265 Main 1536×1152 at 25 fps, 6 Mbps. The app costs about 5% of one core on it (v4l2), or about 50% with `decoder = "software"`.

jazz must run kernel ≥ 6.18 for the HEVC decoder (it runs 6.18.50+rpt).

## Design points worth preserving

- **System Python, no venv.** PyGObject and gst-python come from apt and must match the system GStreamer. The manifest runs `/usr/bin/python3` and sets `PYTHONPATH` to `src/` plus pi-panel-varlink's source, using a relative layout path. Don't add pip dependencies.
- **Two pipelines, joined by proxysink → proxysrc.** The display pipeline (`input-selector` between a black/textoverlay status branch and a `proxysrc`, then waylandsink) runs for the life of the process, so the window stays mapped: pi-panel-ctl can only switch to a mapped window. Each session ends in a `proxysink`, which `Display.attach` plugs in. Alternatives measured on jazz:
  - The intervideo elements copy frames to system memory, which rules out zero copy.
  - appsink/appsrc don't forward queries, so the decoder can't negotiate DMABuf (not-negotiated).
  - `glupload ! glcolorconvert ! gldownload` on the SAND frames gives **black frames**.
- **Attach when the answer is applied; detach before teardown.** input-selector answers caps and allocation queries only on its active pad. If the decoder negotiates while the video pad is inactive, it fails ("No valid frames decoded"). Use `sync-streams=false`, or the inactive pad blocks. Detach before setting the session pipeline to NULL, so waylandsink lets go of the decoder's buffers.
- **H.265 decodes on v4l2slh265dec, zero copy.**
  - The decoder outputs Broadcom SAND128 (`NV12:0x0700000000000004`) DMABufs. The pi-panel compositor (wlroots 0.19, GLES2) advertises that modifier, so waylandsink hands the frames over untouched.
  - **Never let the frames be detiled on the CPU:** `v4l2slh265dec ! videoconvert` cost 5.4s of CPU per 201 frames, against 2.7s for decoding the same clip in software.
  - It needs kernel ≥ 6.18. On 6.12, `rpi-hevc-dec` doesn't implement `VIDIOC_ENUM_FRAMESIZES`, and GStreamer fails with "Unsupported pixel format / format UNKNOWN".
- **No decodebin for H.265.** `v4l2slh265dec` (rank 257) outranks `avdec_h265` (256). The decoder is chosen explicitly by `decoders.DecoderLadder`: with `auto`, a v4l2 failure (a bus error in its branch, a missing element, or coded input but no frames) switches the process to software until a reload.
- **Offer only the codec go2rtc knows the stream has** (`/api/streams?src=`). Offered H.265 first for the H.264 `front_door`, go2rtc 1.9.14 answered H.265 and sent nothing. Its not-yet-started `ffmpeg:` producer seems to match any codec. When go2rtc doesn't know the codec, offer H.265 then H.264.
- **`hevc_vaapi` dimensions must be multiples of 64.** At 1440×1080, Frigate's encoder sent 1472×1088 with no conformance window, so every decoder showed garbage edges.
- **go2rtc's exec transcode can wedge.** On 2026-10-04, after a lot of rapid connect/disconnect testing, `front_door_hevc` stopped (`read … i/o timeout`), and every later start logged `[exec] timeout`; even a plain RTSP probe failed. It was the host's **amdgpu** hanging, not go2rtc. After a Frigate restart, Frigate logged "Did not detect hwaccel", and every VAAPI ffmpeg, including Frigate's own detect, failed with `amdgpu_query_gpu_info_init failed` and then hung. Restarting Frigate doesn't help; the host needs a reboot. The app sees it as "no video within 20s". Logs: `GET http://frigate.home.local:5000/api/logs/go2rtc` (and `/api/logs/frigate`).
- **Open: one SEGV during switch churn.** It happened once on 2026-10-04: next, reconnect and next again at 1–1.5s intervals, H.265 (v4l2) ⇄ H.264, under the unit. About 140 more iterations, including randomised gaps down to 0.1s, didn't reproduce it. The manifest sets `PYTHONFAULTHANDLER=1`, so the next one leaves every thread's Python stack in the journal.
- **`controller.py` has no GStreamer.** It holds the connection policy (visible, linger, keep_connected) and camera switching, and is unit-tested with a fake session and clock. Keep policy there.
- **Every webrtcbin callback marshals to the GLib loop,** tagged with a generation (`CameraSession._on_main` / `_timer`). A late callback from a torn-down connection must never touch the next one.
- **Promise replies need `python3-gst-1.0`,** the gst-python overrides. Without them, `promise.get_reply().get_value("offer")` hands back memory the promise still owns, and the process segfaults or silently passes a NULL description. Also keep every session description referenced (`self._keep`) until webrtcbin has applied it; it is applied asynchronously.
- **Use go2rtc's WebSocket API, not HTTP** (`signalling = "websocket"`, the default). Measured on jazz:
  - Over HTTP, `POST /api/webrtc` holds its answer until go2rtc's own ICE gathering finishes, including its default STUN server. That server is unreachable from the LAN-only Frigate, so every answer took 7.8s, even with the camera already streaming.
  - Over `/api/ws`, the answer arrives in about 10ms, and candidates are trickled afterwards.
  - The WebSocket must stay open for the whole session: go2rtc ties the consumer to it. The worker hands the socket to the session under `_socket_lock`, so a teardown that happens mid-handshake still closes it. Otherwise the thread would block in `recv()` forever and go2rtc would keep streaming.
  - A dropped consumer disappears from go2rtc's `/api/streams` within about 3–6s of a reconnect; there is no leak.
- **We always send a complete offer.** Wait for `ice-gathering-state == complete` (about 0.35s) before sending it, on either path.
- **H.264 is software only.** The Pi 5 has no H.264 hardware decoder, so H.264 decodes with `avdec_h264`, then `videoconvert` to BGRx, because waylandsink's shm path needs RGB.
- **waylandsink scales through `wp_viewporter`,** which the pi-panel compositor provides for this reason.
