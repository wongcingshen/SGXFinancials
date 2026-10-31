"""AI step: read the selected statement pages and return numbers mapped to the template.

Two providers, picked from the environment (override with AI_PROVIDER=gemini|anthropic):
  Gemini   GEMINI_API_KEY      model SGX_GEMINI_MODEL, else the first available of GEMINI_MODELS
           (GOOGLE_API_KEY, GOOGLE_GENERATIVE_AI_API_KEY or GEMINI_KEY also work)
  Claude   ANTHROPIC_API_KEY   model CLAUDE_MODEL, else claude-opus-5-5
Both get the same prompt, the same PDF excerpt and the same JSON schema.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import template as T

CLAUDE_DEFAULT = "claude-opus-5-5"
# Flash-Lite only: its free daily request allowance is far larger than Flash's. Tried in order
# when a model is missing for the key or its quota is used up (each model has its own quota).
# Set SGX_GEMINI_MODEL to pin one, e.g. gemini-3.1-pro-preview for the hardest filings. (Not
# GEMINI_MODEL: other apps sharing the Vercel team's variables, such as TravelFriend, use that.)
GEMINI_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_KEY")
GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]
UNIT_FACTORS = {"units": 1, "thousands": 1_000, "lakhs": 100_000, "millions": 1_000_000,
                "crores": 10_000_000, "billions": 1_000_000_000}
DEFAULT_UNIT = "thousands"  # when a filing doesn't say: SGX statements are almost always in '000


def _statement_schema(statement: str, formats: dict) -> dict:
    keys = [r.key for r in T.data_rows(statement, formats=formats)]
    return {
        "type": "object",
        "properties": {
            "page_numbers": {"type": "array", "items": {"type": "integer"}},
            "periods": {
                "type": "array",
                "description": "The numeric columns of the table used, left to right; col 1 is the first",
                "items": {
                    "type": "object",
                    "properties": {
                        "col": {"type": "integer", "description": "1-based column number"},
                        "label": {"type": "string", "description": "column heading as printed"},
                        "period_end": {"type": "string", "description": "YYYY-MM-DD"},
                        "months": {"type": "integer", "description": "3, 6, 9 or 12; 0 for balance sheet dates"},
                    },
                    "required": ["col", "label", "period_end", "months"],
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
                        "cells": {
                            "type": "array",
                            "description": "one entry per column that has a figure on this line",
                            "items": {
                                "type": "object",
                                "properties": {"col": {"type": "integer"}, "value": {"type": "number"}},
                                "required": ["col", "value"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["key", "source_label", "cells"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["page_numbers", "periods", "rows"],
        "additionalProperties": False,
    }


def build_schema(formats: dict | None = None) -> dict:
    formats = formats or T.BUILTIN
    return {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "format": {"type": "string", "enum": list(formats),
                   "description": "id of the format whose 'use for' fits this company"},
        "currency": {"type": "string", "description": "ISO code, e.g. SGD, USD, RMB, INR"},
        "unit": {"type": "string", "enum": list(UNIT_FACTORS)},
        "basis": {"type": "string", "description": "Group (consolidated), Standalone or Company"},
        "income_statement": _statement_schema("income_statement", formats),
        "balance_sheet": _statement_schema("balance_sheet", formats),
        "cash_flow": _statement_schema("cash_flow", formats),
        "notes": {"type": "string"},
    },
    "required": ["company_name", "format", "currency", "unit", "basis",
                 "income_statement", "balance_sheet", "cash_flow", "notes"],
    "additionalProperties": False,
    }


def _template_text(formats: dict) -> str:
    parts, seen = [], {}
    for fid, fmt in formats.items():
        parts.append(f"# format {fid} ({fmt['name']}) — use for: {fmt['applies_to'] or fmt['name']}")
        for st, (title, rows) in fmt["statements"].items():
            sig = tuple(rows)
            if sig in seen:
                parts.append(f"## format {fid}: {st} — same rows as format {seen[sig]}")
                continue
            seen[sig] = fid
            lines = [f"## format {fid}: {st} ({title})"]
            for r in rows:
                if r.kind != "header":
                    lines.append(f"- {r.key}: {r.label}" + (f" — {r.hint}" if r.hint else ""))
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


RULES = """You transcribe primary financial statements from SGX-listed company filings into a fixed template.

