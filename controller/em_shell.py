"""
Root shell sessions on a device, over the /shell plane: one programmatic
session per device at a time, commands with a deadline, and file transfers
verified by md5 before they replace anything.

The device dials /shell when asked (`shell_open`), and em_controller's
handle_shell hands the socket over through em_state.shell_pending. Everything
the controller does to a device beyond the control messages goes through
here: OTA, payload syncs, wake word assets, credentials, the emOS update.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import posixpath as _posixpath
import re
import time
from shlex import quote as _sh_quote

import em_state
import em_tcp

log = logging.getLogger("echomuse.api")

# How long to wait before asking again when the shell plane did not answer:
# seconds after a register it is often not up yet.
CONNECT_RETRY_S = 10.0

sockets: dict = {}   # device_id → live ws for programmatic sessions
locks:   dict = {}   # device_id → asyncio.Lock (one session at a time)
# device_id → the asyncio Task that actually holds its lock.
#
# `Lock.locked()` answers "is anyone holding this", never "am I", and the
# cleanup paths used it as though it meant the second. So a caller that gave
# up WAITING for the lock ran the same cleanup as one that had it, and
# released the lock out from under the transfer still using it — two shell
# sessions on one device, which is the exact thing the lock exists to stop.
# Seen end to end on EFF 2026-09-04: a debloat push hung for 108s, the wake
# word reconcile behind it timed out and released the debloat's lock, and the
# slot detect that followed then failed with `Lock is not acquired` and
# reported an empty result — surfacing to the operator as "could not
# determine active slot", three steps from anything to do with locking.
owners:  dict = {}   # device_id → task holding its lock


async def get_ws(live) -> object:
    """
    Request a programmatic shell connection from the device.

    Acquires a per-device lock so sessions are strictly sequential.
    handle_shell resolves the future with the ws, then waits for ws.close()
    before returning — so the connection stays alive while we use it.
    """
    device_id = live.device_id

    if device_id not in locks:
        locks[device_id] = asyncio.Lock()

    try:
        await asyncio.wait_for(locks[device_id].acquire(), timeout=20.0)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Shell lock acquisition timed out for {device_id}")
    # Claimed immediately after a successful acquire and never before it:
    # everything that cleans up asks whether it is the owner, so a caller
    # that timed out above must not be able to answer yes.
    owners[device_id] = asyncio.current_task()

    try:
        return await _open_session(live)
    except asyncio.TimeoutError:
        release_lock(device_id)
        raise


async def _open_session(live) -> object:
    """Ask the device to dial /shell and wait for handle_shell to hand it over.
    The caller holds the device's shell lock."""
    device_id = live.device_id
    future = asyncio.get_event_loop().create_future()
    em_state.shell_pending[device_id] = future
    # Deliberately do NOT set em_state.shell_dashboard — signals programmatic mode.

    await live.send_control({"type": "shell_open"})
    try:
        ws = await asyncio.wait_for(future, timeout=15.0)
    except asyncio.TimeoutError:
        em_state.shell_pending.pop(device_id, None)
        sockets.pop(device_id, None)
        raise
    sockets[device_id] = ws
    # Programmatic sessions are where the bulk transfers run. Interactive
    # consoles go through em_routes_shell and are left alone.
    transport = getattr(ws, "transport", None)
    if transport is not None:
        em_tcp.limit_unsent(transport.get_extra_info("socket"))
    return ws


async def reopen_ws(live) -> object:
    """
    Replace the calling task's session with a fresh one, keeping the lock.

    For a transfer whose socket dropped part-way: releasing the lock and
    queueing for it again would let anything else waiting on the device run
    against a half-written file. The device replaces its own session when a
    second `shell_open` arrives, so nothing is left running at its end.
    """
    device_id = live.device_id
    if owners.get(device_id) is not asyncio.current_task():
        raise RuntimeError(f"the shell session of {device_id} is not this task's to reopen")
    old = sockets.pop(device_id, None)
    if old is not None:
        # Bounded: on a link that just failed, a close handshake can wait out
        # websockets' whole close timeout for an answer that is not coming.
        try:
            await asyncio.wait_for(old.close(), timeout=2.0)
        except Exception:
            pass
    try:
        return await _open_session(live)
    except asyncio.TimeoutError:
        raise RuntimeError("the device did not open a new shell session within 15s") from None


