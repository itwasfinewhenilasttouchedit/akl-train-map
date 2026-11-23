"""End-to-end test of the order latch through server._apply_order_latch.

Unit tests cover OrderLatch itself; this exercises the real integration path
with payload-shaped data, including the two cases that would be invisible to a
unit test: that a stationary pair gets its order back from memory, and that a
terminus reversal does NOT inherit the previous working's order.
"""
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + detail if not cond else ""))
    if not cond:
        FAILED.append(name)


def fresh_server(tmp: Path):
    """Import server with the latch pointed at a temp file, not the volume."""
    import os
    os.environ["ORDER_LATCH"] = str(tmp / "latch.json")
    os.environ.setdefault("TRAINS_CACHE", str(tmp / "cache.json"))
    for m in ("server",):
        sys.modules.pop(m, None)
    import server
    server._LATCH = None
    return server


TRIP_OUT = "258-880001-60480-2-W144520-OUT"
TRIP_BACK = "258-880001-61200-2-W144520-BACK"


def moving(front, rear, trip, gap=72, spd=22.0, brg=0.6):
    """A merged-table pair as consist_at.merge() would produce it, moving."""
    return {
        str(front): {"partner": str(rear), "consist": 6, "source": "at",
                     "position": "front", "cars": [1, 2, 3], "gap_m": gap,
                     "order_source": "inferred",
                     "speed_mps": spd, "brg_diff": brg},
        str(rear): {"partner": str(front), "consist": 6, "source": "at",
                    "position": "rear", "cars": [4, 5, 6], "gap_m": gap,
                    "order_source": "inferred",
                    "speed_mps": spd, "brg_diff": brg},
    }


def stopped(a, b):
    """The same pair stationary: geometry has declined to order it."""
    return {
        str(a): {"partner": str(b), "consist": 6, "source": "at",
                 "position": None, "cars": None},
        str(b): {"partner": str(a), "consist": 6, "source": "at",
                 "position": None, "cars": None},
    }


def test_latch_then_recover():
    print("a stopped 6-car keeps the order it had while moving")
    with tempfile.TemporaryDirectory() as d:
        srv = fresh_server(Path(d))
        trips = {"415": TRIP_OUT, "605": TRIP_OUT}

        m1 = moving("415", "605", TRIP_OUT)
        srv._apply_order_latch(m1, trips)
        check("live determination is left as inferred",
              m1["415"]["order_source"] == "inferred")

        # Next poll: train stopped at Karanga-a-Hape, geometry returns nothing.
        m2 = stopped("415", "605")
        srv._apply_order_latch(m2, trips)
        check("front half recovered from memory",
              m2["415"]["position"] == "front", str(m2["415"]))
        check("rear half recovered from memory",
              m2["605"]["position"] == "rear", str(m2["605"]))
        check("recovered order is labelled 'latched', not 'inferred'",
              m2["415"]["order_source"] == "latched")
        check("cars follow the recovered position",
              m2["415"]["cars"] == [1, 2, 3] and m2["605"]["cars"] == [4, 5, 6])


def test_terminus_reversal_e2e():
    print("terminus reversal does not inherit the old order")
    with tempfile.TemporaryDirectory() as d:
        srv = fresh_server(Path(d))

        srv._apply_order_latch(moving("415", "605", TRIP_OUT),
                               {"415": TRIP_OUT, "605": TRIP_OUT})

        # Same pair, stationary at Swanson, now on the RETURN working.
        back = stopped("415", "605")
        srv._apply_order_latch(back, {"415": TRIP_BACK, "605": TRIP_BACK})
        check("no order is invented for the new trip",
              back["415"]["position"] is None and back["605"]["position"] is None,
              f"415={back['415']['position']} 605={back['605']['position']}")

        # Once it moves again the inverted order is learned and served.
        srv._apply_order_latch(moving("605", "415", TRIP_BACK),
                               {"415": TRIP_BACK, "605": TRIP_BACK})
        back2 = stopped("415", "605")
        srv._apply_order_latch(back2, {"415": TRIP_BACK, "605": TRIP_BACK})
        check("the inverted order is now served for the return working",
              back2["605"]["position"] == "front"
              and back2["415"]["position"] == "rear",
              f"605={back2['605']['position']} 415={back2['415']['position']}")


