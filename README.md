# pi-panel-app-webrtc

A pi-panel app that shows a live camera feed full screen over WebRTC. It works
with [go2rtc](https://github.com/AlexxIT/go2rtc) (standalone, Frigate's
built-in one, or the Home Assistant add-on), or any
[WHEP](https://www.ietf.org/archive/id/draft-ietf-wish-whep-01.html) server
such as MediaMTX.

It is made for event pop-ups, such as the doorbell camera on screen for a
minute when someone rings. It stays out of the normal rotation
(`rotate = false`), and only connects while it is on screen.

## Install

The app uses GStreamer from the system, so a few apt packages come first (one
time, as root):

```bash
sudo apt install python3-gi python3-gst-1.0 gir1.2-gst-plugins-bad-1.0 \
     gstreamer1.0-plugins-bad gstreamer1.0-plugins-good gstreamer1.0-libav gstreamer1.0-nice
```

`pi-panel-pkg` checks these and prints the exact command if any are missing.
Then, as the panel user:

```bash
pi-panel-pkg install https://github.com/mattlokes/pi-panel-app-webrtc.git
$EDITOR ~/.config/pi-panel/apps/webrtc/config.toml      # server + cameras
pi-panel-ctl enable webrtc
pi-panel-ctl show webrtc --seconds 30                   # try it
```

## Configuration

```toml
server = "http://frigate.home.local:1984"   # go2rtc
keep_connected = false     # true: always connected, so there is no delay when it pops up
linger_seconds = 30        # stay connected this long after being hidden
signalling = "websocket"   # or "http"; can also be set per camera
decoder = "auto"           # H.265: "auto" (hardware, else software), "v4l2" or "software"

[[camera]]
name = "front_door"        # used in action names: camera_front_door
label = "Front door"
src = "front_door"         # go2rtc stream name, or: whep_url = "http://…/whep"
```

### Applying changes

Edit `config.toml`, then reload. No restart is needed:

```bash
pi-panel-ctl action webrtc reload                       # or:
systemctl kill -s HUP pi-panel-app@webrtc              # or:
pi-panel-ctl app webrtc io.pipanel.app.WebRTC.Reload    # reports errors
```

- **Policy changes** (`keep_connected`, `linger_seconds`) apply at once, without dropping the feed.
- **Changing a camera's source, or the server,** reconnects that camera.
- **Adding or removing cameras** updates the actions (and the Home Assistant buttons).
- **If the new file is invalid,** the running configuration stays in effect. The error is logged, and `Reload` returns it as `InvalidConfig`.

**H.265 or H.264.** The Pi 5 decodes H.265 in hardware, and the frames go to
the screen without being copied. It has no H.264 hardware, so H.264 is
decoded on the CPU. Measured on a Pi 5, for the whole app (decode and
display):

| Stream | Codec | Resolution | Decoder | CPU |
|---|---|---|---|---|
| go2rtc transcode (`front_door_hevc`) | H.265 | 1536×1152, 25 fps | v4l2 (hardware) | ~5% of one core |
| the same | H.265 | 1536×1152, 25 fps | software | ~50% of one core |
| Reolink main (`front_door`) | H.264 | 2560×1920, ~10 fps | software | ~45% of one core |
| Reolink sub (`front_door_sub`) | H.264 | 640×480, ~16–21 fps | software | a few % |

With `keep_connected = true`, you pay that cost all the time, not just while
the feed is on screen.

**How long before the picture appears.** With the default
`signalling = "websocket"`, the picture appears **1.2–2.3s** after the feed is
shown or reconnected. That time is WebRTC setup plus the camera's next
keyframe. With `signalling = "http"`, it takes about 9s, because go2rtc's HTTP
API answers only after gathering its own network addresses, and a LAN-only
go2rtc spends about 8s waiting for its default STUN server to time out.
`keep_connected = true` removes even the 1–2s, since the feed is already
playing when it's shown.

`whep_url` cameras always use HTTP, which WHEP requires. If such a server is
slow to answer, give it a LAN-only ICE configuration with no public STUN
server.

### H.265

H.265 is used whenever the stream is H.265: nothing needs setting. The app
asks go2rtc which codec the stream has, and offers only that one. If go2rtc
doesn't know yet (the stream's source hasn't started), it offers H.265 and
then H.264. Offering H.265 to an H.264 stream is unsafe: go2rtc 1.9.14 can
answer H.265 and then send nothing.

The hardware decoder needs **kernel 6.18 or newer**. On 6.12, `rpi-hevc-dec`
doesn't report its frame sizes, so GStreamer fails with "Unsupported pixel
format". `decoder = "auto"` (the default) falls back to software if the
hardware decoder fails, until the next reload.

If the camera itself can't send H.265, go2rtc can transcode. For example, with
VAAPI (Frigate's go2rtc; the host has an AMD GPU):

```yaml
front_door_hevc:
  - "exec:ffmpeg -hide_banner -v error -rtsp_transport tcp -hwaccel vaapi
     -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi
     -i rtsp://127.0.0.1:8554/front_door -an -vf scale_vaapi=w=1536:h=1152
     -c:v hevc_vaapi -profile:v main -g 25 -bf 0 -b:v 6M -f rtsp {output}"
```

**Keep both dimensions multiples of 64** (1280×960, 1536×1152, 2048×1536,
or the camera's own 2560×1920). `hevc_vaapi` pads the picture up to the next
multiple of 64 without marking the padding to be cropped, so 1440×1080
arrives as 1472×1088 with a smeared strip on the right and a green bar at the
bottom, on every decoder. Signalling the crop instead (e.g. with ffmpeg's
`h265_metadata` bitstream filter) would fix the picture, but GStreamer 1.26.2
adds an extra copy for cropped frames (RPi-Distro/repo#406). Aligned
dimensions avoid both problems.

A stream that is neither H.265 nor H.264 is reported as such. Have go2rtc
transcode it, e.g. `front_door_h264: ffmpeg:front_door#video=h264`.

## Doorbell automation

With the MQTT plugin installed, every action becomes a Home Assistant button,
and the same commands are available on MQTT:

```yaml
# Home Assistant automation action
- action: mqtt.publish
  data: {topic: pi-panel/jazz/command/app/webrtc/action/camera_front_door}
- action: mqtt.publish
  data:
    topic: pi-panel/jazz/command/show
    payload: '{"app": "webrtc", "seconds": 60, "priority": 50}'
```

Priority 50 puts it above schedules, so the feed shows even when a night
schedule has turned the display off. It returns to what was showing after 60
seconds.

## Control

```bash
pi-panel-ctl action webrtc next                      # also: previous, reconnect, reload, camera_<name>
pi-panel-ctl app webrtc io.pipanel.app.WebRTC.GetStatus
pi-panel-ctl app webrtc io.pipanel.app.WebRTC.SelectCamera '{"name": "front_door"}'
journalctl -u pi-panel-app@webrtc -f
```

`GetStatus` reports:
- the state (`idle`, `connecting`, `playing` or `retrying`);
- the error, if there is one;
- the codec (`h265` or `h264`) and the decoder in use (`v4l2`, `software`, or `libav` for H.264);
- the video size;
- the number of decoded frames.

While a feed is connecting or retrying, the screen shows the camera's name
and the reason. A lost feed is retried with backoff (1s up to 30s).

## How it works

```
display (always running):
    videotestsrc (black) ─▶ textoverlay ─▶ ┐
    proxysrc ─────────────────────────────▶ input-selector ─▶ waylandsink
session (per connection):
    webrtcbin ─▶ rtph265depay ─▶ h265parse ─▶ v4l2slh265dec ─▶ proxysink    (H.265, hardware)
    webrtcbin ─▶ rtph265depay ─▶ h265parse ─▶ avdec_h265 ─▶ videoconvert ─▶ proxysink
    webrtcbin ─▶ decodebin (avdec_h264) ─▶ videoconvert ─▶ proxysink        (H.264)
```

- **The window never closes.** pi-panel only switches to apps with a mapped window, so the display pipeline runs all the time and shows the status screen between connections. A connection is built and torn down separately, and its `proxysink` is plugged into the display while it plays.
- **Zero copy for H.265.** The hardware decoder's frames are DMABufs in the Broadcom SAND tile layout. They pass through proxysink/proxysrc by reference, and waylandsink hands them to the compositor, whose GPU detiles them. Detiling on the CPU (`videoconvert`) would cost more than decoding in software.
- **Signalling.** The app gathers its own ICE candidates first. With `signalling = "websocket"`, it then sends the offer over go2rtc's `/api/ws?src=<stream>`, applies the answer, and adds the candidates go2rtc trickles afterwards. The socket stays open for the whole connection, because go2rtc stops the stream when it closes. With `signalling = "http"`, it POSTs the offer to `/api/webrtc?src=<stream>` (or the WHEP URL) and applies the answer.
- **No third-party code.** The WebSocket client (`wsclient.py`) is a small standard-library implementation.
- **System Python.** The app runs on `/usr/bin/python3`, because PyGObject and gst-python must match the system GStreamer. It has no pip dependencies; pi-panel-varlink is pure Python and found by path.

## Development

```bash
uv run --no-project --with pytest pytest     # config, signalling, connection policy; no GStreamer
```

The GStreamer side is tested on the Pi. Run it against a headless compositor
(see pi-panel-core-compositor's README), then check that `GetStatus` reports
`playing` and a rising `frames` count. No screen is needed.
