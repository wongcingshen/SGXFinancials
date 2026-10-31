"""AI step: read the selected statement pages and return numbers mapped to the template."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import anthropic
from pypdf import PdfReader, PdfWriter

from . import template as T

DEFAULT_MODEL = "claude-opus-5-5"
UNIT_FACTORS = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}


def _statement_schema(statement: str) -> dict:
    keys = [r.key for r in T.data_rows(statement)]
    return {
        "type": "object",
        "properties": {
            "page_numbers": {"type": "array", "items": {"type": "integer"}},
            "periods": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "period_end": {"type": "string", "description": "YYYY-MM-DD"},
                        "months": {"type": "integer", "description": "0 for balance sheet dates"},
                    },
                    "required": ["label", "period_end", "months"],
                    "additionalProperties": False,
                },
            },
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": keys},
                        "source_label": {"type": "string"},
                        "values": {"type": "array", "items": {"type": ["number", "null"]}},
                    },
                    "required": ["key", "source_label", "values"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["page_numbers", "periods", "rows"],
        "additionalProperties": False,
    }


SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "currency": {"type": "string", "description": "ISO code, e.g. SGD, USD, RMB"},
        "unit": {"type": "string", "enum": list(UNIT_FACTORS)},
        "basis": {"type": "string", "description": "Group (consolidated) or Company"},
        "income_statement": _statement_schema("income_statement"),
        "balance_sheet": _statement_schema("balance_sheet"),
        "cash_flow": _statement_schema("cash_flow"),
        "notes": {"type": "string"},
    },
    "required": ["company_name", "currency", "unit", "basis",
                 "income_statement", "balance_sheet", "cash_flow", "notes"],
    "additionalProperties": False,
}


def _template_text() -> str:
    parts = []
    for st, (title, rows) in T.STATEMENTS.items():
        lines = [f"## {st} ({title})"]
        for r in rows:
            if r.kind == "header":
                continue
            lines.append(f"- {r.key}: {r.label}" + (f" — {r.hint}" if r.hint else ""))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


SYSTEM = """You transcribe primary financial statements from SGX-listed company filings into a fixed template.

Rules:
- Use the GROUP (consolidated) columns. Ignore Company-only columns when both are shown.
- Copy numbers exactly as printed, in the unit the statement states (report that unit once in `unit`). Do not rescale.
- Parentheses mean negative. Expenses, costs, tax expense, cash outflows, treasury shares are negative numbers.
- EPS goes in cents (multiply by 100 if printed in dollars).
- `periods` lists the statement's numeric columns left to right as printed (current period first if that is the layout); every row's `values` has one entry per period in the same order. Use null where a cell is blank or "-".
- Income statement and cash flow periods are durations (months = 3, 6, 9 or 12). Balance sheet periods are dates (months = 0).
- Map each template key to the line the company reports. If a template line is a sum of several reported lines (e.g. "Reserves" = retained earnings + other reserves), add them and list the source labels joined with " + ". If a company line has no matching key, fold it into the closest "Other ..." key.
- Only include a key if the filing gives the figure (or it is a direct sum of shown figures). Omit keys you cannot support; never invent numbers.
- Totals (gross profit, total assets, net cash from operating activities...) must be the printed totals, not your own arithmetic, when they are printed.
- `page_numbers` are the page numbers within the attached excerpt (1-based) the statement came from.
- Put caveats (restatements, unusual presentation, bank/REIT format) in `notes`.

Template keys:
""" + _template_text()


@dataclass
class Extraction:
    data: dict
    model: str
    usage: dict


def excerpt_pdf(pdf: Path, pages: list[int]) -> bytes:
    reader = PdfReader(str(pdf))
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def extract(pdf: Path, pages: list[int], model: str = DEFAULT_MODEL, effort: str = "high",
            cache_dir: Path | None = None, client: anthropic.Anthropic | None = None) -> Extraction:
    pdf_bytes = excerpt_pdf(pdf, pages)
    cache_key = hashlib.sha256(pdf_bytes + model.encode() + SYSTEM.encode()).hexdigest()[:16]
    cache_file = (cache_dir or pdf.parent) / f"{pdf.stem}.{cache_key}.extract.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        return Extraction(cached["data"], cached["model"], cached.get("usage", {}))

    client = client or anthropic.Anthropic()
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        system=SYSTEM,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": (
                    f"These {len(pages)} pages (original pages {pages} of {pdf.name}) were "
                    "selected as the income statement, balance sheet and cash flow statement. "
                    "Fill the template.")},
            ],
        }],
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {getattr(msg, 'stop_details', None)}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Output hit max_tokens before the JSON was complete")
    text = next(b.text for b in msg.content if b.type == "text")
    data = json.loads(text)
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    cache_file.write_text(json.dumps({"data": data, "model": msg.model, "usage": usage}, indent=2))
    return Extraction(data, msg.model, usage)


def normalise(data: dict) -> dict:
    """Rescale to the template's output unit and key rows by template key.

    Returns {statement: {"periods": [...], "values": {key: [..]}, "labels": {key: str}}}.
    """
    factor = UNIT_FACTORS.get(data.get("unit", "units"), 1) / T.OUTPUT_UNIT
    out = {}
    for st in T.STATEMENTS:
        block = data.get(st) or {"periods": [], "rows": []}
        n = len(block["periods"])
        values, labels = {}, {}
        for row in block["rows"]:
            vals = (row["values"] + [None] * n)[:n]
            if row["key"] not in ("eps_basic", "eps_diluted"):
                vals = [None if v is None else round(v * factor, 3) for v in vals]
            values[row["key"]] = vals
            labels[row["key"]] = row["source_label"]
        if st == "cash_flow" and "cf_net_operating" in values:
            capex = values.get("cf_capex", [None] * n)
            values["free_cash_flow"] = [
                None if o is None else round(o + (c or 0), 3)
                for o, c in zip(values["cf_net_operating"], capex)
            ]
            labels["free_cash_flow"] = "computed: net operating cash flow + capex"
        out[st] = {"periods": block["periods"], "values": values, "labels": labels}
    return out
