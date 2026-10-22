"""
owwPatience: frames in a row a wake score must hold (em_wakepatience).

The rule is pure and tested here; the listener that uses it cannot be
imported by this suite, so where it calls the rule and where it resets the
run are held as source guards over `_stream_listen`.
"""

import ast
import random

import pytest

import _sources
import em_wakepatience as P

BAR = 0.175


def _fire(scores, patience, *, trusted=None, resets=()):
    """Indices of the frames that wake, scored at BAR, the way the listener
    feeds them; `resets` are indices reset() runs before."""
    run = P.Run()
    out = []
    for i, s in enumerate(scores):
        if i in resets:
            run.reset()
        ok = True if trusted is None else trusted[i]
        if run.feed(ok, s >= BAR, s, P.need(patience, BAR, BAR)):
            out.append(i)
    return out


# ── The rule ────────────────────────────────────────────────────────────────

def test_one_frame_over_the_bar_does_not_wake_at_patience_2():
    assert _fire([0.0, 0.40, 0.0, 0.0], 2) == []


def test_two_frames_in_a_row_wake_at_patience_2():
    assert _fire([0.0, 0.40, 0.30, 0.0], 2) == [2]


def test_two_frames_apart_are_not_two_in_a_row():
    assert _fire([0.40, 0.0, 0.40, 0.0, 0.40], 2) == []


def test_patience_1_is_the_old_single_frame_rule():
    assert _fire([0.0, 0.40, 0.0, 0.0], 1) == [1]
    # And exactly the old condition, `trusted and score >= bar`, frame by
    # frame, over anything.
    rnd = random.Random(1)
    run = P.Run()
    for _ in range(5000):
        s, ok = rnd.random() * 0.4, rnd.random() > 0.1
        assert run.feed(ok, s >= BAR, s, 1) == (ok and s >= BAR)


def test_every_frame_from_the_patience_on_counts():
    # As every frame over the bar did before: "on" mode records each one for
    # the comparison, and the listener acts on the first.
    assert _fire([0.3, 0.3, 0.3, 0.3, 0.0], 2) == [1, 2, 3]
    assert _fire([0.3, 0.3, 0.3, 0.3, 0.0], 4) == [3]


def test_the_score_at_exactly_the_bar_counts():
    # Both scorers compare with >=; patience must not move the edge.
    assert _fire([BAR, BAR], 2) == [1]


def test_reset_on_a_buffer_clear_breaks_the_run():
    assert _fire([0.3, 0.3], 2, resets={1}) == []
    assert _fire([0.3, 0.3, 0.3], 2, resets={1}) == [2]


def test_an_untrusted_frame_breaks_the_run():
    # A model reset re-arms the warm-up gate, so the frames after it are
    # untrusted: that alone ends the run, with no reset() at those sites.
    assert _fire([0.3, 0.3, 0.3], 2, trusted=[True, False, True]) == []


def test_the_lowered_barge_bar_keeps_one_frame():
    # While music plays or an alarm rings with barge-in on, the listener
    # lowers the bar to bargeInThreshold, and that keeps its old rule.
    assert P.need(3, 0.15, 0.5) == 1
    assert P.need(3, 0.5, 0.5) == 3
    assert P.need(None, 0.5, 0.5) == P.DEFAULT


def test_the_run_counts_the_frames_that_woke():
    run = P.Run()
    for s in (0.3, 0.3, 0.3):
        hit = run.feed(True, True, s, 3)
    assert hit and run.frames == 3


# ── A run that ends short is reported once ──────────────────────────────────

def test_a_short_run_is_reported_with_its_peak():
    run = P.Run()
    run.feed(True, True, 0.31, 2)
    assert run.short is None
    run.feed(True, False, 0.02, 2)
    assert run.short == P.Short(1, 0.31, 2)
    assert run.short.describe() == \
        "1 frame over the bar (peak 0.310), patience 2: not a wake"
    run.feed(True, False, 0.02, 2)
    assert run.short is None


def test_a_run_that_woke_is_not_short():
    run = P.Run()
    for s in (0.3, 0.3, 0.0):
        run.feed(True, s >= BAR, s, 2)
    assert run.short is None


def test_a_run_cut_by_a_gap_or_a_model_reset_is_not_reported():
    run = P.Run()
    run.feed(True, True, 0.3, 2)
    run.reset()
    run.feed(True, False, 0.0, 2)
    assert run.short is None
    run.feed(True, True, 0.3, 2)
    run.feed(False, False, 0.0, 2)
    assert run.short is None


