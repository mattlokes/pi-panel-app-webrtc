"""config.toml: the go2rtc server and the cameras."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Camera:
    name: str
    label: str
    src: str | None = None        # go2rtc stream name
    whep_url: str | None = None   # or any WHEP endpoint

    def offer_url(self, server: str) -> str:
        """Where to POST the SDP offer. go2rtc's /api/webrtc speaks WHEP."""
        if self.whep_url:
            return self.whep_url
        return f"{server.rstrip('/')}/api/webrtc?src={quote(self.src or '', safe='')}"

    @property
    def source(self) -> str:
        return self.whep_url or f"go2rtc:{self.src}"


@dataclass(frozen=True, slots=True)
class Config:
    server: str
    cameras: tuple[Camera, ...]
    keep_connected: bool = False
    linger_seconds: float = 30.0

    def camera(self, name: str) -> Camera | None:
        return next((c for c in self.cameras if c.name == name), None)

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"{path} does not exist (copy config.toml.example)") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        unknown = set(data) - {"server", "camera", "keep_connected", "linger_seconds"}
        if unknown:
            raise ConfigError(f"unknown key(s): {', '.join(sorted(unknown))}")
        server = data.get("server", "")
        if not isinstance(server, str):
            raise ConfigError("server must be a URL string")
        linger = data.get("linger_seconds", 30)
        if isinstance(linger, bool) or not isinstance(linger, (int, float)) or linger < 0:
            raise ConfigError("linger_seconds must be a number >= 0")
        keep = data.get("keep_connected", False)
        if not isinstance(keep, bool):
            raise ConfigError("keep_connected must be true or false")

        raw = data.get("camera", [])
        if not isinstance(raw, list) or not raw:
            raise ConfigError("at least one [[camera]] is required")
        cameras: list[Camera] = []
        for i, c in enumerate(raw):
            if not isinstance(c, dict):
                raise ConfigError(f"camera {i} must be a table")
            extra = set(c) - {"name", "label", "src", "whep_url"}
            if extra:
                raise ConfigError(f"camera {i}: unknown key(s): {', '.join(sorted(extra))}")
            name = c.get("name")
            if not isinstance(name, str) or not _NAME.match(name):
                raise ConfigError(f"camera {i}: name must be lowercase letters, digits, '-' or '_'")
            if any(x.name == name for x in cameras):
                raise ConfigError(f"camera {name!r} is listed twice")
            src, whep = c.get("src"), c.get("whep_url")
            if bool(src) == bool(whep):
                raise ConfigError(f"camera {name!r}: give exactly one of src or whep_url")
            if src and not server:
                raise ConfigError(f"camera {name!r} uses src, so server is required")
            label = c.get("label") or name
            cameras.append(Camera(name, str(label), src, whep))
        return cls(server=server, cameras=tuple(cameras), keep_connected=keep,
                   linger_seconds=float(linger))