def release_lock(device_id: str) -> None:
    """
    Release the shell lock, but only if THIS task is the one holding it.

    `Lock.locked()` cannot answer that question — it says whether anyone
    holds the lock — so guarding a release with it lets a caller that never
    acquired release somebody else's. See the owners comment.
    """
    if owners.get(device_id) is not asyncio.current_task():
        return
    owners.pop(device_id, None)
    lock = locks.get(device_id)
    if lock and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass


async def release_ws(device_id: str, live=None) -> None:
    """
    Close the programmatic shell session.

    Closing ws wakes handle_shell's ws.wait_closed(), which then returns
    and lets the device clean up its side too.

    **A task that does not hold the lock cleans up nothing.** Callers run
    this from a `finally`, which is reached whether or not the acquire
    succeeded — so a caller that timed out waiting used to close the
    websocket belonging to the transfer that was still using it, send that
    device `shell_close`, and release its lock. Every one of those is an
    action against another operation's session.
    """
    if owners.get(device_id) is not asyncio.current_task():
        return
    ws = sockets.pop(device_id, None)
    if ws:
        try:
            await ws.close()
        except Exception:
            pass
    em_state.shell_pending.pop(device_id, None)
    if live is not None:
        await live.send_control({"type": "shell_close"})
    release_lock(device_id)


_SENTINEL = "__CMD_DONE_9f3a__"


class ShellError(RuntimeError):
    """run_strict could not say what the command printed: no session, a
    dropped socket, or no completion sentinel before the deadline."""


def _text(msg) -> str:
    return msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg


async def run(live, cmd: str, timeout: float = 30.0) -> str:
    """
    Run a shell command on the device and return its stdout as a string.

    Appends a sentinel marker to detect when output is complete. Forgiving:
    any failure returns "", and 5s without output returns whatever arrived.
    A caller that must tell "printed nothing" from "never answered" appends
    OK_MARK, or uses run_strict.
    """
    return await _run(live, cmd, timeout, strict=False)


async def run_strict(live, cmd: str, timeout: float = 30.0) -> str:
    """
    run(), for a caller that acts on what is ABSENT from the output.

    Raises ShellError unless the completion sentinel arrived, and waits out
    silence up to `timeout` rather than 5s. run's "" on a failure and its
    partial output after a quiet spell read, to the wake word reconcile, as
    an Echo with nothing installed, and the answer to that was an 11.7MB push
    over the link that had just failed (10-21/22, four times in one night).
    """
    return await _run(live, cmd, timeout, strict=True)


async def _run(live, cmd: str, timeout: float, strict: bool) -> str:
    device_id = live.device_id
    buf = ""
    try:
        ws = await get_ws(live)
        await ws.send(f"{cmd} ; echo '{_SENTINEL}'\n")
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                # md5 over the 12MB runtime can be silent for longer than 5s.
                msg = await asyncio.wait_for(ws.recv(), timeout=left if strict else 5.0)
            except asyncio.TimeoutError:
                if strict:
                    continue
                break
            buf += _text(msg)
            # Searched in everything received, not per message: the device
            # sends output in whatever pieces its reads produce, and the
            # sentinel can straddle two of them.
            if _SENTINEL in buf:
                return buf[:buf.index(_SENTINEL)].strip()
        if strict:
            raise ShellError(f"no completion marker within {timeout:.0f}s "
                             f"({len(buf)} bytes of output)")
        return buf.strip()
    except ShellError:
        raise
    except Exception as e:
        if strict:
            raise ShellError(str(e) or type(e).__name__) from e
        log.error(f"[api] shell_run failed ({cmd!r}): {e}")
        return ""
    finally:
        await release_ws(device_id, live)


