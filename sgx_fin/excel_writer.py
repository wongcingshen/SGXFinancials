"""Write / merge extracted statements into a ShareInvestor-style workbook (one per company).

Sheets: Income Statement, Balance Sheet, Cash Flow (row labels down column A,
one column per period), Checks (arithmetic tie-outs), Source Map (which
company line each figure came from). Every run merges into the existing
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
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

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


def merge_records(existing: list[dict], normalised: dict, meta: dict) -> list[dict]:
    """One record per (statement, period_end, months); newer filings replace older ones."""
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
                "values": vals, "labels": block["labels"], "is_comparative": i > 0, **meta,
            }
    return sorted(recs.values(), key=lambda r: (r["statement"], r["period_end"], r["months"]))


# ------------------------------------------------------------------ writing
def write_workbook(path: Path, normalised: dict, meta: dict) -> tuple[Path, list[dict]]:
    """Merge into the workbook at `path` (created if missing) and save it."""
    data, checks = workbook_bytes(path.read_bytes() if path.exists() else None, normalised, meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path, checks


def workbook_bytes(existing: bytes | None, normalised: dict, meta: dict) -> tuple[bytes, list[dict]]:
    """Merge into an existing workbook (or start fresh) and return the new .xlsx bytes."""
    records = merge_records(_load_store(existing), normalised, meta)
    wb = Workbook()
    wb.remove(wb.active)
    company = meta.get("company_name", "")
    currency = meta.get("currency", "")

    for st, (title, rows) in T.STATEMENTS.items():
        ws = wb.create_sheet(title)
        recs = [r for r in records if r["statement"] == st]
        recs.sort(key=lambda r: (r["period_end"], r["months"]), reverse=T.LATEST_FIRST)
        _write_statement(ws, title, rows, recs, company, meta.get("stock_code", ""), currency)

    checks = run_checks(records)
    _write_checks(wb.create_sheet("Checks"), checks)
    _write_sources(wb.create_sheet("Source Map"), records)

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
CHECKS = [
    ("income_statement", "Revenue + COGS = Gross Profit", ["revenue", "cost_of_sales"], "gross_profit"),
    ("income_statement", "PBT + Tax = PAT (continuing)", ["profit_before_tax", "tax"], "profit_after_tax"),
    ("income_statement", "Shareholders + MI (+perps) = PAT", ["pat_shareholders", "pat_minority", "perpetual_holders"], "profit_after_tax"),
    ("balance_sheet", "NCA + CA = Total Assets", ["total_nca", "total_ca"], "total_assets"),
    ("balance_sheet", "CL + NCL = Total Liabilities", ["total_cl", "total_ncl"], "total_liabilities"),
    ("balance_sheet", "Total Liabilities + Total Equity = Total Assets", ["total_liabilities", "total_equity"], "total_assets"),
    ("balance_sheet", "Shareholders' Equity + MI = Total Equity", ["shareholders_equity", "minority_interests"], "total_equity"),
    ("cash_flow", "Operating + Investing + Financing = Net Change", ["cf_net_operating", "cf_net_investing", "cf_net_financing"], "cf_net_change"),
    ("cash_flow", "Begin + Net Change + FX = End", ["cf_cash_begin", "cf_net_change", "cf_fx_effect"], "cf_cash_end"),
]


def run_checks(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        v = r["values"]
        for st, name, parts, total in CHECKS:
            if st != r["statement"]:
                continue
            if total not in v or parts[0] not in v or parts[1] not in v:
                status, diff = "n/a", None
            else:
                s = sum(v.get(p) or 0 for p in parts)
                diff = round(s - v[total], 3)
                tol = max(2.0, abs(v[total]) * 0.002)  # rounding in '000
                status = "PASS" if abs(diff) <= tol else "FAIL"
            out.append({"statement": st, "period": period_label(r["period_end"], r["months"]),
                        "check": name, "status": status, "difference": diff})
    return out


def _write_checks(ws, checks):
    ws.append(["Statement", "Period", "Check", "Status", f"Difference ({T.OUTPUT_UNIT_LABEL})"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
    colours = {"PASS": "C6EFCE", "FAIL": "FFC7CE", "n/a": "EDEDED"}
    for ch in checks:
        ws.append([ch["statement"], ch["period"], ch["check"], ch["status"], ch["difference"]])
        ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor=colours[ch["status"]])
    for col, w in zip("ABCDE", (18, 16, 50, 10, 18)):
        ws.column_dimensions[col].width = w


def _write_sources(ws, records):
    ws.append(["Statement", "Period", "Template line", "Company's label", "Filing", "Source PDF", "Pages", "Model"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
    labels = {r.key: r.label for _, rows in T.STATEMENTS.values() for r in rows}
    for r in records:
        for k in r["values"]:
            ws.append([r["statement"], period_label(r["period_end"], r["months"]), labels.get(k, k),
                       r["labels"].get(k, ""), r.get("filing", ""), r.get("source_pdf", ""),
                       r.get("pages", ""), r.get("model", "")])
    for col, w in zip("ABCDEFGH", (18, 16, 40, 60, 50, 40, 12, 18)):
        ws.column_dimensions[col].width = w
