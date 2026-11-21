"""Passenger feedback about individual train units.

Keyed on the **set number**, not the car label. AT's AM-class EMUs are
three-car sets whose cars carry different prefixes (AMP / AMT / AMA) but share
one number, and the realtime feed only ever reports the AMP car. So a rider
noting "AMT701" and the map showing "AMP 701" are the same train — normalising
to the bare number is what makes a report attach to the unit you actually rode.

Evidence for that, from the seed log: 7 of the 8 AMT/AMP numbers Matt recorded
matched an AMP unit live in the feed at the time, and the eighth (646) sits
exactly in the fleet's numbering sequence between 633 and 659.

Storage is SQLite — one file, no server, survives a container restart via the
bind-mounted output/ directory.
"""
from __future__ import annotations

import re
import json
import os
import time
import hashlib
import secrets
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

LOG = logging.getLogger("akl.feedback")

ROOT = Path(__file__).resolve().parent.parent

# The DB lives on whatever persistent storage the host offers. Locally that is
# output/; on Fly it is the mounted volume at /data. It MUST NOT default to a
# path inside the image: mounting a volume over output/ would shadow the
# 12 MB rail_geo.json baked in at build time and break the map entirely.
DB_PATH = Path(os.environ.get("FEEDBACK_DB")
               or (ROOT / "output" / "feedback.db"))
NZ = timezone(timedelta(hours=13))

# Categories a rider can pick. Kept short and concrete: a free-text-only form
# gets you a pile of prose nobody can aggregate.
CATEGORIES = {
    "smell": "Smell",
    "cleanliness": "Cleanliness",
    "aircon": "Too hot / too cold",
    "crowding": "Overcrowding",
    "damage": "Damage or vandalism",
    "noise": "Noise",
    "ruffians": "Ruffians \u2014 rowdy or threatening behaviour",
    "accessibility": "Accessibility",
    "praise": "Something good",
    "other": "Other",
}
SEVERITIES = {1: "minor", 2: "moderate", 3: "severe"}

MAX_NOTE = 500
# AT case references look like CAS-PB1H5-S9R4F8; keep the shape loose but bounded.
CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,31}$")


def set_number(raw: str | None) -> int | None:
    """"AMT701", "amp 701", "701" -> 701.

    Any AM-class car prefix maps to the same set, because that is what a rider
    is actually identifying. Returns None if there is no plausible number.
    """
    if not raw:
        return None
    # Anchor on a whole run of digits: a bare \d{2,4} would silently truncate
    # "12345" to 1234 and file the report against a real, innocent unit.
    m = re.search(r"(?<!\d)(\d{2,4})(?!\d)", str(raw))
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 9999 else None


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")      # concurrent reads during a write
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init_db() -> None:
    migrated = False
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                set_no      INTEGER NOT NULL,
                raw_unit    TEXT,
                category    TEXT NOT NULL,
                severity    INTEGER NOT NULL,
                note        TEXT,
                case_ref    TEXT,
                occurred_at TEXT,
                carriage    TEXT,
                created_at  TEXT NOT NULL,
                hidden      INTEGER NOT NULL DEFAULT 0
            )""")
        # Confirmations. One row per (report, voter) so a voter can change
        # their mind but not stuff the ballot. `vote` is +1 "still smells" or
        # -1 "smells fine now". voter_hash is a salted hash of a random token
        # the browser generates and keeps in localStorage — see _voter_hash().
        c.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                report_id   INTEGER NOT NULL,
                voter_hash  TEXT NOT NULL,
                vote        INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (report_id, voter_hash)
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_votes_report ON votes(report_id)")
        _add_column(c, "reports", "carriage", "TEXT")
        _add_column(c, "reports", "car_pos", "INTEGER")
        _add_column(c, "reports", "consist", "INTEGER")
        # Set when a 6-car report was routed to the correct coupled half via
        # the inferred pairing; drives attribution() = confirmed vs shared.
        _add_column(c, "reports", "half_resolved", "INTEGER NOT NULL DEFAULT 0")
        # People reports: the fixed behaviour, and the trip the rider was on so
        # the report can expire when that journey ends. Both NULL for every
        # vehicle report, by design.
        _add_column(c, "reports", "behaviour", "TEXT")
        _add_column(c, "reports", "trip_id", "TEXT")
        # Backfill the legacy A/B/C answers into positions. consist stays NULL
        # on purpose: those reports were collected before we asked about train
        # length, so we genuinely do not know whether they were 3- or 6-car.
        # Writing 3 would be inventing data to make the column look tidy.
        n = c.execute("""UPDATE reports
                         SET car_pos = CASE carriage
                               WHEN 'a' THEN 1 WHEN 'b' THEN 2 WHEN 'c' THEN 3 END
                         WHERE car_pos IS NULL
                           AND carriage IN ('a','b','c')""").rowcount
        if n:
            LOG.info("migration: backfilled car_pos for %d legacy report(s)", n)
        c.execute("CREATE INDEX IF NOT EXISTS idx_reports_set ON reports(set_no)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at)")
        migrated = _drop_reporter(c)
    if migrated:
        # VACUUM rewrites the file, discarding the freed pages that still hold
        # the old name bytes. Must run outside a transaction, hence out here.
        with _conn() as c:
            c.execute("VACUUM")
        LOG.info("privacy migration: vacuumed, old reporter bytes reclaimed")


def _add_column(c: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN — SQLite has no IF NOT EXISTS for it."""
    if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        LOG.info("migration: added %s.%s", table, col)


