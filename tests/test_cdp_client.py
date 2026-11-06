"""The debugging client speaks WebSocket without opening a browser."""

from __future__ import annotations

import json
import socket
import struct
import threading

import pytest

import signin.cdp as signin_cdp


@pytest.fixture(scope="module")
def cdp():
    return signin_cdp


def server_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def test_call_survives_a_ping_and_a_long_reply(cdp):
    ready = threading.Event()
    ports: list[int] = []
    pong: list[bytes] = []

    def serve():
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        ports.append(server.getsockname()[1])
        ready.set()
        conn, _addr = server.accept()
        try:
            raw = bytearray()
            while b"\r\n\r\n" not in raw:
                raw += conn.recv(4096)
            head = bytes(raw).split(b"\r\n\r\n", 1)[0].decode()
            key = next(
                line.split(":", 1)[1].strip()
                for line in head.split("\r\n")
                if line.lower().startswith("sec-websocket-key:")
            )
            accept = cdp._accept_key(key)
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "\r\n"
                ).encode()
            )
            pending = bytearray(bytes(raw).split(b"\r\n\r\n", 1)[1])

            def pull(size: int) -> bytes:
                while len(pending) < size:
                    chunk = conn.recv(size - len(pending))
                    if not chunk:
                        raise cdp.CDPError("closed")
                    pending.extend(chunk)
                out = bytes(pending[:size])
                del pending[:size]
                return out

            _fin, _opcode, payload = cdp.read_frame(pull)
            message = json.loads(payload)
            reply = {"id": message["id"], "result": {"echo": "y" * 200, "method": message["method"]}}
            conn.sendall(server_frame(0x9, b"hi") + server_frame(0x1, json.dumps(reply).encode()))
            conn.settimeout(2)
            _fin, opcode, body = cdp.read_frame(pull)
            assert opcode == 0xA
            pong.append(body)
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(2)
    client = cdp.Client(f"ws://127.0.0.1:{ports[0]}/devtools/page/abc", timeout=2)
    try:
        result = client.call("Runtime.evaluate", {"expression": "z" * 180})
    finally:
        client.close()
    thread.join(2)
    assert result == {"echo": "y" * 200, "method": "Runtime.evaluate"}
    assert pong == [b"hi"]


def test_a_rejected_command_has_no_payload(cdp):
    ready = threading.Event()
    ports: list[int] = []

    def serve():
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        ports.append(server.getsockname()[1])
        ready.set()
        conn, _addr = server.accept()
        try:
            raw = bytearray()
            while b"\r\n\r\n" not in raw:
                raw += conn.recv(4096)
            head = bytes(raw).split(b"\r\n\r\n", 1)[0].decode()
            key = next(
                line.split(":", 1)[1].strip()
                for line in head.split("\r\n")
                if line.lower().startswith("sec-websocket-key:")
            )
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    f"Sec-WebSocket-Accept: {cdp._accept_key(key)}\r\n"
                    "\r\n"
                ).encode()
            )
            pending = bytearray(bytes(raw).split(b"\r\n\r\n", 1)[1])

            def pull(size: int) -> bytes:
                while len(pending) < size:
                    pending.extend(conn.recv(4096))
                out = bytes(pending[:size])
                del pending[:size]
                return out

            _fin, _opcode, payload = cdp.read_frame(pull)
            message = json.loads(payload)
            reply = {"id": message["id"], "error": {"message": "No such method"}}
            conn.sendall(server_frame(0x1, json.dumps(reply).encode()))
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(2)
    client = cdp.Client(f"ws://127.0.0.1:{ports[0]}/devtools/page/abc", timeout=2)
    try:
        with pytest.raises(cdp.CDPError, match="No such method"):
            client.call("Nope", {"secret": "do-not-echo"})
    finally:
        client.close()
        thread.join(2)