Rules:
- `format`: the id of the format below whose "use for" fits this company (banks have interest earned / interest expended, deposits and loans or advances). Use only that format's keys.
- Use the GROUP (consolidated) statements and columns. When a filing has both standalone and consolidated statements, use the consolidated ones; ignore Company-only / standalone columns.
- Copy numbers exactly as printed, in the unit the statement states (report it once in `unit`: units, thousands, lakhs, millions, crores or billions; "S$'000" = thousands, "₹ in crore" = crores). If no unit is stated, use thousands. Do not rescale.
- Parentheses mean negative. Expenses, costs, tax expense, cash outflows, treasury shares are negative numbers.
- EPS goes in the currency's minor unit: cents (or paise), multiplying by 100 if printed in dollars (or rupees).
- Columns: `periods` lists the numeric columns of the table you used, left to right, numbered `col` 1, 2, 3... Every figure goes in `cells` as {col, value} with the column it sits under, so check the column of every number. Leave a column out of `cells` when the cell is blank or "-". Never shift numbers between columns.
- Income statement and cash flow periods are durations: months = 3 (quarter ended), 6 (half year / six months ended), 9 (nine months ended) or 12 (year ended). Balance sheet columns are dates: months = 0. period_end is the column's end date as YYYY-MM-DD.
- Map each template key to the line the company reports. If a template line is a sum of several reported lines (e.g. "Reserves" = retained earnings + other reserves), add them and list the source labels joined with " + ". If a company line has no matching key, fold it into the closest "Other ..." key.
- Only include a key if the filing gives the figure (or it is a direct sum of shown figures). Omit keys you cannot support; never invent numbers.
- Expenses by nature (general format): when the income statement has no cost of sales line and lists expenses by nature instead, fill raw_materials (raw materials, consumables used and changes in inventories), employee_benefits and depreciation_amortisation from the face of the statement, and leave cost_of_sales and gross_profit out unless they are printed. The program derives them.
- Totals (gross profit, net interest income, total assets, net cash from operating activities...) must be the printed totals, not your own arithmetic, when they are printed; leave them out when not printed.
- `page_numbers` are the page numbers within the attached excerpt (1-based) the statement came from.
- Put caveats (restatements, unusual presentation, standalone figures used) in `notes`.