def _drop_reporter(c: sqlite3.Connection) -> bool:
    """Remove the legacy `reporter` column and the names already in it.

    Reports are anonymous. Overwrite the values BEFORE dropping the column:
    DROP COLUMN leaves the old bytes in freed pages, so a blind drop can leave
    names recoverable from the file. Writing NULL first, then dropping, then
    VACUUM (done by the caller) rewrites the database without them.
    Idempotent — a no-op once the column is gone.
    """
    cols = [r[1] for r in c.execute("PRAGMA table_info(reports)")]
    if "reporter" not in cols:
        return False
    n = c.execute("SELECT COUNT(*) FROM reports WHERE reporter IS NOT NULL").fetchone()[0]
    c.execute("UPDATE reports SET reporter=NULL WHERE reporter IS NOT NULL")
    c.execute("ALTER TABLE reports DROP COLUMN reporter")
    LOG.info("privacy migration: dropped reporter column (%d name(s) cleared)", n)
    return True


# An AM-class EMU is a 3-car set: AMP (driving, pantograph), AMT (trailer),
# AMA (the other driving car). Many services run as SIX cars — two 3-car sets
# coupled together.
#
# WHAT AT TELLS US, AND WHAT WE WORK OUT (verified live, 2026-11-19):
# Both realtime endpoints report exactly ONE vehicle label per trip, and the
# only vehicle fields are id/label/empty license_plate. So consist length and
# the coupled partner are NOT published.
#
# They are, however, INFERABLE: both halves of a 6-car keep reporting their own
# GPS, ~50-90 m apart (a 3-car set is ~72 m), at matching speed and bearing,
# persistently as they travel. src/consist.py does that detection and its
# docstring carries the measurements. Front/rear order was stable across 10
# consecutive checks with zero flips.
#
# Consequence for attribution: when the pairing IS known, a rear-half car
# (4-6) is filed against the PARTNER set — the physically correct train, which
# is often NOT the set AT attached the trip to. When the pairing is unknown
# (stationary stock, or a half not currently reporting) we file against the set
# we know and mark the report `shared`, because silently blaming it would
# mis-brand an innocent train — the same class of error the set_number()
# lookarounds exist to prevent.
#
# Positions are counted from the FRONT in the direction of travel, which is
# what a rider can actually perceive. 1-3 on a 3-car, 1-6 on a 6-car.
CONSISTS = {3: "3 cars", 6: "6 cars (two sets)"}
MAX_CAR = 6

# Legacy A/B/C values, kept so old rows and old clients still read correctly.
CARRIAGES = {"a": "Car A (front)", "b": "Car B (middle)", "c": "Car C (rear)",
             "unsure": "Not sure"}
_LEGACY_POS = {"a": 1, "b": 2, "c": 3}


def car_label(car_pos: int | None, consist: int | None,
              half_resolved: bool = False) -> str | None:
    """Human label for a car position, honest about what we don't know."""
    if not car_pos:
        return None
    if consist == 6 and not half_resolved:
        return f"Car {car_pos} of 6"
    if consist == 6 and half_resolved:
        # Renumbered into this set's own frame at insert time, and the rider
        # was in this half of the couple — so say which car of this set, and
        # note it came off a 6-car so the "of 3" isn't confusing.
        return f"Car {car_pos} of 3 (6-car service)"
    if consist == 3:
        return f"Car {car_pos} of 3"
    return f"Car {car_pos}"


