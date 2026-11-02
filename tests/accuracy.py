"""Score an output workbook against a hand-checked answer key, figure by figure.

  python tests/accuracy.py output/2V6B.xlsx                       # Bank of Baroda key by default
  python tests/accuracy.py some.xlsx --key tests/answers/other.json

An answer key (tests/answers/*.json) lists, for one filing, every template line and period with the
figure as printed (in the key's unit; EPS in cents / paise) or null where the line should be blank.
Keys joined with "+" are scored on their sum (for lines a company may split either way).
Each figure is one of: correct, wrong (with what the workbook has), missing (blank where a figure
is printed), or extra (a figure where the line should be blank).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sgx_fin import template as T  # noqa: E402
from sgx_fin.extractor import UNIT_FACTORS  # noqa: E402


def records(xlsx: Path) -> dict:
    wb = load_workbook(xlsx)
    out = {}
    for row in wb["_store"].iter_rows(values_only=True):
        if row and row[0]:
            r = json.loads(row[0])
            out[(r["statement"], r["period_end"], int(r["months"]))] = r["values"]
    return out


def score(xlsx: Path, key: dict) -> dict:
    recs = records(xlsx)
    scale = UNIT_FACTORS[key.get("unit", "thousands")] / T.OUTPUT_UNIT
    results = []
    for line in key["lines"]:
        vals = recs.get((line["statement"], line["period_end"], line["months"]), {})
        keys = line["key"].split("+")
        got = [vals.get(k) for k in keys]
        have = None if all(g is None for g in got) else sum(g or 0 for g in got)
        eps = line["key"].startswith("eps")
        want = line["value"]
        accept = [want, *line.get("also_accept", [])]
        if want is None:
            status = "correct" if not have else "extra"
        elif have is None:
            status = "missing"
        else:
            ok = any(a is not None and abs(have - (a if eps else a * scale)) <= (0.5 if eps else max(1.0, scale / 2))
                     for a in accept)
            status = "correct" if ok else "wrong"
        results.append({**line, "status": status, "got": have,
                        "want": None if want is None else (want if eps else want * scale)})
    return {"results": results, "counts": Counter(r["status"] for r in results)}


def report(name: str, s: dict) -> str:
    c = s["counts"]
    total = sum(c.values())
    figures = [r for r in s["results"] if r["value"] is not None]
    right = sum(r["status"] == "correct" for r in figures)
    lines = [f"{name}: {c['correct']}/{total} lines right ({100 * c['correct'] / total:.1f}%); "
             f"printed figures {right}/{len(figures)} ({100 * right / len(figures):.1f}%) · "
             f"wrong {c['wrong']} · missing {c['missing']} · extra {c['extra']}"]
    by_st = {}
    for r in s["results"]:
        by_st.setdefault(r["statement"], Counter())[r["status"]] += 1
    for st, cc in by_st.items():
        n = sum(cc.values())
        lines.append(f"  {st:17s} {cc['correct']:3d}/{n:<3d} ({100 * cc['correct'] / n:5.1f}%)")
    for r in s["results"]:
        if r["status"] != "correct":
            fmt = lambda v: "blank" if v is None else f"{v:,.0f}"  # noqa: E731
            lines.append(f"  {r['status']:7s} {r['statement']:16s} {r['key']:40s} {r['months']:>2}M {r['period_end']}  "
                         f"got {fmt(r['got'])}  want {fmt(r['want'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("workbooks", nargs="+", type=Path)
    ap.add_argument("--key", type=Path, default=HERE / "answers" / "bank_of_baroda_2026q2.json")
    a = ap.parse_args()
    key = json.loads(a.key.read_text())
    for wb in a.workbooks:
        print(report(wb.name, score(wb, key)))
