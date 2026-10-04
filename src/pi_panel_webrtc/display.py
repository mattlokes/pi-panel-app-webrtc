"""The window: always running; a session's video is plugged in and out.

    videotestsrc black ─▶ textoverlay ─▶ sel.sink_0   (the status screen)
    proxysrc ──────────────────────────▶ sel.sink_1   (a session's proxysink)
    input-selector sel ─▶ waylandsink fullscreen

pi-panel-ctl only switches to apps whose window is mapped, so the window must
outlive every connection. Each session pipeline ends in a `proxysink`;
`attach` points the `proxysrc` at it and selects the video.

proxysink/proxysrc pass buffers by reference, and queries too, so the H.265
hardware decoder's DMABufs (Broadcom SAND tiles) reach waylandsink untouched
and the compositor's GPU detiles them. The intervideo elements copy frames
into system memory, and on the CPU, detiling SAND costs more than decoding
H.265 in software. appsink/appsrc don't forward the queries, so the decoder
fails to negotiate DMABuf through them.

The video pad must be selected before media flows: input-selector answers
the decoder's caps and allocation queries only on its active pad. So
`attach` comes when the answer is applied, and until the first frame the
screen keeps the last status frame ("connecting…"). `detach` comes before a
session pipeline is torn down, so waylandsink lets go of that pipeline's
buffers.

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
    def __init__(self, on_fatal: Callable[[str], None]) -> None:
        self.pipeline = Gst.parse_launch(
            # sync-streams=false: otherwise the inactive pad blocks waiting
            # for the active one's running time, and the two pipelines' clocks
            # have nothing in common.
            "input-selector name=sel sync-streams=false "
            "! waylandsink name=sink fullscreen=true sync=false "
            "videotestsrc pattern=black is-live=true "
            "! video/x-raw,format=BGRx,width=1280,height=720,framerate=5/1 "
            "! textoverlay name=status valignment=center halignment=center "
            "  font-desc=\"Sans 28\" shaded-background=true wait-text=false "
            "! sel.sink_0 "
            "proxysrc name=video ! sel.sink_1"
        )
        self.overlay = self.pipeline.get_by_name("status")
        self.selector = self.pipeline.get_by_name("sel")
        self.video = self.pipeline.get_by_name("video")
        self._status_pad = self.selector.get_static_pad("sink_0")
        self._video_pad = self.selector.get_static_pad("sink_1")
        self._attached: Gst.Element | None = None
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

    def attach(self, proxysink: Gst.Element) -> None:
        """Show the video arriving at |proxysink| (in a session pipeline)."""
        if self._attached is not proxysink:
            self._attached = proxysink
            self.video.set_property("proxysink", proxysink)
        self.selector.set_property("active-pad", self._video_pad)

    def detach(self, proxysink: Gst.Element) -> None:
        """Back to the status screen, if |proxysink| is the one showing."""
        if self._attached is proxysink:
            self._attached = None
            self.selector.set_property("active-pad", self._status_pad)

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
