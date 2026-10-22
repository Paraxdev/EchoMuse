"""
When a device WebSocket counts as dead: /data by its own rule, the rest by
the library's.

/control and /shell keep websockets' keepalive: a ping every
WS_PING_INTERVAL_S, and the connection fails when one goes unanswered for
WS_PING_TIMEOUT_S. That rule kills /data while it is working. The firmware's
gorilla/websocket (v1.5.3) answers a ping from its read loop with a one
second write deadline (conn.go `writeWait`), and its default ping handler
drops the pong without a word when that deadline passes. A mic-frame write
blocked on a lossy link holds the write lock for up to 10s, so an Echo
streaming its wake audio loses pongs while its frames keep arriving, and one
lost pong was a `1011 keepalive ping timeout`. Measured on one Echo,
2026-10-11 to 10-22: 1807 /data closes that way against 121 on /control.

So /data is pinged at the same interval, which is what an idle Echo answers,
and is closed only when NOTHING has arrived from it for DATA_SILENCE_S: no
pong, no frame, not even the device's own ping. Any bytes off the socket
count. A link that is really dead is still found from both ends: here after
DATA_SILENCE_S, and on the device by its 45s read deadline (wsPongWait).
"""

from __future__ import annotations

import asyncio

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed
from websockets.frames import CloseCode

DATA_PATH = "/data"
DATA_SILENCE_S = 60.0


class DeviceConnection(ServerConnection):
    """A device-plane connection that applies the /data rule above to /data."""

    heard_at = 0.0

    def data_received(self, data: bytes) -> None:
        self.heard_at = self.loop.time()
        super().data_received(data)

    def start_keepalive(self) -> None:
        if (self.ping_interval is None or self.request is None
                or self.request.path != DATA_PATH):
            super().start_keepalive()
            return
        self.heard_at = self.loop.time()
        # Held where the library keeps its own, so connection_lost cancels it.
        self.keepalive_task = self.loop.create_task(self._data_keepalive())

    async def _data_keepalive(self) -> None:
        next_ping = self.loop.time() + self.ping_interval
        while True:
            now = self.loop.time()
            deadline = self.heard_at + DATA_SILENCE_S
            if now >= deadline:
                break
            if now < next_ping:
                await asyncio.sleep(min(next_ping, deadline) - now)
                continue
            next_ping = now + self.ping_interval
            try:
                # A send stuck behind a dead link waits on drain for as long
                # as TCP keeps retrying, and must not hold up the check.
                async with asyncio.timeout_at(deadline):
                    await self.ping()
            except TimeoutError:
                pass
            except ConnectionClosed:
                return
        try:
            async with asyncio.timeout(self.close_timeout):
                await self.close(CloseCode.INTERNAL_ERROR,
                                 f"no pong or frame in {DATA_SILENCE_S:.0f}s")
        except TimeoutError:
            self.transport.abort()
