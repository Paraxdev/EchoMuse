"""
Why an Echo went offline: `lastDisconnect` in /api/devices.

The Device object is per connection, replaced on every reconnect and dropped
on close, so the last control-plane close is held by em_device_view beside
the link refusal and merged into each device's record. Its shape is read by
the dashboard: {"at": epoch ms, "why": str, "routine": bool}, and the field is
absent when nothing is known, never a guess.
"""

import ast
import time

import pytest

import _sources
import em_db as db
import em_device_view
import em_wsclose

DEVICE = "G090LF0000000005"


@pytest.fixture
def row(tmp_path, monkeypatch):
    db.init(str(tmp_path / "test.db"))
    db.register_new_device(DEVICE, "192.168.1.136", "v2.18.1-ea.1")
    monkeypatch.setattr(em_device_view, "_last_disconnects", {})
    return db.get_device(DEVICE)


def test_absent_until_a_close_is_seen(row):
    assert "lastDisconnect" not in em_device_view.merge_device(row)


def test_a_keepalive_timeout_is_recorded_as_ours_and_not_routine(row):
    before = int(time.time() * 1000)
    why = em_device_view.note_disconnect(DEVICE, (1011, "keepalive ping timeout", None, None))
    last = em_device_view.merge_device(row)["lastDisconnect"]
    assert set(last) == {"at", "why", "routine"}
    assert isinstance(last["at"], int) and before <= last["at"] <= int(time.time() * 1000)
    assert last["why"] == why == "we closed it: 1011 (internal error) 'keepalive ping timeout'"
    assert last["routine"] is False


def test_a_normal_close_is_routine(row):
    em_device_view.note_disconnect(DEVICE, (1000, "", 1000, ""))
    assert em_device_view.merge_device(row)["lastDisconnect"]["routine"] is True


def test_a_link_that_died_without_a_word_says_so(row):
    """The firmware never sends a close frame; a dropped link has none either."""
    em_device_view.note_disconnect(DEVICE, (None, None, None, None))
    last = em_device_view.merge_device(row)["lastDisconnect"]
    assert "no close frame" in last["why"] and last["routine"] is False


def test_the_latest_close_wins_and_the_record_is_a_copy(row):
    em_device_view.note_disconnect(DEVICE, (1011, "keepalive ping timeout", None, None))
    em_device_view.note_disconnect(DEVICE, (None, None, 1001, "going away"))
    first = em_device_view.merge_device(row)["lastDisconnect"]
    first["why"] = "edited by a reader"
    last = em_device_view.merge_device(row)["lastDisconnect"]
    assert last["why"] == em_wsclose.describe(None, None, 1001, "going away")
    assert last["routine"] is True


def test_the_control_plane_close_records_it_and_logs_the_reason():
    """
    One call in handle_control's teardown, fed from the connection rather
    than the exception: a normal close ends `async for` without raising, and
    it must be recorded too. The device log row carries the same words.
    """
    _, node = _sources.find_def("handle_control")
    calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "em_device_view.note_disconnect"]
    assert len(calls) == 1
    assert ast.unparse(calls[0].args[1]) == "em_wsclose.from_connection(ws)"
    src = _sources.code_only(_sources.def_source("handle_control"))
    assert "f'Disconnected: {why}'" in src
