"""MicroPython WebREPL over a standard-library WebSocket byte stream."""

import base64
import hashlib
import os
import socket
import struct
import time

from .backends import DeviceError, RawReplBackend

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_POLL = 0.05
_MAX_HEADER = 16 * 1024
_MAX_FRAME = 1024 * 1024


class WebSocket:
    """Unencrypted RFC 6455 client exposing REPL data as a byte stream."""

    def __init__(self, host, port, timeout=3.0):
        self._timeout = timeout
        self._incoming = bytearray()
        self._payload = bytearray()
        self._fragmented = False
        self.closed = False
        try:
            self._socket = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            raise DeviceError("WebREPL is unreachable. Check the board address, port, and Wi-Fi.") from None
        try:
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._upgrade(host, port)
        except BaseException:
            self._disconnect()
            raise

    def _upgrade(self, host, port):
        key = base64.b64encode(os.urandom(16))
        authority = f"[{host}]" if ":" in host else host
        try:
            authority = authority.encode("idna")
        except UnicodeError:
            raise DeviceError("Invalid WebREPL host.") from None
        if any(c in authority for c in b" \r\n\t"):
            raise DeviceError("Invalid WebREPL host.")
        request = (
            b"GET / HTTP/1.1\r\nHost: " + authority + b":" + str(port).encode("ascii")
            + b"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key
            + b"\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._send(request)
        deadline = time.monotonic() + self._timeout
        while b"\r\n\r\n" not in self._incoming:
            if len(self._incoming) >= _MAX_HEADER or not self._receive(deadline):
                raise DeviceError("WebREPL did not complete the WebSocket handshake.")
        head, _, rest = self._incoming.partition(b"\r\n\r\n")
        self._incoming = rest
        status, *lines = bytes(head).split(b"\r\n")
        if len(head) > _MAX_HEADER or status.split()[:2] != [b"HTTP/1.1", b"101"]:
            raise DeviceError("WebREPL rejected the WebSocket upgrade.")
        headers = {}
        for line in lines:
            name, sep, value = line.partition(b":")
            if not sep:
                raise DeviceError("Invalid WebREPL handshake headers.")
            headers.setdefault(name.strip().lower(), []).extend(
                part.strip() for part in value.split(b",")
            )
        expected = base64.b64encode(hashlib.sha1(key + _GUID).digest())
        if (
            b"websocket" not in [v.lower() for v in headers.get(b"upgrade", [])]
            or b"upgrade" not in [v.lower() for v in headers.get(b"connection", [])]
            or headers.get(b"sec-websocket-accept") != [expected]
            or b"sec-websocket-extensions" in headers
            or b"sec-websocket-protocol" in headers
        ):
            raise DeviceError("Invalid WebREPL WebSocket handshake.")

    def _send(self, data):
        if self.closed:
            raise DeviceError("WebREPL connection is closed.")
        try:
            self._socket.settimeout(self._timeout)
            self._socket.sendall(data)
        except OSError:
            self._disconnect()
            raise DeviceError("WebREPL connection failed while sending data.") from None

    def _receive(self, deadline):
        if self.closed:
            raise DeviceError("WebREPL connection is closed.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            self._socket.settimeout(remaining)
            data = self._socket.recv(4096)
        except TimeoutError:
            return False
        except OSError:
            self._disconnect()
            raise DeviceError("WebREPL connection failed while receiving data.") from None
        if not data:
            self._disconnect()
            raise DeviceError("WebREPL closed the connection.")
        self._incoming.extend(data)
        return True

    def _send_frame(self, opcode, data):
        length = len(data)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 65535:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        mask = os.urandom(4)
        encoded = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
        self._send(header + mask + encoded)

    def write(self, data, *, binary=False):
        self._send_frame(2 if binary else 1, data)
        return len(data)

    def _protocol_error(self):
        self._disconnect()
        raise DeviceError("Invalid WebSocket frame from WebREPL.")

    def _frame(self):
        buf = self._incoming
        if len(buf) < 2:
            return None
        first, second = buf[:2]
        opcode = first & 0x0F
        final = bool(first & 0x80)
        if first & 0x70 or second & 0x80 or opcode not in (0, 1, 2, 8, 9, 10):
            self._protocol_error()
        length = second & 0x7F
        offset = 2
        if opcode >= 8 and (not final or length > 125 or (opcode == 8 and length == 1)):
            self._protocol_error()
        if length >= 126:
            offset += 2 if length == 126 else 8
            if len(buf) < offset:
                return None
            length = int.from_bytes(buf[2:offset], "big")
            if length < (126 if offset == 4 else 65536):
                self._protocol_error()
        if length > _MAX_FRAME:
            self._protocol_error()
        if len(buf) < offset + length:
            return None
        payload = buf[offset : offset + length]
        del buf[: offset + length]
        if opcode == 0:
            if not self._fragmented:
                self._protocol_error()
            self._fragmented = not final
        elif opcode in (1, 2):
            if self._fragmented:
                self._protocol_error()
            self._fragmented = not final
        return opcode, payload

    def read(self, size, *, timeout=_POLL):
        deadline = time.monotonic() + timeout
        while not self._payload:
            frame = self._frame()
            if frame is None:
                if not self._receive(deadline):
                    return b""
                continue
            opcode, payload = frame
            if opcode == 8:
                self.close(payload)
                raise DeviceError("WebREPL closed the connection.")
            if opcode == 9:
                self._send_frame(10, payload)
            if opcode < 8:
                self._payload = payload
        data = bytes(self._payload[:size])
        del self._payload[:size]
        return data

    def reset_input_buffer(self):
        # The serial raw-REPL code uses this after Ctrl-C. Consume complete frames
        # until quiet, retaining a partial WebSocket frame for the next read.
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if not self.read(4096):
                break

    def _wait_for(self, markers):
        received = bytearray()
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline and len(received) < _MAX_HEADER:
            received.extend(self.read(512, timeout=deadline - time.monotonic()))
            for marker in markers:
                index = received.find(marker)
                if index >= 0:
                    self._payload[:0] = received[index + len(marker) :]
                    return marker
        raise DeviceError("WebREPL did not answer during login. The server may be busy.")

    def login(self, password):
        self._wait_for((b"Password: ",))
        self.write(password.encode("utf-8") + b"\r")
        denied = b"\r\nAccess denied\r\n"
        if self._wait_for((denied, b"\r\nWebREPL connected\r\n>>> ")) == denied:
            raise DeviceError("WebREPL rejected the password.")

    def _disconnect(self):
        self.closed = True
        self._socket.close()

    def close(self, payload=b""):
        if self.closed:
            return
        try:
            self._send_frame(8, payload)
        except DeviceError:
            pass
        finally:
            self._disconnect()


def connect(settings, timeout=3.0):
    """Open and authenticate the transport without sending REPL commands."""
    password = settings.password()
    client = WebSocket(settings.host, settings.port, timeout=timeout)
    try:
        client.login(password)
    except BaseException:
        client.close()
        raise
    return client


class WebReplBackend(RawReplBackend):
    name = "wifi"

    def __init__(self, settings, passive=False):
        self.endpoint = (settings.host, settings.port)
        super().__init__(connect(settings), passive=passive)

    def close(self, force=False):
        super().close(force=force or self._stream.closed)
