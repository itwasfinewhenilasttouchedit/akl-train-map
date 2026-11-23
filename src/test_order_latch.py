"""Tests for the 6-car front/rear order latch.

The case that matters most is TERMINUS REVERSAL: the same physical pair turns
around at the end of the line and front/rear invert with no uncoupling. A
latch keyed on the pair alone would serve a confidently wrong order for the
whole return working — worse than returning None, because it looks certain.
Keying on the trip id is what prevents it, so that is tested explicitly.
"""
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import order_latch  # noqa: E402
from order_latch import OrderLatch, score, key_for  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


# --------------------------------------------------------------- scoring
def test_score():
    print("score()")
    fast = score(25.0, 0.5, 72.0)      # 90 km/h, bearings agree, textbook gap
    slow = score(2.5, 11.0, 120.0)     # barely moving, noisy, wrong gap
    check("a clean moving sample is confident",
          fast >= order_latch.CONFIDENT, f"got {fast}")
    check("a marginal sample is not confident",
          slow < order_latch.CONFIDENT, f"got {slow}")
    check("better sample scores higher", fast > slow, f"{fast} vs {slow}")
    check("the 415/605 stationary-adjacent case scores low",
          score(0.0, 21.4, 54.6) < 0.5, f"got {score(0.0, 21.4, 54.6)}")
    check("score is bounded 0..1",
          0.0 <= score(0, 180, 999) <= 1.0 and 0.0 <= score(99, 0, 72) <= 1.0)


def test_fix_skew_penalty():
    """The measured failure mode: non-simultaneous GPS fixes flip the sign.

    Observed over 28 pairs / 6 polls: the one pair whose order contradicted
    itself between polls (1074+565) had 10 s of fix skew at 75.8 km/h = 211 m
    of phantom offset against a ~72 m signal. The 27 stable pairs had a median
    skew of 3 s.
    """
    print("fix-skew penalty")
    # The real contradicting sample: fast, but 10 s of skew.
    bad = score(21.1, 1.0, 72.0, fix_skew_s=10.0)
    # Same train, fixes taken together.
    good = score(21.1, 1.0, 72.0, fix_skew_s=0.0)
    check("skew is penalised", bad < good, f"{bad} vs {good}")
    check("the measured flip case is NOT confident",
          bad < order_latch.CONFIDENT, f"got {bad}")
    check("the simultaneous version IS confident",
          good >= order_latch.CONFIDENT, f"got {good}")

    # 3 s skew was the stable median; it should survive.
    typical = score(21.1, 1.0, 72.0, fix_skew_s=3.0)
    check("the stable median skew still latches",
          typical >= order_latch.CONFIDENT, f"got {typical}")

    # Skew costs in proportion to how far the train moved during it, so the
    # same skew is far cheaper slowly than at line speed. (It is NOT free at
    # 2.5 m/s over 30 s - that is 75 m of real movement - which is why this
    # compares like for like at a fixed skew.)
    # Compare the COST of the skew, not the totals: a slow train also scores
    # lower on the speed term, so its total is lower either way.
    slow_cost = (score(3.0, 1.0, 72.0, fix_skew_s=0.0)
                 - score(3.0, 1.0, 72.0, fix_skew_s=6.0))
    fast_cost = (score(21.1, 1.0, 72.0, fix_skew_s=0.0)
                 - score(21.1, 1.0, 72.0, fix_skew_s=6.0))
    check("the same skew costs less at low speed",
          slow_cost < fast_cost, f"slow {slow_cost:.3f} vs fast {fast_cost:.3f}")
    check("small skew on a slow train is near-free",
          score(3.0, 1.0, 72.0, fix_skew_s=2.0)
          >= score(3.0, 1.0, 72.0, fix_skew_s=0.0) - 0.03)

    check("unmeasured skew is a discount, not a veto",
          0.0 < score(21.1, 1.0, 72.0, None) < good)

    # A cleaner later sample must be able to displace a skewed one.
    lat = OrderLatch()
    lat.observe("T-SK", "1074", "565", speed_mps=21.1, brg_diff=1.0,
                gap_m=72.0, fix_skew_s=10.0)
    first = lat.get("T-SK", "1074", "565")
    lat.observe("T-SK", "565", "1074", speed_mps=21.1, brg_diff=1.0,
                gap_m=72.0, fix_skew_s=0.0)
    after = lat.get("T-SK", "1074", "565")
    check("a simultaneous-fix sample overrides a skewed one",
          after["front"] == "565",
          f"front={after['front']} ({first['score']} -> {after['score']})")