def attribution(consist: int | None, resolved_half: bool = False) -> str:
    """Can this report be pinned to the set number we recorded?

    'confirmed'  - 3-car service (one set), or a 6-car where we resolved which
                   half the rider was in via the inferred pairing.
    'shared'     - 6-car with no pairing available: the complaint could belong
                   to either coupled set.
    'unknown'    - the reporter did not say how long the train was.
    """
    if consist == 3:
        return "confirmed"
    if consist == 6:
        return "confirmed" if resolved_half else "shared"
    return "unknown"


def add_report(unit: str | None, category: str, severity: int,
               note: str | None = None, case_ref: str | None = None,
               occurred_at: str | None = None, carriage: str | None = None,
               car_pos: int | None = None, consist: int | None = None,
               partner_set: int | None = None, behaviour: str | None = None,
               trip_id: str | None = None, **_ignored) -> dict:
    """Validate and store one report. Raises ValueError with a usable message."""
    sn = set_number(unit)
    if sn is None:
        raise ValueError("unit must contain a set number, e.g. AMP 701 or 701")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
    # People categories take a FIXED behaviour, never free text.
    #
    # The note is dropped server-side rather than merely hidden in the UI: a
    # client can post whatever it likes to this endpoint, so a front-end-only
    # restriction is no restriction at all. A closed vocabulary cannot describe
    # a person, which is the entire point — see BEHAVIOURS.
    behaviour = (behaviour or "").strip().lower() or None
    if category in PEOPLE_CATEGORIES:
        if not behaviour:
            raise ValueError(
                f"pick what was happening: {', '.join(BEHAVIOURS)}")
        if behaviour not in BEHAVIOURS:
            raise ValueError(f"behaviour must be one of: {', '.join(BEHAVIOURS)}")
        note = None
    elif behaviour:
        raise ValueError("behaviour only applies to a report about passengers")
    try:
        severity = int(severity)
    except (TypeError, ValueError):
        raise ValueError("severity must be 1, 2 or 3")
    if severity not in SEVERITIES:
        raise ValueError("severity must be 1 (minor), 2 (moderate) or 3 (severe)")

    note = (note or "").strip()[:MAX_NOTE] or None
    case_ref = (case_ref or "").strip() or None
    if case_ref and not CASE_RE.match(case_ref):
        raise ValueError("case reference looks wrong (expected something like CAS-PB1H5-S9R4F8)")

    carriage = (carriage or "").strip().lower() or None
    if carriage and carriage not in CARRIAGES:
        raise ValueError(f"carriage must be one of: {', '.join(CARRIAGES)}")

    if consist not in (None, "", 3, 6, "3", "6"):
        raise ValueError("consist must be 3 or 6 (cars)")
    consist = int(consist) if consist not in (None, "") else None
    if car_pos in (None, ""):
        car_pos = _LEGACY_POS.get(carriage or "")   # accept old clients
    else:
        try:
            car_pos = int(car_pos)
        except (TypeError, ValueError):
            raise ValueError("car position must be a number")
        if not 1 <= car_pos <= MAX_CAR:
            raise ValueError(f"car position must be between 1 and {MAX_CAR}")
    # A position past the end of the stated formation is a contradiction, not
    # a preference — reject rather than silently storing something impossible.
    if car_pos and consist and car_pos > consist:
        raise ValueError(f"car {car_pos} does not exist on a {consist}-car train")

    # 6-car re-attribution. The caller resolved, from the inferred pairing,
    # that the rider's carriage sits in the OTHER coupled set — so the report
    # is filed against that set and the car renumbered into its own 1-3 frame.
    # Car 5 of 6 is car 2 of the rear set; storing "car 5" against a 3-car set
    # would be a position that does not exist on it.
    half_resolved = False
    if partner_set and consist == 6 and car_pos:
        sn = int(partner_set)
        car_pos = car_pos - 3 if car_pos > 3 else car_pos
        half_resolved = True

    # The trip the rider was on, so a people report can expire when that
    # journey ends rather than on a timer. Opaque GTFS id; bounded and stored
    # as-is. Only meaningful for people categories, so don't keep it otherwise
    # — it is one more identifier attached to a report for no benefit.
    trip_id = (str(trip_id).strip()[:80] or None) if trip_id else None
    if category not in PEOPLE_CATEGORIES:
        trip_id = None

    occ = (occurred_at or "").strip() or None
    if occ:
        # Accept a date or a full timestamp; store ISO. Reject the unparseable
        # rather than silently filing a report under the wrong day.
        try:
            parsed = datetime.fromisoformat(occ)
        except ValueError:
            raise ValueError("occurred_at must be ISO format, e.g. 2026-11-17 or 2026-11-17T08:30")
        if len(occ) == 10:
            # Date only (what the form's date picker sends). Midnight would sort
            # a report made today *below* everything else filed today, so give
            # today's date the current time and past dates a neutral midday.
            today = datetime.now(NZ).date()
            t = datetime.now(NZ).time() if parsed.date() == today else parsed.time().replace(hour=12)
            parsed = datetime.combine(parsed.date(), t)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=NZ)
        occ = parsed.isoformat(timespec="seconds")
    else:
        occ = datetime.now(NZ).isoformat(timespec="seconds")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO reports
               (set_no, raw_unit, category, severity, note, case_ref,
                occurred_at, carriage, car_pos, consist, half_resolved,
                behaviour, trip_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sn, (unit or "").strip()[:20] or None, category, severity, note,
             case_ref, occ, carriage, car_pos, consist,
             1 if half_resolved else 0, behaviour, trip_id, now))
        rid = cur.lastrowid
    LOG.info("feedback #%d: set %d %s sev%d", rid, sn, category, severity)
    return {"id": rid, "set_no": sn, "category": category, "severity": severity}


