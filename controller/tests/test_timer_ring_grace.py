"""
A timer alarm around a dropped control link (CONTROL_RECONNECT_GRACE_S).

Home Assistant keeps the satellite, and the ring callback in it, for the whole
grace, and that callback holds the Device that dropped. Two things went wrong
once the grace was long enough for most Echoes to come back inside it:

- A timer finishing in the gap started a ring on the dropped Device. It played
  to nothing for up to MAX_RING_S, held the media session's speaker ownership
  the whole time (music commands only recorded, never played), and nothing
  could dismiss it, since the returning Echo's callbacks point at its new
  Device.
- A ring the drop cut off, or one refused as above, left the timer registry
  ringing with no ring. Only a registry going from quiet to ringing starts
  one, so every later timer on that Echo finished in silence.

So the dropped Device refuses, and the Echo that comes back rings.
"""

import asyncio

import pytest

import _oww_stub

_oww_stub.install()
try:
    import em_controller
    import em_esphome
finally:
    _oww_stub.uninstall()

import em_state  # noqa: E402
import em_timers  # noqa: E402

DEVICE = "G090LF0000000001"


def _device():
    return em_controller.Device(DEVICE, "10.0.0.1", [], None)


@pytest.fixture
def rings(monkeypatch):
    """Which Device each ring task was started for. The ring itself is not
    under test, and the real one decodes the chime and drives the speaker."""
    started = []

    async def ring(device):
        started.append(device)
        await asyncio.sleep(3600)

    monkeypatch.setattr(em_controller, "_ring_timer_alarm", ring)
    monkeypatch.delitem(em_state.devices, DEVICE, raising=False)
    return started


@pytest.fixture
def server(monkeypatch):
    """A satellite server through the grace: its port is still up."""
    s = em_esphome.DeviceESPhomeServer(
        DEVICE, "Test", "02:EC:00:00:00:01", "hey_jarvis_v0.1", 16101, None)
    s._server = object()
    monkeypatch.setitem(em_esphome._servers, DEVICE, s)
    return s


def _cancel(device) -> None:
    if device.timer_alarm_task is not None:
        device.timer_alarm_task.cancel()


def test_the_dropped_device_does_not_ring(rings):
    dropped = _device()

    async def run():
        await em_controller.start_timer_alarm(dropped)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert dropped.timer_alarm_task is None
    assert rings == []


def test_the_live_device_still_rings(rings, monkeypatch):
    live = _device()
    monkeypatch.setitem(em_state.devices, DEVICE, live)

    async def run():
        await em_controller.start_timer_alarm(live)
        await asyncio.sleep(0)
        ringing = live.timer_alarm_ringing
        _cancel(live)
        return ringing

    assert asyncio.run(run())
    assert rings == [live]


def test_a_timer_due_in_the_gap_rings_once_the_echo_is_back(rings, server, monkeypatch):
    dropped, back = _device(), _device()
    server._ring_alarm = lambda: em_controller.start_timer_alarm(dropped)

    async def run():
        await server.on_timer_event(em_timers.TIMER_FINISHED, "t1")
        await asyncio.sleep(0)
        assert rings == [], "the dropped Device must not ring"
        monkeypatch.setitem(em_state.devices, DEVICE, back)
        await em_esphome.device_connected(
            DEVICE, ring_alarm=lambda: em_controller.start_timer_alarm(back))
        await asyncio.sleep(0)
        _cancel(back)

    asyncio.run(run())
    assert rings == [back]


def test_a_ring_the_drop_cut_off_resumes_on_reconnect(server):
    """The dropped connection's teardown cancels its ring and the registry
    still says ringing, so a later FINISHED is no transition and would ring
    nothing (test_timers). The connection that comes back has to ring."""
    server._timers.apply(em_timers.TIMER_FINISHED, "t1")
    calls = []

    async def ring():
        calls.append("ring")

    asyncio.run(em_esphome.device_connected(DEVICE, ring_alarm=ring))
    assert calls == ["ring"]


def test_a_quiet_registry_rings_nothing_on_reconnect(server):
    server._timers.apply(em_timers.TIMER_STARTED, "t1")
    calls = []

    async def ring():
        calls.append("ring")

    asyncio.run(em_esphome.device_connected(DEVICE, ring_alarm=ring))
    assert calls == []
    assert server._ring_alarm is ring
