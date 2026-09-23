"""The window: always running, fed through an intervideo channel.

    intervideosrc channel=<c> ─▶ videoconvert ─▶ textoverlay ─▶ waylandsink fullscreen

pi-panel-ctl only switches to apps whose window is mapped, so the window must
outlive every connection. `intervideosrc` shows black when nothing feeds the
channel, which doubles as the "connecting…" background. The text overlay
carries the status and is empty while video plays.

waylandsink scales frames to the window through wp_viewporter, which the
pi-panel compositor provides.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

log = logging.getLogger("pi-panel-webrtc")


class Display:
    def __init__(self, channel: str, on_fatal: Callable[[str], None]) -> None:
        self.pipeline = Gst.parse_launch(
            f"intervideosrc channel={channel} ! videoconvert "
            "! textoverlay name=status valignment=center halignment=center "
            "  font-desc=\"Sans 28\" shaded-background=true wait-text=false "
            "! waylandsink name=sink fullscreen=true sync=false"
        )
        self.overlay = self.pipeline.get_by_name("status")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        # Without its window the app is useless: exit, and let systemd restart
        # it (it is BindsTo= the compositor anyway).
        bus.connect("message::error", lambda _b, m: on_fatal(
            f"display: {m.parse_error()[0].message}"))
        self._text = None

    def start(self) -> None:
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("cannot open the display (is WAYLAND_DISPLAY a running compositor?)")

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.overlay.set_property("text", text)
            self.overlay.set_property("silent", not text)

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
