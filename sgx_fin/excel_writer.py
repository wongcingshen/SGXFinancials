"""Write / merge extracted statements into a standard-layout workbook (one per company).

Sheets: Income Statement, Balance Sheet, Cash Flow (row labels down column A,
one column per period), Checks (each filing's verdict, then the arithmetic
tie-outs), Source Map (which company line each figure came from), COGS Basis (each filing's
industry and how its cost of goods sold was made up, line by line), and for filings whose statements are
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


BULKY = ("transcription", "located", "pictures", "rechecked", "cogs_basis")  # kept once per filing, not per figure


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


def _load_cogs(src: bytes | None) -> list[dict]:
    if not src:
        return []
    wb = load_workbook(io.BytesIO(src))
    if "_cogs" not in wb.sheetnames:
        return []
    return [json.loads(r[0]) for r in wb["_cogs"].iter_rows(values_only=True) if r and r[0]]


def merge_cogs(existing: list[dict], normalised: dict, meta: dict) -> list[dict]:
    """How each filing's cost of goods sold was made up, one entry per filing (a re-run or a correction
    replaces it), newest first."""
    basis = (normalised.get("income_statement") or {}).get("cogs_basis") or meta.get("cogs_basis")
    if not basis:
        return existing
    key = meta.get("filing") or meta.get("source_pdf") or ""
    entry = {"key": key, "filing": meta.get("filing") or meta.get("source_pdf") or "This filing", "basis": basis}
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
    cogs = merge_cogs(_load_cogs(existing), normalised, meta)
    wb = Workbook()
    wb.remove(wb.active)
    company = meta.get("company_name", "")
    currency = meta.get("currency", "")
    industry = next((r.get("industry") for r in sorted(records, key=lambda r: r["period_end"], reverse=True)
                     if r.get("industry")), meta.get("industry", ""))

    # A bank gets the bank layout; the newest filing decides if a workbook somehow has both.
    fmt = next((r.get("format") for r in sorted(records, key=lambda r: r["period_end"], reverse=True)
                if r.get("format")), meta.get("format") or "general")
    verdicts = filing_verdicts(records)
    bad = [v for v in verdicts if not v["verdict"].get("ok")]
    warning = ("Not verified: " + " | ".join(f"{v['filing']}: {'; '.join(v['verdict'].get('reasons') or [])}" for v in bad[:2])
               + ". Check these figures against the PDF before relying on them (Checks sheet).") if bad else ""
    for st in T.STATEMENT_IDS:
        rows, title = T.rows_for(st, fmt, formats), T.title_for(st, fmt, formats)
        ws = wb.create_sheet(T.STATEMENT_TITLES[st])  # fixed sheet names; the format's title goes in A2
        recs = [r for r in records if r["statement"] == st]
        # Group columns by length (full year, 9M, half year, quarter), latest first within each,
        # so quarters and years aren't interleaved; balance sheet dates simply by date.
        recs.sort(key=lambda r: (r["months"], r["period_end"]), reverse=T.LATEST_FIRST)
        _write_statement(ws, title, rows, recs, company, meta.get("stock_code", ""), currency, warning, industry)

    checks = run_checks(records)
    _write_checks(wb.create_sheet("Checks"), checks, verdicts)
    _write_sources(wb.create_sheet("Source Map"), records, formats)
    if cogs:
        _write_cogs(wb.create_sheet("COGS Basis"), cogs)
        hidden = wb.create_sheet("_cogs")
        for e in cogs:
            hidden.append([json.dumps(e)])
        hidden.sheet_state = "hidden"

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


def _write_statement(ws, title, rows, recs, company, code, currency, warning="", industry=""):
    ws["A1"] = f"{company} ({code})" if code else company
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"{title} — {currency} {T.OUTPUT_UNIT_LABEL} (EPS in cents)" + (f" · Industry: {industry}" if industry else "")
    ws["A2"].font = Font(italic=True, color="595959")
    if warning:  # a filing whose figures nothing confirms: said where the figures are, not only on Checks
        ws["A3"] = warning
        ws["A3"].font = Font(bold=True, color="9C0006")

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
                        "check": c["check"], "status": c["status"], "difference": c["difference"], "note": c["note"]})
    return out


def filing_verdicts(records: list[dict]) -> list[dict]:
    """The verdict each filing in the workbook got when it was read, newest first:
    [{"filing", "periods", "verdict"}]. Workbooks made before verdicts existed have none."""
    out: dict[str, dict] = {}
    for r in sorted(records, key=lambda r: (r["period_end"], r["months"]), reverse=True):
        if not isinstance(r.get("verdict"), dict):
            continue
        e = out.setdefault(r.get("filing") or r.get("source_pdf") or "This filing",
                           {"filing": r.get("filing") or r.get("source_pdf") or "This filing", "periods": [], "verdict": r["verdict"]})
        label = period_label(r["period_end"], r["months"])
        if label not in e["periods"]:
            e["periods"].append(label)
    return list(out.values())


def _write_checks(ws, checks, verdicts=()):
    for v in verdicts:  # each filing's verdict first: can its figures be relied on?
        ok = v["verdict"].get("ok")
        why = ("; ".join(v["verdict"].get("reasons") or []) if not ok else
               f"{v['verdict'].get('pass', 0)} tie-out checks pass and none fails; every statement and gross profit found")
        ws.append([f"{'OK' if ok else 'NOT VERIFIED'} — {v['filing']} ({', '.join(v['periods'])}): {why}"])
        r = ws.max_row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        cell = ws.cell(r, 1)
        cell.font = Font(bold=True, color="006100" if ok else "9C0006")
        cell.fill = PatternFill("solid", fgColor="C6EFCE" if ok else "FFC7CE")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 32
    if verdicts:
        ws.append([])
    ws.append(["Statement", "Period", "Check", "Status", f"Difference ({T.OUTPUT_UNIT_LABEL})", "Note"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
    colours = {"PASS": "C6EFCE", "FAIL": "FFC7CE", "n/a": "EDEDED", "derived": "FFF2CC"}
    for ch in checks:
        ws.append([ch["statement"], ch["period"], ch["check"], ch["status"], ch["difference"], ch.get("note", "")])
        ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor=colours[ch["status"]])
    for col, w in zip("ABCDEF", (18, 16, 50, 10, 18, 22)):
        ws.column_dimensions[col].width = w


METHODS = {"printed": "printed in the filing", "by_nature": "judged from expenses by nature",
           "judged": "judged from the expense lines", "none": "nothing on the statement is a cost of earning the revenue",
           "program": "worked out by the program", "chosen": "your choice of lines", "missing": "not found"}


def _write_cogs(ws, entries):
    """Each filing's industry and how its cost of goods sold was made up: every expense line on the
    face of the income statement with its figures, whether it is in COGS and why, and the total."""
    ws.column_dimensions["A"].width = 46
    ws.cell(1, 1, "How the cost of goods sold was worked out when a filing doesn't print a cost of sales: the lines "
                  "marked Yes are added up, and gross profit = revenue + cost of goods sold. Change the rules in the "
                  "web app (Settings > Cost of goods sold) or tick other lines in the filing's details.").font = Font(italic=True, color="595959")
    row = 2
    for e in entries:
        b = e.get("basis") or {}
        periods = b.get("periods") or []
        row += 1  # a blank row between filings
        ws.cell(row, 1, f"{e.get('filing')} — industry: {b.get('industry_name', '')}"
                        + (f" ({b['evidence']})" if b.get("evidence") else "")).font = Font(bold=True, size=12)
        by = f", by {b['rule']}" if b.get("rule") and b.get("method") in ("judged", "by_nature", "none") else ""
        row += 1
        ws.cell(row, 1, f"Cost of goods sold: {METHODS.get(b.get('method'), b.get('method') or '')}{by}")
        if b.get("explanation"):
            row += 1
            ws.cell(row, 1, f"How: {b['explanation']}")
        if b.get("also_suggests"):
            row += 1
            ws.cell(row, 1, f"The filing's lines also suggest: {b['also_suggests']}").font = Font(color="9C5700")
        row += 1
        for j, h in enumerate(["Line (as printed)", *[period_label(p["period_end"], p["months"]) for p in periods],
                               "In COGS", "Why"], start=1):
            c = ws.cell(row, j, h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = HEAD_FILL
        for ln in b.get("lines") or []:
            row += 1
            ws.cell(row, 1, ln.get("label"))
            for j, v in enumerate(ln.get("values") or [], start=2):
                ws.cell(row, j, v).number_format = NUM_FMT
            yes = ws.cell(row, len(periods) + 2, "Yes" if ln.get("in_cogs") else "No")
            if ln.get("in_cogs"):
                yes.fill = FIXED_FILL
            ws.cell(row, len(periods) + 3, ln.get("why", ""))
        row += 1
        ws.cell(row, 1, "Cost of goods sold").font = Font(bold=True)
        for j, v in enumerate(b.get("cogs") or [], start=2):
            c = ws.cell(row, j, v)
            c.number_format = NUM_FMT
            c.font = Font(bold=True)
            c.border = Border(top=THIN)
        ws.cell(row, len(periods) + 3, b.get("cogs_label", ""))
        if b.get("da_in_cogs"):  # FactSet shows cost of revenue including D&A, and D&A on its own
            for name, vals in (("of which depreciation & amortisation", b["da_in_cogs"]),
                               ("Cost of goods sold excluding D&A", b.get("cogs_excl_da") or [])):
                row += 1
                ws.cell(row, 1, name).font = Font(italic=True)
                for j, v in enumerate(vals, start=2):
                    ws.cell(row, j, v).number_format = NUM_FMT
            ws.cell(row, len(periods) + 3, "FactSet's cost of revenue including D&A is the total above; excluding D&A, this")
        for j in range(2, len(periods) + 2):
            ws.column_dimensions[get_column_letter(j)].width = max(ws.column_dimensions[get_column_letter(j)].width or 0, 15)
        ws.column_dimensions[get_column_letter(len(periods) + 3)].width = 60
        row += 1


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
        own = {row.key: row.label for row in T.rows_for(r["statement"], r.get("format"), formats)}  # a bank's own words
        for k in r["values"]:
            ws.append([r["statement"], period_label(r["period_end"], r["months"]), own.get(k) or labels.get(k, k),
                       r["labels"].get(k, ""), r.get("filing", ""), r.get("source_pdf", ""),
                       r.get("pages", ""), r.get("model", "")])
    for col, w in zip("ABCDEFGH", (18, 16, 40, 60, 50, 40, 12, 18)):
        ws.column_dimensions[col].width = w
