#!/usr/bin/env python3
"""Self-host Leaflet instead of hotlinking unpkg.com.

WHY
The page loaded leaflet.css and leaflet.js from unpkg.com, which means every
visitor's IP and User-Agent went to a third-party CDN before the map drew --
exactly the thing src/fetch_fonts.py exists to prevent for fonts. Fixing the
fonts while leaving the map library on a CDN was half a privacy stance.

It is also an availability argument: unpkg is a single point of failure the
project does not control, and a map that cannot load Leaflet is a blank page,
not a degraded one.

LICENCE
Leaflet is BSD-2-Clause, which permits redistribution in source or binary
form provided the copyright notice and conditions travel with it. The upstream
licence header is preserved verbatim inside leaflet.js, and the full text is
written to web/vendor/LICENSE-LEAFLET.txt.

IMAGE ASSETS
leaflet.css references marker/layer images by relative URL (images/*.png).
Those are fetched too, into web/vendor/images/, so the stylesheet resolves
locally. The project draws its own markers, but the layers control and the
default shadow still reference them.

Run:  .venv/bin/python src/fetch_leaflet.py
"""
import hashlib
import os
import re
import sys
import urllib.request

VER = "1.9.4"
BASE = f"https://unpkg.com/leaflet@{VER}/dist/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

OUT = os.path.join(os.path.dirname(__file__), "..", "web", "vendor")

# Published SRI digests for leaflet 1.9.4 as served by unpkg. Pinning these
# means a tampered or truncated download fails loudly here, at build time,
# rather than shipping a broken or hostile map to visitors.
#
# PROVENANCE -- do not "correct" these from memory. The leaflet.js digest is
# the exact value that was in index.html's integrity= attribute while the page
# loaded from unpkg in production, i.e. every browser that ever rendered this
# map verified it. The css digest was confirmed against the live download.
# An earlier version of this file carried a mis-remembered js digest and the
# check correctly refused to write the file.
EXPECT_SHA256 = {
    "leaflet.css": "p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
    "leaflet.js": "20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
}

LICENSE_URL = f"https://unpkg.com/leaflet@{VER}/LICENSE"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def b64sha256(data: bytes) -> str:
    import base64
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def main() -> int:
    os.makedirs(os.path.join(OUT, "images"), exist_ok=True)
    ok = True

    for name, want in EXPECT_SHA256.items():
        data = fetch(BASE + name)
        got = b64sha256(data)
        if got != want:
            print(f"  FAIL {name}: sha256-{got} != expected sha256-{want}",
                  file=sys.stderr)
            ok = False
            continue
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(data)
        print(f"  saved {name:16s} {len(data):7d} B  sha256-{got[:16]}... OK")

    if not ok:
        print("ERROR: integrity check failed; nothing trustworthy written.",
              file=sys.stderr)
        return 1

    # Images referenced by leaflet.css, so the local stylesheet resolves.
    css = open(os.path.join(OUT, "leaflet.css"), "rb").read().decode("utf-8")
    imgs = sorted(set(re.findall(r"url\((images/[^)\"']+)\)", css)))
    for rel in imgs:
        data = fetch(BASE + rel)
        dest = os.path.join(OUT, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        print(f"  saved {rel:16s} {len(data):7d} B")
    if not imgs:
        print("  note: leaflet.css referenced no images/ assets")

    lic = fetch(LICENSE_URL).decode("utf-8")
    with open(os.path.join(OUT, "LICENSE-LEAFLET.txt"), "w") as fh:
        fh.write(f"Leaflet {VER} -- bundled under web/vendor/.\n"
                 "Fetched and integrity-checked by src/fetch_leaflet.py.\n"
                 "Upstream: https://github.com/Leaflet/Leaflet\n\n" + lic)
    print(f"\nwrote {OUT}/LICENSE-LEAFLET.txt")
    print(f"done: leaflet {VER} is now served from /static/vendor/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
