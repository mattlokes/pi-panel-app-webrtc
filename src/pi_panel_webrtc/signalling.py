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
