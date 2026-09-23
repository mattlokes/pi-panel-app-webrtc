"""pi-panel-app-webrtc: a full-screen WebRTC camera feed.

Everything runs on one GLib main loop: the display pipeline, the camera
session, a 1s policy tick, and requests from the io.pipanel.App service. The
service answers on its own thread and hands every change over with
GLib.idle_add.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from pi_panel_varlink import AppService, Call, Interface, VarlinkError  # noqa: E402

from .config import Config, ConfigError  # noqa: E402
from .controller import Controller  # noqa: E402

log = logging.getLogger("pi-panel-webrtc")
CHANNEL = "pi-panel-webrtc"


def overlay_text(status: dict[str, Any]) -> str:
    state, label = status["state"], status["label"]
    if state == "connecting":
        return f"{label}\nconnecting…"
    if state == "retrying":
        retry = status.get("retry_in")
        return f"{label}\n{status.get('error') or 'offline'}" + (
            f"\nretrying in {retry:.0f}s" if retry else "")
    if state == "idle" and status["visible"]:
        return label
    return ""


def on_main(fn, *args) -> None:
    """Hand a call from the service thread to the GLib loop, and wait for it."""
    done = threading.Event()
    result: list[Any] = []

    def run() -> bool:
        result.append(fn(*args))
        done.set()
        return False
    GLib.idle_add(run)
    done.wait(5)
    return result[0] if result else None


def webrtc_interface(controller: Controller) -> Interface:
    iface = Interface(files(__package__).joinpath("io.pipanel.app.WebRTC.varlink")
                      .read_text(encoding="utf-8"))

    @iface.method("ListCameras")
    async def list_cameras(call: Call) -> dict:
        return {"cameras": [{"name": c.name, "label": c.label, "source": c.source}
                            for c in controller.config.cameras],
                "current": controller.camera.name}

    @iface.method("SelectCamera")
    async def select_camera(call: Call) -> None:
        name = call.param("name", str)
        if controller.config.camera(name) is None:
            raise VarlinkError("io.pipanel.app.WebRTC.NoSuchCamera", {"name": name})
        on_main(controller.select, name)

    @iface.method("GetStatus")
    async def get_status(call: Call) -> dict:
        return on_main(controller.status) or {}

    return iface


def main() -> int:
    p = argparse.ArgumentParser(prog="pi-panel-webrtc")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)-7s %(message)s", stream=sys.stderr)
    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    Gst.init(None)
    from .display import Display
    from .session import CameraSession

    loop = GLib.MainLoop()
    exit_code = [0]

    def fatal(message: str) -> None:
        log.error("%s", message)
        exit_code[0] = 1
        loop.quit()

    display = Display(CHANNEL, on_fatal=fatal)

    def refresh() -> None:
        display.set_text(overlay_text(controller.status()))

    controller = Controller(
        config,
        make_session=lambda cam: CameraSession(cam, config.server, CHANNEL, on_change=refresh),
        clock=time.monotonic,
        on_change=refresh,
    )

    def idle(fn, *a) -> None:
        GLib.idle_add(lambda: fn(*a) and False)

    actions = {
        "next": ("Next camera", lambda: idle(controller.step, +1)),
        "previous": ("Previous camera", lambda: idle(controller.step, -1)),
        "reconnect": ("Reconnect", lambda: idle(controller.reconnect)),
    }
    for cam in config.cameras:
        actions[f"camera_{cam.name}"] = (f"Show {cam.label}", lambda n=cam.name: idle(controller.select, n))

    service = AppService(
        product="pi-panel-app-webrtc", version="0.1.0",
        on_visible=lambda visible: idle(controller.set_visible, visible),
        actions=actions,
        interfaces=[webrtc_interface(controller)],
    )
    under_panel = service.start()
    if not under_panel:
        log.info("not running under pi-panel: always visible")
        controller.set_visible(True)

    # SIGTERM from systemd: leave the loop, then tear down; well inside the
    # app contract's 5 seconds.
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: loop.quit() or False)

    try:
        display.start()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    GLib.timeout_add(1000, lambda: (controller.tick(), refresh(), True)[-1])
    controller.tick()
    refresh()
    log.info("cameras: %s; showing %s", ", ".join(c.name for c in config.cameras),
             controller.camera.name)

    loop.run()
    log.info("stopping")
    controller.shutdown()
    display.stop()
    service.stop()
    return exit_code[0]


if __name__ == "__main__":
    raise SystemExit(main())