# ---------------------------------------------------------------- confirmations
#
# "Does it still smell?" is the whole point: a smell report is a snapshot of one
# carriage on one day, and AT does clean trains. Without a way to clear a report
# the map becomes a permanent accusation and riders stop trusting it.
#
# RESOLUTION RULE: a report is resolved when at least CLEAR_MIN people say it is
# fine AND the margin over those still complaining is at least CLEAR_MARGIN.
# One "smells fine to me" must NOT clear a report — the voter may have been in a
# different carriage, or boarded after a clean. Two independent clears with
# nobody contradicting them is a defensible bar. The rule is symmetrical, so a
# fresh "still smells" vote on a resolved report un-resolves it automatically.
CLEAR_MIN, CLEAR_MARGIN = 2, 2

# A smell nobody has confirmed in this long is reported as stale rather than
# current. It is not resolved — nobody said it was fixed — but presenting a
# months-old complaint as today's condition would be dishonest.
STALE_DAYS = 90

# Categories that describe PEOPLE on a train rather than the train itself.
#
# These behave differently from every other category and the difference is not
# cosmetic. A smell belongs to a unit: the carpet still reeks next Tuesday, so
# a 90-day-old smell report is still evidence about that set. Ruffians get off
# at the next station. They were on a train, not of it, and attributing them to
# a set number for three months would permanently brand a vehicle for something
# that lasted four stops and has nothing to do with the vehicle at all.
#
# So: a people-category report ages out of the fault aggregates in days, not
# months, and never drives the carriage advice. It still exists in the log as
# history, and it can still be voted on while fresh.
PEOPLE_CATEGORIES = {"ruffians"}

# A people report lives for ONE JOURNEY, and we can be exact about that because
# the realtime feed gives each running train a trip_id. The report is stored
# against the trip the rider was on, and it stops counting the moment that
# trip_id leaves the feed — which is the train reaching its terminus. That is
# the honest lifetime: the people got off, so the warning should go with them.
#
# Fallback for a report with no trip (the train was stabled, or AT had not
# attached a trip yet): expire on the clock instead. The longest Auckland rail
# run is about 1h40, so two hours covers any journey without keeping a stale
# warning alive all day.
PEOPLE_FALLBACK_HOURS = 2

# Fixed vocabulary for people reports, which take NO free text.
#
# This is the whole safeguard, not a convenience. A text box next to "report
# the people" on an anonymous public map collects descriptions of individuals
# — clothing, ethnicity, "the group near door 3" — and no filter catches that
# reliably. A closed list cannot describe a person at all: it can only say what
# was happening. It also makes the data aggregatable, which free text never was.
BEHAVIOURS = {
    "loud": "Loud",
    "phone": "Phone or music without headphones",
    "aggressive": "Aggressive or threatening",
    "drunk": "Drunk or intoxicated",
    "smoking": "Smoking or vaping",
    "feet": "Feet on seats / blocking seats",
    "harassment": "Harassing other passengers",
}