def test_split_clears():
    print("a split clears the remembered order")
    with tempfile.TemporaryDirectory() as d:
        srv = fresh_server(Path(d))
        trips = {"415": TRIP_OUT, "605": TRIP_OUT}
        srv._apply_order_latch(moving("415", "605", TRIP_OUT), trips)

        # The pair no longer appears together: 415 is now a lone 3-car.
        split = {"415": {"partner": None, "consist": 3, "source": "at",
                         "position": None}}
        srv._apply_order_latch(split, trips)

        # Re-coupled later on the same trip id; must not resurrect the order.
        again = stopped("415", "605")
        srv._apply_order_latch(again, trips)
        check("order is not resurrected after a split",
              again["415"]["position"] is None, str(again["415"]))


def test_restart_keeps_order():
    print("a redeploy does not blank every stationary 6-car")
    with tempfile.TemporaryDirectory() as d:
        trips = {"415": TRIP_OUT, "605": TRIP_OUT}
        srv = fresh_server(Path(d))
        srv._apply_order_latch(moving("415", "605", TRIP_OUT), trips)

        srv._LATCH = None          # simulate process restart, same volume
        after = stopped("415", "605")
        srv._apply_order_latch(after, trips)
        check("order survived the restart",
              after["415"]["position"] == "front", str(after["415"]))
        check("still labelled latched",
              after["415"]["order_source"] == "latched")


def test_never_seen_moving_stays_unknown():
    print("a pair never seen moving stays honestly unknown")
    with tempfile.TemporaryDirectory() as d:
        srv = fresh_server(Path(d))
        m = stopped("111", "222")
        srv._apply_order_latch(m, {"111": "T-NEW", "222": "T-NEW"})
        check("no order invented",
              m["111"]["position"] is None and m["222"]["position"] is None)
        check("no order_source invented",
              m["111"].get("order_source") is None)


def test_three_car_untouched():
    print("single units are untouched")
    with tempfile.TemporaryDirectory() as d:
        srv = fresh_server(Path(d))
        m = {"999": {"partner": None, "consist": 3, "source": "at",
                     "position": None}}
        before = dict(m["999"])
        srv._apply_order_latch(m, {"999": "T-SOLO"})
        check("3-car row unchanged", m["999"] == before)


def test_feed_gap_does_not_look_like_mass_splitting():
    print("a feed outage does not wipe the whole store")
    with tempfile.TemporaryDirectory() as d:
        srv = fresh_server(Path(d))
        pairs = (("415", "605", "T1"), ("100", "200", "T2"),
                 ("300", "400", "T3"), ("500", "600", "T4"))
        trips = {}
        all_moving = {}
        for a, b, trip in pairs:
            trips[a] = trips[b] = trip
            all_moving.update(moving(a, b, trip))
        srv._apply_order_latch(all_moving, trips)   # one poll, all four present
        check("four pairs latched", len(srv._latch().known_pairs()) == 4,
              f"{sorted(srv._latch().known_pairs())}")

        # AT's consist calls fail: the merged table arrives empty.
        srv._apply_order_latch({}, trips)
        check("an empty table forgets nothing",
              len(srv._latch().known_pairs()) == 4,
              f"{len(srv._latch().known_pairs())} left")

        # Orders still recoverable once the feed returns.
        back = dict(all_moving)
        for k in ("415", "605"):
            back[k] = dict(back[k], position=None, cars=None)
            back[k].pop("order_source", None)
        srv._apply_order_latch(back, trips)
        check("order survived the gap", back["415"]["position"] == "front",
              str(back["415"]))

        # One genuine split, the rest still present, IS believed.
        present = {}
        for a, b, trip in pairs[:3]:
            present.update(moving(a, b, trip))
        srv._apply_order_latch(present, trips)
        check("a single genuine split is still detected",
              len(srv._latch().known_pairs()) == 3,
              f"{sorted(srv._latch().known_pairs())}")


if __name__ == "__main__":
    for fn in (test_latch_then_recover, test_terminus_reversal_e2e,
               test_split_clears, test_restart_keeps_order,
               test_never_seen_moving_stays_unknown, test_three_car_untouched,
               test_feed_gap_does_not_look_like_mass_splitting):
        fn()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {', '.join(FAILED)}")
        sys.exit(1)
    print("order latch integration: all tests pass")
