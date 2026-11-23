#!/usr/bin/env python3
"""Generate the Hoki brand mark's SVG path data from an editable pixel grid.

WHY THIS EXISTS
The mark was first hand-written as SVG <path> data. At 240px it turned out the
tail was floating detached from the body, the body never tapered, and the fins
were so close in value to the back that the silhouette disappeared. None of
that is visible in path data -- it is obvious in a character grid.

Edit GRID, run this, paste the output into the #brand <svg> in web/index.html.
Run-length encodes each colour into a single <path>, which is both smaller and
far easier to diff than per-pixel rects.
"""
GRID = [
    "..........F.....................",
    ".........FFF.FFFFFFFFFFFF.......",
    "....KKKKKKKKKKKKKKKKKKKKKKF.....",
    "..KKBBBBBBBBBBBBBBBKKKKKKKKFF...",
    ".KBEEBBBBBBBBBBBBBBBBBBBKKKKFFF.",
    "MBBPEBBBBBBBBBBBBBBBBBBBBBKKFFFF",
    ".BBBBWWWWWWWWWWWWWWWWWWWWWWKKFFF",
    "..WWWWWWWWWWWWWWWWWWWWWWWWFF.FF.",
    "....FFFFF...FFFFFFFFFFFFF.......",
    ".....FFF.......FFFFF............",
]
COL = {
    "K": ("#3d5f80", "dark blue-grey back"),
    "B": ("#c8d6e2", "pale blue-silver flank"),
    "W": ("#f2f6fa", "silver belly"),
    "F": ("#8fa9c2", "fins + tail (lighter than the back so the silhouette reads)"),
    "E": ("#e8c54a", "the oversized yellow eye"),
    "P": ("#16212e", "pupil"),
    "M": ("#2c4257", "jaw"),
}

def runs(ch, grid):
    out = []
    for y, row in enumerate(grid):
        x = 0
        while x < len(row):
            if row[x] == ch:
                s = x
                while x < len(row) and row[x] == ch:
                    x += 1
                out.append((s, y, x - s))
            else:
                x += 1
    return out

def main():
    w = max(len(r) for r in GRID)
    grid = [r.ljust(w, ".") for r in GRID]
    print(f'<svg viewBox="0 0 {w} {len(grid)}" width="38" height="12" shape-rendering="crispEdges">')
    for ch, (hexc, label) in COL.items():
        rs = runs(ch, grid)
        if not rs:
            continue
        d = "".join(f"M{x} {y}h{n}v1h-{n}z" for x, y, n in rs)
        print(f"  <!-- {label} -->")
        print(f'  <path fill="{hexc}" d="{d}"/>')
    print("</svg>")

if __name__ == "__main__":
    main()