def _voter_salt(c: sqlite3.Connection) -> bytes:
    """Persistent per-install salt for voter hashes.

    Unlike the rate-limit salt this MUST survive restarts, or every restart
    would let the same person vote again. The voter token it hashes is a random
    value the browser generated — it is not derived from anything personal, so
    this is pseudonymity for dedupe, not protection of an identity.
    """
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    r = c.execute("SELECT v FROM meta WHERE k='voter_salt'").fetchone()
    if r:
        return bytes.fromhex(r["v"] if isinstance(r, sqlite3.Row) else r[0])
    salt = secrets.token_bytes(16)
    c.execute("INSERT INTO meta (k,v) VALUES ('voter_salt',?)", (salt.hex(),))
    return salt


def _voter_hash(c: sqlite3.Connection, token: str) -> str:
    return hashlib.blake2b(token.encode("utf-8", "replace"),
                           key=_voter_salt(c), digest_size=16).hexdigest()


def cast_vote(report_id: int, token: str, vote: int) -> dict:
    """Record one confirmation. vote: +1 still smells, -1 smells fine now.

    Idempotent per voter — re-voting replaces their previous answer rather than
    adding to it, so the same person cannot stack confirmations.
    """
    if vote not in (1, -1):
        raise ValueError("vote must be +1 (still smells) or -1 (fine now)")
    token = (token or "").strip()
    if len(token) < 16:
        raise ValueError("missing or too-short voter token")
    now = datetime.now(NZ).isoformat(timespec="seconds")
    with _conn() as c:
        if not c.execute("SELECT 1 FROM reports WHERE id=? AND hidden=0",
                         (report_id,)).fetchone():
            raise ValueError("no such report")
        vh = _voter_hash(c, token)
        c.execute("""INSERT INTO votes (report_id, voter_hash, vote, created_at)
                     VALUES (?,?,?,?)
                     ON CONFLICT(report_id, voter_hash)
                     DO UPDATE SET vote=excluded.vote,
                                   created_at=excluded.created_at""",
                  (report_id, vh, vote, now))
        tally = _tally(c, report_id)
    LOG.info("vote on #%d: %+d -> %s", report_id, vote,
             "resolved" if tally["resolved"] else "open")
    return tally


def _tally(c: sqlite3.Connection, report_id: int) -> dict:
    r = c.execute("""SELECT
                       SUM(CASE WHEN vote=1  THEN 1 ELSE 0 END) confirms,
                       SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END) clears
                     FROM votes WHERE report_id=?""", (report_id,)).fetchone()
    confirms, clears = (r["confirms"] or 0), (r["clears"] or 0)
    return {"id": report_id, "confirms": confirms, "clears": clears,
            "resolved": clears >= CLEAR_MIN and (clears - confirms) >= CLEAR_MARGIN}


def _vote_map(c: sqlite3.Connection) -> dict[int, dict]:
    """All tallies in one query — avoids a per-report round trip when listing."""
    out: dict[int, dict] = {}
    for r in c.execute("""SELECT report_id,
                            SUM(CASE WHEN vote=1  THEN 1 ELSE 0 END) confirms,
                            SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END) clears
                          FROM votes GROUP BY report_id"""):
        confirms, clears = (r["confirms"] or 0), (r["clears"] or 0)
        out[r["report_id"]] = {
            "confirms": confirms, "clears": clears,
            "resolved": clears >= CLEAR_MIN and (clears - confirms) >= CLEAR_MARGIN}
    return out


def _is_stale(occurred_at: str | None, created_at: str | None,
              category: str | None = None) -> bool:
    ts = occurred_at or created_at
    if not ts:
        return False
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=NZ)
    # People move on; upholstery doesn't. A people report expires with the
    # journey it was made on — see _people_live() — so the only clock-based
    # rule left here is the fallback for reports with no trip attached.
    if category in PEOPLE_CATEGORIES:
        return (datetime.now(NZ) - d).total_seconds() > PEOPLE_FALLBACK_HOURS * 3600
    return (datetime.now(NZ) - d).days > STALE_DAYS


def _people_expired(trip_id: str | None, occurred_at: str | None,
                    created_at: str | None, live_trips: set | None) -> bool:
    """Has this people report's journey ended?

    Trip still in the realtime feed -> the service is running, keep it.
    Trip gone -> the train reached its terminus, everyone got off, expire it.

    Two deliberate fallbacks:
      * `live_trips is None` means the poller has not answered yet. We do not
        know what is running, so we must not expire everything on the strength
        of an empty answer — fall back to the clock.
      * a report with no trip_id (train was stabled, or AT had not attached a
        trip) has no journey to end, so it also uses the clock.
    """
    if not trip_id or live_trips is None:
        ts = occurred_at or created_at
        if not ts:
            return False
        try:
            d = datetime.fromisoformat(ts)
        except ValueError:
            return False
        if d.tzinfo is None:
            d = d.replace(tzinfo=NZ)
        return (datetime.now(NZ) - d).total_seconds() > PEOPLE_FALLBACK_HOURS * 3600
    return trip_id not in live_trips


