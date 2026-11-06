"""WebSocket client for Chrome's local debugging port. Standard library only.

Chrome is launched separately. This talks to 127.0.0.1 and never prints page data.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import socket
import struct
from collections.abc import Callable
from urllib.parse import urlparse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT = 0x1
_CLOSE = 0x8
_PING = 0x9
_PONG = 0xA


class CDPError(Exception):
    """The debugging connection failed. The message has no page contents."""


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """A masked client frame. Servers reject unmasked client frames."""
    mask = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + mask + masked


def read_frame(pull: Callable[[int], bytes]) -> tuple[bool, int, bytes]:
    """One frame. `pull` returns the next n bytes. The payload is unmasked."""
    first, second = pull(2)
    finished = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", pull(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", pull(8))[0]
    mask = pull(4) if second & 0x80 else b""
    payload = pull(length)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return finished, opcode, payload


def _accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _GUID).encode()).digest()
    return base64.b64encode(digest).decode()


class Client:
    """One Chrome debugging target. `call` waits until that command's reply arrives."""

    def __init__(self, ws_url: str, timeout: float = 20):
        parsed = urlparse(ws_url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise CDPError("Chrome's debugging URL is not a local ws:// address")
        port = parsed.port or 80
        try:
            self._sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
        except OSError as err:
            raise CDPError("Could not connect to Chrome's debugging port") from err
        self._sock.settimeout(timeout)
        self._buf = bytearray()
        self._timeout = timeout
        self._next = 1
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode()
        host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode())
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            try:
                chunk = self._recv()
            except TimeoutError as err:
                raise CDPError("Chrome did not answer") from err
            raw += chunk
            if len(raw) > 65536:
                raise CDPError("Chrome's debugging handshake was too large")
        head, _, pending = bytes(raw).partition(b"\r\n\r\n")
        self._buf = bytearray(pending)
        lines = head.decode("iso-8859-1").split("\r\n")
        if "101" not in lines[0]:
            raise CDPError("Chrome did not accept the debugging connection")
        accept = ""
        for line in lines[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
        if accept != _accept_key(key):
            raise CDPError("Chrome's debugging handshake did not match")

    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        call_id = self._next
        self._next += 1
        message = {"id": call_id, "method": method}
        if params:
            message["params"] = params
        self._sock.sendall(encode_frame(_TEXT, json.dumps(message).encode()))
        self._sock.settimeout(timeout or self._timeout)
        while True:
            parts: list[bytes] = []
            kind = None
            while True:
                try:
                    finished, opcode, payload = read_frame(self._pull)
                except TimeoutError as err:
                    raise CDPError(f"Chrome did not answer {method}") from err
                if opcode == _PING:
                    self._sock.sendall(encode_frame(_PONG, payload))
                    continue
                if opcode == _PONG:
                    continue
                if opcode == _CLOSE:
                    raise CDPError("Chrome closed the debugging connection")
                if opcode == _TEXT:
                    kind = opcode
                    parts = [payload]
                elif opcode == 0x0 and kind == _TEXT:
                    parts.append(payload)
                else:
                    continue
                if finished:
                    break
            try:
                data = json.loads(b"".join(parts).decode())
            except json.JSONDecodeError as err:
                raise CDPError("Chrome's debugging reply was not JSON") from err
            if data.get("id") != call_id:
                continue
            if "error" in data:
                detail = data["error"]
                text = detail.get("message") if isinstance(detail, dict) else str(detail)
                raise CDPError(str(text or "Chrome rejected a debugging command")[:300])
            result = data.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()

    def _recv(self) -> bytes:
        chunk = self._sock.recv(4096)
        if not chunk:
            raise CDPError("Chrome closed the debugging connection")
        return chunk

    def _pull(self, size: int) -> bytes:
        while len(self._buf) < size:
            self._buf += self._recv()
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out
