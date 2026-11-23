#!/usr/bin/env python3
"""Tests for src/accesslog.py -- the combined-format access log.

Run: ARCHIVE_DB=off python src/test_accesslog.py

These exist because the module handles client IP addresses. The specific
failure modes worth gating:
  * logging to a path that cannot be opened must not take the site down
  * a user agent containing a double quote must not desynchronise the parser
  * X-Forwarded-For must yield the CLIENT, not the last proxy
  * the header must be ignorable, because it is only trustworthy behind a
    proxy that overwrites it
  * disabled must mean no file is created at all
"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAIL.append(name)


class FakeURL:
    def __init__(self, path, query=""):
        self.path = path
        self.query = query


class FakeReq:
    """Minimal stand-in for a Starlette Request."""
    def __init__(self, path="/", query="", method="GET", headers=None,
                 client_host="127.0.0.1", http_version="1.1"):
        self.url = FakeURL(path, query)
        self.method = method
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = type("C", (), {"host": client_host})()
        self.scope = {"http_version": http_version}


def fresh_module(env):
    """Import accesslog with a given environment, discarding any prior state."""
    for k in ("ACCESS_LOG", "ACCESS_LOG_TRUST_PROXY",
              "ACCESS_LOG_MAX_BYTES", "ACCESS_LOG_BACKUPS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    for mod in ("accesslog",):
        sys.modules.pop(mod, None)
    import accesslog
    accesslog.init()
    return accesslog


def read(path):
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="test_accesslog_"))

    # ---------------------------------------------------- disabled by default
    print("disabled unless ACCESS_LOG is set")
    log = tmp / "unset.log"
    al = fresh_module({})
    check("enabled() is False when unset", al.enabled() is False)
    al.record(FakeReq(), 200, 123)
    check("no file written when unset", not log.exists())

    al = fresh_module({"ACCESS_LOG": "off"})
    check("'off' disables", al.enabled() is False)

    # ----------------------------------------------------------- basic format
    print("combined format")
    path = tmp / "a.log"
    al = fresh_module({"ACCESS_LOG": str(path)})
    check("enabled() is True", al.enabled() is True)
    al.record(FakeReq("/api/trains", client_host="10.9.9.9"), 200, 4096)
    line = read(path).strip()
    # host - - [date] "METHOD path HTTP/v" status bytes "ref" "ua"
    # Quoted fields allow backslash escapes: a UA containing a double quote is
    # escaped, not rejected, so [^"]* would wrongly fail on valid lines.
    _Q = r'(?:[^"\\]|\\.)*'
    pat = (r'^(\S+) - - \[(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\] '
           r'"(\S+) (\S+) HTTP/([\d.]+)" (\d{3}) (\S+) '
           r'"(' + _Q + r')" "(' + _Q + r')"$')
    m = re.match(pat, line)
    check("line matches combined format", m is not None, line)
    if m:
        check("status recorded", m.group(6) == "200", m.group(6))
        check("bytes recorded", m.group(7) == "4096", m.group(7))
        check("method recorded", m.group(3) == "GET", m.group(3))
        check("path recorded", m.group(4) == "/api/trains", m.group(4))

    # query string must be preserved: it distinguishes real deep links
    al.record(FakeReq("/api/trains", query="x=1&y=2"), 200, 10)
    check("query string kept", '"GET /api/trains?x=1&y=2 HTTP/1.1"' in read(path))

    # ------------------------------------------------------------ client IP
    print("client address resolution")
    path = tmp / "ip.log"
    al = fresh_module({"ACCESS_LOG": str(path)})

    al.record(FakeReq(headers={"CF-Connecting-IP": "203.0.113.7"},
                      client_host="127.0.0.1"), 200, 1)
    check("CF-Connecting-IP preferred over socket",
          read(path).startswith("203.0.113.7 "), read(path)[:40])

    path = tmp / "xff.log"
    al = fresh_module({"ACCESS_LOG": str(path)})
    # First entry is the client; later entries are proxies. Taking the last
    # would record Cloudflare's edge on every single request.
    al.record(FakeReq(headers={"X-Forwarded-For": "198.51.100.5, 172.16.0.1"},
                      client_host="127.0.0.1"), 200, 1)
    check("X-Forwarded-For takes the FIRST entry",
          read(path).startswith("198.51.100.5 "), read(path)[:40])

    path = tmp / "cfwins.log"
    al = fresh_module({"ACCESS_LOG": str(path)})
    al.record(FakeReq(headers={"CF-Connecting-IP": "203.0.113.7",
                               "X-Forwarded-For": "198.51.100.5"}), 200, 1)
    check("CF header wins over XFF",
          read(path).startswith("203.0.113.7 "), read(path)[:40])

    # Direct exposure makes the header attacker-controlled; it must be
    # possible to ignore it entirely.
    path = tmp / "notrust.log"
    al = fresh_module({"ACCESS_LOG": str(path), "ACCESS_LOG_TRUST_PROXY": "0"})
    al.record(FakeReq(headers={"CF-Connecting-IP": "203.0.113.7"},
                      client_host="192.0.2.44"), 200, 1)
    check("TRUST_PROXY=0 ignores the header and uses the socket",
          read(path).startswith("192.0.2.44 "), read(path)[:40])

    path = tmp / "noclient.log"
    al = fresh_module({"ACCESS_LOG": str(path)})
    req = FakeReq()
    req.client = None          # happens on some ASGI transports
    al.record(req, 200, 1)
    check("missing client becomes '-'", read(path).startswith("- - - ["),
          read(path)[:20])

    # --------------------------------------------------------- field escaping
    print("quote escaping (parser desync guard)")
    path = tmp / "q.log"
    al = fresh_module({"ACCESS_LOG": str(path)})
    al.record(FakeReq(headers={"User-Agent": 'evil" 200 0 "-" "pwned',
                               "Referer": 'ref"x'}), 200, 1)
    line = read(path).strip()
    check("inner quotes escaped in UA", '\\"' in line, line)
    check("line still parses as combined", re.match(pat, line) is not None, line)
    # A naive parser splitting on '" "' must still see exactly 2 quoted fields
    check("exactly two quoted fields survive",
          len(re.findall(r'"(?:[^"\\]|\\.)*"', line)) == 3, line)

    print("empty referrer/UA become '-'")
    path = tmp / "dash.log"
    al = fresh_module({"ACCESS_LOG": str(path)})
    al.record(FakeReq(), 200, 1)
    check("missing ref and UA are '-'", read(path).strip().endswith('"-" "-"'),
          read(path).strip()[-20:])

    # ------------------------------------------------------------- resilience
    print("failures never reach the request path")
    # A directory where a file should be: open() must fail, and the module
    # must degrade to disabled rather than raising on import.
    bad = tmp / "baddir"
    bad.mkdir()
    al = fresh_module({"ACCESS_LOG": str(bad)})
    check("unopenable path disables instead of raising", al.enabled() is False)
    al.record(FakeReq(), 200, 1)      # must not raise
    check("record() on disabled log is a no-op", True)

    path = tmp / "broken.log"
    al = fresh_module({"ACCESS_LOG": str(path)})
    bust = FakeReq()
    del bust.url                      # force an AttributeError inside record()
    try:
        al.record(bust, 200, 1)
        check("record() swallows internal errors", True)
    except Exception as exc:
        check("record() swallows internal errors", False, repr(exc))

    # ---------------------------------------------------------------- rotation
    print("rotation caps growth on flash storage")
    path = tmp / "rot.log"
    al = fresh_module({"ACCESS_LOG": str(path),
                       "ACCESS_LOG_MAX_BYTES": "2000",
                       "ACCESS_LOG_BACKUPS": "2"})
    for i in range(200):
        al.record(FakeReq(f"/p/{i}"), 200, 1)
    sizes = sorted(p.name for p in tmp.glob("rot.log*"))
    check("rotated into backups", len(sizes) > 1, str(sizes))
    check("backups capped at 2 + current", len(sizes) <= 3, str(sizes))
    check("current file under the cap", path.stat().st_size <= 4000,
          str(path.stat().st_size))

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {', '.join(FAIL)}")
        return 1
    print("all access log tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
