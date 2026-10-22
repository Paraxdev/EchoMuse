"""
#315: a control-plane drop used to tear down a device's services
immediately — HA entities deregistered, BLE proxy dropped, media session
killed — and rebuilt all of it when the device returned seconds later.
The data plane has had DATA_RECONNECT_GRACE_S for exactly this reason;
the control plane never got the equivalent.

em_controller is deliberately not importable here (see conftest); these
are shape guards on the shipped source.
"""

import ast
import re
from pathlib import Path

import _sources

CONTROLLER = Path(__file__).resolve().parents[1]
CONTROL_GO = CONTROLLER.parent / "device" / "internal" / "client" / "control.go"


def _constant(name: str):
    """A module-level constant's value, wherever in the controller it lives."""
    found = [ast.literal_eval(node.value)
             for module in _sources.MODULES for node in _sources.tree(module).body
             if isinstance(node, ast.Assign)
             and any(getattr(t, "id", None) == name for t in node.targets)]
    assert len(found) == 1, f"{name} is assigned {len(found)} times"
    return found[0]


def test_the_grace_outlasts_the_device_redial():
    """
    5s was shorter than the firmware's own redial: it waits after any drop,
    then probes the last-known controller before dialling, so 35 of 118
    reconnects on one Echo (2026-10) came back at 5.0-13.2s, just outside
    the window, and each took HA's ESPHome voice and Bluetooth proxy ports
    down and up again. Read from both sides, so neither can move alone.
    Twice the bare redial leaves room for the dial and the register on a
    lossy link.
    """
    go = CONTROL_GO.read_text()
    wait = re.search(r"wait := (\d+) \* time\.Second\s*\n\s*if usingStatic \{", go)
    probe = re.search(r"probeTCP\(server\.Addr, (\d+)\*time\.Second\)", go)
    assert wait and probe, "the device's redial wait or probe moved"
    redial = int(wait.group(1)) + int(probe.group(1))
    assert _constant("CONTROL_RECONNECT_GRACE_S") >= 2 * redial


def test_a_dropped_device_refuses_announcements_during_the_grace():
    """
    Home Assistant keeps the satellite, and with it this callback, through
    the grace. The callback holds the Device that dropped: audio sent to it
    reaches nothing, and the playback path would still report it played once
    its backstop ran out. So the refusal comes first, before the callback
    pauses music or touches the microphone, and HA hears success=False.
    """
    _, node = _sources.find_def("_standalone_play")
    first = node.body[0]
    assert isinstance(first, ast.If), "the liveness check must come first"
    assert ast.unparse(first.test) == "em_state.devices.get(_d.device_id) is not _d"
    assert ast.unparse(first.body[0]) == "return False"


def _finally_src() -> str:
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("log.info(f\"[control] Device disconnected")
    end = src.index("# ─── Data plane handler", start)
    return src[start:end]


def test_teardown_is_deferred_not_immediate():
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index('log.info(f"[control] Device disconnected')
    seg = src[start:start + 1200]
    task_call = seg.index("em_tasks.spawn(")
    sync_path = seg[:task_call]
    for call in ("esphome.device_disconnected",
                 "em_ble_proxy.device_disconnected",
                 "device_gone", "notify_device_disconnected"):
        assert call not in sync_path, \
            f"{call} must move into the grace task, not run on close"
    assert "_release_device_services" in seg[task_call:], \
        "the close path must hand over to the grace task"


def test_the_grace_task_checks_for_a_replacement():
    src = (CONTROLLER / "em_controller.py").read_text()
    start = src.index("async def _release_device_services")
    task = src[start:start + 2500]
    assert "CONTROL_RECONNECT_GRACE_S" in task
    assert "em_state.devices.get(device.device_id)" in task, \
        "the task must check whether a replacement registered"
    for call in ("notify_device_disconnected", "esphome.device_disconnected",
                 "em_ble_proxy.device_disconnected", "device_gone"):
        assert call in task, f"{call} belongs in the deferred release"


def test_the_grace_window_exists_and_is_documented():
    src = (CONTROLLER / "em_controller.py").read_text()
    assert "CONTROL_RECONNECT_GRACE_S" in src
    # The data-plane constant this mirrors:
    assert "DATA_RECONNECT_GRACE_S = 3.0" in src


def test_the_stale_connection_guard_survives():
    """
    The 2026-07-14 guard solves a different ordering problem (close arriving
    AFTER a replacement registered) and must stay untouched.
    """
    src = (CONTROLLER / "em_controller.py").read_text()
    # the message wraps across two f-string lines — match the fragments
    assert "replacement is active" in src and "services up" in src, \
        "the out-of-order stale guard is still needed alongside the grace"
