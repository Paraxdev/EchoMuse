"""
Bulk transfers over the shell plane, against a simulated device whose
sessions are a REAL `sh`.

The firmware's programmatic session is a pipe: every WebSocket message is
written verbatim into sh's stdin and whatever sh prints comes back
(device/internal/client/control.go, runShellSession with pty=false). The
device here is exactly that, so the commands em_shell.stream_file sends are
executed, not described: a heredoc that never closes, a dd that trims the
wrong amount or a resume that appends a block twice fails here.

A dropped link is modelled as the firmware meets it: the bytes that reached
the device before the drop are in sh's stdin, then stdin closes and sh is
left to finish or fail on what it had, so a block can be half-written.

Without busybox the session finds python3 to decode and coreutils md5sum,
which is the device's own fallback order (stream_file's detection).
"""

import asyncio
import base64
import os
import random
import re
import shutil
import sys

import pytest

import em_shell
import em_state

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("sh") is None
    or shutil.which("python3") is None or shutil.which("md5sum") is None,
    reason="needs a POSIX sh, python3 and md5sum")


class Session:
    """One /shell connection: a real sh behind a pipe."""

    def __init__(self, proc, drop_at=None, tamper=None, on_drop=None):
        self.proc = proc
        self.drop_at = drop_at        # stdin bytes after which the link dies
        self.tamper = tamper          # (message index, bytes) -> bytes
        self.on_drop = on_drop
        self.stdin = bytearray()      # everything that reached sh
        self.messages = []            # size of every message sent
        self.closed = False
        self.out = asyncio.Queue()
        self.pump = asyncio.ensure_future(self._pump())

    @classmethod
    async def open(cls, env=None, **spec):
        proc = await asyncio.create_subprocess_exec(
            "sh", stdin=asyncio.subprocess.PIPE, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        return cls(proc, **spec)

    async def _pump(self):
        while True:
            chunk = await self.proc.stdout.read(4096)
            if not chunk:
                return
            await self.out.put(chunk)

    async def send(self, msg):
        if self.closed:
            raise ConnectionError("closed")
        data = msg.encode() if isinstance(msg, str) else bytes(msg)
        if self.tamper:
            data = self.tamper(len(self.messages), data)
        self.messages.append(len(data))
        if self.drop_at is not None and len(self.stdin) + len(data) > self.drop_at:
            data = data[:self.drop_at - len(self.stdin)]
            self.proc.stdin.write(data)
            self.stdin += data
            await self._lose()
            raise ConnectionError("link dropped")
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()
        self.stdin += data

    async def _lose(self):
        """What the firmware does on a dead socket: close sh's stdin and let
        it run to the end of what it was given."""
        self.closed = True
        try:
            await self.proc.stdin.drain()
            self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), 10)
        except Exception:
            self.proc.kill()
            await self.proc.wait()
        if self.on_drop:
            self.on_drop()

    async def recv(self):
        if self.closed:
            raise ConnectionError("closed")
        return await self.out.get()

    async def close(self):
        self.closed = True
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.pump.cancel()


class Device:
    """The live handle stream_file is given. It answers each shell_open with
    a new Session, built from the next entry of `plan`."""

    def __init__(self, plan=(), env=None):
        self.device_id = "dev"
        self.plan = list(plan)
        self.env = env
        self.sessions = []
        self.control = []

    async def send_control(self, msg):
        self.control.append(msg["type"])
        if msg["type"] != "shell_open":
            return
        spec = dict(self.plan.pop(0)) if self.plan else {}
        before = spec.pop("before", None)
        if before:
            before()
        s = await Session.open(env=self.env, **spec)
        self.sessions.append(s)
        em_state.shell_pending[self.device_id].set_result(s)


@pytest.fixture(autouse=True)
def clean():
    for d in (em_shell.locks, em_shell.owners, em_shell.sockets,
              em_state.shell_pending, em_state.devices):
        d.clear()
    yield
    for d in (em_shell.locks, em_shell.owners, em_shell.sockets,
              em_state.shell_pending, em_state.devices):
        d.clear()


@pytest.fixture
def small_blocks(monkeypatch):
    """Blocks of 100 base64 lines, so a few kB of test data is many blocks."""
    monkeypatch.setattr(em_shell, "BLOCK_BYTES", 57 * 100)
    return 57 * 100


