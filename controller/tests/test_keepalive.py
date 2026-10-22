"""
/data's liveness rule (em_keepalive), against websockets' real server.

The firmware's gorilla/websocket drops a pong when a mic-frame write holds its
write lock past one second, and websockets' own keepalive failed /data on that
one pong while frames were still arriving. These tests serve with the shipped
connection class and drive it from a raw client that never answers a ping,
which is that device at its worst, and from one that goes silent. Timings are
scaled down: a ping every PING seconds, the library failing a ping unanswered
for PING, and SILENCE in place of the 60s window.
"""

import asyncio
import base64
import contextlib
import os
import struct

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

import em_keepalive
import em_wsclose

PING = 0.05
SILENCE = 0.4
CLOSE_TIMEOUT = 0.2


class RawClient:
    """A WebSocket client that sends only what it is told to: no automatic pong."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.pings = 0
        self.closed_with = None

    @classmethod
    async def connect(cls, port: int, path: str) -> "RawClient":
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = await reader.readuntil(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101"), head
        return cls(reader, writer)

    def send(self, opcode: int, payload: bytes = b"") -> None:
        assert len(payload) < 126
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.writer.write(bytes((0x80 | opcode, 0x80 | len(payload))) + mask + body)

    async def read(self, answer_pings: bool = False) -> None:
        """Read server frames until it closes, counting pings."""
        try:
            while True:
                b0, b1 = await self.reader.readexactly(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", await self.reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
                payload = await self.reader.readexactly(n)
                if b0 & 0x0F == 0x9:
                    self.pings += 1
                    if answer_pings:
                        self.send(0xA, payload)
                elif b0 & 0x0F == 0x8:
                    self.closed_with = (struct.unpack("!H", payload[:2])[0],
                                        payload[2:].decode())
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            return


async def _drain_handler(ws):
    with contextlib.suppress(ConnectionClosed):
        async for _ in ws:
            pass


def _serve(handler=_drain_handler):
    return serve(handler, "127.0.0.1", 0, ping_interval=PING,
                 ping_timeout=PING, close_timeout=CLOSE_TIMEOUT,
                 create_connection=em_keepalive.DeviceConnection)


def _port(server) -> int:
    return server.sockets[0].getsockname()[1]


def _scaled(monkeypatch):
    monkeypatch.setattr(em_keepalive, "DATA_SILENCE_S", SILENCE)


def test_frames_keep_data_open_with_every_pong_missing(monkeypatch):
    """Twenty-odd unanswered pings, each one past the library's timeout."""
    _scaled(monkeypatch)

    async def run():
        async with _serve() as server:
            client = await RawClient.connect(_port(server), "/data")
            reading = asyncio.create_task(client.read())
            for _ in range(int(3 * SILENCE / PING)):
                client.send(0x2, b"\x01\x00\x00mic")
                await asyncio.sleep(PING)
            assert not reading.done(), f"closed: {client.closed_with}"
            reading.cancel()
            client.writer.close()
            return client.pings

    pings = asyncio.run(run())
    assert pings >= 10, "the server must keep pinging an Echo that never answers"


def test_pongs_alone_keep_an_idle_echo(monkeypatch):
    """An Echo listening for its own wake word sends no frames, only pongs."""
    _scaled(monkeypatch)

    async def run():
        async with _serve() as server:
            client = await RawClient.connect(_port(server), "/data")
            reading = asyncio.create_task(client.read(answer_pings=True))
            await asyncio.sleep(3 * SILENCE)
            assert not reading.done(), f"closed: {client.closed_with}"
            reading.cancel()
            client.writer.close()

    asyncio.run(run())


def test_silence_closes_data_after_the_window_and_not_before(monkeypatch):
    _scaled(monkeypatch)
    seen = {}

    async def handler(ws):
        try:
            async for _ in ws:
                pass
        except ConnectionClosed as e:
            seen["why"] = em_wsclose.from_exception(e)

    async def run():
        async with _serve(handler) as server:
            client = await RawClient.connect(_port(server), "/data")
            client.send(0x2, b"\x01\x00\x00mic")
            t0 = asyncio.get_running_loop().time()
            await asyncio.wait_for(client.read(), 5)
            elapsed = asyncio.get_running_loop().time() - t0
            client.send(0x8, struct.pack("!H", 1011))
            await asyncio.sleep(0.1)
            return elapsed, client

    elapsed, client = asyncio.run(run())
    assert SILENCE - 0.05 <= elapsed < SILENCE + 1.0, elapsed
    assert client.closed_with is not None, "the close must say why"
    code, reason = client.closed_with
    assert code == 1011 and "no pong or frame" in reason
    # handle_data reads the same exception for its log line, which must
    # attribute the close to us.
    assert em_wsclose.describe(*seen["why"]).startswith("we closed")


def test_a_send_stuck_behind_a_dead_link_does_not_hold_the_close(monkeypatch):
    """
    Speaker audio queued at a link that has gone quiet: the socket stops
    draining, and every send, the ping and the close frame included, waits
    on it. The silence rule must still end the connection.
    """
    _scaled(monkeypatch)
    ended = {}

    async def flood(ws):
        try:
            while True:
                await ws.send(b"\x02" + bytes(65536))
        except Exception:
            ended["at"] = asyncio.get_running_loop().time()

    async def run():
        async with _serve(flood) as server:
            client = await RawClient.connect(_port(server), "/data")
            t0 = asyncio.get_running_loop().time()
            for _ in range(int((SILENCE + CLOSE_TIMEOUT + 2.0) / 0.05)):
                if "at" in ended:
                    break
                await asyncio.sleep(0.05)
            client.writer.close()
            return t0

    t0 = asyncio.run(run())
    assert "at" in ended, "a blocked send held the silence check open"
    assert ended["at"] - t0 < SILENCE + CLOSE_TIMEOUT + 1.0


def test_control_keeps_the_library_rule(monkeypatch):
    """Only /data changed: /control still fails on one unanswered ping."""
    _scaled(monkeypatch)

    async def run():
        async with _serve() as server:
            client = await RawClient.connect(_port(server), "/control")
            reading = asyncio.create_task(client.read())
            t0 = asyncio.get_running_loop().time()
            while not reading.done() and asyncio.get_running_loop().time() - t0 < 5:
                client.send(0x1, b"{}")
                await asyncio.sleep(PING / 2)
            return asyncio.get_running_loop().time() - t0, client.closed_with

    elapsed, closed_with = asyncio.run(run())
    assert closed_with == (1011, "keepalive ping timeout")
    assert elapsed < SILENCE


def test_the_window_is_a_minute():
    assert em_keepalive.DATA_SILENCE_S == 60.0