def test_describe_counts_in_plural():
    assert P.Short(3, 0.5, 4).describe().startswith("3 frames over the bar")


# ── The stored value ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (1, 1), (2, 2), (3, 3), (4, 4),
    (0, 1), (-3, 1), (5, 4), (99, 4),
    (None, P.DEFAULT), ("2", P.DEFAULT), (2.0, P.DEFAULT),
    (True, P.DEFAULT), (False, P.DEFAULT),
])
def test_clamp_from_the_edges(value, expected):
    assert P.clamp(value) == expected


def test_the_range_and_default():
    assert (P.MIN, P.MAX, P.DEFAULT) == (1, 4, 2)
    assert P.CAPABILITY == "oww_patience"


def test_clamp_config_holds_a_stored_value_to_the_range():
    cfg = {P.KEY: 9, "owwThreshold": 0.5}
    out = P.clamp_config(cfg, "fleet")
    assert out == {P.KEY: 4, "owwThreshold": 0.5}
    assert cfg[P.KEY] == 9, "the caller's dict must not be edited under them"
    assert P.clamp_config({P.KEY: 0}, "D1") == {P.KEY: 1}


def test_clamp_config_leaves_good_and_wrongly_typed_values_alone():
    for cfg in ({P.KEY: 3}, {}, {P.KEY: "9"}, {P.KEY: True}, {P.KEY: 2.5}):
        assert P.clamp_config(cfg, "fleet") is cfg


def test_both_db_writes_clamp(tmp_path):
    import em_db
    em_db.init(str(tmp_path / "em.db"))
    em_db.set_global_device_config({P.KEY: 7})
    assert em_db.get_global_device_config()[P.KEY] == 4
    em_db.register_new_device("D1", "10.0.0.1", "v1")
    em_db.set_device_config("D1", {P.KEY: 0})
    assert em_db.get_device_config("D1")[P.KEY] == 1


def test_the_default_reaches_every_fleet(tmp_path):
    # Underlaid, not pinned: a fleet saved before the key existed gets 2.
    import em_db
    em_db.init(str(tmp_path / "em.db"))
    em_db.set_global_device_config({"owwThreshold": 0.3})
    assert em_db.get_global_device_config()[P.KEY] == P.DEFAULT == 2


# ── The listener uses it ────────────────────────────────────────────────────

def _listener() -> ast.AST:
    _, node = _sources.find_def("_stream_listen")
    return node


def _calls(stmts, target: str) -> bool:
    return any(isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
               and ast.unparse(s.value.func) == target for s in stmts)


def test_the_wake_decision_comes_from_the_run():
    src = _sources.code_only(_sources.def_source("_stream_listen"))
    assert "ctrl_hit = run.feed(trusted, score >= eff_threshold, score, need)" in src
    assert "em_wakepatience.need(device.oww_patience, eff_threshold, device.oww_threshold)" in src


def test_every_buffer_clear_also_ends_the_run():
    """
    A buffer clear is a gap in the audio: frames either side of it are not
    in a row. Where the model is reset too, the warm-up gate's untrusted
    frames end the run; everywhere else the run is reset beside the clear.
    """
    blocks = [getattr(n, f) for n in ast.walk(_listener())
              for f in ("body", "orelse") if isinstance(getattr(n, f, None), list)]
    clears = [b for b in blocks if _calls(b, "buf.clear")]
    assert len(clears) >= 5, "the buffer clears have moved; update this guard"
    for b in clears:
        assert _calls(b, "run.reset") or _calls(b, "warmup.reset"), \
            ast.unparse(b[0])


def test_frames_dropped_unscored_end_the_run():
    """Frames skipped while a turn has the stream or the Echo is speaking
    never reach the model, so the run must not span them."""
    for test in ("device.oww_paused.is_set()", "device.speaking and (not ringing)"):
        found = [n for n in ast.walk(_listener())
                 if isinstance(n, ast.If) and ast.unparse(n.test) == test]
        assert found, f"no `if {test}:` in _stream_listen"
        assert any(_calls(n.body, "run.reset") for n in found), test


def test_the_mode_where_the_echo_decides_does_not_log_short_runs():
    src = _sources.def_source("_stream_listen")
    assert "if run.short and device.oww_on_device != em_shadow.MODE_ON:" in src
