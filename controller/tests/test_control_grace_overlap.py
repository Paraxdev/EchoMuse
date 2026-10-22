"""
Two control drops inside one grace (CONTROL_RECONNECT_GRACE_S).

Every close spawns its own grace task. An Echo that comes back and drops again
before the first grace has run out (a crash-looping binary after an OTA, a
credential bounce, an RST on a flapping link) left the first task waking to an
empty registry partway through the second drop's grace, and it released there:
HA's satellite port down, the timer registry cleared, the BLE proxy and the
media session gone. An Echo back inside the second grace then found the port
down and no ringing timer to resume, so the alarm was lost.

Driven through the real grace task and the real esphome connect and
disconnect, with the grace scaled to fractions of a second. Each step waits
for an absolute time, so a slow loop cannot reorder it against the grace
timers.
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

import em_api  # noqa: E402
import em_ble_proxy  # noqa: E402
import em_player  # noqa: E402
import em_state  # noqa: E402
import em_tasks  # noqa: E402
import em_timers  # noqa: E402

DEVICE = "G090LF0000000002"
GRACE = 0.3


@pytest.fixture
def world(monkeypatch):
    """The services a release takes down, each recording that it went, and
    the satellite server with its port up and a timer ringing."""
    released, rings = [], []
    monkeypatch.setattr(em_controller, "CONTROL_RECONNECT_GRACE_S", GRACE)
    monkeypatch.delitem(em_state.devices, DEVICE, raising=False)

    async def api_down(device_id):
        released.append("api")

    async def ble_down(device_id):
        released.append("ble")

    monkeypatch.setattr(em_api, "notify_device_disconnected", api_down)
    monkeypatch.setattr(em_ble_proxy, "device_disconnected", ble_down)
    monkeypatch.setattr(em_player, "device_gone", lambda d: released.append("player"))

    async def ring(device):
        rings.append(device)
        await asyncio.sleep(3600)

    monkeypatch.setattr(em_controller, "_ring_timer_alarm", ring)

    server = em_esphome.DeviceESPhomeServer(
        DEVICE, "Test", "02:EC:00:00:00:02", "hey_jarvis_v0.1", 16102, None)
    server._server = object()

    async def stop():
        server._server = None
        released.append("port")

    async def start(host):
        server._server = object()

    server.stop, server.start = stop, start
    server._timers.apply(em_timers.TIMER_FINISHED, "t1")
    monkeypatch.setitem(em_esphome._servers, DEVICE, server)
    yield server, released, rings
    em_state.devices.pop(DEVICE, None)


def _device():
    return em_controller.Device(DEVICE, "10.0.0.1", [], None)


async def _register(device):
    em_state.devices[DEVICE] = device
    await em_esphome.device_connected(
        DEVICE, ring_alarm=lambda: em_controller.start_timer_alarm(device))
    await asyncio.sleep(0)   # let a ring it started begin


def _drop(device):
    """What handle_control's finally does when the live connection closes."""
    if device.timer_alarm_task is not None:
        device.timer_alarm_task.cancel()
        device.timer_alarm_task = None
    em_state.devices.pop(DEVICE, None)
    em_tasks.spawn(em_controller._release_device_services(device))


def _clock():
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    async def at(t):
        await asyncio.sleep(max(0.0, t0 + t - loop.time()))

    return at


def test_the_first_grace_does_not_release_inside_the_second(world):
    server, released, rings = world
    a, b, c = _device(), _device(), _device()

    async def run():
        at = _clock()
        await _register(a)
        _drop(a)                    # D1: A's grace runs to 0.3
        await at(0.10)
        await _register(b)
        await at(0.15)
        _drop(b)                    # D2: B's grace runs to 0.45
        await at(0.35)              # past A's grace, inside B's
        inside = (list(released), server._server is not None, server._timers.ringing)
        await _register(c)          # D3: back inside B's grace
        await at(0.55)              # past B's grace, with C live
        if c.timer_alarm_task is not None:
            c.timer_alarm_task.cancel()
        return inside

    released_inside, port_up, ringing = asyncio.run(run())
    assert released_inside == [], "the first drop's task released inside the second grace"
    assert port_up, "HA's satellite port went down inside the second grace"
    assert ringing, "the ringing timer was cleared inside the second grace"
    assert rings == [a, b, c], "the Echo back inside the grace must ring again"
    assert released == [], "C is live, so nothing is released"


def test_the_newest_drop_releases_once_when_nobody_comes_back(world):
    server, released, _ = world
    a, b = _device(), _device()

    async def run():
        at = _clock()
        await _register(a)
        _drop(a)
        await at(0.10)
        await _register(b)
        await at(0.15)
        _drop(b)
        await at(0.35)
        inside = list(released)
        await at(0.60)
        return inside

    inside = asyncio.run(run())
    assert inside == [], "nothing is released before the newest drop's grace runs out"
    assert sorted(released) == ["api", "ble", "player", "port"], \
        "the newest drop's task releases everything, once"
    assert server._server is None
    assert not server._timers.ringing
