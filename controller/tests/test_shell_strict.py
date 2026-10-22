"""
em_shell.run and run_strict: what a caller can tell apart.

run is forgiving on purpose, and its callers rely on it: a failure is "", and
5s of silence returns what arrived. That made a listing that failed and one
that found nothing the same string, and the wake word reconcile answered the
first with an 11.7MB push over the link that had just failed (10-21/22).
run_strict is for a caller that acts on what is ABSENT: it raises unless the
completion sentinel arrived, and silence is waited out up to the deadline, or
up to silence_s when the caller bounds it.

Every session holds the device's shell lock, which the next caller waits
em_shell.LOCK_WAIT_S for unless it asks for longer.
"""

import asyncio
import time

import pytest

import em_oww_sync
import em_shell
import em_state


class ScriptedWS:
    """A shell session that answers from a script. A TimeoutError entry is a
    quiet spell: the recv times out, as wait_for would make it."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if not self.replies:
            await asyncio.sleep(3600)
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    async def close(self):
        pass


class Live:
    def __init__(self, ws=None):
        self.device_id = "dev"
        self.ws = ws

    async def send_control(self, msg):
        if msg["type"] == "shell_open" and self.ws is not None:
            em_state.shell_pending[self.device_id].set_result(self.ws)


@pytest.fixture(autouse=True)
def clean():
    for d in (em_shell.locks, em_shell.owners, em_shell.sockets, em_state.shell_pending):
        d.clear()
    yield
    for d in (em_shell.locks, em_shell.owners, em_shell.sockets, em_state.shell_pending):
        d.clear()


S = em_shell._SENTINEL


def _run(fn, live, timeout=30.0):
    return asyncio.run(fn(live, "true", timeout=timeout))


def test_a_sentinel_split_across_two_messages_is_still_found():
    """The device sends output in whatever pieces its reads produce."""
    for fn in (em_shell.run, em_shell.run_strict):
        ws = ScriptedWS([b"a 1\nFREE 5\n" + S[:6].encode(), S[6:].encode() + b"\n"])
        assert _run(fn, Live(ws)) == "a 1\nFREE 5"


def test_strict_waits_out_a_quiet_spell_and_run_does_not():
    """md5 over a 12MB runtime on an Echo can be silent for more than 5s.
    run returns what it had (the old behaviour its callers expect); strict
    keeps reading to the sentinel."""
    script = [b"first 1\n", asyncio.TimeoutError(), b"second 2\n" + S.encode() + b"\n"]
    assert _run(em_shell.run, Live(ScriptedWS(script))) == "first 1"
    assert _run(em_shell.run_strict, Live(ScriptedWS(script))) == "first 1\nsecond 2"


def test_strict_raises_where_run_returns_empty():
    """No session at all: the device never dialled back."""
    async def no_wait(aw, timeout):
        aw.close() if hasattr(aw, "close") else None
        raise asyncio.TimeoutError()

    real = asyncio.wait_for

    async def acquire_then_give_up(aw, timeout):
        # The lock acquire succeeds; the wait for the device's dial does not.
        if timeout == 20.0:
            return await real(aw, timeout)
        return await no_wait(aw, timeout)

    asyncio.wait_for = acquire_then_give_up
    try:
        assert _run(em_shell.run, Live()) == ""
        em_shell.locks.clear()
        with pytest.raises(em_shell.ShellError):
            _run(em_shell.run_strict, Live())
    finally:
        asyncio.wait_for = real


def test_strict_raises_on_a_dropped_socket():
    ws = ScriptedWS([b"a 1\n", ConnectionError("1011 keepalive ping timeout")])
    with pytest.raises(em_shell.ShellError, match="keepalive"):
        _run(em_shell.run_strict, Live(ws))
    ws = ScriptedWS([b"a 1\n", ConnectionError("1011 keepalive ping timeout")])
    assert _run(em_shell.run, Live(ws)) == "", "run keeps its forgiving answer"


def test_strict_raises_when_the_sentinel_never_comes():
    ws = ScriptedWS([b"a 1\n"])
    with pytest.raises(em_shell.ShellError, match="no completion marker"):
        _run(em_shell.run_strict, Live(ws), timeout=0.2)


def test_the_session_is_released_either_way():
    """A strict failure still closes the session and frees the lock."""
    ws = ScriptedWS([ConnectionError("gone")])
    with pytest.raises(em_shell.ShellError):
        _run(em_shell.run_strict, Live(ws))
    assert not em_shell.locks["dev"].locked()
    assert "dev" not in em_shell.owners and "dev" not in em_shell.sockets


class TricklingWS(ScriptedWS):
    """Output that keeps arriving, `gap` seconds apart."""

    def __init__(self, replies, gap):
        super().__init__(replies)
        self.gap = gap

    async def recv(self):
        await asyncio.sleep(self.gap)
        return await super().recv()


def test_strict_gives_up_after_silence_s_rather_than_the_timeout():
    """A stalled link kept the session, and the device's shell lock with it,
    for the whole timeout: the asset listing's 120s, past the lock wait of an
    OTA started from the same Updates tab."""
    ws = ScriptedWS([b"a 1\n"])
    started = time.monotonic()
    with pytest.raises(em_shell.ShellError, match="no output"):
        asyncio.run(em_shell.run_strict(Live(ws), "true", timeout=30.0, silence_s=0.2))
    assert time.monotonic() - started < 5.0
    assert not em_shell.locks["dev"].locked()


def test_output_that_keeps_coming_is_not_silence():
    """The bound is on each quiet spell, never on the total."""
    pieces = [b"a 1\n", b"b 2\n", b"c 3\n", b"d 4\n", S.encode() + b"\n"]
    ws = TricklingWS(pieces, gap=0.1)
    out = asyncio.run(em_shell.run_strict(Live(ws), "true", timeout=30.0, silence_s=0.3))
    assert out == "a 1\nb 2\nc 3\nd 4"


def test_the_asset_listing_gives_up_on_a_stalled_link(monkeypatch):
    monkeypatch.setattr(em_oww_sync, "LISTING_SILENCE_S", 0.2)
    monkeypatch.setattr(em_oww_sync, "LISTING_TIMEOUT_S", 10.0)
    ws = ScriptedWS([b"1111 1700000000 /data/local/oww/x.onnx\n"])
    started = time.monotonic()
    with pytest.raises(em_shell.ShellError, match="no output"):
        asyncio.run(em_oww_sync.device_state(Live(ws)))
    assert time.monotonic() - started < 5.0


def _while_held(hold_s, lock_wait):
    """run() against a device whose shell lock someone else holds for hold_s."""
    async def main():
        lock = em_shell.locks.setdefault("dev", asyncio.Lock())
        await lock.acquire()
        asyncio.get_running_loop().call_later(hold_s, lock.release)
        live = Live(ScriptedWS([b"SLOT:server_a\n" + S.encode() + b"\n"]))
        return await em_shell.run(live, "true", lock_wait=lock_wait)
    return asyncio.run(main())


def test_run_waits_for_a_busy_shell_as_long_as_it_is_told(monkeypatch):
    monkeypatch.setattr(em_shell, "LOCK_WAIT_S", 0.1)
    assert _while_held(0.3, lock_wait=2.0) == "SLOT:server_a"
    em_shell.locks.clear()
    assert _while_held(0.3, lock_wait=None) == "", (
        "without lock_wait the wait is LOCK_WAIT_S, read when called")
