"""WHEP-style signalling: POST the SDP offer, get the SDP answer back.

go2rtc's `POST /api/webrtc?src=<stream>` and standard WHEP endpoints (such as
MediaMTX's `/<path>/whep`) both work this way. The offer must already hold
every ICE candidate, because neither side trickles here: the session waits
for ICE gathering to complete before posting.
"""

from __future__ import annotations

import urllib.error
import urllib.request

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