class TransferResult:
    """
    The outcome of a device file transfer, carrying the STAGE it stopped at.

    Truthy on success, so every existing `if not await stream_file(
    ...)` call site keeps working unchanged.

    The stage exists because one message covered five different outcomes, and
    the two that matter most are at opposite ends: "the bytes arrived corrupt"
    and "we never opened a shell, so no byte was ever sent". #121 reported the
    second and read as the first — three Dots failing with `failed or did not
    verify`, fifteen seconds after starting, which is far too fast to have
    attempted a 10MB payload. Nothing short of the controller's own stdout
    could tell them apart, and a user cannot be asked for that mid-update.
    """

    __slots__ = ("ok", "stage", "detail")

    def __init__(self, ok: bool, stage: str = "ok", detail: str = ""):
        self.ok     = ok
        self.stage  = stage
        self.detail = detail

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        return self.detail or self.stage


# Stage detail text. Phrased for someone reading a device log who is deciding
# what to try next, so each one says whether any data left the controller —
# that is the difference between "retry, the link was bad" and "the payload or
# the device is wrong".
_TRANSFER_STAGES = {
    "shell":   "could not open a shell session on the device — no data was sent",
    "decoder": "no base64 decoder found on the device — no data was sent",
    "md5tool": "device has no md5 tool, refusing to send unverified",
    "send":    "did not finish sending: the link dropped or stalled part-way",
    "write":   "the device could not write the file, so sending stopped",
    "verify":  "sent, but timed out waiting for md5 verification",
    "corrupt": "arrived corrupt — md5 did not match what was sent",
    "error":   "transfer error",
}


def _transfer_failed(stage: str, extra: str = "") -> TransferResult:
    detail = _TRANSFER_STAGES.get(stage, stage)
    if extra:
        detail = f"{detail} ({extra})"
    return TransferResult(False, stage, detail)


# ── Bulk transfer ────────────────────────────────────────────────────────────
#
# The file goes as base64 heredocs into a programmatic session, which is a
# PIPE into the device's sh (shell_open without `pty`, every firmware since
# v2.2.0 writes each message's bytes into it verbatim, with no read limit).
# The 4096-byte canonical line limit that shapes tools/push_file.py belongs
# to the dashboard's PTY and does not apply here. Lines stay 76 characters
# anyway, so the shell reads exactly what it always has; only the framing
# and the block structure below are new.

# Bytes per heredoc. A multiple of 57, the bytes behind one 76-character
# base64 line, so each block encodes to whole lines with no padding mid-file.
BLOCK_BYTES = 57 * 9198            # 524,286
# Largest WebSocket message. One base64 line per message was ~190,000
# messages for a 10.7MB binary, each a coroutine on the event loop.
MSG_BYTES = 32 * 1024
# New sessions one transfer may open after its socket dropped.
RESUME_TRIES = 3
# How long the device has to confirm a block, counted from when the block
# after it finished sending (or from the last block, for the last one).
BLOCK_ACK_S = 60.0
# How long the socket may take to accept one message. A connection that
# cannot is treated as dropped; the keepalive would usually have said so.
SEND_S = 45.0

_DELIM = "__END_B64_42__"   # '_' is not in the base64 alphabet


def _messages(text: str, limit: int = MSG_BYTES) -> list[str]:
    """`text` as WebSocket messages of at most `limit` bytes, cut at line ends."""
    out, start = [], 0
    while len(text) - start > limit:
        end = text.rfind("\n", start, start + limit) + 1
        if end <= start:
            end = start + limit
        out.append(text[start:end])
        start = end
    if start < len(text):
        out.append(text[start:])
    return out


async def _send_text(ws, text: str) -> None:
    for msg in _messages(text):
        await asyncio.wait_for(ws.send(msg), timeout=SEND_S)


class _Refused(Exception):
    """The device answered that it could not write a block. Resending will
    not change that (a full /data, a read-only mount)."""


