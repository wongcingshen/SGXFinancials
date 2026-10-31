"""Command line.

  python -m sgx_fin screen "https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17"
  python -m sgx_fin run    --date 2026-10-30 --watchlist watchlist.txt
  python -m sgx_fin pdf    results.pdf --code D05 [--pages 7,8,9,10] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import extractor
from .excel_writer import write_workbook
from .page_scanner import format_report
from .pipeline import NotResults, Result, process_announcement, process_pdf
from .sgx_client import FINANCIAL_RESULTS_CATEGORY, SGXClient, Screen, classify_title


def _date(s: str) -> date:
    return datetime.strptime(s.replace("-", ""), "%Y%m%d").date()


def _screen_from_args(a) -> Screen:
    if a.url:
        return Screen.from_url(a.url)
    sgt_today = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    d_from = _date(a.date_from or a.date) if (a.date_from or a.date) else sgt_today
    d_to = _date(a.date_to or a.date) if (a.date_to or a.date) else d_from
    return Screen(d_from, d_to, {"ANNC": [a.category]})


def _watchlist(path: str | None) -> list[str]:
    if not path:
        return []
    lines = Path(path).read_text().splitlines()
    return [l.split("#")[0].strip() for l in lines if l.split("#")[0].strip()]


def _report(res: Result, out_dir: Path, code: str) -> None:
    print(format_report(res.scan))
    if res.normalised is None:
        print("  (dry run / no pages selected: AI step skipped)")
        return
    name = f"{code or res.meta.get('company_name', 'company')}".replace(" ", "_")
    path, checks = write_workbook(out_dir / f"{name}.xlsx", res.normalised, res.meta)
    fails = [c for c in checks if c["status"] == "FAIL"]
    print(f"  -> {path}  tokens in/out {res.usage.get('input_tokens')}/{res.usage.get('output_tokens')}"
          f"  checks: {sum(c['status'] == 'PASS' for c in checks)} pass, {len(fails)} fail")
    for c in fails:
        print(f"     FAIL {c['period']}: {c['check']} (diff {c['difference']})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sgx_fin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def screen_args(p):
        p.add_argument("url", nargs="?", help="SGX company-announcements page URL (from=, to=, ANNC=)")
        p.add_argument("--date", help="single SGT date YYYY-MM-DD (default: today)")
        p.add_argument("--from", dest="date_from")
        p.add_argument("--to", dest="date_to")
        p.add_argument("--category", default=FINANCIAL_RESULTS_CATEGORY)
        p.add_argument("--company", help="filter by issuer name substring or stock code")

    def ai_args(p):
        p.add_argument("--model", default=None, help="default: SGX_GEMINI_MODEL / CLAUDE_MODEL or the provider default")
        p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
        p.add_argument("--dry-run", action="store_true", help="scan pages only, no AI call")
        p.add_argument("--out", default="output", help="folder for workbooks")

    p = sub.add_parser("screen", help="list announcements for an SGX page URL / date")
    screen_args(p)

    p = sub.add_parser("run", help="screen, download, scan, extract, write Excel")
    screen_args(p)
    ai_args(p)
    p.add_argument("--watchlist", help="file of issuer names / stock codes, one per line")
    p.add_argument("--all", action="store_true", help="also process notices, releases and other non-results filings")
    p.add_argument("--work", default="downloads")

    p = sub.add_parser("pdf", help="process a local PDF")
    p.add_argument("pdf")
    p.add_argument("--code", default="", help="stock code / name used for the workbook file")
    p.add_argument("--company", default="")
    p.add_argument("--pages", help="override page selection, e.g. 7,8,9")
    ai_args(p)

    a = ap.parse_args(argv)
    out_dir = Path(getattr(a, "out", "output"))

    if a.cmd == "pdf":
        pages = [int(x) for x in a.pages.split(",")] if a.pages else None
        res = process_pdf(Path(a.pdf), pages=pages, model=a.model, effort=a.effort, dry_run=a.dry_run,
                          meta={"company_name": a.company, "stock_code": a.code, "filing": Path(a.pdf).name})
        _report(res, out_dir, a.code)
        return 0

    screen = _screen_from_args(a)
    print(f"Screening {screen.page_url}")
    client = SGXClient()
    try:
        anns = client.screen(screen, company=a.company, attachments=a.cmd == "run")
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1
    if client.last_debug and not anns:
        print(f"  (browser: {client.last_debug})")
    others = [x for x in anns if (x.raw.get("kind") or classify_title(x.title)) != "results"]
    if a.cmd == "run" and not a.all:  # notices of release dates, press releases, presentations...
        anns = [x for x in anns if x not in others]
        if others:
            print(f"  skipping {len(others)} non-results announcement(s); --all to include them")
    targets = _watchlist(getattr(a, "watchlist", None))
    if targets:
        t = [x.lower() for x in targets]
        anns = [x for x in anns if any(w == x.stock_code.lower() or w in x.issuer.lower() for w in t)]
    for x in anns:
        print(f"  {x.broadcast:16s} {x.stock_code:6s} {x.issuer[:40]:40s} {x.title[:70]}")
    print(f"{len(anns)} announcement(s)")
    if a.cmd == "screen":
        return 0

    for x in anns:
        print(f"\n== {x.issuer}: {x.title}")
        try:
            res = process_announcement(client, x, Path(a.work), model=a.model, effort=a.effort, dry_run=a.dry_run)
        except NotResults as e:
            print(f"  skipped: not a results filing. {e}")
            continue
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        if res is None:
            print("  no PDF attachment found")
            continue
        _report(res, out_dir, x.stock_code or x.slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
