"""AI step: read the selected statement pages and return numbers mapped to the template.

Two providers, picked from the environment (override with AI_PROVIDER=gemini|anthropic):
  Gemini   GEMINI_API_KEY      model GEMINI_MODEL, else the first available of GEMINI_MODELS
  Claude   ANTHROPIC_API_KEY   model CLAUDE_MODEL, else claude-opus-5-5
Both get the same prompt, the same PDF excerpt and the same JSON schema.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import template as T

CLAUDE_DEFAULT = "claude-opus-5-5"
# Flash-Lite only: its free daily request allowance is far larger than Flash's. Tried in order
# when a model is missing for the key or its quota is used up (each model has its own quota).
# Set GEMINI_MODEL to pin one, e.g. gemini-3.1-pro-preview for the hardest filings.
GEMINI_MODELS = ["gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]
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


def provider() -> str | None:
    """Which AI provider this deployment uses, or None if no key is configured."""
    choice = os.environ.get("AI_PROVIDER", "").strip().lower()
    if choice in ("gemini", "google"):
        return "gemini"
    if choice in ("anthropic", "claude"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def default_model(prov: str | None = None) -> str:
    prov = prov or provider()
    if prov == "gemini":
        return os.environ.get("GEMINI_MODEL") or GEMINI_MODELS[0]
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


def _instruction(pdf: Path, pages: list[int]) -> str:
    return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) were selected as the "
            "income statement, balance sheet and cash flow statement. Fill the template.")


def extract(pdf: Path, pages: list[int], model: str | None = None, effort: str = "high",
            cache_dir: Path | None = None, client=None) -> Extraction:
    """Send only `pages` of `pdf` to the configured provider and return the parsed JSON."""
    prov = provider()
    if prov is None:
        raise RuntimeError("No AI key configured: set GEMINI_API_KEY or ANTHROPIC_API_KEY")
    model = model or default_model(prov)
    pdf_bytes = excerpt_pdf(pdf, pages)
    cache_key = hashlib.sha256(pdf_bytes + f"{prov}:{model}".encode() + SYSTEM.encode()).hexdigest()[:16]
    cache_file = (cache_dir or pdf.parent) / f"{pdf.stem}.{cache_key}.extract.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        return Extraction(cached["data"], cached["model"], cached.get("usage", {}))

    run = _gemini if prov == "gemini" else _claude
    ex = run(pdf_bytes, _instruction(pdf, pages), model, effort, client)
    cache_file.write_text(json.dumps({"data": ex.data, "model": ex.model, "usage": ex.usage}, indent=2))
    return ex


# ------------------------------------------------------------------ Claude
def _claude(pdf_bytes: bytes, instruction: str, model: str, effort: str, client) -> Extraction:
    import anthropic

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


def _gemini(pdf_bytes: bytes, instruction: str, model: str, effort: str, client) -> Extraction:
    from google import genai
    from google.genai import errors, types

    client = client or genai.Client(
        api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
        http_options=types.HttpOptions(timeout=280_000),
    )
    pinned = bool(os.environ.get("GEMINI_MODEL"))
    candidates = [model] + ([] if pinned else [m for m in GEMINI_MODELS if m != model])
    schema = gemini_schema(SCHEMA)
    contents = [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), instruction]

    def config(thinking: bool):
        return types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=65536,
            thinking_config=types.ThinkingConfig(thinking_level=GEMINI_THINKING.get(effort, "MEDIUM"))
            if thinking else None,
        )

    last_err: Exception | None = None
    for name in candidates:
        thinking = not name.startswith("gemini-2")  # 2.x models use thinking budgets, not levels
        try:
            try:
                resp = client.models.generate_content(model=name, contents=contents, config=config(thinking))
            except errors.ClientError as e:
                if e.code != 400 or not thinking:
                    raise
                # Some models reject a thinking level: retry once with the model's default.
                resp = client.models.generate_content(model=name, contents=contents, config=config(False))
        except errors.ClientError as e:
            if e.code in (404, 429):
                last_err = e
                continue  # not available for this key, or its quota is used up: try the next one
            raise
        return _gemini_result(resp, name)
    if getattr(last_err, "code", None) == 429:
        raise RuntimeError(f"Gemini quota used up on {', '.join(candidates)}. "
                           "Free-tier limits reset daily; try again later or set GEMINI_MODEL "
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