# ------------------------------------------------------- terminus reversal
def test_terminus_reversal():
    print("terminus reversal (the one that bites)")
    lat = OrderLatch()
    trip_out = "258-880001-60480-2-W144520-OUTBOUND"
    trip_back = "258-880001-61200-2-W144520-RETURN"

    lat.observe(trip_out, "415", "605", speed_mps=22.0, brg_diff=0.8, gap_m=72.0)
    check("outbound order latched",
          lat.position_for("415", trip_out, "605") == "front")
    check("outbound partner is rear",
          lat.position_for("605", trip_out, "415") == "rear")

    # Same physical pair, new working, now stationary at the terminus.
    check("the return working inherits NOTHING",
          lat.position_for("415", trip_back, "605") is None,
          "pair-keyed latch would wrongly say 'front' here")
    check("the return working inherits nothing for the other half either",
          lat.position_for("605", trip_back, "415") is None)

    # And it can latch the opposite order on the new trip without conflict.
    lat.observe(trip_back, "605", "415", speed_mps=20.0, brg_diff=1.0, gap_m=71.0)
    check("return working latches the INVERTED order",
          lat.position_for("605", trip_back, "415") == "front"
          and lat.position_for("415", trip_back, "605") == "rear")
    check("outbound entry is untouched by the return",
          lat.position_for("415", trip_out, "605") == "front")


# ------------------------------------------------------ best-sample-not-first
def test_best_sample_wins():
    print("latch the best sample, not the first")
    lat = OrderLatch()
    trip = "T-BEST"

    # A weak first sample: crawling out of a depot, bearings disagreeing.
    lat.observe(trip, "100", "200", speed_mps=2.4, brg_diff=10.0, gap_m=110.0)
    first = lat.get(trip, "100", "200")
    check("weak sample is stored when nothing better exists",
          first is not None and first["front"] == "100")
    check("weak sample is flagged low-confidence",
          first["score"] < order_latch.CONFIDENT, f"got {first['score']}")

    # A clean sample at line speed, disagreeing about the order. It should win.
    lat.observe(trip, "200", "100", speed_mps=25.0, brg_diff=0.3, gap_m=72.0)
    now = lat.get(trip, "100", "200")
    check("a much better sample overwrites a weak one",
          now["front"] == "200", f"front={now['front']}")
    check("the stored score improved",
          now["score"] > first["score"])

    # Once confident, a later noisy sample must NOT flip it.
    good = now["score"]
    lat.observe(trip, "100", "200", speed_mps=2.1, brg_diff=11.5, gap_m=150.0)
    after = lat.get(trip, "100", "200")
    check("a confident latch is not flipped by noise",
          after["front"] == "200" and after["score"] == good,
          f"front={after['front']} score={after['score']}")


def test_marginal_improvement_ignored():
    print("jitter does not rewrite the entry")
    lat = OrderLatch()
    lat.observe("T-J", "1", "2", speed_mps=5.0, brg_diff=5.0, gap_m=72.0)
    s1 = lat.get("T-J", "1", "2")["score"]
    stored = lat.observe("T-J", "1", "2", speed_mps=5.1, brg_diff=5.0, gap_m=72.0)
    check("a negligibly better sample is not re-stored", stored is False)
    check("score unchanged", lat.get("T-J", "1", "2")["score"] == s1)


