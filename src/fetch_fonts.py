#!/usr/bin/env python3
"""Self-host the two pixel fonts instead of hotlinking Google Fonts.

WHY THIS EXISTS
The pixel skin is becoming the DEFAULT view, which changes the privacy
calculus completely. An @import from fonts.googleapis.com means every single
visitor's IP address and User-Agent go to Google before a pixel is painted.
This site's stance is no third-party tracking, no client-side analytics JS,
and no public attribution of who reported what -- shipping a third-party
font CDN as the default would quietly hand every visitor to Google on the
very first paint. (The origin does keep a rotating server-side access log
for its own traffic analytics; that is first-party, stays on the origin, and
is never shared. It is not a reason to hand the same data to a CDN.)
Self-hosting also removes a render-blocking third-party request from the
critical path.

LICENCE -- the reason this is allowed where the tileset was not
Pixelify Sans and Press Start 2P are both SIL Open Font License 1.1, which
EXPLICITLY permits redistribution (bundled or sold, modified or not) provided
the licence travels with the font and the Reserved Font Name is respected. We
rename nothing, so there is no RFN issue. Contrast with the purchased
tileset, whose licence forbids redistribution "edited or not" -- that art must
stay out of this public repo, these fonts may live in it. Two assets, two
licences, opposite conclusions; do not reason about one from the other.

SUBSETTING
We take the Latin subsets only (0000-00FF). The retired body face was a
Japanese font served as 120+ unicode-range subsets; the project enforces zero
CJK, so that rule was load-bearing and is kept for any future face.

Pixelify Sans is served by Google as a VARIABLE font, so the 400/600/700 URLs
all point at one byte-identical file. We hash the downloads and emit a single
woff2 with `font-weight: 400 700` rather than three copies of the same bytes.
"""
import hashlib
import os
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# BODY FACE: Pixelify Sans, not DotGothic16.
# DotGothic16 is a hairline 1px face designed for CJK at 16px. On macOS
# subpixel antialiasing it renders visibly wispy -- reported from a real Mac,
# and the cause is the glyph weight, NOT the delivery origin (these fonts were
# already self-hosted when it was reported). Swapping the CDN would have fixed
# nothing. Pixelify Sans carries a heavier stem and reads cleanly at 12-13px.
CSS_URL = ("https://fonts.googleapis.com/css2"
           "?family=Pixelify+Sans:wght@400;600;700"
           "&family=Press+Start+2P&display=swap")

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "fonts")

# Latin + latin-ext only. These are the ranges Google labels latin/latin-ext;
# we match on the range rather than on a subset name, because the name is only
# present as a CSS comment and comments are not a contract.
WANTED_PREFIXES = (
    "U+0000-00FF",   # latin
    "U+0100-02AF",   # latin-ext
)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    css = fetch(CSS_URL).decode("utf-8")

    # Split into @font-face blocks, keeping only Latin ones.
    blocks = re.findall(r"@font-face\s*\{[^}]*\}", css)
    print(f"total @font-face blocks: {len(blocks)}")

    out_rules = []
    saved = 0
    # sha256 -> filename already written. Google serves Pixelify Sans as a
    # VARIABLE font: the 400, 600 and 700 CSS blocks all point at the SAME
    # woff2 (verified identical sha256). Writing one file per weight shipped
    # three byte-identical 12 KB copies and made the browser fetch the same
    # bytes up to three times. Dedupe on content and emit a weight RANGE.
    by_hash: dict = {}
    for b in blocks:
        rng = re.search(r"unicode-range:\s*([^;]+);", b)
        if not rng or not rng.group(1).strip().startswith(WANTED_PREFIXES):
            continue
        fam = re.search(r"font-family:\s*'([^']+)'", b).group(1)
        url = re.search(r"url\((https://[^)]+\.woff2)\)", b).group(1)
        weight = re.search(r"font-weight:\s*([^;]+);", b)
        weight = weight.group(1).strip() if weight else "400"

        slug = fam.lower().replace(" ", "-")
        tag = rng.group(1).strip().split(",")[0].replace("U+", "").replace("-", "_")
        data = fetch(url)
        digest = hashlib.sha256(data).hexdigest()

        if digest in by_hash:
            # Same bytes as a face we already saved: record the weight so the
            # emitted rule can span the full range, and do not write again.
            prev = by_hash[digest]
            prev["weights"].append(weight)
            print(f"  dedup {prev['name']:44s} also serves weight {weight}")
            continue

        name = f"{slug}-{tag}.woff2"
        path = os.path.join(OUT_DIR, name)
        with open(path, "wb") as fh:
            fh.write(data)
        saved += 1
        print(f"  saved {name:44s} {len(data):7d} B  ({fam} {weight})")
        by_hash[digest] = {"name": name, "fam": fam, "weights": [weight],
                           "range": rng.group(1).strip()}

    for e in by_hash.values():
        # A variable face advertises a range ("400 700"); a static one a single
        # value. Declaring 400 only, then asking for 600, makes the browser
        # synthesise a fake bold -- which on a pixel font looks like smearing.
        ws = sorted({int(w) for w in e["weights"] if w.isdigit()})
        wspec = f"{ws[0]} {ws[-1]}" if len(ws) > 1 else (str(ws[0]) if ws else "400")
        out_rules.append(
            "@font-face {\n"
            f"  font-family: '{e['fam']}';\n"
            "  font-style: normal;\n"
            f"  font-weight: {wspec};\n"
            "  font-display: swap;\n"
            f"  src: url('/static/fonts/{e['name']}') format('woff2');\n"
            f"  unicode-range: {e['range']};\n"
            "}"
        )

    if not saved:
        print("ERROR: no Latin subsets matched -- Google changed the format.",
              file=sys.stderr)
        return 1

    header = (
        "/* Self-hosted pixel fonts. GENERATED by src/fetch_fonts.py -- do not\n"
        "   hand-edit; re-run the script instead.\n\n"
        "   Both faces are SIL Open Font License 1.1, which permits\n"
        "   redistribution provided the licence travels with them: see\n"
        "   web/fonts/OFL.txt. Self-hosted rather than @import'ed from Google\n"
        "   because the pixel skin is the DEFAULT view, and an @import would\n"
        "   send every visitor's IP to a third party on first paint, which\n"
        "   this site does not do.\n"
        "   Latin subsets only: the project renders no CJK. */\n\n"
    )
    dest = os.path.join(OUT_DIR, "pixelfonts.css")
    with open(dest, "w") as fh:
        fh.write(header + "\n".join(out_rules) + "\n")
    print(f"\nwrote {dest} ({saved} faces)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
