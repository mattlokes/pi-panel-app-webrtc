"""Signalling: getting an SDP answer for our offer.

Two ways, chosen per camera by `signalling` in config.toml:

- "websocket" (go2rtc only, the default): `Go2rtcSocket`, below. go2rtc
  answers at once and trickles its ICE candidates.
- "http": WHEP style, below. POST the SDP offer, get the SDP answer back.

go2rtc's `POST /api/webrtc?src=<stream>` and standard WHEP endpoints (such as
MediaMTX's `/<path>/whep`) both work this way. The offer must already hold
every ICE candidate, because neither side trickles here: the session waits
for ICE gathering to complete before posting.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator
from urllib.parse import quote, urlsplit

from .wsclient import WebSocket, WebSocketError

TIMEOUT = 10.0


class SignallingError(Exception):
    pass


def post_offer(url: str, sdp: str, timeout: float = TIMEOUT) -> str:
    request = urllib.request.Request(
        url, data=sdp.encode(), method="POST",
        headers={"Content-Type": "application/sdp", "Accept": "application/sdp"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace").strip()[:200]
        raise SignallingError(f"HTTP {exc.code} from {url}: {body or exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SignallingError(f"cannot reach {url}: {reason}") from None
    if not answer.startswith("v=0"):
        raise SignallingError(f"{url} did not answer with SDP: {answer[:120]!r}")
    return answer


CODECS = ("h265", "h264")      # what we can decode, in order of preference


def stream_codecs(server: str, src: str, timeout: float = 3.0) -> set[str]:
    """The video codecs go2rtc already knows for |src|: those of its producers
    that are running. Empty when none is running yet, or on any error."""
    url = f"{server.rstrip('/')}/api/streams?src={quote(src, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            stream = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return set()
    return video_codecs(stream) if isinstance(stream, dict) else set()


def video_codecs(stream: dict) -> set[str]:
    """Video codecs in one go2rtc stream object, from its producers' medias
    ("video, recvonly, H264"). Producers that have not started list none."""
    found: set[str] = set()
    for producer in stream.get("producers") or ():
        for media in producer.get("medias") or ():
            kind, _, rest = str(media).partition(",")
            if kind.strip() != "video":
                continue
            for codec in rest.split(",")[-1].split():
                name = codec.split("/")[0].strip().upper()
                found.add({"HEVC": "h265"}.get(name, name.lower()))
    return found


def offer_codecs(known: set[str]) -> list[str]:
    """What to offer, best first: H.265, then H.264.

    If go2rtc already knows the stream's codec, offer only that. Asked for a
    codec it cannot serve, go2rtc may still answer with it and then send
    nothing: a producer that has not started yet (e.g. `ffmpeg:front_door#
    audio=opus`) matches any codec. Measured on go2rtc 1.9.14 with an H.264
    stream: offered H.265 first, it answered H.265.
    """
    return [c for c in CODECS if c in known] or list(CODECS)


def answer_codec(sdp: str) -> str | None:
    """The video codec the server chose: "h265", "h264", or None.

    We offer H.265 first and H.264 second; the answer lists what the server
    will send, in its order of preference. The first known codec on the first
    accepted m=video line wins (payload types such as rtx or red are skipped).
    """
    payloads: list[str] | None = None
    names: dict[str, str] = {}
    in_video = False
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            fields = line[2:].split()
            # A port of 0 means the server rejected the section.
            in_video = payloads is None and len(fields) > 3 and fields[0] == "video" \
                and fields[1] != "0"
            if in_video:
                payloads = fields[3:]
        elif in_video and line.startswith("a=rtpmap:"):
            pt, _, encoding = line[len("a=rtpmap:"):].partition(" ")
            names[pt] = encoding.split("/")[0].upper()
    for pt in payloads or ():
        name = names.get(pt)
        if name in ("H265", "HEVC"):
            return "h265"
        if name == "H264":
            return "h264"
    return None


# --- go2rtc's WebSocket API -----------------------------------------------------

class Go2rtcSocket:
    """Signalling over go2rtc's `/api/ws?src=<stream>`.

    Unlike the HTTP API, which holds its answer until go2rtc's own ICE
    gathering completes (including any unreachable STUN server: 7.8s on a
    LAN-only Frigate), the WebSocket answers at once and trickles its
    candidates as separate messages.

    The socket must stay open for as long as the stream is wanted: go2rtc ties
    the consumer to it, and closing it stops the stream.

        sock = Go2rtcSocket.open(server, "front_door")
        sock.send_offer(sdp)
        for kind, value in sock.messages():   # ("answer"|"candidate"|"error", str)
            ...
    """

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    @staticmethod
    def url(server: str, src: str) -> str:
        parts = urlsplit(server.rstrip("/"))
        scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
        if scheme is None:
            raise SignallingError(f"server must be an http(s):// URL, not {server!r}")
        return f"{scheme}://{parts.netloc}{parts.path}/api/ws?src={quote(src, safe='')}"

    @classmethod
    def open(cls, server: str, src: str, timeout: float = TIMEOUT) -> "Go2rtcSocket":
        url = cls.url(server, src)
        try:
            return cls(WebSocket.connect(url, timeout=timeout))
        except WebSocketError as exc:
            raise SignallingError(str(exc)) from None

    def send_offer(self, sdp: str) -> None:
        try:
            self.ws.send_text(json.dumps({"type": "webrtc/offer", "value": sdp}))
        except WebSocketError as exc:
            raise SignallingError(f"go2rtc: {exc}") from None

    def messages(self) -> Iterator[tuple[str, str]]:
        """Yield (kind, value) until the socket closes (then SignallingError)."""
        while True:
            try:
                raw = self.ws.recv()
            except WebSocketError as exc:
                raise SignallingError(f"go2rtc connection lost: {exc}") from None
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind, value = msg.get("type"), msg.get("value")
            if kind == "webrtc/answer" and isinstance(value, str):
                yield "answer", value
            elif kind == "webrtc/candidate" and isinstance(value, str) and value:
                yield "candidate", value
            elif kind == "error":
                yield "error", str(value)
            # anything else (e.g. keepalives, stats) is not for us

    def close(self) -> None:
        self.ws.close()
