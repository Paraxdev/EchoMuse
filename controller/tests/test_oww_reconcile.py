"""
The wake word asset reconcile, end to end against a scripted device.

Two field failures pinned here (2026-10-21/22, one Echo on a lossy 2.4GHz
link):

- A listing that failed or stopped early read as "nothing installed". The
  reconcile only stood down when reading RAISED, and em_shell.run never did,
  so each failed listing became "10 asset(s) missing" and an 11.7MB push over
  the link that had just failed. The sync's own md5 compare minutes later
  said "already up to date".
- The push then started seconds after a reconnect, over a link graded poor,
  and died with it.
"""

import asyncio
import json
import logging
import time as _time
from pathlib import Path

import pytest

import em_db
import em_events
import em_oww_assets
import em_oww_sync
import em_routes_oww
import em_shell
import em_state
import em_tasks

RUNTIME = em_oww_assets.Asset(em_oww_assets.RUNTIME_NAME, Path("/nonexistent"),
                              "1" * 32, 12_300_000, "runtime")
VAD = em_oww_assets.Asset(em_oww_assets.VAD_NAME, Path("/nonexistent"),
                          "2" * 32, 1_800_000, "vad")
JARVIS = em_oww_assets.Asset("hey_jarvis_v0.1.onnx", Path("/nonexistent"),
                             "3" * 32, 1_270_000, "classifier")
DESIRED = [RUNTIME, VAD, JARVIS]


def listing(*assets, free="FREE /dev/block/dm-0 1010 648 346 65% /data"):
    rows = [f"{a.md5} 1700000000 {em_oww_assets.device_path(a.name)}" for a in assets]
    return "\n".join(rows + ([free] if free is not None else []))


