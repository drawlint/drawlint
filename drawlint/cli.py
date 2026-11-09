"""drawlint CLI: lint patent drawing sheets (SVG, or PDF with one sheet per page) against 37 CFR 1.84.

Exit codes: 0 no errors, 1 formality errors found, 2 input could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import engine, intake


def run(paths, profile="auto", numerals=None):
    reports = []
    for p in paths:
        reports.extend(intake.lint_file(Path(p).read_bytes(), name=str(p), profile=profile))
    return engine.report(reports, engine.lint_set(reports, numerals))


def render_text(rep):
    lines = [f"drawlint {rep['version']}  {rep['rules']}", rep["disclaimer"], ""]
    for s in rep["sheets"]:
        pg = s["page"]
        lines.append(f"{s['file']}: {pg['size'] or 'unknown size'} {pg['orientation'] or ''}, "
                     f"{s['errors']} errors, {s['advisories']} advisories")
        for f in s["findings"]:
            n = f" (x{f['count']})" if f["count"] > 1 else ""
            lines.append(f"  {f['severity']:8} {f['code']:22} {f['message']}{n}  [{f['rule']}]")
    if rep["set_findings"]:
        lines.append("whole set:")
        for f in rep["set_findings"]:
            lines.append(f"  {f['severity']:8} {f['code']:22} {f['message']}  [{f['rule']}]")
    lines += ["", "PASS: no formality errors found" if rep["pass"] else f"FAIL: {rep['errors']} errors"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="drawlint", description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", help="SVG sheets (one file per sheet) or PDFs (one sheet per page), the whole set in order")
    ap.add_argument("--profile", choices=["auto", "letter", "a4"], default="auto")
    ap.add_argument("--numerals", help="comma-separated reference numerals that must appear (e.g. 10,12,14a)")
    ap.add_argument("--json", action="store_true", help="print the JSON report")
    a = ap.parse_args(argv)
    numerals = [n.strip() for n in a.numerals.split(",") if n.strip()] if a.numerals else None
    try:
        rep = run(a.files, a.profile, numerals)
    except (engine.InputError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2) if a.json else render_text(rep))
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
