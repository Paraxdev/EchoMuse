"""
em_ota.MAX_HOLD_S against a transfer that finishes late in it.

The cap wrapped the whole update, the slot flip and the reconnect watch
included. Resumable transfers let a slow link finish the binary late in the
cap; the flip went out, the Echo restarted onto the new slot, and the cap then
fired inside the watch: "Update abandoned" kept in errors[] and no
confirmation ever sent, for an Echo already running the new firmware.

The real run_update with the cap scaled to seconds. The shell, the transfer
and the watch are stand-ins; the 1s pause before the flip is real.
"""

import asyncio

import pytest

import em_db
import em_events
import em_ota
import em_payloads
import em_shell
import em_state

DEVICE = "G090LF0000000003"
RELEASE = {"version": "v2.0.0", "url": ""}


class Live:
    device_id = DEVICE
    android_userspace = False


@pytest.fixture
def ota(monkeypatch):
    sent, events = [], []
    monkeypatch.setitem(em_state.devices, DEVICE, Live())
    monkeypatch.setattr(em_db, "get_device", lambda d: {"firmware_ver": "v1.0.0"})
    monkeypatch.setattr(em_db, "set_firmware_previous", lambda d, v: None)

    async def push_log(device_id, level, source, msg):
        events.append(msg)

    async def push(event):
        events.append(event)

    monkeypatch.setattr(em_events, "push_log", push_log)
    monkeypatch.setattr(em_events, "push", push)

    async def no_sync(live, device_id):
        pass

    monkeypatch.setattr(em_payloads, "sync_start_script", no_sync)

    async def run(live, cmd, timeout=30.0, **kwargs):
        sent.append(cmd)
        return "SLOT:server_a" if "readlink" in cmd else ""

    monkeypatch.setattr(em_shell, "run", run)
    yield sent, events
    em_ota.errors.pop(DEVICE, None)


def _update(monkeypatch, cap, transfer_s, watch_s):
    monkeypatch.setattr(em_ota, "MAX_HOLD_S", cap)

    async def transfer(live, binary, slot):
        await asyncio.sleep(transfer_s)
        return em_shell.TransferResult(True)

    async def watch(device_id, version, previous_version=None, timeout=90):
        await asyncio.sleep(watch_s)
        return True

    monkeypatch.setattr(em_ota, "_stream_binary_to_slot", transfer)
    monkeypatch.setattr(em_ota, "_monitor_reconnect", watch)
    asyncio.run(em_ota.run_update(DEVICE, RELEASE, binary_override=b"\0" * 64))
    assert not em_ota.queue.locked(), "the queue must be released either way"
    assert DEVICE not in em_ota.in_progress


def _flipped(sent):
    return any("ln -sf server_b" in cmd for cmd in sent)


def test_a_transfer_that_finishes_near_the_cap_is_confirmed(ota, monkeypatch):
    """Done 0.3s in, flipped after the 1s pause at about 1.3s, and back 0.5s
    later: the watch runs past a 1.5s cap."""
    sent, events = ota
    _update(monkeypatch, cap=1.5, transfer_s=0.3, watch_s=0.5)
    assert _flipped(sent)
    assert DEVICE not in em_ota.errors, em_ota.errors.get(DEVICE)
    assert {"type": "device_updated", "device_id": DEVICE, "version": "v2.0.0"} in events
    assert not any("abandoned" in str(e) for e in events)


def test_a_transfer_that_outlasts_the_cap_is_abandoned_before_the_flip(ota, monkeypatch):
    """The cap still bounds the transfer, whose sends can block for good."""
    sent, events = ota
    _update(monkeypatch, cap=0.3, transfer_s=3600, watch_s=0)
    assert not _flipped(sent), "an abandoned update must never reach the flip"
    assert em_ota.errors[DEVICE].startswith("Update abandoned after 0s")
    assert not any(isinstance(e, dict) and e.get("type") == "device_updated" for e in events)