class Strip:
    """live.tcp_minutes, answering each summary() from a list of verdicts."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)

    def summary(self, now):
        v = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        return None if v is None else {"verdict": v, "lossPct": 0.0, "minutes": []}


class Live:
    def __init__(self, verdicts=(None,)):
        self.device_id = "dev"
        self.oww_shadow_capable = True
        self.tcp_minutes = Strip(verdicts)
        self.control = []

    async def send_control(self, msg):
        self.control.append(msg)


class Clock:
    """em_oww_sync's `time`: monotonic moves only when the code sleeps."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1.7e9 + self.now


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    rec = {"syncs": [], "events": [], "tasks": [], "listings": [], "shell": []}

    async def fake_sleep(s):
        clock.now += s

    async def fake_sync(live, device_id, progress=None):
        rec["syncs"].append(device_id)
        return {"ok": True, "pushed": [], "pruned": []}

    async def fake_log(device_id, level, source, message):
        rec["events"].append((level, message))

    def fake_spawn(coro, *, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        rec["tasks"].append(task)
        return task

    monkeypatch.setattr(em_oww_sync, "time", clock)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(em_oww_sync, "sync", fake_sync)
    monkeypatch.setattr(em_events, "push_log", fake_log)
    monkeypatch.setattr(em_tasks, "spawn", fake_spawn)
    monkeypatch.setattr(em_oww_sync, "wanted_models", lambda d: ["hey_jarvis_v0.1"])
    monkeypatch.setattr(em_oww_assets, "desired_assets", lambda models: (list(DESIRED), []))
    monkeypatch.setattr(em_db, "get_effective_device_config",
                        lambda d: {"owwOnDevice": rec.get("mode", "off")})
    em_oww_sync.held.clear()
    em_state.devices.clear()
    rec["clock"] = clock
    yield rec
    em_oww_sync.held.clear()
    em_state.devices.clear()


def _shell_says(monkeypatch, env, *answers):
    """em_shell.run_strict answering from a script; an exception entry raises."""
    async def fake(live, cmd, timeout=30.0, silence_s=None):
        env["shell"].append(cmd)
        a = answers[min(len(env["shell"]) - 1, len(answers) - 1)]
        if isinstance(a, BaseException):
            raise a
        return a
    monkeypatch.setattr(em_shell, "run_strict", fake)


def _reconcile(live, env, connected_at=None, run_held=True):
    async def main():
        em_state.devices[live.device_id] = live
        await em_oww_sync.reconcile("dev", live, connected_at)
        if run_held:
            while env["tasks"] and not all(t.done() for t in env["tasks"]):
                await asyncio.gather(*env["tasks"])
    asyncio.run(main())


# ── Failure to look is not evidence of absence ───────────────────────────────

def test_a_listing_that_failed_pushes_nothing(monkeypatch, env):
    _shell_says(monkeypatch, env, em_shell.ShellError("1011 keepalive ping timeout"))
    _reconcile(Live(), env, connected_at=0.0)
    assert env["syncs"] == [], "a failed listing must never become a push"
    assert env["events"] == []


def test_a_listing_cut_short_is_not_an_empty_device(monkeypatch, env):
    """The old reading of run's partial output after a quiet spell: two of
    three files and no FREE line. The FREE line is printed last, so without
    it the inventory is incomplete and nothing may be planned from it."""
    _shell_says(monkeypatch, env, listing(VAD, JARVIS, free=None))
    _reconcile(Live(), env, connected_at=0.0)
    assert env["syncs"] == []


def test_a_complete_listing_is_read(monkeypatch, env):
    _shell_says(monkeypatch, env, listing(RUNTIME, VAD, JARVIS))
    state = asyncio.run(em_oww_sync.device_state(Live()))
    assert set(state["installed"]) == {RUNTIME.name, VAD.name, JARVIS.name}
    assert state["free_mb"] == 346


def test_an_unreadable_df_still_ends_the_listing(monkeypatch, env):
    """`busybox df` printing nothing leaves a bare FREE, which the strip of
    the output leaves without its space. That is a finished listing with no
    free-space reading, which carries on (see parse_free_mb)."""
    _shell_says(monkeypatch, env, listing(RUNTIME, free="FREE"))
    state = asyncio.run(em_oww_sync.device_state(Live()))
    assert state["free_mb"] is None and RUNTIME.name in state["installed"]


def test_a_device_with_everything_costs_one_listing(monkeypatch, env):
    _shell_says(monkeypatch, env, listing(RUNTIME, VAD, JARVIS))
    _reconcile(Live(), env, connected_at=0.0)
    assert env["syncs"] == [] and env["tasks"] == [] and len(env["shell"]) == 1


def test_the_sync_refuses_to_plan_from_an_unread_device(monkeypatch, env):
    _shell_says(monkeypatch, env, em_shell.ShellError("no completion marker"))
    sent = []

    async def no_stream(*a, **kw):
        sent.append(a)
        return em_shell.TransferResult(True)
    monkeypatch.setattr(em_shell, "stream_file", no_stream)
    res = asyncio.run(em_oww_sync._sync_locked(Live(), "dev"))
    assert res["ok"] is False and "could not read" in res["error"]
    assert sent == []


def test_the_assets_panel_says_unknown_rather_than_not_installed(monkeypatch, env):
    _shell_says(monkeypatch, env, em_shell.ShellError("1011 keepalive ping timeout"))
    em_state.devices["dev"] = Live()

    class Request:
        match_info = {"id": "dev"}

    resp = asyncio.run(em_routes_oww._get_oww_assets.__wrapped__(Request()))
    body = json.loads(resp.text)
    assert body["status"] == "unknown" and body["connected"] is True
    assert "keepalive" in body["error"]
    assert body["installed"] is None


# ── When a multi-MB push may start ───────────────────────────────────────────

def test_push_wait():
    assert em_oww_sync.push_wait(5, "good")
    assert em_oww_sync.push_wait(59.9, None)
    assert em_oww_sync.push_wait(600, "poor")
    for v in ("good", "fair", None):
        assert em_oww_sync.push_wait(60, v) is None, (
            "an unknown grade is not a poor one: it must not hold anything back")


def test_a_small_repair_goes_straight_away_on_a_fresh_connection(monkeypatch, env):
    """A lone classifier is under the gate, so a fresh connection does not
    delay it."""
    _shell_says(monkeypatch, env, listing(RUNTIME, VAD))
    clock = env["clock"]
    _reconcile(Live(verdicts=["poor"]), env, connected_at=clock.now - 1)
    assert env["syncs"] == ["dev"] and env["tasks"] == []


def test_a_multi_mb_push_waits_out_the_first_minute(monkeypatch, env, caplog):
    _shell_says(monkeypatch, env, listing(VAD, JARVIS))
    clock = env["clock"]
    start = clock.now
    caplog.set_level(logging.INFO, logger="echomuse.api")

    async def main():
        live = Live()
        em_state.devices["dev"] = live
        await em_oww_sync.reconcile("dev", live, start - 2)
        assert env["syncs"] == [], "nothing may start 2s into a connection"
        assert em_oww_sync.held.get("dev") is live
        await asyncio.gather(*env["tasks"])

    asyncio.run(main())
    assert env["syncs"] == ["dev"]
    assert clock.now - (start - 2) >= em_oww_sync.SETTLE_S
    assert sum("holding the push" in r.message for r in caplog.records) == 1
    assert "dev" not in em_oww_sync.held


def test_a_poor_link_holds_the_push_until_it_recovers_and_says_so_once(monkeypatch, env, caplog):
    _shell_says(monkeypatch, env, listing(VAD, JARVIS))
    clock = env["clock"]
    caplog.set_level(logging.INFO, logger="echomuse.api")
    live = Live(verdicts=["poor", "poor", "poor", "poor", "fair"])

    _reconcile(live, env, connected_at=clock.now - 600)

    assert env["syncs"] == ["dev"]
    assert sum("holding the push" in r.message for r in caplog.records) == 1, (
        "a held push is logged when it is held, not on every check")
    assert clock.now >= 1000.0 + 3 * em_oww_sync.RECHECK_S


def test_a_held_push_ends_with_its_connection(monkeypatch, env):
    _shell_says(monkeypatch, env, listing(VAD, JARVIS))
    clock = env["clock"]
    live = Live(verdicts=["poor"])

    async def main():
        em_state.devices["dev"] = live
        await em_oww_sync.reconcile("dev", live, clock.now - 600)
        em_state.devices["dev"] = Live()      # the device redialled
        await asyncio.gather(*env["tasks"])

    asyncio.run(main())
    assert env["syncs"] == []
    assert "dev" not in em_oww_sync.held


def test_a_deaf_device_is_warned_once_across_a_hold(monkeypatch, env):
    """Scoring on the Echo and missing its classifier: the warning goes out
    when the push is held, and the re-run that installs does not repeat it."""
    env["mode"] = "on"
    _shell_says(monkeypatch, env, listing(VAD))
    clock = env["clock"]
    _reconcile(Live(), env, connected_at=clock.now)

    warns = [m for lvl, m in env["events"] if lvl == "warn"]
    assert len(warns) == 1 and JARVIS.name in warns[0], env["events"]
    assert env["syncs"] == ["dev"]
    assert any("listening for its wake word again" in m for _, m in env["events"])
