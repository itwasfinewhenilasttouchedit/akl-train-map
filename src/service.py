"""The SERVICE: the train a rider actually boards.

WHY THIS EXISTS
---------------
`in_service` used to carry three meanings that were identical for a 3-car and
came apart the moment 6-cars were modelled:

  1. has a trip          -- AT attached a trip_id to this vehicle
  2. carrying passengers -- true for BOTH halves of a 6-car
  3. should be drawn     -- true for exactly ONE half

Nothing in the data model owned "the train running trip X". `unit`, `trip`,
`place` and `obs` existed; the coupling was a nullable `obs.partner_pk`
self-reference. So a service got reconstructed twice, in two languages:
`server.py:_annotate_consists()` copied twelve fields from one vehicle dict
onto another at request time, and the client recomputed the draw predicate
`in_service !== false && !trailing_half` -- which `server.py` had to keep
byte-identical by hand or the HUD disagreed with the icons.

A `Service` makes the three meanings three different things, so they cannot be
conflated again:

  * `in_service`  -> a property of the SERVICE (is this train working?)
  * `is_lead`     -> a property of a MEMBER (which half carries the icon?)
  * `trip_id`     -> owned by the service, not duplicated onto each half

AND IT MAKES THE OLD CONTRADICTIONS UNREPRESENTABLE
---------------------------------------------------
A record was once observed carrying `trailing_half: True` and
`consist_position: "front"` at the same time (see server.py's docstring) --
the model disagreeing with itself about which end of a train it was looking
at. That was fixed by adding a second pass that partially reversed the first.
Here it cannot be written down: `pos` is the only ordering, `is_lead` is
derived from it, and `_check()` rejects anything else.

THE CARRIAGE BASE IS DERIVED, NEVER PASSED
------------------------------------------
A report's `car_pos` is relative to its own set, so car 2 of the rear half is
absolute carriage 5. That offset used to be threaded through the front end by
hand as `halfVerdict(sn, base)` because nothing owned the frame of reference.
A member knows its own `pos`, so `base` is `(pos - 1) * 3 + 1` -- arithmetic,
not an argument that can be forgotten or passed wrongly.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not decide WHO is coupled to whom (that is `consist.py` inferring from
GPS and `consist_at.py` reading AT's published unit list, merged by
`consist_at.merge()`), and it does not decide front/rear (that is the geometric
projection, latched by `order_latch.py`). It takes those determinations and
gives the result an identity, preserving their provenance so an answer stays
auditable.

ORDERING, AND REFUSING TO GUESS IT
----------------------------------
`pos` is only assigned when the coupling order is actually known. When both
halves are stationary the geometry returns nothing (by design -- in a depot
every unit reads speed 0 and sets park a set-length apart), and then
`ordered` is False, `pos` is None on every member, and `carriage_base()`
returns None. Callers must say "the other half" rather than number carriages
from a guessed front: pointing a rider at the wrong end of the train is worse
than admitting the end is unknown.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CARS_PER_UNIT = 3
MAX_MEMBERS = 2

# Service-level attributes. These describe the TRAIN, not a vehicle, and used
# to be copied onto both halves by _annotate_consists().
SERVICE_FIELDS = ("route", "line", "colour", "dest", "to", "from",
                  "platform", "eta_s", "delay_s", "trip_id",
                  "origin", "terminus")


class ModelError(AssertionError):
    """An invariant was violated. Deliberately loud: a service that breaks
    these is not a service, and silently repairing it is how the old model
    ended up with a front half marked as trailing."""


@dataclass(frozen=True)
class Member:
    """One 3-car unit within a service."""
    set_no: int
    unit: str | None = None
    pos: int | None = None          # 1 = front half, 2 = rear. None = unknown.
    is_lead: bool = False           # carries the drawn icon
    vehicle_id: str | None = None

    @property
    def carriage_base(self) -> int | None:
        """Absolute number of this member's first carriage, or None.

        Derived from `pos`, never supplied. None when the halves could not be
        ordered -- see the module docstring on refusing to guess.
        """
        if self.pos is None:
            return None
        return (self.pos - 1) * CARS_PER_UNIT + 1

    def carriage_range(self) -> tuple[int, int] | None:
        base = self.carriage_base
        return None if base is None else (base, base + CARS_PER_UNIT - 1)

    def absolute_carriage(self, car_pos: int | None) -> int | None:
        """Map a set-relative car position (1-3) to an absolute carriage."""
        base = self.carriage_base
        if base is None or car_pos is None:
            return None
        if not 1 <= car_pos <= CARS_PER_UNIT:
            raise ModelError(f"car_pos {car_pos} outside 1-{CARS_PER_UNIT}")
        return base + car_pos - 1


@dataclass(frozen=True)
class Service:
    """One train: 1 or 2 coupled units working (or not working) a trip."""
    members: tuple[Member, ...]
    in_service: bool = False
    attrs: dict = field(default_factory=dict)
    consist_src: str | None = None
    order_src: str | None = None

    def __post_init__(self) -> None:
        _check(self)

    @property
    def cars(self) -> int:
        return len(self.members) * CARS_PER_UNIT

    @property
    def six(self) -> bool:
        return len(self.members) == MAX_MEMBERS

    @property
    def ordered(self) -> bool:
        """True when front/rear is known for every member."""
        return all(m.pos is not None for m in self.members)

    @property
    def lead(self) -> Member:
        return next(m for m in self.members if m.is_lead)

    @property
    def set_nos(self) -> tuple[int, ...]:
        return tuple(m.set_no for m in self.members)

    def member(self, set_no: int) -> Member | None:
        return next((m for m in self.members if m.set_no == int(set_no)), None)

    def halves_in_order(self) -> tuple[Member, ...]:
        """Members front-first. Falls back to declaration order when unknown,
        so callers can still iterate -- but they must check `ordered` before
        attaching any carriage NUMBER to the result."""
        if not self.ordered:
            return self.members
        # `ordered` guarantees every pos is set; the `or 0` makes that
        # provable to a type checker instead of only to a reader.
        return tuple(sorted(self.members, key=lambda m: m.pos or 0))

    def absolute_carriage(self, set_no: int, car_pos: int | None) -> int | None:
        m = self.member(set_no)
        return None if m is None else m.absolute_carriage(car_pos)

    def get(self, key: str, default=None):
        return self.attrs.get(key, default)


def _check(s: Service) -> None:
    n = len(s.members)
    if not 1 <= n <= MAX_MEMBERS:
        raise ModelError(f"a service has 1 or {MAX_MEMBERS} units, got {n}")

    leads = [m for m in s.members if m.is_lead]
    if len(leads) != 1:
        raise ModelError(f"exactly one member must be lead, got {len(leads)}")

    sets = [m.set_no for m in s.members]
    if len(set(sets)) != n:
        raise ModelError(f"a set cannot be coupled to itself: {sets}")

    positions = [m.pos for m in s.members if m.pos is not None]
    if positions and len(positions) != n:
        raise ModelError("partial ordering: either every half has a position "
                         "or none does, otherwise a caller will number "
                         "carriages from a half-known front")
    if len(set(positions)) != len(positions):
        raise ModelError(f"two halves cannot share a position: {positions}")
    for p in positions:
        if not 1 <= p <= n:
            raise ModelError(f"pos {p} outside 1-{n} for a {n}-unit service")

    # The old model's actual contradiction, now unrepresentable.
    if s.ordered and n == MAX_MEMBERS and s.lead.pos != 1:
        raise ModelError("the lead member must be the FRONT half: the drawn "
                         "icon belongs at the leading cab, not ~72 m behind it")


def build(set_nos, *, in_service=False, positions=None, units=None,
          vehicle_ids=None, attrs=None, consist_src=None,
          order_src=None) -> Service:
    """Assemble a Service, choosing the lead by geometry when known.

    `positions` maps set_no -> 1|2. When it is absent or incomplete the service
    is unordered, the first named set leads (arbitrary but stable), and no
    carriage numbers can be derived from it.
    """
    sets = [int(s) for s in set_nos if s is not None]
    sets = list(dict.fromkeys(sets))
    if not sets:
        raise ModelError("a service needs at least one unit")

    positions = {int(k): v for k, v in (positions or {}).items()
                 if v is not None}
    full = len(positions) == len(sets) and set(positions) == set(sets)

    if full:
        lead_set = min(sets, key=lambda s: positions[s])
    else:
        positions = {}
        lead_set = sets[0]

    units = units or {}
    vehicle_ids = vehicle_ids or {}
    members = tuple(
        Member(set_no=s, unit=units.get(s), pos=positions.get(s),
               is_lead=(s == lead_set), vehicle_id=vehicle_ids.get(s))
        for s in sorted(sets, key=lambda s: (positions.get(s, 9), s))
    )
    return Service(members=members, in_service=bool(in_service),
                   attrs=dict(attrs or {}), consist_src=consist_src,
                   order_src=order_src)


def annotate(d: dict, couples: dict, set_number) -> dict:
    """Project services back onto the flat `trains` payload, IN PLACE.

    This is the phase-2 seam. It replaces `server._annotate_consists()`'s body
    and MUST reproduce it exactly -- the parity gate
    (`src/test_service_parity.py`) diffs the two implementations key by key on
    captured live payloads, so any divergence here is a test failure, not a
    silent production change.

    Returns a dict of counters: {"rescued": n, "regeom": n}.

    WHY IT STILL SPEAKS THE FLAT SHAPE
    ----------------------------------
    The client and the archive both read vehicle-shaped records. Changing the
    wire format and the model in one commit would mean a regression could not
    be attributed to either. So phase 2 moves the LOGIC behind the entity while
    the OUTPUT stays byte-identical; phase 3 adds `services[]` alongside and
    moves the client.

    DELIBERATELY PRESERVED: STABLED PAIRS STAY TWO ICONS
    ----------------------------------------------------
    A coupled pair where NEITHER half holds a trip (measured live: 4 such
    pairs, both halves moving and ordered -- empty stock running to or from a
    depot) is left completely alone, exactly as the legacy code left it. The
    service model would happily collapse it to one 6-car with one icon, which
    is arguably the better drawing, but it would change what a rider sees and
    it would change `out_of_service`. That is a behaviour decision and it gets
    its own phase, not a free ride inside a refactor that promised to change
    nothing. The gate that preserves it is `not partner.get("in_service")`
    below: with no trip on either half there is nothing to rescue a half INTO,
    so both are left as stabled stock and both keep their icon.
    """
    counters = {"rescued": 0, "regeom": 0}
    if not d:
        return counters
    trains = d.get("trains") or []
    if not trains:
        # Still recount: an empty feed must report 0 services, not whatever
        # the previous poll left behind.
        recount(d)
        return counters
    if not couples:
        # No coupling info this poll (3-car-only running, or the consist feed
        # failed). Every vehicle is its own train, so the count must not be
        # skipped -- returning early without counting was how a stale vehicle
        # figure survived.
        recount(d)
        return counters

    by_set: dict[str, dict] = {}
    for t in trains:
        sn = set_number(t.get("unit"))
        if sn:
            by_set[str(sn)] = t

    rescued = 0
    pairs: list[tuple[dict, dict]] = []
    for t in trains:
        sn = set_number(t.get("unit"))
        pair = couples.get(str(sn)) if sn else None
        if not pair:
            continue
        t["consist"] = pair.get("consist")
        t["consist_source"] = pair.get("source")
        t["partner_set"] = pair.get("partner")
        t["consist_position"] = pair.get("position")
        # "inferred" = geometry resolved it from THIS poll's positions;
        # "latched" = remembered from an earlier moving sample on the same
        # trip. Exposed so the ordering's provenance stays auditable.
        if pair.get("order_source"):
            t["order_source"] = pair["order_source"]

        partner = by_set.get(str(pair.get("partner") or ""))
        if not partner or t.get("in_service") or not partner.get("in_service"):
            continue
        # This half has no trip but its partner is working a service: it is the
        # other end of that train, not stabled stock.
        t["trailing_half"] = True
        t["trailing_source"] = "no_trip"
        t["service_set"] = set_number(partner.get("unit"))
        for k in SERVICE_FIELDS:
            if k in partner:
                t[k] = partner[k]
        rescued += 1
        pairs.append((t, partner))

    # Geometry, not trip ownership, decides which half is the tail: AT is known
    # to put the trip on the REAR set at least some of the time (ground truth
    # 2026-11-23), so "has no trip" is not "is behind".
    regeom = 0
    for half, partner in pairs:
        hp, pp = half.get("consist_position"), partner.get("consist_position")
        front = None
        if hp == "front" or pp == "rear":
            front = half
        elif hp == "rear" or pp == "front":
            front = partner
        if front is None:
            continue  # both stationary: geometry has nothing, keep the guess
        rear = partner if front is half else half
        front["trailing_half"] = False
        front["trailing_source"] = "geometry"
        rear["trailing_half"] = True
        rear["trailing_source"] = "geometry"
        if rear is partner:
            # The drawn icon moves to the trip-less half, so it must carry the
            # identity of the service it is physically leading -- including
            # `in_service`, or the client's OOS filter would hide the only icon
            # for a running train.
            front["in_service"] = True
            front.setdefault("service_set", set_number(partner.get("unit")))
            regeom += 1

    if rescued:
        # These are no longer "out of service" in any sense a rider would
        # recognise, even though they carry no trip of their own.
        d["trailing_halves"] = rescued
        d["out_of_service"] = max(0, int(d.get("out_of_service") or 0) - rescued)
        d["in_service"] = int(d.get("in_service") or 0) + rescued

    # Must run after the annotation above, because it keys off `trailing_half`.
    recount(d)
    counters["rescued"], counters["regeom"] = rescued, regeom
    return counters


def recount(d: dict) -> None:
    """Count TRAINS, not GPS vehicles.

    A 6-car service is two coupled 3-car sets and AT publishes a separate GPS
    vehicle for each half, so counting vehicles counted every 6-car twice:
    measured on production, the HUD claimed 71 in service while 39 markers
    existed, overstating by 32. 39 was also exactly the number of distinct
    trip_ids -- independent confirmation that the service figure is the honest
    answer to "how many trains are running".

    `in_service` is the number a rider reads, so it means SERVICES.
    `vehicles_in_service` keeps the raw vehicle figure for the health endpoint
    and the archive, which legitimately care about hardware.

    Called from every return path, including the early ones: an empty feed must
    report 0 rather than last poll's figure.
    """
    trains = d.get("trains") or []
    veh_in = sum(1 for t in trains if t.get("in_service") is not False)
    # The same predicate the client uses to decide whether to draw a marker,
    # so the HUD number and the icon count cannot disagree. Phase 3 removes
    # this duplication by shipping `services[]` and letting the client count
    # what it draws.
    svc = sum(1 for t in trains
              if t.get("in_service") is not False and not t.get("trailing_half"))
    d["vehicles_in_service"] = veh_in
    d["in_service"] = svc
    d["out_of_service"] = sum(1 for t in trains if t.get("in_service") is False)
    d["coupled_halves"] = veh_in - svc


def build_services(trains, couples=None) -> list[Service]:
    """Group a live `trains` payload into services.

    A vehicle whose set is named in `couples` with a partner that is ALSO
    present joins one service; everything else is its own 3-car service. A
    service is `in_service` when ANY member is -- which is the whole point:
    AT attaches the trip to one half only, so asking each vehicle in isolation
    reports a working train as stabled stock.
    """
    couples = couples or {}
    by_set: dict[int, dict] = {}
    for t in trains or []:
        sn = t.get("set_no")
        if sn is None:
            continue
        by_set[int(sn)] = t

    services: list[Service] = []
    seen: set[int] = set()
    for sn, t in by_set.items():
        if sn in seen:
            continue
        pair = couples.get(str(sn)) or couples.get(sn) or {}
        partner = pair.get("partner")
        partner = int(partner) if partner not in (None, "") else None

        group = [sn]
        if partner is not None and partner in by_set and partner != sn:
            group.append(partner)
        seen.update(group)

        positions, units, vids = {}, {}, {}
        for g in group:
            p = (couples.get(str(g)) or couples.get(g) or {})
            pos = p.get("position")
            if pos in ("front", 1):
                positions[g] = 1
            elif pos in ("rear", 2):
                positions[g] = 2
            units[g] = by_set[g].get("unit")
            vids[g] = by_set[g].get("id")

        # The trip-bearing half is the authority on what the train is doing.
        src = next((by_set[g] for g in group
                    if by_set[g].get("in_service")), by_set[group[0]])
        attrs = {k: src.get(k) for k in SERVICE_FIELDS if src.get(k) is not None}

        pair0 = couples.get(str(group[0])) or couples.get(group[0]) or {}
        services.append(build(
            group,
            in_service=any(by_set[g].get("in_service") for g in group),
            positions=positions, units=units, vehicle_ids=vids, attrs=attrs,
            consist_src=pair0.get("source"),
            order_src=pair0.get("order_source"),
        ))
    return services