# ---------------------------------------------------------------- splitting
def test_split_forgets():
    print("split invalidates the memory")
    lat = OrderLatch()
    lat.observe("T-A", "300", "400", speed_mps=20.0, brg_diff=1.0, gap_m=72.0)
    lat.observe("T-B", "300", "400", speed_mps=20.0, brg_diff=1.0, gap_m=72.0)
    check("two trips latched for one pair", len(lat.known_pairs()) == 1)
    n = lat.forget_pair("400", "300")        # order-insensitive
    check("both entries forgotten on split", n == 2, f"forgot {n}")
    check("nothing returned after a split",
          lat.position_for("300", "T-A", "400") is None)


def test_recoupled_to_someone_else():
    print("re-coupling to a different set invalidates the order")
    lat = OrderLatch()
    lat.observe("T-C", "500", "600", speed_mps=20.0, brg_diff=1.0, gap_m=72.0)
    check("original pair resolves",
          lat.position_for("500", "T-C", "600") == "front")
    check("same set, same trip, DIFFERENT partner resolves to nothing",
          lat.position_for("500", "T-C", "700") is None,
          "set numbers must be part of the key")


# -------------------------------------------------------------- no trip id
def test_no_trip_no_latch():
    print("no trip id means no latch")
    lat = OrderLatch()
    check("key_for refuses a missing trip", key_for(None, "1", "2") is None)
    check("observe refuses a missing trip",
          lat.observe(None, "1", "2", speed_mps=25.0) is False)
    check("get refuses a missing trip", lat.get(None, "1", "2") is None)


# ------------------------------------------------------------- persistence
def test_persistence():
    print("survives a restart")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "latch.json"
        a = OrderLatch(p)
        a.observe("T-P", "800", "900", speed_mps=25.0, brg_diff=0.5, gap_m=72.0)
        a.save()
        check("file written", p.exists())

        b = OrderLatch(p)        # simulate a redeploy
        check("entry survived the restart",
              b.position_for("800", "T-P", "900") == "front")
        check("loaded count reported", b.loaded == 1, f"loaded={b.loaded}")

        # An expired entry must not come back from disk.
        raw = json.loads(p.read_text())
        k = next(iter(raw["entries"]))
        raw["entries"][k]["seen"] = 0        # epoch: long expired
        p.write_text(json.dumps(raw))
        c = OrderLatch(p)
        check("expired entries are dropped on load", c.loaded == 0)
        check("expired entry does not resolve",
              c.position_for("800", "T-P", "900") is None)


def test_ttl():
    print("time-to-live")
    t0 = 1_000_000.0
    lat = OrderLatch()
    lat.observe("T-T", "11", "22", speed_mps=25.0, brg_diff=0.5, gap_m=72.0,
                now=t0)
    check("resolves within TTL",
          lat.position_for("11", "T-T", "22",
                           now=t0 + order_latch.TTL_S - 60) == "front")

    # A read TOUCHES the entry — a lookup means the pair is still present in
    # the feed — so expiry is measured from the last read, not from the latch.
    # Use a fresh entry to test the expiry itself.
    lat2 = OrderLatch()
    lat2.observe("T-T2", "11", "22", speed_mps=25.0, brg_diff=0.5, gap_m=72.0,
                 now=t0)
    check("gone after TTL with no intervening read",
          lat2.position_for("11", "T-T2", "22",
                            now=t0 + order_latch.TTL_S + 60) is None)
    check("a read keeps an active entry alive",
          lat.position_for("11", "T-T", "22",
                           now=t0 + order_latch.TTL_S + 60) == "front",
          "touch-on-read should have extended this one")


def test_corrupt_file_is_survivable():
    print("a corrupt store does not take the server down")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "latch.json"
        p.write_text("{not json at all")
        lat = OrderLatch(p)
        check("loads to empty instead of raising", lat.stats()["entries"] == 0)
        check("still usable",
              lat.observe("T-X", "1", "2", speed_mps=25.0) is True)


if __name__ == "__main__":
    for fn in (test_score, test_fix_skew_penalty, test_terminus_reversal, test_best_sample_wins,
               test_marginal_improvement_ignored, test_split_forgets,
               test_recoupled_to_someone_else, test_no_trip_no_latch,
               test_persistence, test_ttl, test_corrupt_file_is_survivable):
        fn()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {', '.join(FAILED)}")
        sys.exit(1)
    print("order latch: all tests pass")
