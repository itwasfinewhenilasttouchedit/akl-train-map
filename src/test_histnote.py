"""The History count must always be reconcilable with what the popup shows.

Guards the bug "the number of reports on the units doesn't match the reports":
the popup badge counts OPEN VEHICLE FAULTS (`n_neg`) while the History button
counts every kept row (`n`). Those legitimately differ -- praise is not a
fault, a cleared report is history, and a people report expires when the train
reaches its terminus -- but a unit showing "History (2)" with no badge and no
explanation reads as the map being broken.

This test extracts the REAL histNote() from web/index.html and runs it under
node against summaries built by the REAL feedback.summary(), so it verifies the
shipped code rather than a re-implementation. Skips cleanly when node is
absent, so it never breaks the suite on a host without it.

Run: python test_histnote.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(os.path.dirname(HERE), "web", "index.html")

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def build_summary():
    """Real summary() over a DB covering every n != n_neg cause."""
    tmp = tempfile.mkdtemp(prefix="histnote_")
    os.environ["FEEDBACK_DB"] = os.path.join(tmp, "fb.db")
    sys.path.insert(0, HERE)
    import feedback as f

    f.init_db()
    # open faults only -> badge == History, no gloss wanted
    f.add_report("701", "smell", 3)
    f.add_report("701", "smell", 2)
    # expired people report -> History 1, badge 0, MUST be explained
    f.add_report("238", "ruffians", 2, behaviour="loud", trip_id="GONE")
    # praise only -> History 1, badge 0, MUST be explained
    f.add_report("1087", "praise", 1)
    # mixed: one open fault + one expired people report
    f.add_report("347", "smell", 2)
    f.add_report("347", "ruffians", 2, behaviour="loud", trip_id="GONE")
    # resolved by riders -> History 1, badge 0, MUST be explained
    r = f.add_report("659", "smell", 2)
    with f._conn() as c:
        for i in range(max(f.CLEAR_MIN, f.CLEAR_MARGIN) + 1):
            c.execute("INSERT INTO votes (report_id, voter_hash, vote, created_at) "
                      "VALUES (?,?,?,datetime('now'))", (r["id"], f"v{i}", -1))
    return f, f.summary(live_trips=set()), tmp


def main():
    f, summary, tmp = build_summary()

    # --- backend half: every row must be attributable to exactly one bucket ---
    for sn, v in summary.items():
        n = v.get("n") or 0
        tot = ((v.get("n_neg") or 0) + (v.get("n_people") or 0)
               + (v.get("n_resolved") or 0) + (v.get("n_expired") or 0)
               + (v.get("n_praise") or 0))
        check(f"set {sn}: buckets account for all {n} rows", tot, n)

    check("expired people report is nameable", summary["238"].get("n_expired"), 1)
    check("expired people report is not a fault", summary["238"]["n_neg"], 0)
    check("praise is not a fault", summary["1087"]["n_neg"], 0)
    check("resolved report is not a fault", summary["659"]["n_neg"], 0)
    check("mixed set counts the open fault", summary["347"]["n_neg"], 1)
    check("mixed set counts the expired one", summary["347"].get("n_expired"), 1)

    # --- repeat offenders must mean open faults, not raw rows ---
    st = f.stats(live_trips=set())
    check("expired-only set is not a repeat offender",
          [x for x in st["repeat_offenders"] if x["set_no"] == 238], [])
    check("genuine offender still listed on its fault count",
          [x for x in st["repeat_offenders"] if x["set_no"] == 701],
          [{"set_no": 701, "n": 2}])
    check("health stats skip the fault maths",
          "repeat_offenders" in f.stats(repeat=False), False)

    # --- UI half: run the shipped histNote() ---
    node = shutil.which("node")
    if not node:
        print("  (node not found -- skipping the histNote() JS checks)")
    else:
        src = open(HTML).read()
        m = re.search(r"\nfunction histNote\(f\) \{.*?\n\}\n", src, re.S)
        if not m:
            check("histNote() present in index.html", False, True)
        else:
            js = """
%s
const summary = %s;
const out = {};
for (const k of Object.keys(summary)) {
  const v = summary[k];
  const n = v.n|0, open = v.n_neg != null ? (v.n_neg|0) : n;
  const text = histNote(v).replace(/<[^>]*>/g, '').trim();
  out[k] = {open: open, n: n, text: text};
}
console.log(JSON.stringify(out));
""" % (m.group(0).replace("esc(", "String("), json.dumps(summary))
            p = os.path.join(tmp, "h.js")
            open(p, "w").write(js)
            r = subprocess.run([node, p], capture_output=True, text=True)
            if r.returncode != 0:
                check(f"histNote() runs ({r.stderr.strip()[:80]})", False, True)
            else:
                got = json.loads(r.stdout)
                for sn, o in got.items():
                    if o["open"] != o["n"]:
                        check(f"set {sn}: mismatch is explained",
                              bool(o["text"]), True)
                    else:
                        check(f"set {sn}: no needless gloss", o["text"], "")
                check("expired wording", got["238"]["text"],
                      "1 ended with the journey")
                check("praise wording", got["1087"]["text"], "1 praise")
                check("resolved wording", got["659"]["text"],
                      "1 cleared by riders")
                check("mixed wording names both", got["347"]["text"],
                      "1 open \u00b7 1 ended with the journey")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
