"""Which H.265 decoder to use: pure logic, no GStreamer.

- "v4l2": the Pi 5's HEVC block through `v4l2slh265dec`. Its frames are
  DMABufs in the Broadcom SAND layout, handed to waylandsink untouched (the
  compositor's GPU detiles them): about 1% of a core.
- "software": `avdec_h265` on the CPU.
- "auto": v4l2, until it fails once; then software for the rest of the
  process, or until the config is reloaded (`reset`). A failure costs one
  reconnect, so it is not worth retrying v4l2 on every connection.

H.264 always decodes in software: the Pi 5 has no H.264 hardware.
"""

from __future__ import annotations

DECODERS = ("auto", "v4l2", "software")


class DecoderLadder:
    def __init__(self) -> None:
        self._failed: set[str] = set()

    def pick(self, setting: str) -> str:
        """The decoder to use for a camera whose `decoder` is |setting|."""
        if setting != "auto":
            return setting          # explicit: no fallback, the session retries it
        return "software" if "v4l2" in self._failed else "v4l2"

    def failed(self, name: str) -> bool:
        """Note that |name| failed. True if that changes what `auto` picks."""
        if name in self._failed:
            return False
        self._failed.add(name)
        return name == "v4l2"

    def reset(self) -> None:
        self._failed.clear()