def _row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "set_no": r["set_no"], "unit": f"AMP {r['set_no']}",
            "raw_unit": r["raw_unit"], "category": r["category"],
            "category_label": CATEGORIES.get(r["category"], r["category"]),
            "severity": r["severity"], "severity_label": SEVERITIES.get(r["severity"]),
            "note": r["note"], "case_ref": r["case_ref"],
            "occurred_at": r["occurred_at"],
            "carriage": r["carriage"] if "carriage" in r.keys() else None,
            "car_pos": r["car_pos"] if "car_pos" in r.keys() else None,
            "consist": r["consist"] if "consist" in r.keys() else None,
            "car_label": car_label(
                r["car_pos"] if "car_pos" in r.keys() else None,
                r["consist"] if "consist" in r.keys() else None,
                bool(r["half_resolved"]) if "half_resolved" in r.keys() else False),
            "attribution": attribution(
                r["consist"] if "consist" in r.keys() else None,
                bool(r["half_resolved"]) if "half_resolved" in r.keys() else False),
            "carriage_label": CARRIAGES.get(
                (r["carriage"] if "carriage" in r.keys() else None) or ""),
            "stale": _is_stale(r["occurred_at"], r["created_at"], r["category"]),
            "about_people": r["category"] in PEOPLE_CATEGORIES,
            "behaviour": r["behaviour"] if "behaviour" in r.keys() else None,
            "behaviour_label": BEHAVIOURS.get(
                (r["behaviour"] if "behaviour" in r.keys() else None) or ""),
            "trip_id": r["trip_id"] if "trip_id" in r.keys() else None,
            "created_at": r["created_at"]}