def _data(n, seed=7):
    return random.Random(seed).randbytes(n)


def _push(dev, data, dest, **kw):
    async def main():
        em_state.devices[dev.device_id] = dev
        try:
            return await em_shell.stream_file(dev, data, str(dest), **kw)
        finally:
            for s in dev.sessions:
                await s.close()
    return asyncio.run(main())


def _b64_lines(stdin: bytes) -> bytes:
    """The base64 lines a session's sh read, in order, without the commands."""
    return b"".join(l + b"\n" for l in stdin.split(b"\n")
                    if re.fullmatch(rb"[A-Za-z0-9+/=]+", l))


# ── The ordinary case ────────────────────────────────────────────────────────

def test_a_file_arrives_whole_in_a_few_large_messages(tmp_path):
    """
    One 77-byte message per base64 line was ~190,000 messages for a 10.7MB
    binary. Messages are now cut at MSG_BYTES on line ends, and the lines the
    shell reads are exactly the ones a whole-file heredoc gave it.
    """
    data = _data(3 * em_shell.BLOCK_BYTES // 2)
    dest = tmp_path / "server_b"
    dev = Device()

    res = _push(dev, data, dest, mode="755", require_verify=True)

    assert res, str(res)
    assert dest.read_bytes() == data
    assert dest.stat().st_mode & 0o777 == 0o755
    assert not (tmp_path / "server_b.part").exists()
    assert len(dev.sessions) == 1

    s = dev.sessions[0]
    lines = len(base64.encodebytes(data).splitlines())
    assert max(s.messages) <= em_shell.MSG_BYTES
    assert len(s.messages) < lines // 100, (
        f"{len(s.messages)} messages for {lines} base64 lines")
    assert _b64_lines(bytes(s.stdin)) == base64.encodebytes(data), (
        "the shell must read the same 76-character lines a single heredoc gave it")


def test_an_empty_file_still_lands(tmp_path, small_blocks):
    dest = tmp_path / "empty"
    assert _push(Device(), b"", dest, mode="644")
    assert dest.exists() and dest.read_bytes() == b""


def test_each_block_is_confirmed_by_the_device(tmp_path, small_blocks):
    """Every block's answer comes back, which is what a resume starts from."""
    data = _data(5 * small_blocks + 123)
    dest = tmp_path / "asset.onnx"
    dev = Device()
    assert _push(dev, data, dest, mode="644")
    sent = bytes(dev.sessions[0].stdin).decode()
    assert sent.count("__BLK_OK_") == 6
    assert sent.count(f" >> {dest}.part ") == 5 and sent.count(f" > {dest}.part ") == 1, (
        "the first block truncates, the rest append")
    assert dest.read_bytes() == data


# ── A link that drops ────────────────────────────────────────────────────────

# A device whose busybox has md5sum and dd but no base64 applet, as Ubuntu's
# (and so CI's) is built: the transfer then decodes with python3 and trims
# with `busybox dd`, the device's own branch.
BUSYBOX_SHIM = """#!/bin/sh
case "$1" in
  md5sum|dd) cmd=$1; shift; exec "$cmd" "$@" ;;
esac
exit 127
"""


@pytest.fixture(params=["plain", "busybox"])
def tools(request, tmp_path):
    if request.param == "plain":
        return None
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "busybox").write_text(BUSYBOX_SHIM)
    (shim / "busybox").chmod(0o755)
    return {**os.environ, "PATH": f"{shim}:{os.environ.get('PATH', '')}"}


