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

[[camera]]
name = "front_door"        # used in action names: camera_front_door
label = "Front door"
src = "front_door_sub"     # go2rtc stream name, or: whep_url = "http://…/whep"
```

The Pi 5 has no H.264 hardware decoder, so video is decoded on the CPU. Use
a sub stream (640×360 or 720p) rather than a 4K main stream. Measured on a
Pi 5, a 640×480 sub stream at about 16 fps costs about 6% of one core.

**How long before the picture appears.** go2rtc opens a camera only when
someone asks for it, so on a stream nothing else is watching, go2rtc takes
about 8 seconds to answer. Most of that time is the camera connection.
Measured against `front_door_sub`, the app goes from shown to picture in
about 9 seconds. For a doorbell, set `keep_connected = true`: the feed is
already playing when it is shown, at the constant cost above.

WebRTC here is H.264 only. If a camera streams H.265, have go2rtc transcode
it, e.g. `front_door_h264: ffmpeg:front_door#video=h264`. Otherwise the app
reports "the stream is not H.264".

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
pi-panel-ctl action webrtc next                      # also: previous, reconnect, camera_<name>
pi-panel-ctl app webrtc io.pipanel.app.WebRTC.GetStatus
pi-panel-ctl app webrtc io.pipanel.app.WebRTC.SelectCamera '{"name": "front_door"}'
journalctl -u pi-panel-app@webrtc -f
```

`GetStatus` reports the state (`idle`, `connecting`, `playing` or
`retrying`), the error if there is one, the video size, and the number of
decoded frames.

While a feed is connecting or retrying, the screen shows the camera's name
and the reason. A lost feed is retried with backoff (1s up to 30s).

## How it works

```
display (always running):  intervideosrc ─▶ videoconvert ─▶ textoverlay ─▶ waylandsink
session (per connection):  webrtcbin ─▶ decodebin ─▶ videoconvert ─▶ intervideosink
```

- **The window never closes.** pi-panel only switches to apps with a mapped window, so the display pipeline runs all the time and shows black between connections. A connection is built and torn down separately.
- **Signalling is non-trickle.** The app gathers every ICE candidate, then POSTs the offer to `{server}/api/webrtc?src=<stream>` (or the WHEP URL) and applies the answer.
- **System Python.** The app runs on `/usr/bin/python3`, because PyGObject and gst-python must match the system GStreamer. It has no pip dependencies; pi-panel-varlink is pure Python and found by path.

## Development

```bash
uv run --no-project --with pytest pytest     # config, signalling, connection policy; no GStreamer
```

The GStreamer side is tested on the Pi. Run it against a headless compositor
(see pi-panel-core-compositor's README), then check that `GetStatus` reports
`playing` and a rising `frames` count. No screen is needed.