class _BlockPush:
    """
    `data` into `landing` one heredoc per BLOCK_BYTES, each confirmed by the
    device, carried across sessions that drop part-way.

    One heredoc for the whole file meant one dropped socket cost everything
    sent so far. On 2026-10-13 five OTAs to one Echo in a row died with `1011
    keepalive ping timeout` on the shell plane while its control plane stayed
    up. The design is tools/push_file.py's: a block is appended and answered,
    and after a drop a new session trims the file back to the confirmed
    blocks and carries on. md5 still decides at the end, so a resume that
    went wrong costs a retry, never a bad file.
    """

    def __init__(self, live, ws, data: bytes, landing: str,
                 decode_cmd: str, md5_cmd: str):
        self.live, self.ws, self.data, self.landing = live, ws, data, landing
        self.decode_cmd, self.md5_cmd = decode_cmd, md5_cmd
        self.count = max(1, -(-len(data) // BLOCK_BYTES))
        self.acked = 0                   # blocks confirmed, always a prefix
        self.acked_md5 = hashlib.md5()   # of data[:acked * BLOCK_BYTES]
        self.out = ""                    # device output not yet consumed
        self.resumes = 0

    def _block(self, i: int) -> bytes:
        return self.data[i * BLOCK_BYTES:(i + 1) * BLOCK_BYTES]

    async def _send_block(self, i: int) -> None:
        # Block 0 truncates whatever an earlier attempt left in landing.
        op = ">" if i == 0 else ">>"
        await _send_text(self.ws, (
            f"{self.decode_cmd} << '{_DELIM}' {op} {self.landing} "
            f"&& echo __BLK_OK_{i}__ || echo __BLK_BAD_{i}__\n"
            + base64.encodebytes(self._block(i)).decode("ascii")
            + f"{_DELIM}\n"))

    def _take_answers(self) -> None:
        end = 0
        for m in re.finditer(r"__BLK_(OK|BAD)_(\d+)__", self.out):
            if m.group(1) == "BAD":
                said = self.out[:m.start()].strip()[-200:]
                raise _Refused(f"block {int(m.group(2)) + 1} of {self.count} "
                               f"to {self.landing}" + (f": {said}" if said else ""))
            # One shell runs the blocks in order, so answers are a prefix.
            if int(m.group(2)) == self.acked:
                self.acked_md5.update(self._block(self.acked))
                self.acked += 1
            end = m.end()
        self.out = self.out[end:]

    async def _read_until(self, done, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not done():
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except asyncio.TimeoutError:
                continue
            self.out += _text(msg)
        return True

    async def _confirmed(self, n: int) -> None:
        def done():
            self._take_answers()
            return self.acked >= n
        if not await self._read_until(done, BLOCK_ACK_S):
            raise TimeoutError(f"no answer for block {self.acked + 1} of "
                               f"{self.count} in {BLOCK_ACK_S:.0f}s")

    async def _trim(self) -> int:
        """
        Cut landing back to the confirmed blocks on a fresh session, and
        return the block to send next.

        The block in flight at the drop may be half-written, or written with
        its answer lost. The confirmed prefix is kept only when the device's
        md5 of it matches ours; anything else starts the file again, which is
        what every attempt did before blocks existed. `dd` + `mv` rather than
        truncate, which push_file.py found silently does nothing on this
        busybox.
        """
        self._take_answers()    # what arrived before the drop still counts
        self.out = ""
        if self.acked == 0:
            return 0
        bb = "busybox " if self.md5_cmd.startswith("busybox") else ""
        part = self.landing
        await _send_text(self.ws, (
            f"{bb}dd if={part} of={part}.t bs={BLOCK_BYTES} count={self.acked} "
            f"2>/dev/null && mv {part}.t {part}; rm -f {part}.t; "
            f"echo \"__BLK_HAVE_$({self.md5_cmd} {part} 2>/dev/null)\"; "
            f"echo __BLK_END__\n"))
        if not await self._read_until(lambda: "__BLK_END__" in self.out, BLOCK_ACK_S):
            raise TimeoutError("no answer while checking the partial file")
        got = re.search(r"__BLK_HAVE_([0-9a-fA-F]{32})", self.out)
        self.out = ""
        if got and got.group(1).lower() == self.acked_md5.hexdigest():
            log.info(f"[api] {self.landing}: resuming at block {self.acked + 1} "
                     f"of {self.count}")
            return self.acked
        log.warning(f"[api] {self.landing}: could not confirm the "
                    f"{self.acked} block(s) already sent; starting again")
        self.acked, self.acked_md5 = 0, hashlib.md5()
        return 0

    async def run(self) -> tuple[str, str] | None:
        """None once every block is confirmed, else (stage, why)."""
        device_id = self.live.device_id
        nxt, why = 0, None
        while True:
            try:
                if why is not None:
                    self.ws = await reopen_ws(self.live)
                    nxt = await self._trim()
                while nxt < self.count:
                    await self._send_block(nxt)
                    nxt += 1
                    # One block in hand: the device writes block i-1 while
                    # block i is on the wire, so confirming costs no round trip.
                    await self._confirmed(nxt - 1)
                await self._confirmed(self.count)
                return None
            except _Refused as e:
                return "write", str(e)
            except Exception as e:
                why = str(e) or type(e).__name__
            if self.resumes >= RESUME_TRIES:
                return "send", (f"the session dropped {self.resumes + 1} times, "
                                f"last: {why}")
            if em_state.devices.get(device_id) is not self.live:
                return "send", f"the device disconnected ({why})"
            self.resumes += 1
            log.warning(f"[api] {device_id}: shell session dropped sending "
                        f"{self.landing} ({why}) with {self.acked} of "
                        f"{self.count} blocks confirmed; opening a new one "
                        f"({self.resumes}/{RESUME_TRIES})")


async def stream_file(live, data: bytes, dest: str,
                                 mode: str = "755",
                                 require_verify: bool = False) -> TransferResult:
    """
    Transfer a file to `dest` on the device via shell heredoc (default mode 755).

    Detects available base64 decoder (busybox base64, python3, python) before
    transferring, since 'base64' is not always in PATH on Android/FireOS.
    Sent in blocks of base64 heredocs, each confirmed, resumed on a new
    session when the socket drops (_BlockPush); no intermediate .b64 file.

    **md5 decides success, not the shell's exit status.** Bytes land in
    `{dest}.part` and are renamed only once their md5 matches what we sent, so
    a bad transfer leaves whatever was at `dest` untouched. `TRANSFER_OK` alone
    only ever proved that the decode pipeline and chmod exited 0 — not that the
    bytes arrived intact (#76). The verification rides the SAME shell session as
    the transfer, so it costs a round trip on an open socket, not a new session.

    `require_verify` decides what happens when the device has no md5 tool at
    all. Callers default to False, which warns and accepts — the same behaviour
    they had before this existed, and the base64 detection below already treats
    a busybox-less device as a contemplated state. Firmware passes True.
    """
    device_id     = live.device_id
    DETECT_MARKER = "__DETECT_DONE__"

    try:
        try:
            ws = await get_ws(live)
        except Exception as e:
            log.error(f"[api] Could not open a shell to {device_id} for "
                      f"{dest}: {e}")
            return _transfer_failed("shell", str(e))

        # `dest` is NOT deleted here, and must not be. This used to open with
        # `rm -f {dest}` to clear a previous attempt, which for firmware means
        # deleting /data/local/bin/server_<inactive> — THE ROLLBACK SLOT —
        # before a single byte of the replacement had been sent. Every failed
        # OTA therefore left the device with a good active slot and an empty
        # partner, so a later crash-loop would flip the symlink onto nothing.
        # It also contradicted the message the user was shown, which promised
        # the slot was left untouched (#121). Nothing needs the delete: the
        # heredoc writes with `>`, which truncates, and a verified transfer
        # arrives by `mv` over whatever was there.

        # ── Detect available base64 decoder ──────────────────────────────────
        # Try busybox first (Magisk provides it), then python3/python.
        # We run a round-trip sanity test so we know the decode flag works.
        # The md5 tool is detected in the SAME round trip, not a second one.
        # busybox first for the same reason as the decoder (Magisk provides
        # it); bare md5sum as a fallback since some SKUs have it in PATH.
        await ws.send(
            "if echo dGVzdA== | busybox base64 -d >/dev/null 2>&1; then echo DECODER:busybox; "
            "elif python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python3; "
            "elif python  -c 'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.read()))' </dev/null >/dev/null 2>&1; then echo DECODER:python; "
            "else echo DECODER:none; fi; "
            "if echo x | busybox md5sum >/dev/null 2>&1; then echo MD5:busybox; "
            "elif echo x | md5sum >/dev/null 2>&1; then echo MD5:plain; "
            f"else echo MD5:none; fi; "
            # Does the destination DIRECTORY exist? Rides the round trip that
            # was already happening, so it costs nothing.
            #
            # Without it a write into a directory that is not there fails, the
            # `echo TRANSFER_OK` after it never runs, and the transfer waits
            # out its full 120s timeout for a confirmation that can never
            # come — holding the device's shell lock throughout. Measured on
            # EFF 2026-09-04: the debloat payload targets Magisk's
            # /sbin/.core overlay, which a device not running Magisk has no
            # daemon to create, and every attempt cost two minutes and took
            # the next shell operation down with it.
            f"if [ -d {_sh_quote(_posixpath.dirname(dest) or '/')} ]; "
            f"then echo DESTDIR:ok; else echo DESTDIR:missing; fi; "
            f"echo {DETECT_MARKER}\n"
        )

        detect_buf = ""
        detect_dl  = time.monotonic() + 15
        while time.monotonic() < detect_dl:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=2)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                detect_buf += text
                if DETECT_MARKER in detect_buf:
                    break
            except asyncio.TimeoutError:
                continue

        # Checked before the decoder, because it is the more specific answer:
        # a device with a perfectly good base64 and nowhere to put the file is
        # not a device that failed to decode.
        if "DESTDIR:missing" in detect_buf:
            log.warning(f"[api] {device_id}: {dest} — the destination "
                        f"directory does not exist on this device; not sending")
            return _transfer_failed(
                "destination",
                f"{_posixpath.dirname(dest)} does not exist on the device")

        if "DECODER:busybox" in detect_buf:
            decode_cmd = "busybox base64 -d"
        elif "DECODER:python3" in detect_buf:
            decode_cmd = ("python3 -c "
                          "'import sys,base64; "
                          "sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))'")
        elif "DECODER:python" in detect_buf:
            decode_cmd = ("python -c "
                          "'import sys,base64; "
                          "sys.stdout.write(base64.b64decode(sys.stdin.read()))'")
        else:
            # Two very different things reach here. DETECT_MARKER present means
            # the device answered and genuinely has no decoder — a property of
            # that device, which retrying will not change. Absent means the
            # round trip produced nothing in 15s, i.e. the shell plane is not
            # carrying output, which is a link problem and IS worth retrying.
            # Reporting both as "no base64 decoder" sent #121 looking at the
            # wrong half.
            if DETECT_MARKER not in detect_buf:
                log.error(f"[api] Shell produced no output in 15s while probing "
                          f"{device_id} for a decoder — link problem, not a "
                          f"missing tool. Output so far: {detect_buf!r}")
                return _transfer_failed(
                    "shell", "device shell produced no output within 15s")
            log.error(f"[api] No base64 decoder found on device. "
                      f"Detection output: {detect_buf!r}")
            return _transfer_failed("decoder")

        log.info(f"[api] Decoder: {decode_cmd.split()[0]} {decode_cmd.split()[1]}")

        if "MD5:busybox" in detect_buf:
            md5_cmd = "busybox md5sum"
        elif "MD5:plain" in detect_buf:
            md5_cmd = "md5sum"
        else:
            md5_cmd = None
            if require_verify:
                log.error(f"[api] No md5 tool on device — refusing to transfer "
                          f"{dest} unverified. Detection output: {detect_buf!r}")
                return _transfer_failed("md5tool")
            log.warning(f"[api] No md5 tool on device — {dest} will be "
                        f"transferred WITHOUT verification")

        log.info(f"[api] Transferring {len(data):,} bytes to {dest}")

        if md5_cmd is None:
            # Nothing to check a resumed file against, so the old way: one
            # heredoc straight to dest, then chmod and confirm. require_verify
            # has already refused this for the payloads where it is not
            # acceptable.
            await _send_text(ws, (
                f"{decode_cmd} << '{_DELIM}' > {dest} && chmod {mode} {dest} "
                f"&& echo TRANSFER_OK\n"
                + base64.encodebytes(data).decode("ascii") + f"{_DELIM}\n"))
            buf, deadline = "", time.monotonic() + 120
            while "TRANSFER_OK" not in buf and time.monotonic() < deadline:
                try:
                    buf += _text(await asyncio.wait_for(ws.recv(), timeout=5))
                except asyncio.TimeoutError:
                    continue
            if "TRANSFER_OK" not in buf:
                log.error(f"[api] Transfer to {dest} timed out waiting for TRANSFER_OK")
                return _transfer_failed("send")
            log.info(f"[api] Transfer to {dest} confirmed (unverified)")
            return TransferResult(True, "unverified")

        # Bytes land in .part; only a matching md5 promotes them to dest.
        landing = f"{dest}.part"
        push = _BlockPush(live, ws, data, landing, decode_cmd, md5_cmd)
        failed = await push.run()
        ws = push.ws
        if failed:
            log.error(f"[api] Transfer to {dest} failed: {failed[1]}. "
                      f"{dest} left untouched.")
            return _transfer_failed(*failed)

        # ── Verify, then promote ─────────────────────────────────────────────
        # Same shell session, so this is a round trip on an open socket rather
        # than a new session. A mismatch removes the .part and leaves dest as
        # it was — for firmware that means the rollback slot keeps its previous
        # binary instead of being replaced by a broken one.
        # No `cut`: md5sum prints "<hash>  <path>", and the busybox-less branch
        # is exactly where `busybox cut` would not be there either. A case glob
        # needs no external tool at all. chmod first, so the mode travels with
        # the file and dest is never present with the wrong one.
        want = hashlib.md5(data).hexdigest()
        await ws.send(
            f'chmod {mode} {landing} && GOT=$({md5_cmd} {landing} 2>/dev/null); '
            f'case "$GOT" in {want}*) mv {landing} {dest} && echo VERIFY_OK ;; '
            f'*) rm -f {landing}; echo "VERIFY_BAD:$GOT" ;; esac\n'
        )

        verify_buf = ""
        verify_dl  = time.monotonic() + 120
        while time.monotonic() < verify_dl:
            try:
                msg  = await asyncio.wait_for(ws.recv(), timeout=5)
                text = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                verify_buf += text
                if "VERIFY_OK" in verify_buf:
                    log.info(f"[api] Transfer to {dest} confirmed "
                             f"({len(data):,} bytes, md5 {want})")
                    return TransferResult(True)
                if "VERIFY_BAD" in verify_buf:
                    got = verify_buf.split("VERIFY_BAD:")[-1].strip().split()
                    log.error(f"[api] Transfer to {dest} CORRUPT — md5 {want} "
                              f"expected, device reported {got[0] if got else '(none)'}. "
                              f"{dest} left untouched.")
                    return _transfer_failed("corrupt",
                                            f"device reported {got[0] if got else '(none)'}")
            except asyncio.TimeoutError:
                continue

        log.error(f"[api] Transfer to {dest} timed out waiting for md5 verification "
                  f"— {dest} left untouched")
        return _transfer_failed("verify")

    except Exception as e:
        log.error(f"[api] File transfer to {dest} failed: {e}")
        return _transfer_failed("error", str(e))
    finally:
        await release_ws(device_id, live)


# Appended to a probe command so an empty run result can be told apart
# from a command that ran and printed nothing. run swallows every
# exception and returns "", so without this "the file is missing" and "the
# device never answered" are the same string — and they want opposite actions.
OK_MARK = "__EM_SHELL_OK__"


async def send_command(live, cmd: str) -> None:
    """Send a command to the device shell and return immediately (fire-and-forget)."""
    try:
        ws = await get_ws(live)
        await ws.send(cmd + "\n")
        await asyncio.sleep(0.5)
    except Exception as e:
        log.warning(f"[api] Shell exec failed ({cmd!r}): {e}")
    finally:
        await release_ws(live.device_id, live)