def test_a_dropped_session_resumes_at_the_confirmed_blocks(tmp_path, small_blocks, tools):
    """
    The case the field kept losing: the shell plane dies part-way while the
    control plane stays up. The transfer opens a new session, trims the file
    back to what the device confirmed, and sends only the rest.
    """
    data = _data(8 * small_blocks + 99)
    total = len(base64.encodebytes(data))
    per_block = len(base64.encodebytes(data[:small_blocks]))
    dest = tmp_path / "server_b"
    dest.write_bytes(b"previous binary")
    # Dies in the middle of the fifth block, after the detection round trip.
    dev = Device(plan=[{"drop_at": 4 * per_block + per_block // 2 + 600}], env=tools)

    res = _push(dev, data, dest, mode="755")

    assert res, str(res)
    assert dest.read_bytes() == data
    assert not (tmp_path / "server_b.part").exists()
    assert not (tmp_path / "server_b.part.t").exists()
    assert dev.control.count("shell_open") == 2
    resumed = len(_b64_lines(bytes(dev.sessions[1].stdin)))
    assert 0 < resumed <= total - 3 * per_block, (
        f"the second session resent {resumed} of {total} base64 bytes: it "
        f"started again rather than resuming")
    if tools:
        assert b"busybox dd if=" in dev.sessions[1].stdin
        assert b"busybox md5sum" in dev.sessions[1].stdin


def test_a_prefix_the_device_cannot_confirm_starts_the_file_again(tmp_path, small_blocks):
    """
    The confirmed prefix is kept only when the device's md5 of it is ours.
    Here something changed it between sessions (a write from the dead session
    landing late would look the same), so the transfer starts again from the
    first block rather than appending to a file it cannot vouch for.
    """
    data = _data(6 * small_blocks)
    total = len(base64.encodebytes(data))
    dest = tmp_path / "server_b"
    part = tmp_path / "server_b.part"

    def spoil():
        raw = bytearray(part.read_bytes())
        raw[10] ^= 0xFF
        part.write_bytes(bytes(raw))

    dev = Device(plan=[{"drop_at": total // 2}, {"before": spoil}])
    res = _push(dev, data, dest, mode="644")

    assert res, str(res)
    assert dest.read_bytes() == data
    assert len(_b64_lines(bytes(dev.sessions[1].stdin))) == total, (
        "an unconfirmed prefix must be sent again in full")


def test_too_many_drops_fail_and_leave_the_destination_alone(tmp_path, small_blocks):
    data = _data(6 * small_blocks)
    dest = tmp_path / "server_b"
    dest.write_bytes(b"previous binary")
    every = [{"drop_at": 3000}] * (em_shell.RESUME_TRIES + 1)
    dev = Device(plan=every)

    res = _push(dev, data, dest, mode="755")

    assert not res
    assert res.stage == "send" and "dropped" in str(res), str(res)
    assert dest.read_bytes() == b"previous binary", (
        "a failed transfer must leave the rollback slot as it was")
    assert len(dev.sessions) == em_shell.RESUME_TRIES + 1


def test_a_device_that_went_away_is_not_chased(tmp_path, small_blocks):
    """The control plane dropped too: the registry holds another object, or
    none, and asking that for a shell would talk to a different connection."""
    data = _data(6 * small_blocks)
    dest = tmp_path / "server_b"
    dev = Device(plan=[{"drop_at": 5000,
                        "on_drop": lambda: em_state.devices.pop("dev", None)}])

    res = _push(dev, data, dest, mode="755")

    assert not res and res.stage == "send" and "disconnected" in str(res), str(res)
    assert dev.control.count("shell_open") == 1
    assert not dest.exists()


# ── Answers that resending cannot change ─────────────────────────────────────

def test_a_device_that_cannot_write_stops_at_once(tmp_path, small_blocks):
    """A failed write is answered, not waited out, and not retried: a full
    /data or a read-only mount is the same on the next session."""
    data = _data(4 * small_blocks)
    dest = tmp_path / "server_b"
    dest.write_bytes(b"previous binary")
    (tmp_path / "server_b.part").mkdir()      # `> server_b.part` cannot succeed

    dev = Device()
    res = _push(dev, data, dest, mode="755")

    assert not res and res.stage == "write", str(res)
    assert len(dev.sessions) == 1
    assert dest.read_bytes() == b"previous binary"


def test_bytes_changed_on_the_way_are_caught_by_md5(tmp_path, small_blocks):
    """
    Every block can be confirmed and the file still be wrong. md5 of the whole
    file decides, as it always has: the .part is removed and dest untouched.
    """
    data = _data(4 * small_blocks)
    dest = tmp_path / "server_b"
    dest.write_bytes(b"previous binary")

    def flip(i, msg):
        if i != 2:       # 0 is the detection probe, 1 the first block
            return msg
        lines = msg.split(b"\n")
        lines[1] = (b"B" if lines[1][:1] != b"B" else b"C") + lines[1][1:]
        return b"\n".join(lines)

    dev = Device(plan=[{"tamper": flip}])
    res = _push(dev, data, dest, mode="755")

    assert not res and res.stage == "corrupt", str(res)
    assert dest.read_bytes() == b"previous binary"
    assert not (tmp_path / "server_b.part").exists()
