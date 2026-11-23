#!/usr/bin/env python3
"""Ramped concurrency load test for the train map.

Why this exists rather than `hey`/`wrk`: the interesting question is not "how
many requests per second" but "at what concurrency does *this* host stop
serving honest data", and that needs (a) a realistic request mix, (b) per-stage
percentiles, and (c) a check that the 20 s AT poller kept running while we hit
it. A flat RPS number from a generic tool answers none of those.

Design notes worth keeping:

- **Ramped stages, not one burst.** A single 100-user slam tells you pass/fail.
  A ramp tells you *where* the knee is, which is the number you can act on.
- **Weighted mix.** Real browsers fetch the page, the 179 KB geometry, then
  poll /api/trains every few seconds. Hammering only /api/trains flatters the
  server (it is a cache read) and ignores the bytes that actually saturate a
  phone's Wi-Fi uplink.
- **Cold vs warm matters.** /api/trains is served from a 20 s server-side
  cache; a fast p50 here measures the cache, not the upstream path. That is
  the correct thing to measure for user experience, but do not read it as
  "the app can do 2000 polls/sec of real AT data".
- **The self-hosted mirror 403s on a default urllib/httpx User-Agent** —
  Cloudflare bot protection on the proxied hostname. A load test that forgets
  the UA measures Cloudflare's block page and reports a triumphant 0 ms.
  Hence DEFAULT_UA, applied always.
- Errors are counted **by status and by exception class**, never collapsed
  into one "failed" bucket: a 429 (Cloudflare rate limit), a 502 (tunnel
  dropped), and a ReadTimeout (phone stalled) are three completely different
  conclusions and must not be averaged together.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# (path, weight) — roughly one page load + geometry per ~8 live polls, which is
# what a browser sitting on the map for a couple of minutes actually does.
# Paths verified against the live page, not assumed: the geometry is served at
# /static/rail_geo.json (178 KB). An earlier version of this file guessed
# /rail_geo.json, which 404s in 22 bytes — i.e. it would have reported a
# gloriously fast load test of an error page.
MIX = [
    ("/", 2),
    ("/static/rail_geo.json", 2),
    ("/api/trains", 8),
    ("/api/health", 1),
]


@dataclass
class Stage:
    users: int
    seconds: float
    lat: list[float] = field(default_factory=list)
    codes: Counter = field(default_factory=Counter)
    errs: Counter = field(default_factory=Counter)
    bytes_in: int = 0
    reqs: int = 0

    def pct(self, p: float) -> float | None:
        if not self.lat:
            return None
        s = sorted(self.lat)
        k = min(int(round(p / 100.0 * (len(s) - 1))), len(s) - 1)
        return round(s[k] * 1000, 1)

    def summary(self) -> dict:
        ok = sum(v for k, v in self.codes.items() if 200 <= k < 400)
        return {
            "users": self.users,
            "requests": self.reqs,
            "rps": round(self.reqs / self.seconds, 1) if self.seconds else 0,
            "ok": ok,
            "ok_pct": round(100.0 * ok / self.reqs, 1) if self.reqs else 0.0,
            "p50_ms": self.pct(50), "p90_ms": self.pct(90),
            "p99_ms": self.pct(99),
            "max_ms": round(max(self.lat) * 1000, 1) if self.lat else None,
            "mb_in": round(self.bytes_in / 1e6, 2),
            "mbit_s": (round(self.bytes_in * 8 / 1e6 / self.seconds, 1)
                       if self.seconds else 0),
            "codes": dict(self.codes),
            "errors": dict(self.errs),
        }


async def worker(client: httpx.AsyncClient, base: str, st: Stage,
                 stop: float, seq: list[str]) -> None:
    i = 0
    while time.monotonic() < stop:
        path = seq[i % len(seq)]
        i += 1
        t0 = time.monotonic()
        try:
            r = await client.get(base + path)
            dt = time.monotonic() - t0
            st.lat.append(dt)
            st.codes[r.status_code] += 1
            st.bytes_in += len(r.content)
        except Exception as exc:  # noqa: BLE001
            st.errs[type(exc).__name__] += 1
        st.reqs += 1


async def run_stage(base: str, users: int, seconds: float) -> Stage:
    st = Stage(users=users, seconds=seconds)
    seq = [p for p, w in MIX for _ in range(w)]
    limits = httpx.Limits(max_connections=users + 20,
                          max_keepalive_connections=users + 20)
    stop = time.monotonic() + seconds
    async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), limits=limits, http2=False,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_UA}) as client:
        # Stagger starts over 1s so we measure sustained concurrency, not a
        # synchronised thundering herd (which measures the accept queue).
        async def delayed(n: int):
            await asyncio.sleep(n / max(users, 1))
            await worker(client, base, st, stop, seq)
        await asyncio.gather(*(delayed(n) for n in range(users)))
    st.seconds = seconds
    return st


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="target origin, e.g. https://example.test")
    ap.add_argument("--stages", default="10,25,50,100")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--only", default=None,
                    help="test a single path instead of the mix, e.g. "
                         "--only /api/trains — use this to find WHICH "
                         "endpoint is the bottleneck once the mix shows a knee")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    base = a.base.rstrip("/")
    if a.only:
        MIX[:] = [(a.only, 1)]
    print(f"target   {base}")
    print(f"mix      {', '.join(f'{p}x{w}' for p, w in MIX)}")
    print(f"stages   {a.stages} users, {a.seconds:g}s each\n")

    results = []
    for u in [int(x) for x in a.stages.split(",")]:
        s = await run_stage(base, u, a.seconds)
        d = s.summary()
        results.append(d)
        print(f"  {d['users']:>4} users  {d['rps']:>7.1f} rps  "
              f"ok {d['ok_pct']:>5.1f}%  p50 {d['p50_ms']:>8}  "
              f"p90 {d['p90_ms']:>8}  p99 {d['p99_ms']:>9}  "
              f"{d['mbit_s']:>6} Mbit/s  {d['codes']} {d['errors'] or ''}")
        await asyncio.sleep(5)  # let caches and the poller breathe between stages

    if a.out:
        with open(a.out, "w") as fh:
            json.dump(results, fh, indent=1)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
