"""Write / merge extracted statements into a ShareInvestor-style workbook (one per company).

Sheets: Income Statement, Balance Sheet, Cash Flow (row labels down column A,
one column per period), Checks (arithmetic tie-outs), Source Map (which
company line each figure came from), and for filings whose statements are
pictures, Transcription (the tables as read from the pictures, re-read figures
and rows that still don't add up marked). Every run merges into the existing
workbook, so quarterly / half-year / full-year filings accumulate as columns.
A hidden "_store" sheet keeps the raw figures so merging never depends on
display labels.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import checks as C
from . import template as T

NUM_FMT = '#,##0;(#,##0);"-"'
EPS_FMT = '#,##0.00;(#,##0.00);"-"'
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
SUB_FILL = PatternFill("solid", fgColor="D9E1F2")
THIN = Side(style="thin", color="8EA9DB")


def period_label(end: str, months: int) -> str:
    try:
        d = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return end
    if months == 0:
        return d.strftime("%d %b %Y")
    if months == 12:
        return f"FY {d.strftime('%b %Y')}"
    return f"{months}M {d.strftime('%b %Y')}"


# ------------------------------------------------------------------ store
def _load_store(src: Path | bytes | None) -> list[dict]:
    if src is None or (isinstance(src, Path) and not src.exists()):
        return []
    wb = load_workbook(io.BytesIO(src) if isinstance(src, bytes) else src)
    if "_store" not in wb.sheetnames:
        return []
    return [json.loads(r[0]) for r in wb["_store"].iter_rows(values_only=True) if r and r[0]]


BULKY = ("transcription", "located", "pictures", "rechecked")  # kept once per filing, not per figure


def _load_tables(src: bytes | None) -> list[dict]:
    if not src:
        return []
    wb = load_workbook(io.BytesIO(src))
    if "_tables" not in wb.sheetnames:
        return []
    return [json.loads(r[0]) for r in wb["_tables"].iter_rows(values_only=True) if r and r[0]]


def merge_tables(existing: list[dict], meta: dict) -> list[dict]:
    """Transcriptions of picture filings, one per filing (a re-run replaces it), newest first."""
    if not meta.get("transcription"):
        return existing
    key = meta.get("filing") or meta.get("source_pdf") or ""
    pics = meta.get("pictures") or {}
    entry = {"key": key, "filing": meta.get("filing", ""), "source_pdf": meta.get("source_pdf", ""),
             "model": pics.get("model", ""), "tables": meta["transcription"], "corrections": pics.get("corrections", []),
             "doubtful": pics.get("doubtful", []), "doubtful_rows": pics.get("doubtful_rows", []), "turned": pics.get("turned", {})}
    return [entry] + [e for e in existing if e.get("key") != key]


def merge_records(existing: list[dict], normalised: dict, meta: dict) -> list[dict]:
    """One record per (statement, period_end, months); newer filings replace older ones."""
    meta = {k: v for k, v in meta.items() if k not in BULKY}
    recs = {(r["statement"], r["period_end"], r["months"]): r for r in existing}
    for st, block in normalised.items():
        for i, p in enumerate(block["periods"]):
            key = (st, p["period_end"], int(p["months"]))
            vals = {k: v[i] for k, v in block["values"].items() if i < len(v) and v[i] is not None}
            if not vals:
                continue
            old = recs.get(key)
            # Prefer the filing where this period is the current one (index 0) over a
            # comparative column in a later filing, unless the later one restates it.
            if old and old.get("is_comparative") is False and i > 0:
                merged = {**vals, **old["values"]}
                old["values"] = merged
                continue
            recs[key] = {
                "statement": st, "period_end": p["period_end"], "months": int(p["months"]),
                "values": vals, "labels": block["labels"], "is_comparative": i > 0,
                "step": block.get("step", 1.0), **meta,
            }
    return sorted(recs.values(), key=lambda r: (r["statement"], r["period_end"], r["months"]))


# ------------------------------------------------------------------ writing
def write_workbook(path: Path, normalised: dict, meta: dict, formats: dict | None = None) -> tuple[Path, list[dict]]:
    """Merge into the workbook at `path` (created if missing) and save it."""
    data, checks = workbook_bytes(path.read_bytes() if path.exists() else None, normalised, meta, formats)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path, checks


def workbook_bytes(existing: bytes | None, normalised: dict, meta: dict,
                   formats: dict | None = None) -> tuple[bytes, list[dict]]:
    """Merge into an existing workbook (or start fresh) and return the new .xlsx bytes."""
    records = merge_records(_load_store(existing), normalised, meta)
    tables = merge_tables(_load_tables(existing), meta)
    wb = Workbook()
    wb.remove(wb.active)
    company = meta.get("company_name", "")
    currency = meta.get("currency", "")

    # A bank gets the bank layout; the newest filing decides if a workbook somehow has both.
    fmt = next((r.get("format") for r in sorted(records, key=lambda r: r["period_end"], reverse=True)
                if r.get("format")), meta.get("format") or "general")
    for st in T.STATEMENT_IDS:
        rows, title = T.rows_for(st, fmt, formats), T.title_for(st, fmt, formats)
        ws = wb.create_sheet(T.STATEMENT_TITLES[st])  # fixed sheet names; the format's title goes in A2
        recs = [r for r in records if r["statement"] == st]
        # Group columns by length (full year, 9M, half year, quarter), latest first within each,
        # so quarters and years aren't interleaved; balance sheet dates simply by date.
        recs.sort(key=lambda r: (r["months"], r["period_end"]), reverse=T.LATEST_FIRST)
        _write_statement(ws, title, rows, recs, company, meta.get("stock_code", ""), currency)

    checks = run_checks(records)
    _write_checks(wb.create_sheet("Checks"), checks)
    _write_sources(wb.create_sheet("Source Map"), records, formats)

    if tables:
        _write_transcription(wb.create_sheet("Transcription"), tables)
        hidden = wb.create_sheet("_tables")
        for e in tables:
            hidden.append([json.dumps(e)])
        hidden.sheet_state = "hidden"

    store = wb.create_sheet("_store")
    for r in records:
        store.append([json.dumps(r)])
    store.sheet_state = "hidden"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), checks


def _write_statement(ws, title, rows, recs, company, code, currency):
    ws["A1"] = f"{company} ({code})" if code else company
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"{title} — {currency} {T.OUTPUT_UNIT_LABEL} (EPS in cents)"
    ws["A2"].font = Font(italic=True, color="595959")

    hdr = 4
    ws.cell(hdr, 1, "Period").font = Font(bold=True, color="FFFFFF")
    ws.cell(hdr + 1, 1, "Period Ended")
    ws.cell(hdr + 2, 1, "Months")
    for c in range(1, len(recs) + 2):
        ws.cell(hdr, c).fill = HEAD_FILL
    for j, r in enumerate(recs, start=2):
        h = ws.cell(hdr, j, period_label(r["period_end"], r["months"]))
        h.font = Font(bold=True, color="FFFFFF")
        h.alignment = Alignment(horizontal="right")
        ws.cell(hdr + 1, j, r["period_end"]).alignment = Alignment(horizontal="right")
        ws.cell(hdr + 2, j, r["months"] or "").alignment = Alignment(horizontal="right")

    row = hdr + 4
    for tr in rows:
        a = ws.cell(row, 1, tr.label)
        if tr.kind == "header":
            a.font = Font(bold=True)
            for c in range(1, len(recs) + 2):
                ws.cell(row, c).fill = SUB_FILL
        else:
            if tr.kind == "total":
                a.font = Font(bold=True)
            else:
                a.alignment = Alignment(indent=1)
            for j, r in enumerate(recs, start=2):
                v = r["values"].get(tr.key)
                cell = ws.cell(row, j, v)
                cell.number_format = EPS_FMT if tr.key.startswith("eps") else NUM_FMT
                if tr.kind == "total":
                    cell.font = Font(bold=True)
                    cell.border = Border(top=THIN)
        row += 1

    ws.column_dimensions["A"].width = 52
    for j in range(2, len(recs) + 2):
        ws.column_dimensions[get_column_letter(j)].width = 15
    ws.freeze_panes = ws.cell(hdr + 3, 2)


# ------------------------------------------------------------------ checks
CHECKS = C.CHECKS


def run_checks(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        v = r["values"]
        for c in C.run(r["statement"], v.get, r.get("labels"), r.get("step", 1.0)):
            out.append({"statement": c["statement"], "period": period_label(r["period_end"], r["months"]),
                        "check": c["check"], "status": c["status"], "difference": c["difference"]})
    return out


def _write_checks(ws, checks):
    ws.append(["Statement", "Period", "Check", "Status", f"Difference ({T.OUTPUT_UNIT_LABEL})"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
    colours = {"PASS": "C6EFCE", "FAIL": "FFC7CE", "n/a": "EDEDED", "derived": "FFF2CC"}
    for ch in checks:
        ws.append([ch["statement"], ch["period"], ch["check"], ch["status"], ch["difference"]])
        ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor=colours[ch["status"]])
    for col, w in zip("ABCDE", (18, 16, 50, 10, 18)):
        ws.column_dimensions[col].width = w


FIXED_FILL = PatternFill("solid", fgColor="C6EFCE")
DOUBT_FILL = PatternFill("solid", fgColor="FFEB9C")


def _write_transcription(ws, entries):
    """The tables as read from the pictures: figures as printed text. Green: changed by the re-read
    (the first reading is in the cell's comment). Yellow: rows that still don't add up."""
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 60
    ws.append(["Read from pictures. Green: figure corrected by a second, enlarged reading (first reading in the "
               "comment). Yellow: a row that still doesn't add up; check it against the PDF."])
    ws["A1"].font = Font(italic=True, color="595959")
    for e in entries:
        fixed = {(c["table"], c["row"], c["col"]): c for c in e.get("corrections", [])}
        doubt = {tuple(x) for x in e.get("doubtful_rows", [])}
        for ti, t in enumerate(e["tables"]):
            ws.append([])
            title = ws.cell(ws.max_row + 1, 1, f"{e.get('filing') or e.get('source_pdf')} — page {t['page']}: {t['title']}"
                                               f" ({t.get('basis', '')}; {t.get('unit') or 'unit not stated'})")
            title.font = Font(bold=True)
            cols = [c["col"] for c in t["columns"]]
            head = ws.max_row + 1
            ws.cell(head, 1, "Ref")
            ws.cell(head, 2, "Line")
            for j, c in enumerate(t["columns"], start=3):
                ws.cell(head, j, c["heading"])
                ws.column_dimensions[get_column_letter(j)].width = max(ws.column_dimensions[get_column_letter(j)].width or 0, 16)
            for j in range(1, len(cols) + 3):
                ws.cell(head, j).font = Font(bold=True, color="FFFFFF")
                ws.cell(head, j).fill = HEAD_FILL
                ws.cell(head, j).alignment = Alignment(wrap_text=True, vertical="top")
            for ri, r in enumerate(t["rows"]):
                row = ws.max_row + 1
                ws.cell(row, 1, r.get("ref", ""))
                ws.cell(row, 2, r.get("label", ""))
                for j, col in enumerate(cols, start=3):
                    cells = r.get("cells", {})
                    cell = ws.cell(row, j, cells.get(str(col), cells.get(col)))
                    cell.alignment = Alignment(horizontal="right")
                    if (ti, ri, col) in fixed:
                        cell.fill = FIXED_FILL
                        cell.comment = Comment(f"re-read from an enlarged picture; first read as {fixed[(ti, ri, col)]['was']}", "SGX Financials")
                    elif (ti, ri) in doubt:
                        cell.fill = DOUBT_FILL
                if (ti, ri) in doubt:
                    ws.cell(row, 2).fill = DOUBT_FILL
        for why in e.get("doubtful", []):
            ws.append(["", f"Doesn't add up: {why}"])
            ws.cell(ws.max_row, 2).font = Font(color="9C5700")


def _write_sources(ws, records, formats=None):
    ws.append(["Statement", "Period", "Template line", "Company's label", "Filing", "Source PDF", "Pages", "Model"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
    labels = T.labels(formats)
    for r in records:
        for k in r["values"]:
            ws.append([r["statement"], period_label(r["period_end"], r["months"]), labels.get(k, k),
                       r["labels"].get(k, ""), r.get("filing", ""), r.get("source_pdf", ""),
                       r.get("pages", ""), r.get("model", "")])
    for col, w in zip("ABCDEFGH", (18, 16, 40, 60, 50, 40, 12, 18)):
        ws.column_dimensions[col].width = w
