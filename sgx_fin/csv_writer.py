"""The statements as one CSV in "long" form: one row per figure, for databases, pandas, BI tools and diffs.

It is made from the same merged records as the workbook (its hidden "_store" sheet), so it holds every
period the workbook holds, in the same unit and signs as the statement sheets (the user's own standard's
when there is one). The workbook stays the master copy: the Checks, Source Map, COGS Basis and
Transcription sheets, and merging new periods in, live only there.

Columns: stock_code, company, currency, statement, line_key, line, company_label, period_end, months,
period, value, unit, verified, filing, source_pdf, pages
(unit: units / thousands / millions / billions, or cents for EPS)
"""

from __future__ import annotations

import csv
import io

from . import template as T
from .excel_writer import _load_store, period_label

COLUMNS = ["stock_code", "company", "currency", "statement", "line_key", "line", "company_label", "period_end",
           "months", "period", "value", "unit", "verified", "filing", "source_pdf", "pages"]


def _number(v: float) -> str:
    """Plain digits, no thousands separators or exponent: 12345.6, -800, 0.25."""
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def csv_from_records(records: list[dict], formats: dict | None = None) -> bytes:
    """Records as merge_records makes them -> CSV bytes (UTF-8 with a BOM, so Excel shows non-ASCII labels right)."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(COLUMNS)
    order = {st: i for i, st in enumerate(T.STATEMENT_IDS)}
    labels = T.labels(formats)
    for r in sorted(records, key=lambda r: (order.get(r["statement"], 9), -r["months"], r["period_end"])):
        fmt = r.get("format")
        conv = T.conventions(fmt, formats)
        scale = T.OUTPUT_UNIT / conv["size"]
        rows = [row for row in T.rows_for(r["statement"], fmt, formats) if row.kind != "header"]
        known = {row.key for row in rows}
        rows += [T.Row(k, labels.get(k, k)) for k in r["values"] if k not in known]  # lines the layout lacks
        verdict = r.get("verdict")
        verified = "" if not isinstance(verdict, dict) else ("true" if verdict.get("ok") else "false")
        pages = r.get("pages", "")  # "7,8,9"
        for row in rows:
            v = r["values"].get(row.key)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            eps = row.key.startswith("eps")
            value = v * row.sign if eps else round(v * scale * row.sign, 6)
            w.writerow([r.get("stock_code", ""), r.get("company_name", ""), r.get("currency", ""), r["statement"],
                        row.key, row.label, (r.get("labels") or {}).get(row.key, ""), r["period_end"], r["months"],
                        period_label(r["period_end"], r["months"]), _number(value),
                        "cents" if eps else conv["unit"], verified, r.get("filing", ""),
                        r.get("source_pdf", ""), pages])
    return out.getvalue().encode("utf-8-sig")


def workbook_csv(workbook: bytes, formats: dict | None = None) -> bytes:
    """The CSV of a workbook made by excel_writer (every period merged into it)."""
    return csv_from_records(_load_store(workbook), formats)