Template keys:
"""


def build_system(formats: dict | None = None) -> str:
    return RULES + _template_text(formats or T.BUILTIN)


SCHEMA = build_schema()  # the built-in formats; extract() rebuilds both for imported formats
SYSTEM = build_system()


@dataclass
class Extraction:
    data: dict
    model: str
    usage: dict


def gemini_key() -> str | None:
    for name in GEMINI_KEY_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def ai_env_names() -> list[str]:
    """Names (never values) of AI-related variables this deployment can see, for diagnostics."""
    return sorted(n for n in os.environ if any(k in n.upper() for k in ("GEMINI", "GOOGLE", "ANTHROPIC", "AI_PROVIDER", "GLM", "NVIDIA", "ZAI"))
                  and os.environ[n].strip())


def provider() -> str | None:
    """Which AI provider this deployment uses, or None if no key is configured."""
    choice = os.environ.get("AI_PROVIDER", "").strip().lower()
    if choice in ("gemini", "google"):
        return "gemini"
    if choice in ("anthropic", "claude"):
        return "anthropic"
    if gemini_key():
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def default_model(prov: str | None = None) -> str:
    prov = prov or provider()
    if prov == "gemini":
        return os.environ.get("SGX_GEMINI_MODEL", "").strip() or GEMINI_MODELS[0]
    return os.environ.get("CLAUDE_MODEL") or CLAUDE_DEFAULT


def provider_label(prov: str | None = None) -> str:
    return {"gemini": "Gemini", "anthropic": "Claude"}.get(prov or provider() or "", "AI")


def excerpt_pdf(pdf: Path, pages: list[int]) -> bytes:
    reader = PdfReader(str(pdf))
    writer = PdfWriter()
    for p in pages:
        writer.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _instruction(pdf: Path, pages: list[int], vision: bool = False, locate: bool = False) -> str:
    if locate and not vision:
        return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) may contain more than the "
                "statements: find the income statement, balance sheet and cash flow statement among them "
                "(the Group's primary statements; ignore commentary, notes, segment and ratio tables), give "
                "each statement's page_numbers, and fill the template. Leave out any statement that isn't there.")
    if vision:
        return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) include scanned or "
                "photographed pages, so read them visually. They contain more than the statements: find the "
                "income statement, balance sheet and cash flow statement among them (ignore commentary, "
                "notes and other pages), give each statement's page_numbers, and fill the template. If a "
                "picture is too unclear to read a figure with confidence, leave that figure out and say so in notes.")
    return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) were selected as the "
            "income statement, balance sheet and cash flow statement. Fill the template.")


def extract(pdf: Path, pages: list[int], model: str | None = None, effort: str = "high",
            cache_dir: Path | None = None, client=None, vision: bool = False,
            locate: bool = False, deadline: float | None = None, formats: dict | None = None) -> Extraction:
    """Send only `pages` of `pdf` to the AI and return the parsed JSON.

    Picture filings (vision=True) go to GLM 5.3 Flash first when GLM_FLASH is set, falling back to
    the main provider (Gemini / Claude) if it fails; everything else goes to the main provider.
    `deadline` (time.monotonic()) bounds the whole step: GLM gets a share that leaves the fallback
    time to answer.
    """
    from . import glm

    prov = provider()
    deadline = deadline or time.monotonic() + 280
    attempts = []
    # GLM on a free tier can be slow; only try it with time to spare for a fallback.
    if vision and glm.api_key() and not model and (deadline - time.monotonic() > 100 or not prov):
        attempts.append("glm")
    if prov:
        attempts.append(prov)
    if not attempts:
        raise RuntimeError("No AI key configured: set GEMINI_API_KEY (or GLM_FLASH for picture filings)")

    formats = formats or T.BUILTIN
    system, schema = build_system(formats), build_schema(formats)
    pdf_bytes = excerpt_pdf(pdf, pages)
    instruction = _instruction(pdf, pages, vision, locate)
    failures = []
    for who in attempts:
        name = glm.label() if who == "glm" else (model or default_model(who))
        cache_key = hashlib.sha256(pdf_bytes + f"{who}:{name}:{vision}:{locate}".encode() + system.encode()).hexdigest()[:16]
        cache_file = (cache_dir or pdf.parent) / f"{pdf.stem}.{cache_key}.extract.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            return Extraction(cached["data"], cached["model"], cached.get("usage", {}))
        try:
            if who == "glm":
                budget = deadline if not prov else min(deadline - 75, time.monotonic() + 150)
                data, used, usage = glm.extract(pdf, pages, system, instruction, deadline=budget, formats=formats)
                ex = Extraction(data, f"{used} via {'NVIDIA' if glm.is_nvidia() else 'Z.ai'}",
                                {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})
            elif who == "gemini":
                ex = _gemini(pdf_bytes, instruction, name, effort, client, vision, deadline, system, schema)
            else:
                ex = _claude(pdf_bytes, instruction, name, effort, client, system, schema)
        except Exception as e:  # try the next provider, report all if none works
            failures.append(f"{glm.label() if who == 'glm' else provider_label(who)}: {e}")
            continue
        if failures:
            ex.data["notes"] = (f"{ex.data.get('notes', '')} (Read by {ex.model} after: {'; '.join(failures)})").strip()
        cache_file.write_text(json.dumps({"data": ex.data, "model": ex.model, "usage": ex.usage}, indent=2))
        return ex
    raise RuntimeError(" | ".join(failures))


# ------------------------------------------------------------------ Claude
def _claude(pdf_bytes: bytes, instruction: str, model: str, effort: str, client,
            system: str = SYSTEM, schema: dict = SCHEMA) -> Extraction:
    import anthropic

    client = client or anthropic.Anthropic()
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": instruction},
            ],
        }],
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request: {getattr(msg, 'stop_details', None)}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Output hit max_tokens before the JSON was complete")
    text = next(b.text for b in msg.content if b.type == "text")
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    return Extraction(json.loads(text), msg.model, usage)


# ------------------------------------------------------------------ Gemini
# Effort setting from the UI -> Gemini thinking level.
GEMINI_THINKING = {"low": "LOW", "medium": "LOW", "high": "MEDIUM", "xhigh": "HIGH", "max": "HIGH"}


def gemini_schema(node):
    """JSON Schema -> the OpenAPI-style schema Gemini's response_schema accepts.

    Drops additionalProperties, turns ["number", "null"] into nullable, and keeps
    property order so the model fills the template top to bottom.
    """
    if isinstance(node, list):
        return [gemini_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k == "additionalProperties":
            continue
        if k == "type" and isinstance(v, list):
            kinds = [t for t in v if t != "null"]
            out["type"] = kinds[0]
            if "null" in v:
                out["nullable"] = True
        elif k == "properties":
            out["properties"] = {pk: gemini_schema(pv) for pk, pv in v.items()}
            out["propertyOrdering"] = list(v)
        else:
            out[k] = gemini_schema(v)
    return out


def _gemini(pdf_bytes: bytes, instruction: str, model: str, effort: str, client, vision: bool = False,
            deadline: float | None = None, system: str = SYSTEM, schema_json: dict = SCHEMA) -> Extraction:
    from google import genai
    from google.genai import errors, types

    client = client or genai.Client(
        api_key=gemini_key(),
        http_options=types.HttpOptions(timeout=int(max(30.0, min(280.0, (deadline or time.monotonic() + 280) - time.monotonic())) * 1000)),
    )
    pinned = bool(os.environ.get("SGX_GEMINI_MODEL", "").strip())
    candidates = [model] + ([] if pinned else [m for m in GEMINI_MODELS if m != model])
    schema = gemini_schema(schema_json)
    contents = [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), instruction]

    def config(extras: bool):
        # extras: thinking level, and high resolution for scanned / photographed pages (small digits)
        return types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=65536,
            thinking_config=types.ThinkingConfig(thinking_level=GEMINI_THINKING.get(effort, "MEDIUM"))
            if extras and thinking else None,
            media_resolution="MEDIA_RESOLUTION_HIGH" if extras and vision else None,
        )

    last_err: Exception | None = None
    for name in candidates:
        thinking = not name.startswith("gemini-2")  # 2.x models use thinking budgets, not levels
        try:
            try:
                resp = client.models.generate_content(model=name, contents=contents, config=config(True))
            except errors.ClientError as e:
                if e.code != 400 or not (thinking or vision):
                    raise
                # Some models reject a thinking level or resolution: retry once with their defaults.
                resp = client.models.generate_content(model=name, contents=contents, config=config(False))
        except errors.ClientError as e:
            if e.code in (404, 429):
                last_err = e
                continue  # not available for this key, or its quota is used up: try the next one
            raise
        return _gemini_result(resp, name)
    if getattr(last_err, "code", None) == 429:
        raise RuntimeError(f"Gemini quota used up on {', '.join(candidates)}. "
                           "Free-tier limits reset daily; try again later or set SGX_GEMINI_MODEL "
                           f"to another model. ({last_err})")
    raise RuntimeError(f"No Gemini model available for this key: {last_err}")


def _gemini_result(resp, name: str) -> Extraction:
    cand = (resp.candidates or [None])[0]
    reason = getattr(getattr(cand, "finish_reason", None), "name", str(getattr(cand, "finish_reason", "")))
    text = resp.text
    if not text:
        block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
        raise RuntimeError(f"Gemini returned no text (finish reason {reason}, block {block})")
    if reason == "MAX_TOKENS":
        raise RuntimeError("Gemini hit its output limit before the JSON was complete")
    u = resp.usage_metadata
    usage = {
        "input_tokens": getattr(u, "prompt_token_count", None),
        "output_tokens": (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0),
    }
    return Extraction(json.loads(text), getattr(resp, "model_version", None) or name, usage)


COGS_BY_NATURE = ("raw_materials", "employee_benefits", "depreciation_amortisation")


def _derive_gross_profit(values: dict, labels: dict, n: int) -> None:
    """No cost of sales line (expenses presented by nature): Cost of Goods Sold = raw materials,
    consumables used and changes in inventories + employee benefits + depreciation & amortisation,
    and Gross Profit = Revenue - Cost of Goods Sold."""
    if any(v is not None for v in values.get("cost_of_sales", [])):
        return
    col = lambda k: values.get(k) or [None] * n  # noqa: E731
    rm, eb, da = (col(k) for k in COGS_BY_NATURE)
    cogs = [None if rm[i] is None and eb[i] is None
            else round(-sum(abs(x) for x in (rm[i], eb[i], da[i]) if x is not None), 3)
            for i in range(n)]
    if all(c is None for c in cogs):
        return
    values["cost_of_sales"] = cogs
    labels["cost_of_sales"] = ("computed (no cost of sales reported): raw materials, consumables used and "
                               "changes in inventories + employee benefits + depreciation and amortisation")
    if not any(v is not None for v in values.get("gross_profit", [])):
        values["gross_profit"] = [None if r is None or c is None else round(r + c, 3)
                                  for r, c in zip(col("revenue"), cogs)]
        labels["gross_profit"] = "computed: revenue - derived cost of goods sold"


def _derive_bank(st: str, values: dict, labels: dict, n: int) -> None:
    """Bank totals some filings don't print (Indian banks rarely print equity or total liabilities)."""
    col = lambda k: values.get(k) or [None] * n  # noqa: E731
    has = lambda k: any(v is not None for v in values.get(k, []))  # noqa: E731

    def derive(key, label, fn):
        vals = [fn(i) for i in range(n)]
        if not has(key) and any(v is not None for v in vals):
            values[key] = [None if v is None else round(v, 3) for v in vals]
            labels[key] = f"computed: {label}"

    def total(*keys):
        return lambda i: None if col(keys[0])[i] is None else sum(col(k)[i] or 0 for k in keys)

    if st == "income_statement":
        derive("net_interest_income", "interest income + interest expense",
               lambda i: None if col("interest_income")[i] is None or col("interest_expense")[i] is None
               else col("interest_income")[i] + col("interest_expense")[i])
    if st == "balance_sheet":
        derive("shareholders_equity", "share capital + reserves (+ perpetual securities, treasury shares)",
               lambda i: None if col("share_capital")[i] is None or col("reserves")[i] is None
               else total("share_capital", "reserves", "perpetual_securities", "treasury_shares")(i))
        derive("total_equity", "shareholders' equity + minority interests", total("shareholders_equity", "minority_interests"))
        derive("total_liabilities", "total assets - total equity",
               lambda i: None if col("total_assets")[i] is None or col("total_equity")[i] is None
               else col("total_assets")[i] - col("total_equity")[i])


