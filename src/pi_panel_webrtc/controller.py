"""Which camera, and whether to be connected: pure logic, no GStreamer.

The GLib main loop calls `tick()` every second and forwards every request
(visibility from pi-panel-ctl, actions, SelectCamera) as a method call. The
controller starts and stops `Session`s through a factory, so tests drive it
with a fake session and a fake clock.

Connection policy:
- connected while visible;
- still connected for `linger_seconds` after being hidden, so flicking away
  and back does not cost a reconnect;
- always connected when `keep_connected` is set (no delay when a doorbell
  brings the feed up, at the cost of decoding all the time).
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from .config import Camera, Config

IDLE = "idle"


class Session(Protocol):
    """One connection to one camera. It retries by itself until stopped."""

    camera: Camera

    @property
    def state(self) -> str: ...          # connecting | playing | retrying
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def status(self) -> dict[str, Any]: ...  # error, width, height, frames, connected_for


SessionFactory = Callable[[Camera], Session]


class Controller:
    def __init__(self, config: Config, make_session: SessionFactory,
                 clock: Callable[[], float], visible: bool = False,
                 on_change: Callable[[], None] | None = None) -> None:
        self.config = config
        self.make_session = make_session
        self.clock = clock
        self.on_change = on_change or (lambda: None)
        self.index = 0
        self.visible = visible
        self.hidden_since: float | None = None if visible else clock() - config.linger_seconds
        self.session: Session | None = None

    # --- queries ---------------------------------------------------------

    @property
    def camera(self) -> Camera:
        return self.config.cameras[self.index]

    def should_connect(self) -> bool:
        if self.config.keep_connected or self.visible:
            return True
        return (self.hidden_since is not None
                and self.clock() - self.hidden_since < self.config.linger_seconds)

    def status(self) -> dict[str, Any]:
        base = {"camera": self.camera.name, "label": self.camera.label, "visible": self.visible,
                "state": IDLE, "error": None, "width": None, "height": None, "frames": 0,
                "connected_for": None}
        if self.session is not None:
            base.update(self.session.status())
            base["state"] = self.session.state
        return base

    # --- requests --------------------------------------------------------

    def set_visible(self, visible: bool) -> None:
        if visible == self.visible:
            return
        self.visible = visible
        self.hidden_since = None if visible else self.clock()
        self.tick()

    def select(self, name: str) -> bool:
        for i, cam in enumerate(self.config.cameras):
            if cam.name == name:
                self._switch_to(i)
                return True
        return False

    def step(self, delta: int) -> None:
        self._switch_to((self.index + delta) % len(self.config.cameras))

    def reconnect(self) -> None:
        self._stop_session()
        self.tick()

    def reconfigure(self, config: Config) -> None:
        """Adopt a reloaded config without a restart.

        The current camera stays selected if it still exists (by name), and
        its connection is kept unless something it depends on changed (its
        source, or the server). The policy settings (keep_connected,
        linger_seconds) apply from the next tick.
        """
        current = self.camera
        old_url = current.offer_url(self.config.server)
        self.config = config
        new = config.camera(current.name)
        self.index = config.cameras.index(new) if new else 0
        if self.session is not None and (
                new is None or new.offer_url(config.server) != old_url or new != current):
            self._stop_session()
        self.tick()
        self.on_change()

    def shutdown(self) -> None:
        self._stop_session()

    # --- the periodic pass -------------------------------------------------

    def tick(self) -> None:
        """Start or stop the session to match the policy. Idempotent."""
        want = self.should_connect()
        if want and self.session is None:
            self.session = self.make_session(self.camera)
            self.session.start()
            self.on_change()
        elif not want and self.session is not None:
            self._stop_session()

    # --- internals ---------------------------------------------------------

    def _switch_to(self, index: int) -> None:
        if index == self.index and self.session is not None:
            return
        self.index = index
        self._stop_session()
        self.tick()
        self.on_change()

    def _stop_session(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None
            self.on_change()
