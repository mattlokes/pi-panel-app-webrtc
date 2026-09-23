"""A minimal WebSocket client (RFC 6455), standard library only.

Just enough for go2rtc's signalling socket: text messages in and out, pings
answered, fragments reassembled. It is blocking; run it on a thread. The app
has no pip dependencies (it runs on the system Python for GStreamer), and
neither `websockets` nor libsoup's GObject bindings are installed by default,
hence this.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    """The peer closed the connection (cleanly or not)."""


class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 10.0) -> "WebSocket":
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise WebSocketError(f"not a ws:// or wss:// URL: {url}")
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            if parts.scheme == "wss":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except OSError as exc:
            raise WebSocketError(f"cannot connect to {host}:{port}: {exc}") from None

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        ws = cls(sock)
        try:
            sock.sendall(request.encode())
            head = ws._read_http_head()
        except OSError as exc:
            sock.close()
            raise WebSocketError(f"handshake with {host}:{port} failed: {exc}") from None
        status, headers = head
        if " 101 " not in status + " ":
            sock.close()
            raise WebSocketError(f"{url} refused the WebSocket upgrade: {status}")
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise WebSocketError(f"{url} sent a bad Sec-WebSocket-Accept")
        sock.settimeout(None)   # from here on, reads block until data or close
        return ws

    def _read_http_head(self) -> tuple[str, dict[str, str]]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._sock.recv(1)
            if not chunk:
                raise WebSocketError("connection closed during the handshake")
            data += chunk
            if len(data) > 16384:
                raise WebSocketError("handshake response too long")
        lines = data.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return lines[0], headers

    # --- frames -------------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except OSError as exc:
                raise WebSocketClosed(str(exc)) from None
            if not chunk:
                raise WebSocketClosed("connection closed")
            buf += chunk
        return buf

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # Clients must mask every frame (RFC 6455 5.3).
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self._sock.sendall(header + mask + masked)
        except OSError as exc:
            raise WebSocketClosed(str(exc)) from None

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._recv_exact(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if b2 & 0x80 else None
        payload = self._recv_exact(n) if n else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    # --- API -----------------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def recv(self) -> str:
        """The next text message. Raises WebSocketClosed when the peer closes."""
        message = b""
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                if not self._closed:
                    try:
                        self._send_frame(OP_CLOSE, payload[:2])
                    except WebSocketClosed:
                        pass
                self._closed = True
                raise WebSocketClosed(f"closed by peer (code {code})")
            if opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                message += payload
                if fin:
                    return message.decode(errors="replace")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
            except WebSocketClosed:
                pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
