#!/usr/bin/env python3
"""Shared fixture helpers for the service gates. NOT a test itself.

`test_service_parity.py` (phase 2) and `test_services_payload.py` (phase 3)
both need to turn a captured production payload back into the raw input the
server saw. That logic was written once for phase 2 and phase 3 needed it
verbatim -- and the phase-3 author's first instinct was to `import` the phase-2
gate, which EXECUTED the whole suite as a side effect of import (it is a flat
script with no `__main__` guard) and produced a confusing double run.

Duplicating it instead would have been worse: this refactor exists because one
definition lived in two places. So it lives here, once.

The payloads in `tests/fixtures/parity_caps.json` carry NO `couples` key, so
`couples_from()` rebuilds the coupling map from the annotations already in the
payload, and `strip()` removes those annotations so the annotator starts from
the same raw input twice. Skipping either step makes `annotate()` take its
`if not couples` early return, and a gate that does that asserts nothing about
the main path while reporting all-green -- which happened, and is why the
"fixture actually has couplings to test" assertion now exists.
"""
from __future__ import annotations

import copy

import feedback
import service as S

__all__ = ["couples_from", "strip", "ADDED_KEYS"]

# Everything the annotators attach to a train dict. Stripping these returns a
# captured payload to its pre-annotation state.
ADDED_KEYS = ("consist", "consist_source", "partner_set", "consist_position",
              "order_source", "trailing_half", "trailing_source",
              "service_set")


def couples_from(payload):
    """Rebuild the couples map `_consist_map()` would have produced, from the
    annotations already present in a captured payload."""
    out = {}
    for t in payload.get("trains") or []:
        sn = feedback.set_number(t.get("unit"))
        if not sn or not t.get("partner_set"):
            continue
        out[str(sn)] = {
            "partner": t.get("partner_set"),
            "consist": t.get("consist"),
            "position": t.get("consist_position"),
            "source": t.get("consist_source"),
            "order_source": t.get("order_source"),
        }
    return out


def strip(payload):
    """Remove everything the annotators add, so both versions run from the
    same input.

    THE SUBTLE BIT -- `service_set` is the only honest marker.
    A first version of this keyed off `trailing_half`, which was wrong: the
    geometry pass marks the TRIP-HOLDING half as trailing whenever the trip
    sits on the front set, so stripping by `trailing_half` deleted the trip
    from a half that genuinely owned one. Both halves then looked stabled, no
    rescue could fire, and the rear-trip case silently vanished from the
    fixtures -- the gate went green over a case it was no longer testing.

    `service_set` is set on, and only on, the half that arrived WITHOUT a trip
    (the trip holder never receives it). So it identifies exactly the records
    whose service identity was borrowed and must be handed back.
    """
    d = copy.deepcopy(payload)
    for t in d.get("trains") or []:
        if t.get("service_set"):
            # This half owned no trip; it inherited 12 fields and, if geometry
            # put it at the front, had in_service forced True. Undo both.
            for k in S.SERVICE_FIELDS:
                t.pop(k, None)
            t["in_service"] = False
        for k in ADDED_KEYS:
            t.pop(k, None)
    # Phase 3 adds `services[]`; it is derived output, so it strips too.
    d.pop("services", None)
    for k in ("in_service", "vehicles_in_service", "out_of_service",
              "coupled_halves", "trailing_halves"):
        d.pop(k, None)
    d["in_service"] = sum(1 for t in d["trains"] if t.get("in_service"))
    d["out_of_service"] = sum(1 for t in d["trains"]
                              if t.get("in_service") is False)
    return d
