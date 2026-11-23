#!/usr/bin/env python3
"""Static gates for index.html / pixel.css. Run before every deploy.

These exist because generated HTML/CSS stays VALID when it is wrong: an
unbalanced brace in an inline script silently kills the rest of the file, and
nothing in the toolchain complains. Cheap to check, expensive to miss.
"""
import glob
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")


def main() -> int:
    fails = []
    s = open(os.path.join(ROOT, "web/index.html"), encoding="utf-8").read()

    n_open, n_close = s.count("<script"), s.count("</script>")
    print(f"script tags: {n_open} open / {n_close} close")
    if n_open != n_close:
        fails.append("script tag mismatch")

    style = re.search(r"<style>(.*?)</style>", s, re.S).group(1)
    print(f"style braces: {style.count('{')} / {style.count('}')}")
    if style.count("{") != style.count("}"):
        fails.append("style brace imbalance")

    # The main application script is the LAST <script> block in the file.
    js = s.split("<script>")[-1].split("</script>")[0]
    print(f"main script braces: {js.count('{')} / {js.count('}')}")
    if js.count("{") != js.count("}"):
        fails.append("main script brace imbalance")

    css = open(os.path.join(ROOT, "web/pixel.css"), encoding="utf-8").read()
    print(f"pixel.css braces: {css.count('{')} / {css.count('}')}")
    if css.count("{") != css.count("}"):
        fails.append("pixel.css brace imbalance")

    # Project rule: no CJK anywhere in shipped web/ or src/.
    pat = re.compile(r"[\u3000-\u30ff\u4e00-\u9fff\uff00-\uffef]")
    bad = []
    for f in glob.glob(os.path.join(ROOT, "web/*")) + \
            glob.glob(os.path.join(ROOT, "src/*.py")):
        if not os.path.isfile(f):
            continue
        if pat.search(open(f, encoding="utf-8", errors="ignore").read()):
            bad.append(os.path.basename(f))
    print(f"CJK files: {bad or 'NONE'}")
    if bad:
        fails.append(f"CJK present in {bad}")

    # The skin is the default now, so no third-party font CDN may remain: it
    # would send every visitor's IP to Google on first paint. Strip comments
    # first -- the file legitimately DISCUSSES the CDN it no longer uses, and a
    # check that cannot tell code from prose gets disabled by the next person.
    css_code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # Third-party CDNs. The fonts were pulled off Google and Leaflet off
    # unpkg for the same reason: a default-skin page must not hand every
    # visitor's IP to someone else before the first paint. Assert BOTH, or the
    # next person re-adds a convenient <script src="https://unpkg...">.
    cdn = [h for h in ("fonts.googleapis.com", "fonts.gstatic.com",
                       "unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com")
           if h in css_code]
    print(f"third-party font CDN in live pixel.css rules: {cdn or 'none'}")
    if cdn:
        fails.append(f"pixel.css still fetches from {cdn}")

    # Same rule for the page itself. Comments are stripped first, so the
    # explanatory notes about the CDNs we NO LONGER use don't trip the check.
    html_cdn = [h for h in ("fonts.googleapis.com", "fonts.gstatic.com",
                            "unpkg.com", "cdn.jsdelivr.net",
                            "cdnjs.cloudflare.com", "cloudflareinsights.com")
                if h in re.sub(r"<!--.*?-->", "", s, flags=re.S)]
    print(f"third-party font CDN in index.html: {html_cdn or 'none'}")
    if html_cdn:
        fails.append(f"index.html still fetches from {html_cdn}")

    for f in ("web/fonts/pixelfonts.css",
              "web/fonts/LICENSE-FONTS.txt",
              "web/fonts/pixelify-sans-0000_00FF.woff2",
              "web/fonts/press-start-2p-0000_00FF.woff2",
              # Leaflet is self-hosted too; if these vanish the map is a blank
              # page, which is a worse failure than a missing font.
              "web/vendor/leaflet.js",
              "web/vendor/leaflet.css",
              "web/vendor/LICENSE-LEAFLET.txt"):
        p = os.path.join(ROOT, f)
        ok = os.path.isfile(p) and os.path.getsize(p) > 0
        print(f"{'ok ' if ok else 'MISSING'} {f}")
        if not ok:
            fails.append(f"missing {f}")

    print()
    if fails:
        for f in fails:
            print(f"FAIL {f}")
        return 1
    print("all static gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
