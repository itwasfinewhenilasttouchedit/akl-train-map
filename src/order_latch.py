"""Remember a 6-car's front/rear order for as long as the pair stays together.

WHY
---
Front/rear order can only be derived while the train MOVES: the geometric
projection in `consist.py` needs a direction of travel, and a stationary unit's
reported bearing is noise. Observed 2026-11-23 at Karanga-a-Hape: a coupled
pair reported bearings 21.4 degrees apart while rigidly joined and stopped.

So `consist.detect()` returns `position: None` for every stationary pair, which
is roughly half of them at any moment, and the order flickers off every time
the train stops at a platform. But the order did not become unknown — the
train was moving forty seconds ago, and two sets coupled by a Scharfenberg
coupler cannot change places without uncoupling first.

This module latches the order from the best moving sample and holds it until
the pair is broken.

WHAT IT IS KEYED ON, AND WHY THAT MATTERS
-----------------------------------------
The key is (trip_id, both set numbers) - NOT the pair alone.

At a terminus the same physical pair reverses and front/rear INVERT, with no
uncoupling: a pair arriving Swanson with 415 leading departs on its next
working with 605 leading. A pair-keyed latch would serve a confidently wrong
order for that entire return journey, which is worse than admitting ignorance,
because a wrong answer looks like a right one. Scoping the latch to the trip
makes one direction of travel structural: a new trip gets no inherited order
and must earn its own.

The set numbers are in the key as well, so that a coupling change invalidates
the latch even mid-trip.

LATCH THE BEST SAMPLE, NOT THE FIRST
------------------------------------
Many trips first appear stationary at a terminus or depot where there is no
order to read. Latching whatever arrives first would freeze GPS noise for the
whole journey, so each sample is scored on the three things that make the
projection trustworthy - speed, agreement between the two halves' bearings,
and a separation consistent with two 3-car sets - and a better sample replaces
a weaker one. Once a sample is clearly good (CONFIDENT) the entry stops
updating: there is nothing left to improve, and the pair cannot reorder without
a split.

WHAT IT WILL NOT DO
-------------------
It never invents an order for a pair that has never been seen moving on this
trip. No moving sample, no latch, `position: None` - unchanged behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

LOG = logging.getLogger("akl.order_latch")

# A sample at or above this score is as good as it needs to be; stop updating.
CONFIDENT = 0.80
# Replace a stored sample only if the new one is meaningfully better, so a
# jittering score does not rewrite the entry every poll.
IMPROVE_BY = 0.05
# Forget an entry this long after it was last confirmed present. Comfortably
# longer than the longest Auckland rail working (Pukekohe-Swanson is ~95 min)
# but short enough that a restart cannot resurrect yesterday's orders.
TTL_S = 3 * 3600
# Hard cap on stored entries; the fleet is 95 units, so this is slack.
MAX_ENTRIES = 400

# Scoring bounds, matching consist.py's own thresholds.
_GOOD_SPEED_MPS = 15.0     # ~54 km/h: projection is unambiguous here
_MIN_SPEED_MPS = 2.0       # consist.MIN_SPEED; below this nothing is produced
_MAX_BRG_DIFF = 12.0       # consist.MAX_BEARING_DIFF
_GAP_LO, _GAP_HI = 53.0, 90.0   # measured coupling separation window
# The offset the projection is trying to read: one 3-car set length. Fix skew
# that implies this much travel has destroyed the signal.
_SIGNAL_M = 72.0


def _clamp(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def score(speed_mps: float | None, brg_diff: float | None,
          gap_m: float | None, fix_skew_s: float | None = None) -> float:
    """How much to trust one front/rear determination, in 0..1.

    Speed dominates because it is what makes the direction of travel real.
    Bearing agreement between two rigidly coupled halves is a direct
    measurement of how much heading noise is present. Separation inside the
    measured window says the pairing itself is sound.

    FIX SKEW is the subtle one, and it is why this term exists. The projection
    compares two POSITIONS to recover a ~72 m offset, which silently assumes
    both fixes were taken at the same instant. They are not: AT's halves
    report independently, and a stale fix means that half has MOVED since.
    At 76 km/h, 10 s of skew is 211 m of phantom offset - three times the real
    signal, and enough to reverse its sign. Measured over 28 pairs across six
    polls: the single pair that reported contradictory orders between polls
    (1074+565) was the one with 10 s of skew at 75.8 km/h, against a median of
    3 s for the 27 stable pairs. The skew penalty scales with SPEED, because
    skew on a stationary train costs nothing.
    """
    spd = speed_mps or 0.0
    s_speed = _clamp((spd - _MIN_SPEED_MPS) / (_GOOD_SPEED_MPS - _MIN_SPEED_MPS))

    if brg_diff is None:
        s_brg = 0.5                      # unknown: neither credit nor penalty
    else:
        s_brg = _clamp(1.0 - (abs(brg_diff) / _MAX_BRG_DIFF))

    if gap_m is None:
        s_gap = 0.5
    elif _GAP_LO <= gap_m <= _GAP_HI:
        s_gap = 1.0
    else:
        # Taper rather than reject: 95 m is suspicious, not impossible.
        off = (_GAP_LO - gap_m) if gap_m < _GAP_LO else (gap_m - _GAP_HI)
        s_gap = _clamp(1.0 - off / 40.0)

    # Skew error in metres, relative to the ~72 m signal the projection is
    # trying to read. 1.0 when the two fixes are simultaneous, falling to 0
    # once the implied position error matches the signal itself.
    if fix_skew_s is None:
        s_skew = 0.75                    # unmeasured: mild discount, not zero
    else:
        err_m = abs(fix_skew_s) * spd    # m/s * s
        # The sign only FLIPS once the error exceeds the real offset, so the
        # tolerance is twice the signal: full credit at simultaneous fixes,
        # zero by the time the phantom offset could reverse the answer. The
        # observed-stable median (3 s) still latches; the observed-flipping
        # case (10 s at 76 km/h) does not.
        s_skew = _clamp(1.0 - err_m / (2.0 * _SIGNAL_M))

    return round(0.35 * s_speed + 0.2 * s_brg + 0.15 * s_gap
                 + 0.3 * s_skew, 4)


def key_for(trip_id: str | None, a: str, b: str) -> str | None:
    """Latch key: one trip, one specific pair of sets.

    Returns None without a trip_id - an orderless identity would merge
    successive workings of the same pair, which is the terminus-reversal bug
    this key exists to prevent.
    """
    if not trip_id:
        return None
    x, y = sorted((str(a), str(b)))
    return f"{trip_id}|{x}+{y}"


class OrderLatch:
    """Trip-scoped store of front/rear decisions, persisted across restarts.

    Persistence matters: `consist_position` previously lived only in the
    in-memory payload, so a redeploy or a machine restart mid-peak would lose
    every latched order and have to wait for each train to move again.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else None
        self._e: dict[str, dict] = {}
        self.loaded = 0
        self.dirty = False
        if self.path:
            self.load()

    # ---------------------------------------------------------------- storage
    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            now = time.time()
            e = {k: v for k, v in (raw.get("entries") or {}).items()
                 if isinstance(v, dict) and now - (v.get("seen") or 0) < TTL_S}
            self._e = e
            self.loaded = len(e)
            LOG.info("order latch: loaded %d live entry/entries (%d expired)",
                     len(e), len(raw.get("entries") or {}) - len(e))
        except Exception as exc:
            LOG.warning("order latch: could not load %s: %s", self.path, exc)

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": 1, "saved": int(time.time()),
                                       "entries": self._e}))
            tmp.replace(self.path)       # atomic: never a half-written latch
            self.dirty = False
        except Exception as exc:
            LOG.warning("order latch: could not save: %s", exc)

    # ------------------------------------------------------------------ logic
    def observe(self, trip_id: str | None, front: str, rear: str,
                speed_mps: float | None = None,
                brg_diff: float | None = None,
                gap_m: float | None = None,
                fix_skew_s: float | None = None,
                now: float | None = None) -> bool:
        """Record a geometry-derived order. True if it was stored.

        Only called with a real front/rear from the projection, so there is no
        path by which a stationary pair enters the latch.
        """
        k = key_for(trip_id, front, rear)
        if not k:
            return False
        now = now if now is not None else time.time()
        sc = score(speed_mps, brg_diff, gap_m, fix_skew_s)
        cur = self._e.get(k)

        if cur:
            cur["seen"] = now
            self.dirty = True
            if cur.get("score", 0.0) >= CONFIDENT:
                # Already as good as it gets; the pair cannot reorder without
                # uncoupling, and an uncouple changes the key.
                if cur.get("front") != str(front):
                    LOG.warning("order latch: %s contradicts latched order "
                                "(latched front %s, now %s, score %.2f vs "
                                "%.2f) - keeping latched",
                                k, cur.get("front"), front,
                                cur.get("score", 0.0), sc)
                return False
            if sc <= cur.get("score", 0.0) + IMPROVE_BY:
                return False

        self._e[k] = {"front": str(front), "rear": str(rear), "score": sc,
                      "seen": now, "at": now, "gap_m": gap_m,
                      "speed_mps": speed_mps, "fix_skew_s": fix_skew_s}
        self.dirty = True
        self._prune(now)
        return True

    def get(self, trip_id: str | None, a: str, b: str,
            now: float | None = None) -> dict | None:
        """Latched order for this trip and this exact pair, or None."""
        k = key_for(trip_id, a, b)
        if not k:
            return None
        e = self._e.get(k)
        if not e:
            return None
        now = now if now is not None else time.time()
        if now - (e.get("seen") or 0) > TTL_S:
            self._e.pop(k, None)
            self.dirty = True
            return None
        e["seen"] = now
        self.dirty = True
        return e

    def position_for(self, set_no: str, trip_id: str | None, partner: str,
                     now: float | None = None) -> str | None:
        """"front"/"rear" for one set of a latched pair, else None."""
        e = self.get(trip_id, set_no, partner, now=now)
        if not e:
            return None
        if e["front"] == str(set_no):
            return "front"
        if e["rear"] == str(set_no):
            return "rear"
        return None

    def forget_pair(self, a: str, b: str) -> int:
        """Drop every latched order for this pair of sets, any trip.

        Used on an observed split: the two sets are no longer one train, so
        nothing about their former order should survive.
        """
        x, y = sorted((str(a), str(b)))
        suffix = f"|{x}+{y}"
        gone = [k for k in self._e if k.endswith(suffix)]
        for k in gone:
            self._e.pop(k, None)
        if gone:
            self.dirty = True
            LOG.info("order latch: forgot %d entry/entries for split %s+%s",
                     len(gone), x, y)
        return len(gone)

    def _prune(self, now: float) -> None:
        if len(self._e) <= MAX_ENTRIES:
            return
        dead = [k for k, v in self._e.items()
                if now - (v.get("seen") or 0) > TTL_S]
        for k in dead:
            self._e.pop(k, None)
        if len(self._e) > MAX_ENTRIES:
            # Oldest-seen first; an active trip is always recently seen.
            for k, _ in sorted(self._e.items(),
                               key=lambda kv: kv[1].get("seen") or 0
                               )[:len(self._e) - MAX_ENTRIES]:
                self._e.pop(k, None)
        self.dirty = True

    def known_pairs(self) -> set[tuple[str, str]]:
        """Every set-pair that currently has a latched order, sorted-tuple form."""
        out: set[tuple[str, str]] = set()
        for v in self._e.values():
            a, b = str(v.get("front")), str(v.get("rear"))
            if a and b:
                out.add(tuple(sorted((a, b))))
        return out

    def stats(self) -> dict:
        n = len(self._e)
        conf = sum(1 for v in self._e.values()
                   if (v.get("score") or 0) >= CONFIDENT)
        return {"entries": n, "confident": conf}
