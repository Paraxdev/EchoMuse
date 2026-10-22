"""
An OTA started while another session holds the device's shell.

Opening an Echo's Updates tab sends the asset listing, which holds the
device's shell lock for as long as its session runs. Pressing Update straight
after started the slot detect, which waited only em_shell.LOCK_WAIT_S (20s)
for that lock; on a stalled link the listing held it longer, run() turned the
lock timeout into "", and the update aborted with "Could not determine active
slot" before anything was written. The rollback detect had the same shape.

The real em_shell.run against a scripted device shell, with the lock waits
scaled to fractions of a second.
"""

import asyncio

import pytest

import em_db
import em_events
import em_ota
import em_oww_sync
import em_payloads
import em_shell
import em_state

DEVICE = "G090LF0000000004"
HOLD_S = 0.5


class Shell:
    """The device end of a shell session, answering each command it is sent."""

    def __init__(self):
        self.sent, self.out = [], []

    async def send(self, msg):
        self.sent.append(msg)
        answer = "SLOT:server_a\n" if "readlink" in msg else ""
        self.out.append((answer + em_shell._SENTINEL + "\n").encode())

    async def recv(self):
        if not self.out:
            await asyncio.sleep(3600)
        return self.out.pop(0)

    async def close(self):
        pass


class Live:
    device_id = DEVICE
    android_userspace = False

    def __init__(self):
        self.shell = Shell()

    async def send_control(self, msg):
        if msg["type"] == "shell_open":
            em_state.shell_pending[DEVICE].set_result(self.shell)


@pytest.fixture
def device(monkeypatch):
    live, events = Live(), []
    monkeypatch.setitem(em_state.devices, DEVICE, live)
    monkeypatch.setattr(em_shell, "LOCK_WAIT_S", 0.1)          # 20s, scaled
    monkeypatch.setattr(em_ota, "DETECT_LOCK_WAIT_S", 2.0)     # 150s, scaled
    monkeypatch.setattr(em_db, "get_device", lambda d: {"firmware_ver": "v1.0.0"})
    monkeypatch.setattr(em_db, "set_firmware_previous", lambda d, v: None)

    async def push_log(device_id, level, source, msg):
        events.append(msg)

    async def push(event):
        events.append(event)

    monkeypatch.setattr(em_events, "push_log", push_log)
    monkeypatch.setattr(em_events, "push", push)
    yield live, events
    em_ota.errors.pop(DEVICE, None)
    for d in (em_shell.locks, em_shell.owners, em_shell.sockets, em_state.shell_pending):
        d.pop(DEVICE, None)


async def _held_for(seconds):
    """Another session on the device, such as the asset listing."""
    lock = em_shell.locks.setdefault(DEVICE, asyncio.Lock())
    await lock.acquire()
    em_shell.owners[DEVICE] = asyncio.current_task()
    await asyncio.sleep(seconds)
    em_shell.release_lock(DEVICE)


def _while_held(work):
    async def main():
        holder = asyncio.create_task(_held_for(HOLD_S))
        await asyncio.sleep(0)    # it has the lock before the OTA asks
        await work()
        await holder
    asyncio.run(main())


def test_the_update_waits_for_a_busy_shell(device, monkeypatch):
    live, _ = device

    async def no_sync(live, device_id):
        pass

    async def transfer(live, binary, slot):
        return em_shell.TransferResult(False, "send", "stopped by the test")

    monkeypatch.setattr(em_payloads, "sync_start_script", no_sync)
    monkeypatch.setattr(em_ota, "_stream_binary_to_slot", transfer)
    _while_held(lambda: em_ota.run_update(
        DEVICE, {"version": "v2.0.0", "url": ""}, binary_override=b"\0" * 64))
    assert any("readlink" in cmd for cmd in live.shell.sent)
    assert em_ota.errors[DEVICE].startswith("Binary transfer to server_b failed"), \
        "the detect must wait the lock out and find server_a"


def test_the_rollback_waits_for_a_busy_shell(device, monkeypatch):
    live, events = device

    async def back(device_id, version, previous_version=None, timeout=90):
        return True

    monkeypatch.setattr(em_ota, "_monitor_reconnect", back)
    _while_held(lambda: em_ota.run_rollback(DEVICE, "v1.0.0"))
    assert DEVICE not in em_ota.errors, em_ota.errors.get(DEVICE)
    assert any("ln -sf server_b" in cmd for cmd in live.shell.sent)
    assert {"type": "device_rolled_back", "device_id": DEVICE, "version": "v1.0.0"} in events


def test_the_detect_outwaits_the_asset_listing():
    """At worst the listing holds the lock for its whole budget, plus 15s for
    the device to open the session and up to 10s for the close handshake."""
    assert em_ota.DETECT_LOCK_WAIT_S > em_oww_sync.LISTING_TIMEOUT_S + 15 + 10