def list_reports(set_no: int | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    q = "SELECT * FROM reports WHERE hidden=0"
    args: list = []
    if set_no is not None:
        q += " AND set_no=?"
        args.append(set_no)
    q += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = [_row(r) for r in c.execute(q, args)]
        votes = _vote_map(c)
    # Attach the tally so the reports log can render thumbs and a resolved
    # badge without a second request per report.
    for r in rows:
        r.update(votes.get(r["id"], {"confirms": 0, "clears": 0, "resolved": False}))
    return rows


def summary(live_trips: set | None = None) -> dict:
    """Per-set totals, for colouring the map without fetching every report.

    `live_trips` is the set of trip_ids currently in the realtime feed. A
    people report is kept only while the trip it was made on is still running:
    when the train reaches its terminus the trip leaves the feed, the people
    get off, and the report stops counting. Passing None means "we don't know
    what's running" (the poller hasn't answered yet) and falls back to the
    clock rather than silently expiring everything.

    Returns {set_no: {n, worst, last, top_category, n_neg, worst_neg, n_praise}}
    keyed by string so it survives JSON round-tripping unchanged.

    `praise` is a report but not a FAULT, so the negative figures are tracked
    separately: a unit someone complimented must not be given a warning
    triangle and a severity colour. Anything that renders a warning should read
    `n_neg`/`worst_neg`; `n` remains the honest total of all reports.
    """
    out: dict[str, dict] = {}
    # Built from the constant, not hand-written, so adding a people category
    # can never silently miss the staleness rule below.
    _people_sql = ", ".join(f"'{k}'" for k in sorted(PEOPLE_CATEGORIES))
    with _conn() as c:
        # Resolved reports are excluded from every FAULT aggregate below. A
        # report the riders have cleared must stop driving the warning
        # triangle and the pee-train verdict, otherwise voting changes nothing
        # visible and nobody bothers voting again. The row itself is kept and
        # still shows in the reports log, marked resolved — this is history,
        # not a deletion.
        c.execute(f"""
            CREATE TEMP VIEW IF NOT EXISTS open_reports AS
            SELECT r.* FROM reports r
            LEFT JOIN (SELECT report_id,
                         SUM(CASE WHEN vote=1  THEN 1 ELSE 0 END) cf,
                         SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END) cl
                       FROM votes GROUP BY report_id) v
              ON v.report_id = r.id
            WHERE r.hidden=0
              AND NOT (COALESCE(v.cl,0) >= {CLEAR_MIN}
                       AND COALESCE(v.cl,0) - COALESCE(v.cf,0) >= {CLEAR_MARGIN})
              -- People-category reports expire with the JOURNEY, not on a day
              -- count. A group of ruffians is something happening on one
              -- service right now; when that train reaches its terminus they
              -- get off and the warning must go with them. _people_expired()
              -- below does the trip check against the live feed. The row stays
              -- in the log as history; it just leaves the live fault maths.
            """)
        for r in c.execute("""
                SELECT set_no, COUNT(*) n, MAX(severity) worst,
                       MAX(occurred_at) last
                FROM reports WHERE hidden=0 GROUP BY set_no"""):
            out[str(r["set_no"])] = {"n": r["n"], "worst": r["worst"],
                                     "last": r["last"],
                                     "n_neg": 0, "worst_neg": 0, "n_praise": 0,
                                     "n_people": 0}
        # Negative-only aggregates: faults with the VEHICLE.
        #
        # Excludes praise (not a fault) and also excludes the people
        # categories. A train that had rowdy passengers on Tuesday is not a
        # defective train, and giving it the same warning triangle as a unit
        # that reeks would both mislead riders and quietly turn the map into a
        # running commentary on who is aboard. Those reports are counted in
        # n_people instead.
        for r in c.execute(f"""
                SELECT set_no, COUNT(*) n, MAX(severity) worst
                FROM open_reports
                WHERE category != 'praise' AND category NOT IN ({_people_sql})
                GROUP BY set_no"""):
            s = out.get(str(r["set_no"]))
            if s:
                s["n_neg"] = r["n"]
                s["worst_neg"] = r["worst"] or 0
        for r in c.execute("""
                SELECT set_no, COUNT(*) n FROM reports
                WHERE hidden=0 AND category = 'praise' GROUP BY set_no"""):
            s = out.get(str(r["set_no"]))
            if s:
                s["n_praise"] = r["n"]
        # Most common category overall, and most common FAULT category. The
        # pee-train verdict keys on the latter, so one compliment can't
        # displace a unit's complaint history.
        for r in c.execute("""
                SELECT set_no, category, COUNT(*) n FROM reports
                WHERE hidden=0 GROUP BY set_no, category
                ORDER BY set_no, n DESC"""):
            s = out.get(str(r["set_no"]))
            if s and "top_category" not in s:
                s["top_category"] = r["category"]
                s["top_category_label"] = CATEGORIES.get(r["category"], r["category"])
        for r in c.execute(f"""
                SELECT set_no, category, COUNT(*) n FROM open_reports
                WHERE category != 'praise' AND category NOT IN ({_people_sql})
                GROUP BY set_no, category ORDER BY set_no, n DESC"""):
            s = out.get(str(r["set_no"]))
            if s and "top_fault" not in s:
                s["top_fault"] = r["category"]
                s["top_fault_label"] = CATEGORIES.get(r["category"], r["category"])
        # Worst car per set, from OPEN smell reports only.
        #
        # Grouped BY CONSIST as well as position, and 6-car reports are
        # excluded entirely. Two reasons, both fatal to a combined figure:
        #   1. "Car 2 of 3" and "Car 2 of 6" are different physical places.
        #      Summing them would invent a consensus that nobody expressed.
        #   2. On a 6-car we cannot tell which of the two coupled sets the
        #      rider was in (see the CONSISTS comment), so the position does
        #      not reliably belong to THIS set at all.
        # A 6-car report still counts as a smell report against the set we
        # know — it just cannot drive "avoid car N".
        # Includes 6-car reports that were routed to the correct half
        # (half_resolved=1): those were renumbered into this set's own 1-3
        # frame, so they describe the same physical places as a 3-car report.
        # Unresolved 6-car reports are still excluded — their car number
        # cannot be tied to one of the two coupled sets.
        for r in c.execute("""
                SELECT set_no, car_pos, COUNT(*) n FROM open_reports
                WHERE category='smell' AND car_pos IS NOT NULL
                  AND (consist = 3 OR half_resolved = 1)
                GROUP BY set_no, car_pos ORDER BY set_no, n DESC"""):
            s = out.get(str(r["set_no"]))
            if s and "worst_car" not in s:
                s["worst_car"] = r["car_pos"]
                s["worst_car_label"] = car_label(r["car_pos"], 3)
                s["worst_car_n"] = r["n"]
            # Per-car tallies, not just the worst one. The popup now draws the
            # carriages, so it needs a count against EVERY car to shade them —
            # "car 2 is worst" tells a rider nothing about whether car 1 is
            # also bad. Keyed by car position as a string for JSON.
            if s is not None:
                s.setdefault("cars", {})[str(r["car_pos"])] = r["n"]
        # How many of this set's open smell reports came from a 6-car service,
        # so the UI can say "2 reports, car unknown (6-car)" instead of
        # pretending there is no carriage information at all.
        for r in c.execute("""
                SELECT set_no, COUNT(*) n FROM open_reports
                WHERE category='smell' AND consist = 6 AND half_resolved = 0
                GROUP BY set_no"""):
            s = out.get(str(r["set_no"]))
            if s:
                s["n_sixcar"] = r["n"]
        # Live people reports (ruffians), counted separately from n_neg so the
        # UI can show "reported on this service" without the map implying the
        # TRAIN is the problem. Filtered in Python rather than SQL because the
        # live trip set comes from the realtime poller, not the database.
        for r in c.execute(f"""
                SELECT set_no, id, trip_id, occurred_at, created_at
                FROM open_reports WHERE category IN ({_people_sql})"""):
            if _people_expired(r["trip_id"], r["occurred_at"],
                               r["created_at"], live_trips):
                continue
            s = out.get(str(r["set_no"]))
            if s:
                s["n_people"] = s.get("n_people", 0) + 1
        # How many of this set's reports the riders have cleared. Shown as
        # "N resolved" so a cleaned-up train gets visible credit.
        for r in c.execute("""
                SELECT r.set_no, COUNT(*) n FROM reports r
                WHERE r.hidden=0 AND r.id NOT IN (SELECT id FROM open_reports)
                GROUP BY r.set_no"""):
            s = out.get(str(r["set_no"]))
            if s:
                s["n_resolved"] = r["n"]
    return out


def set_hidden(report_id: int, hidden: bool = True) -> bool:
    """Hide or unhide one report. Returns False if the id doesn't exist.

    Hiding is reversible and is the normal moderation action: a report that
    looks like rubbish today may turn out to be a real complaint phrased badly,
    and an accidental click shouldn't destroy evidence. Every read path already
    filters on hidden=0, so this removes it from the map immediately.
    """
    with _conn() as c:
        cur = c.execute("UPDATE reports SET hidden=? WHERE id=?",
                        (1 if hidden else 0, int(report_id)))
        return cur.rowcount > 0


def delete_report(report_id: int) -> bool:
    """Permanently remove one report. Returns False if the id doesn't exist.

    Prefer set_hidden(); this exists for content that must actually be gone
    (abuse, personal information) rather than merely not shown.
    """
    with _conn() as c:
        cur = c.execute("DELETE FROM reports WHERE id=?", (int(report_id),))
        return cur.rowcount > 0


def list_all(limit: int = 200, include_hidden: bool = True) -> list[dict]:
    """Moderation view: newest first, hidden ones included and marked."""
    limit = max(1, min(int(limit or 200), 500))
    q = "SELECT * FROM reports"
    if not include_hidden:
        q += " WHERE hidden=0"
    q += " ORDER BY datetime(created_at) DESC, id DESC LIMIT ?"
    with _conn() as c:
        return [_row(r) for r in c.execute(q, (limit,))]


def vote_stats() -> dict:
    """Totals for the health endpoint — cheap, no per-report work."""
    with _conn() as c:
        v = c.execute("SELECT COUNT(*) n FROM votes").fetchone()["n"]
        resolved = sum(1 for t in _vote_map(c).values() if t["resolved"])
    return {"votes": v, "resolved": resolved}


def stats() -> dict:
    with _conn() as c:
        tot = c.execute("SELECT COUNT(*) n FROM reports WHERE hidden=0").fetchone()["n"]
        sets = c.execute("SELECT COUNT(DISTINCT set_no) n FROM reports WHERE hidden=0").fetchone()["n"]
        cats = {r["category"]: r["n"] for r in c.execute(
            "SELECT category, COUNT(*) n FROM reports WHERE hidden=0 "
            "GROUP BY category ORDER BY n DESC")}
        repeat = [dict(r) for r in c.execute(
            "SELECT set_no, COUNT(*) n FROM reports WHERE hidden=0 "
            "GROUP BY set_no HAVING n > 1 ORDER BY n DESC, set_no LIMIT 10")]
    return {"reports": tot, "units": sets, "by_category": cats,
            "repeat_offenders": repeat}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print(json.dumps(stats(), indent=1))
