"""
em_wakepatience.py: how many frames in a row a wake score must hold before
it counts as a wake (`owwPatience`).

ONE FRAME WAS THE WHOLE RULE
----------------------------
The controller's wake listener started a turn on the first trusted 80ms chunk
scoring at or over `owwThreshold`, and the Echo's own scorer does the same at
its normal bar (`crossed` in device/internal/wakeword/shadow/shadow.go). One
Echo running a custom two-syllable model at 0.175 on the controller logged
133 wake turns between 2026-10-11 and 10-21, and 69 of them heard no speech
at all: something cleared the bar for one frame and the room was then quiet.

A spoken wake word holds a high score across several hops as the phrase moves
through the classifier's 1.28s window, so asking for a second frame costs a
real wake 80ms. A knock or a bump on the table is over in one. `em_barge`
has asked for two frames at the barge bar since 2026-08-20 for the same
reason; this is that rule at the normal bar, made a setting, because a short
wake word may hold fewer frames than a long one (the concern on upstream
#294) and only a replay of real wakes can say how many.

Whether the 69 would have held a second frame is not in the turn rows, which
record only the frame that fired. The wake log line therefore says how many
frames a wake held, and a run that ends short of the patience is logged as
not a wake, so the effect can be read from the log rather than assumed.

WHERE IT APPLIES
----------------
- The controller's own decision at the normal bar, wherever it decides
  ("off" and "shadow"). 1 is the old single-frame behaviour.
- Not the crossings it records in "on" mode, where the Echo decides: those
  are the score comparison (`ctrl_wake_score`), and holding one side of a
  comparison to a rule the other side does not apply would record a wake the
  Echo fired on one frame as one this controller never heard.
- Not the lowered bar used while music plays or an alarm rings with barge-in
  on, and not `_barge_watcher` during a reply: both keep their own rules.
- An Echo that detects its own wake word applies it only on firmware that
  announces CAPABILITY. The key is pushed to every Echo regardless, since
  older firmware ignores fields it does not know, and the dashboard shows the
  control disabled with the reason where the Echo decides and cannot apply it.

A RUN IS FRAMES THE MODEL SAW IN A ROW
--------------------------------------
Two frames either side of a gap in the audio are not two frames in a row, so
the listener calls `reset()` wherever it drops or splices audio: a backlog
skip, a stream boundary, frames dropped while the wake word is off or while
the Echo is speaking. A model reset needs no call of its own: the warm-up
gate marks the next FEATURE_WINDOW chunks untrusted (em_oww_warmup), and an
untrusted chunk ends a run without being reported as a short one.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

log = logging.getLogger("echomuse.db")

KEY = "owwPatience"
MIN = 1
MAX = 4
DEFAULT = 2

# What firmware announces when it applies owwPatience to its own detection.
# Coordinated by name with device/internal/client/control.go; until a firmware
# announces it, an Echo that decides its own wakes fires on one frame.
CAPABILITY = "oww_patience"


def _is_int(value) -> bool:
    # bool is an int subclass, and True is not a frame count.
    return isinstance(value, int) and not isinstance(value, bool)


def clamp(value) -> int:
    """
    `owwPatience` as the listener uses it.

    A whole number is held to MIN..MAX, so a stored 0 means the old rule
    rather than a wake word that cannot fire. Anything else, including an
    absent key, is DEFAULT: em_config_types drops a stored value of the wrong
    type before it gets here, which leaves absence as the only other case.
    """
    if not _is_int(value):
        return DEFAULT
    return max(MIN, min(MAX, value))


def clamp_config(config: dict, where: str) -> dict:
    """
    Hold a stored `owwPatience` to MIN..MAX, for em_db's two write choke
    points beside its threshold clamp, and in the same shape: a NEW dict when
    something changes, and a wrong type passed through untouched, since that
    is em_config_types' refusal to make.
    """
    value = config.get(KEY)
    if not _is_int(value) or MIN <= value <= MAX:
        return config
    fixed = clamp(value)
    log.warning(f"[db] {where}: {KEY} {value} outside [{MIN}, {MAX}]; storing {fixed}")
    return {**config, KEY: fixed}


def need(patience, bar: float, wake_threshold: float) -> int:
    """
    Frames in a row the listener needs at `bar`.

    `bar` is below `wake_threshold` only while the listener has lowered it to
    the barge-in bar for music or a ringing alarm, which keeps its old rule.
    """
    return clamp(patience) if bar >= wake_threshold else 1


class Short(NamedTuple):
    """A run that ended below the bar before it was long enough to wake."""
    frames: int
    peak: float
    need: int

    def describe(self) -> str:
        s = "" if self.frames == 1 else "s"
        return (f"{self.frames} frame{s} over the bar (peak {self.peak:.3f}), "
                f"patience {self.need}: not a wake")


# At most one short-run line per this many seconds, as for near-misses: speech
# hovering at a low bar can end a run every other frame, six lines a second.
REPORT_EVERY_S = 2.0


class Run:
    """
    Consecutive trusted frames at or over the bar, for one listener.

    `feed()` once per chunk scored, `reset()` at every gap in the audio.
    `frames` is the length of the current run, so the frame that wakes can
    say how many it took. `short` is set by the feed that ended a run short
    of its patience, and cleared by the next one; `report()` is its log line.
    """

    def __init__(self) -> None:
        self.frames = 0
        self.peak = 0.0
        self.short: Short | None = None
        self._need = 1
        self._reported_at = float("-inf")
        self._held = 0

    def report(self, now: float) -> str | None:
        """
        The log line for a run the last feed ended short, or None. Rate
        limited to one per REPORT_EVERY_S, and a line counts the short runs
        held back since the one before it, so none goes unaccounted.
        """
        if self.short is None:
            return None
        if now - self._reported_at < REPORT_EVERY_S:
            self._held += 1
            return None
        line = self.short.describe()
        if self._held:
            line += f" ({self._held} more since the last line)"
        self._reported_at, self._held = now, 0
        return line

    def reset(self) -> None:
        """The next frame does not follow the last one."""
        self.frames = 0
        self.peak = 0.0
        self.short = None

    def feed(self, trusted: bool, above: bool, score: float, need: int) -> bool:
        """
        Record one scored chunk. True when it is at least the `need`th in a
        row over the bar.

        Every frame of a run from the `need`th on returns True, as every
        frame over the bar did before, so patience 1 is exactly the old
        condition `trusted and score >= bar`.
        """
        self.short = None
        if trusted and above:
            self.frames += 1
            self.peak = max(self.peak, float(score))
            self._need = need
            return self.frames >= need
        if trusted and 0 < self.frames < self._need:
            self.short = Short(self.frames, self.peak, self._need)
        self.frames = 0
        self.peak = 0.0
        return False
