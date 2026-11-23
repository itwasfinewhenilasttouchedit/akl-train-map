"""Exercise _annotate_consists against the two real 2026-11-23 ground-truth
services plus the stationary fallback case. Run: python3 src/test_trailing.py
"""
import sys, pathlib
root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "src"))

import server


def run(trains, couples):
    server.STATE["data"] = {
        "trains": trains, "in_service": 1, "out_of_service": 1, "count": len(trains),
    }
    server._consist_map = lambda: couples
    server._save_cache = lambda d: None
    server._annotate_consists()
    return {t["unit"]: t for t in server.STATE["data"]["trains"]}


def case_moving():
    """Te Waihorotiu->Swanson, 2026-11-23 08:30. Trip on 1005 (rear);
    1196 physically in FRONT, confirmed by eyeball on the platform."""
    t1196 = {"unit": "AMP 1196", "id": "59196", "in_service": False, "speed_kmh": 55.9}
    t1005 = {"unit": "AMP 1005", "id": "59005", "in_service": True, "speed_kmh": 56.3,
             "trip_id": "258-880001-28080-2-W034610-ff0502e3", "dest": "Swanson",
             "route": "E-W", "line": "Eastern / Western", "colour": "#97C93D"}
    couples = {
        "1196": {"consist": 6, "source": "at", "partner": "1005", "position": "front"},
        "1005": {"consist": 6, "source": "at", "partner": "1196", "position": "rear"},
    }
    r = run([t1196, t1005], couples)
    assert r["AMP 1196"]["trailing_half"] is False, r["AMP 1196"]
    assert r["AMP 1005"]["trailing_half"] is True, r["AMP 1005"]
    assert r["AMP 1196"]["trailing_source"] == "geometry"
    assert r["AMP 1196"]["in_service"] is True
    assert r["AMP 1196"]["dest"] == "Swanson"
    assert r["AMP 1196"]["service_set"] == 1005
    print("moving:   front=AMP 1196 drawn, rear=AMP 1005 suppressed  OK")


def case_stationary():
    """Glen Eden platform 1, both halves speed 0 -> geometry returns None,
    so we keep the old trip-ownership guess rather than invent an order."""
    t917 = {"unit": "AMP 917", "id": "59917", "in_service": False, "speed_kmh": 0}
    t1033 = {"unit": "AMP 1033", "id": "591033", "in_service": True, "speed_kmh": 0,
             "trip_id": "258-880008-27780-2-W039631-5655861b", "dest": "Manukau"}
    couples = {
        "917": {"consist": 6, "source": "at", "partner": "1033", "position": None},
        "1033": {"consist": 6, "source": "at", "partner": "917", "position": None},
    }
    r = run([t917, t1033], couples)
    assert r["AMP 917"]["trailing_half"] is True
    assert r["AMP 917"]["trailing_source"] == "no_trip"
    assert "trailing_half" not in r["AMP 1033"]
    print("stationary: geometry absent, falls back to no_trip guess     OK")


def case_trip_on_front():
    """Control: when the trip IS on the leading half, nothing should flip."""
    tA = {"unit": "AMP 101", "id": "59101", "in_service": True, "trip_id": "x",
          "dest": "Papakura"}
    tB = {"unit": "AMP 202", "id": "59202", "in_service": False}
    couples = {
        "101": {"consist": 6, "source": "at", "partner": "202", "position": "front"},
        "202": {"consist": 6, "source": "at", "partner": "101", "position": "rear"},
    }
    r = run([tA, tB], couples)
    assert r["AMP 202"]["trailing_half"] is True
    assert r["AMP 101"]["trailing_half"] is False
    assert r["AMP 101"]["trip_id"] == "x"
    print("trip-on-front: unchanged, front still drawn                  OK")


def case_single():
    """3-car unit, no partner: must not acquire any trailing flag."""
    t = {"unit": "AMP 303", "id": "59303", "in_service": True, "trip_id": "y"}
    r = run([t], {})
    assert "trailing_half" not in r["AMP 303"]
    print("single unit: untouched                                       OK")


if __name__ == "__main__":
    case_moving()
    case_stationary()
    case_trip_on_front()
    case_single()
    print("\nall 4 cases pass")