def _apply_formulas(st: str, rows: list, values: dict, labels: dict, n: int) -> None:
    """Rows with a formula ("x + y") are calculated, top to bottom, where the filing printed nothing.
    A missing input counts as nothing; a period with no inputs at all stays blank."""
    refs = T.formula_refs(st, rows)
    for r in rows:
        if not r.formula or any(v is not None for v in values.get(r.key, [])):
            continue
        out = []
        for i in range(n):
            total, seen = 0.0, False
            for sign, ref in T.formula_terms(r.formula):
                if re.fullmatch(r"\d+(?:\.\d+)?", ref):
                    total += sign * float(ref)
                    continue
                key = refs.get(ref) or refs.get(T.normalise_label(ref))
                v = (values.get(key) or [None] * n)[i] if key else None
                if v is not None:
                    total, seen = total + sign * v, True
            out.append(round(total, 3) if seen else None)
        if any(v is not None for v in out):
            values[r.key] = out
            labels[r.key] = f"formula: {r.formula}"


def iso_date(text: str) -> str:
    """2026-09-30 / 30.09.2026 / 30/09/2026 / 30 Sep 2026 / Sep 30, 2026 -> 2026-09-30 (else unchanged)."""
    s = str(text or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                "%d.%m.%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _row_values(row: dict, cols: list[int]) -> list:
    """A row's figures in period order: from explicit {col, value} cells, or a plain list (older answers)."""
    if "cells" in row:
        by_col = {c.get("col"): c.get("value") for c in row.get("cells") or [] if isinstance(c, dict)}
        return [by_col.get(c) for c in cols]
    vals = list(row.get("values") or [])
    return (vals + [None] * len(cols))[:len(cols)]


def normalise(data: dict, formats: dict | None = None) -> dict:
    """Rescale to the template's output unit ('000) and key rows by template key.

    Returns {statement: {"periods": [...], "values": {key: [..]}, "labels": {key: str}, "format": str}}.
    """
    formats = formats or T.BUILTIN
    fmt = data.get("format") if data.get("format") in formats else ("general" if "general" in formats else next(iter(formats)))
    factor = UNIT_FACTORS.get(data.get("unit") or DEFAULT_UNIT, UNIT_FACTORS[DEFAULT_UNIT]) / T.OUTPUT_UNIT
    out = {}
    for st in T.STATEMENTS:
        block = data.get(st) or {"periods": [], "rows": []}
        periods = []
        for i, p in enumerate(block.get("periods") or [], 1):
            months = 0 if st == "balance_sheet" else int(p.get("months") or 0)
            periods.append({**p, "col": p.get("col") or i, "period_end": iso_date(p.get("period_end")), "months": months})
        cols = [p["col"] for p in periods]
        n = len(periods)
        values, labels = {}, {}
        for row in block.get("rows") or []:
            vals = _row_values(row, cols)
            if row["key"] not in ("eps_basic", "eps_diluted"):
                vals = [None if v is None else round(v * factor, 3) for v in vals]
            if row["key"] in values:  # the same line twice: keep the first figure per column
                vals = [a if a is not None else b for a, b in zip(values[row["key"]], vals)]
            values[row["key"]] = vals
            labels.setdefault(row["key"], row["source_label"])
        # Derived lines work by meaning (key), whatever the format calls them; each only fills a
        # line the filing didn't print, from parts the filing did print.
        _derive_bank(st, values, labels, n)
        if st == "income_statement":
            _derive_gross_profit(values, labels, n)
        _apply_formulas(st, T.rows_for(st, fmt, formats), values, labels, n)
        if st == "cash_flow" and "cf_net_operating" in values:
            capex = values.get("cf_capex", [None] * n)
            values["free_cash_flow"] = [
                None if o is None else round(o + (c or 0), 3)
                for o, c in zip(values["cf_net_operating"], capex)
            ]
            labels["free_cash_flow"] = "computed: net operating cash flow + capex"
        out[st] = {"periods": periods, "values": values, "labels": labels, "format": fmt}
    return out
