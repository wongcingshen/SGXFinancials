"""AI step: read the selected statement pages and return numbers mapped to the template.

Two providers, picked from the environment (override with AI_PROVIDER=gemini|anthropic):
  Gemini   GEMINI_API_KEY      model SGX_GEMINI_MODEL, else the first available of GEMINI_MODELS
           (GOOGLE_API_KEY, GOOGLE_GENERATIVE_AI_API_KEY or GEMINI_KEY also work)
  Claude   ANTHROPIC_API_KEY   model CLAUDE_MODEL, else claude-opus-5-5
Both get the same prompt, the same PDF excerpt and the same JSON schema.

Scanned / photographed filings are first transcribed by sgx_fin.pictures (tables copied as printed,
checked, doubtful rows re-read); extract_text() then fills the template from that text, the same
way as for a text filing. extract(vision=True) reads picture pages directly when that fails.
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

from . import checks, industries
from . import template as T

CLAUDE_DEFAULT = "claude-opus-5-5"
# Flash-Lite only: its free daily request allowance is far larger than Flash's. Tried in order
# when a model is missing for the key or its quota is used up (each model has its own quota).
# Set SGX_GEMINI_MODEL to pin one, e.g. gemini-3.1-pro-preview for the hardest filings. (Not
# GEMINI_MODEL: other apps sharing the Vercel team's variables, such as TravelFriend, use that.)
GEMINI_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_KEY")
GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]
# Picture filings are few and hard to read: a stronger model reads their 2-6 statement pages, with
# Flash-Lite behind it when Flash's smaller daily quota is used up. SGX_VISION_MODEL pins one.
VISION_MODELS = ["gemini-3.5-flash", "gemini-flash-latest"]
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


COGS_METHODS = ("printed", "by_nature", "judged", "none")


def _cogs_basis_schema() -> dict:
    return {
        "type": "object",
        "description": "How the cost of sales (COGS) is made up",
        "properties": {
            "method": {"type": "string", "enum": list(COGS_METHODS)},
            "lines": {
                "type": "array",
                "description": "No cost of sales printed: every expense line on the face of the income statement above "
                               "profit from operations, with its figures in the income statement's columns",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_label": {"type": "string"},
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"col": {"type": "integer"}, "value": {"type": "number"}},
                                "required": ["col", "value"],
                                "additionalProperties": False,
                            },
                        },
                        "in_cogs": {"type": "boolean"},
                        "why": {"type": "string"},
                    },
                    "required": ["source_label", "cells", "in_cogs", "why"],
                    "additionalProperties": False,
                },
            },
            "explanation": {"type": "string", "description": "what the COGS is made of, what was left out and why"},
        },
        "required": ["method", "lines", "explanation"],
        "additionalProperties": False,
    }


def build_schema(formats: dict | None = None, rules: dict | None = None) -> dict:
    formats = formats or T.BUILTIN
    return {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "format": {"type": "string", "enum": list(formats),
                   "description": "id of the format whose 'use for' fits this company"},
        "industry": {"type": "string", "enum": industries.ids(rules or industries.for_filing()),
                     "description": "the company's industry, from the list in the rules"},
        "industry_evidence": {"type": "string", "description": "the words in the filing that show the industry"},
        "currency": {"type": "string", "description": "ISO code, e.g. SGD, USD, RMB, INR"},
        "unit": {"type": "string", "enum": list(UNIT_FACTORS)},
        "basis": {"type": "string", "description": "Group (consolidated), Standalone or Company"},
        "income_statement": _statement_schema("income_statement", formats),
        "balance_sheet": _statement_schema("balance_sheet", formats),
        "cash_flow": _statement_schema("cash_flow", formats),
        "cost_of_sales_basis": _cogs_basis_schema(),
        "notes": {"type": "string"},
    },
    "required": ["company_name", "format", "industry", "industry_evidence", "currency", "unit", "basis",
                 "income_statement", "balance_sheet", "cash_flow", "cost_of_sales_basis", "notes"],
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
- `industry`: the company's industry from the list at the end, judged by what it sells and how it earns its revenue; `industry_evidence`: the words in the filing that show it (e.g. "sale of precision parts; raw materials and consumables used").
- Gross profit must never end up blank, and how the cost of sales (COGS, the gross expense) is made up must be shown in `cost_of_sales_basis`:
  - a cost of sales / cost of revenue line is printed: map it to cost_of_sales (and gross_profit if printed); method "printed", lines [], explanation e.g. "Cost of sales as printed".
  - none is printed: leave cost_of_sales and gross_profit out. List in `lines` EVERY expense line on the face of the income statement above profit from operations (not finance costs, tax or share of associates), each with its figures in `cells` under the income statement's columns, with the sign it has on profit (costs negative, an increase in inventories positive). Mark `in_cogs` by the industry's COGS rule (a rule the user gave for this company comes first) and say `why` in a few words. Materials always go in: raw materials, consumables, purchases, changes in inventories, cost of inventories. Do not add them up: the program adds the lines marked in_cogs and works out gross profit = revenue + COGS. method "by_nature" when the statement lists expenses by nature, else "judged"; `explanation`: one or two sentences on what the COGS is made of, what was left out and why.
  - nothing on the statement is a cost of earning the revenue (e.g. an investment holding company's dividends): mark no line in_cogs, method "none", and explain; gross profit then equals revenue.
  - expenses by nature (general format): still fill raw_materials, employee_benefits and depreciation_amortisation from the face of the statement.
  - bank format: gross income (revenue) is interest income + non-interest income ('Total Income' when printed as one line); interest expense, operating expenses and allowances go to their own keys as well as into `lines`.
- Other totals (net interest income, total assets, net cash from operating activities...) must be the printed totals, not your own arithmetic, when they are printed; leave them out when not printed.
- `page_numbers` are the page numbers within the attached excerpt (1-based) the statement came from.
- Put caveats (restatements, unusual presentation, standalone figures used) in `notes`.

Template keys:
"""


def build_system(formats: dict | None = None, rules: dict | None = None) -> str:
    return RULES + _template_text(formats or T.BUILTIN) + "\n\n" + industries.prompt(rules)


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


def vision_models() -> list[str]:
    """Gemini models for reading picture pages, in order."""
    pinned = os.environ.get("SGX_VISION_MODEL", "").strip() or os.environ.get("SGX_GEMINI_MODEL", "").strip()
    return [pinned] if pinned else list(dict.fromkeys(VISION_MODELS + GEMINI_MODELS))


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


def render_jpegs(pdf: Path, pages: list[int], long_side: int, quality: int = 80) -> list[bytes]:
    """The given 1-based pages as JPEG bytes, scaled so the longer side is `long_side` px."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    try:
        out = []
        for p in pages:
            page = doc[p - 1]
            w, h = page.get_size()
            img = page.render(scale=long_side / max(w, h)).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality)
            out.append(buf.getvalue())
        return out
    finally:
        doc.close()


def _instruction(pdf: Path, pages: list[int], vision: bool = False, locate: bool = False) -> str:
    if locate and not vision:
        return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) may contain more than the "
                "statements: find the income statement, balance sheet and cash flow statement among them "
                "(the Group's primary statements; ignore commentary, notes, segment and ratio tables), give "
                "each statement's page_numbers, and fill the template. Leave out any statement that isn't there.")
    if vision and locate:
        return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) include scanned or "
                "photographed pages, so read them visually. They contain more than the statements: find the "
                "income statement, balance sheet and cash flow statement among them (ignore commentary, "
                "notes and other pages), give each statement's page_numbers, and fill the template. If a "
                "picture is too unclear to read a figure with confidence, leave that figure out and say so in notes.")
    if vision:
        return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) are scanned or photographed "
                "pages that hold the statements. Read them visually, line by line and digit by digit: follow each "
                "line across to its own figures, keep every figure under its own column heading, and read the "
                "sign from the brackets. If a figure is too unclear to read with confidence, leave it out and say "
                "so in notes.")
    return (f"These {len(pages)} pages (original pages {pages} of {pdf.name}) were selected as the "
            "income statement, balance sheet and cash flow statement. Fill the template.")


RECHECK = ("\n\nA first reading of these pages did not add up:\n{fails}\nRead the lines involved again, digit by "
           "digit, and check each figure's column, sign and template line (e.g. a total taken for a sub-total). Then "
           "return the whole template again with the figures exactly as printed.")
GAPS = ("\n\nA first reading of these pages left gross profit blank:\n{gaps}\nReturn the whole template again, "
        "figures exactly as printed, with cost_of_sales filled.")


def extract(pdf: Path, pages: list[int], model: str | None = None, effort: str = "high",
            cache_dir: Path | None = None, client=None, vision: bool = False,
            locate: bool = False, deadline: float | None = None, formats: dict | None = None,
            context: str = "", recheck: str = "", gaps: str = "", cogs: dict | None = None) -> Extraction:
    """Send only `pages` of `pdf` to the AI and return the parsed JSON.

    Picture pages (vision=True) go to the main provider as sharp images (Gemini: Flash, then
    Flash-Lite), with GLM 5.3 Flash behind it when GLM_FLASH is set; everything else goes to the
    main provider as a PDF excerpt, with DeepSeek Flash then GLM Flash (NVIDIA, GLM_FLASH key)
    reading the pages' text layer when it fails (a 503, quota used up, ...). `context` is added to the instruction (what the pages are);
    `recheck` lists checks a first reading failed, and `gaps` where it left gross profit blank, for a
    second look. `cogs`: the industries and COGS rules for this filing (industries.for_filing). `deadline`
    (time.monotonic()) bounds the whole step.
    """
    from . import glm

    prov = provider()
    deadline = deadline or time.monotonic() + 280
    attempts = [prov] if prov else []
    if vision and glm.api_key() and not model:
        attempts.append("glm")
    elif not vision and glm.api_key() and not model:
        attempts.append("text")  # Gemini down or out of quota: DeepSeek / GLM Flash read the text layer
    if not attempts:
        raise RuntimeError("No AI key configured: set GEMINI_API_KEY (or GLM_FLASH, an NVIDIA or Z.ai key)")

    formats = formats or T.BUILTIN
    system, schema = build_system(formats, cogs), build_schema(formats, cogs)
    pdf_bytes = excerpt_pdf(pdf, pages)
    instruction = _instruction(pdf, pages, vision, locate) + (f"\n\n{context}" if context else "")
    if recheck:
        instruction += RECHECK.format(fails=recheck)
    if gaps:
        instruction += GAPS.format(gaps=gaps)
    failures = []
    for who in attempts:
        if who in ("glm", "text") and failures and deadline - time.monotonic() < 45:
            failures.append(f"{_who_label(who)}: skipped, not enough time left")
            break
        name = (glm.label() if who == "glm" else glm.text_label() if who == "text"
                else model or (vision_models()[0] if vision and who == "gemini" else default_model(who)))
        cache_key = hashlib.sha256(pdf_bytes + f"{who}:{name}:{vision}:{locate}".encode()
                                   + system.encode() + instruction.encode()).hexdigest()[:16]
        cache_file = (cache_dir or pdf.parent) / f"{pdf.stem}.{cache_key}.extract.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            return Extraction(cached["data"], cached["model"], cached.get("usage", {}))
        try:
            if who == "glm":
                data, used, usage = glm.extract(pdf, pages, system, instruction, deadline=deadline, formats=formats)
                ex = Extraction(data, f"{used} via {'NVIDIA' if glm.is_nvidia() else 'Z.ai'}",
                                {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})
            elif who == "text":
                text = TEXT_LAYER.format(pages=_pages_text(pdf, pages)) + instruction
                answer, used, usage = glm.ask(system + "\n\n" + glm.SKELETON, text, None, deadline, glm.text_models())
                ex = Extraction(glm.coerce(answer, formats), f"{used} via {'NVIDIA' if glm.is_nvidia() else 'Z.ai'}",
                                {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})
            elif who == "gemini" and vision:
                # A few pages: big images at the finest resolution; a long shortlist: smaller ones.
                few = len(pages) <= 8
                images = [(f"Page {i}:", jpg) for i, jpg in
                          enumerate(render_jpegs(pdf, pages, 2400 if few else 1600, 85 if few else 75), 1)]
                ex = _gemini(None, instruction, name, effort, client, deadline=deadline, system=system,
                             schema_json=schema, images=images, candidates=None if model else vision_models(),
                             resolution="MEDIA_RESOLUTION_HIGH",
                             part_resolution="MEDIA_RESOLUTION_ULTRA_HIGH" if few else None)
            elif who == "gemini":
                ex = _gemini(pdf_bytes, instruction, name, effort, client, deadline=deadline, system=system,
                             schema_json=schema)
            else:
                ex = _claude(pdf_bytes, instruction, name, effort, client, system, schema)
        except Exception as e:  # try the next provider, report all if none works
            failures.append(f"{_who_label(who)}: {e}")
            continue
        if failures:
            ex.data["notes"] = (f"{ex.data.get('notes', '')} (Read by {ex.model} after: {'; '.join(failures)})").strip()
        cache_file.write_text(json.dumps({"data": ex.data, "model": ex.model, "usage": ex.usage}, indent=2))
        return ex
    raise RuntimeError(" | ".join(failures))


def _who_label(who: str) -> str:
    from . import glm
    return glm.label() if who == "glm" else glm.text_label() if who == "text" else provider_label(who)


TEXT_LAYER = ("The PDF's own text for these pages follows (text models can't see the PDF itself). Table rows "
              "keep their line order, but columns may run together: use the column headings and the printed "
              "totals to place each figure under its own period.\n\n{pages}\n\n")
TEXT_LIMIT = 150_000  # characters of page text sent to a text model


def _pages_text(pdf: Path, pages: list[int]) -> str:
    """The text layer of the given 1-based pages, each under a "=== Page n ===" heading."""
    from .page_scanner import page_texts

    texts = page_texts(pdf)
    out = "\n\n".join(f"=== Page {p} ===\n{texts[p - 1].strip()}" for p in pages if 1 <= p <= len(texts))
    return out[:TEXT_LIMIT]


TABLES_INSTRUCTION = """These tables were copied from pictures of pages {pages} of {name}, figures exactly as printed (brackets and minus signs are negatives; "-" is nil; "?" was unreadable: leave it out).

{tables}
Fill the template from them, as you would from the statements themselves. `periods` lists the columns you used with their numbers as in [brackets]; each figure goes in `cells` under that column number. When there are consolidated and standalone tables, use the consolidated ones. `page_numbers` are the page numbers in the table titles. Lines printed as deductions ("Less: ...", "Provision for taxes", "Exceptional items" in "(7-8-9)") are charges: give them the sign they have on profit."""


def extract_text(pdf: Path, pages: list[int], tables: str, model: str | None = None, effort: str = "high",
                 cache_dir: Path | None = None, client=None, deadline: float | None = None,
                 formats: dict | None = None, recheck: str = "", gaps: str = "", cogs: dict | None = None) -> Extraction:
    """Fill the template from tables already transcribed as text (picture filings): no pictures,
    so the main provider's text model maps them (GLM when there is no other key)."""
    from . import glm

    prov = provider()
    deadline = deadline or time.monotonic() + 120
    formats = formats or T.BUILTIN
    system, schema = build_system(formats, cogs), build_schema(formats, cogs)
    instruction = TABLES_INSTRUCTION.format(pages=pages, name=pdf.name, tables=tables)
    if recheck:
        instruction += RECHECK.format(fails=recheck).replace("Read the lines involved again, digit by digit, and check",
                                                             "Look at the lines involved again and check")
    if gaps:
        instruction += GAPS.format(gaps=gaps)
    attempts = ([prov] if prov else []) + (["glm"] if glm.api_key() else [])
    if not attempts:
        raise RuntimeError("No AI key configured: set GEMINI_API_KEY")
    failures = []
    for who in attempts:
        name = glm.label() if who == "glm" else (model or default_model(who))
        key = hashlib.sha256(f"{who}:{name}".encode() + system.encode() + instruction.encode()).hexdigest()[:16]
        cache_file = (cache_dir or pdf.parent) / f"{pdf.stem}.{key}.map.json"
        if cache_file.exists():
            c = json.loads(cache_file.read_text())
            return Extraction(c["data"], c["model"], c.get("usage", {}))
        try:
            if who == "glm":
                answer, used, usage = glm.ask(system + "\n\n" + glm.SKELETON, instruction, None, deadline)
                ex = Extraction(glm.coerce(answer, formats), f"{used} via {'NVIDIA' if glm.is_nvidia() else 'Z.ai'}",
                                {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})
            elif who == "gemini":
                ex = _gemini(None, instruction, name, effort, client, deadline=deadline, system=system, schema_json=schema)
            else:
                ex = _claude(None, instruction, name, effort, client, system, schema)
        except Exception as e:
            failures.append(f"{glm.label() if who == 'glm' else provider_label(who)}: {e}")
            continue
        cache_file.write_text(json.dumps({"data": ex.data, "model": ex.model, "usage": ex.usage}, indent=2))
        return ex
    raise RuntimeError(" | ".join(failures))


# ------------------------------------------------------------------ Claude
def _claude(pdf_bytes: bytes | None, instruction: str, model: str, effort: str, client,
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
            "content": ([{"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode()}}] if pdf_bytes else [])
            + [{"type": "text", "text": instruction}],
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


def _gemini(pdf_bytes: bytes | None, instruction: str, model: str, effort: str, client, vision: bool = False,
            deadline: float | None = None, system: str = SYSTEM, schema_json: dict = SCHEMA,
            images: list[tuple[str, bytes]] | None = None, candidates: list[str] | None = None,
            resolution: str | None = None, part_resolution: str | None = None) -> Extraction:
    """One Gemini call: a PDF excerpt, or labelled JPEG pages (`images`). `resolution` is the media
    resolution for the request (vision=True means high); `part_resolution` a finer one set on each
    image (e.g. ULTRA_HIGH, which needs the v1alpha API). Settings a model rejects are dropped in turn."""
    from google import genai
    from google.genai import errors, types

    timeout = int(max(30.0, min(280.0, (deadline or time.monotonic() + 280) - time.monotonic())) * 1000)
    if client is None:
        client = genai.Client(api_key=gemini_key(), http_options=types.HttpOptions(timeout=timeout))
        alpha = (genai.Client(api_key=gemini_key(), http_options=types.HttpOptions(timeout=timeout, api_version="v1alpha"))
                 if part_resolution else None)
    else:
        alpha = client
    if candidates is None:
        pinned = bool(os.environ.get("SGX_GEMINI_MODEL", "").strip())
        candidates = [model] + ([] if pinned else [m for m in GEMINI_MODELS if m != model])
    candidates = list(dict.fromkeys([model, *candidates] if model not in candidates else candidates))
    schema = gemini_schema(schema_json)
    resolution = resolution or ("MEDIA_RESOLUTION_HIGH" if vision else None)

    def contents(fine: bool):
        if images is None:
            return ([types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")] if pdf_bytes else []) + [instruction]
        parts = []
        for label, jpg in images:
            parts.append(types.Part.from_text(text=label))
            part = types.Part.from_bytes(data=jpg, mime_type="image/jpeg")
            if fine:
                part.media_resolution = types.PartMediaResolution(level=part_resolution)
            parts.append(part)
        parts.append(types.Part.from_text(text=instruction))
        return [types.Content(role="user", parts=parts)]

    def config(thinking: bool, res: str | None):
        return types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=65536,
            thinking_config=types.ThinkingConfig(thinking_level=GEMINI_THINKING.get(effort, "MEDIUM")) if thinking else None,
            media_resolution=res,
        )

    last_err: Exception | None = None
    for name in candidates:
        thinking = not name.startswith("gemini-2")  # 2.x models use thinking budgets, not levels
        # Finest settings first; on a 400 (setting not supported) step down to the model's defaults.
        variants = []
        if part_resolution and images is not None:
            variants.append((alpha, True, thinking, None))
        if resolution or thinking:
            variants.append((client, False, thinking, resolution))
        variants.append((client, False, False, None))
        try:
            for n, (cl, fine, think, res) in enumerate(variants):
                try:
                    resp = cl.models.generate_content(model=name, contents=contents(fine), config=config(think, res))
                    break
                except errors.ClientError as e:
                    if e.code != 400 or n == len(variants) - 1:
                        raise
        except errors.ClientError as e:
            if e.code in (404, 429):
                last_err = e
                continue  # not available for this key, or its quota is used up: try the next one
            raise
        except errors.ServerError as e:
            if e.code in (500, 502, 503, 504):
                last_err = e
                continue  # overloaded or down on Google's side: another model may still answer
            raise
        return _gemini_result(resp, name)
    if getattr(last_err, "code", None) == 429:
        raise RuntimeError(f"Gemini quota used up on {', '.join(candidates)}. "
                           "Free-tier limits reset daily; try again later or set SGX_GEMINI_MODEL "
                           f"to another model. ({last_err})")
    if getattr(last_err, "code", None) in (500, 502, 503, 504):
        raise RuntimeError(f"Gemini unavailable on {', '.join(candidates)} ({last_err})")
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


def _derive_gross_profit(values: dict, labels: dict, n: int, by_formula: bool = False) -> None:
    """Gross profit, never left blank when it can be worked out.

    Cost of Goods Sold (the gross expense) is the printed line or the AI's judged sum of cost lines;
    without either, when expenses are presented by nature, raw materials, consumables used and changes
    in inventories + employee benefits + depreciation and amortisation. Gross Profit = Revenue + Cost of
    Goods Sold wherever none is printed (unless the format has a formula of its own for it)."""
    has = lambda k: any(v is not None for v in values.get(k, []))  # noqa: E731
    col = lambda k: values.get(k) or [None] * n  # noqa: E731
    if not has("cost_of_sales"):
        rm, eb, da = (col(k) for k in COGS_BY_NATURE)
        cogs = [None if rm[i] is None and eb[i] is None
                else round(-sum(abs(x) for x in (rm[i], eb[i], da[i]) if x is not None), 3)
                for i in range(n)]
        if any(c is not None for c in cogs):
            values["cost_of_sales"] = cogs
            labels["cost_of_sales"] = ("computed (no cost of sales reported): raw materials, consumables used and "
                                       "changes in inventories + employee benefits + depreciation and amortisation")
    if by_formula or has("gross_profit") or not has("cost_of_sales") or not has("revenue"):
        return
    cogs = values["cost_of_sales"]
    if any(c is not None and c > 0 for c in cogs):  # a cost read without its brackets
        values["cost_of_sales"] = cogs = [None if c is None else -abs(c) for c in cogs]
        labels["cost_of_sales"] = f"{labels.get('cost_of_sales', '')} (sign corrected: a cost)".strip()
    gp = [None if r is None or c is None else round(r + c, 3) for r, c in zip(col("revenue"), cogs)]
    if any(v is not None for v in gp):
        values["gross_profit"] = gp
        labels["gross_profit"] = "computed: revenue + cost of goods sold (the gross expense)"


def _derive_bank_gross(values: dict, labels: dict, n: int) -> None:
    """A bank's gross lines, which banks rarely print: Gross Income = interest income + fee and other
    non-interest income; Gross Expense = interest expense + operating expenses + allowances, the costs of
    earning that income. A bank that prints no gross interest income gets the same gross profit from its
    net total operating income less operating expenses and allowances."""
    has = lambda k: any(v is not None for v in values.get(k, []))  # noqa: E731
    if not (has("interest_income") or has("total_operating_income")):
        return  # not a bank's income statement
    col = lambda k: values.get(k) or [None] * n  # noqa: E731
    ii, ie, fee, oth, toi, opex, allw = (col(k) for k in (
        "interest_income", "interest_expense", "fee_income", "other_non_interest_income", "total_operating_income",
        "total_operating_expenses", "allowances"))
    rev, cost, net = [None] * n, [None] * n, False
    for i in range(n):
        if ii[i] is not None:
            rev[i], parts = round(ii[i] + (fee[i] or 0) + (oth[i] or 0), 3), (ie[i], opex[i], allw[i])
        elif toi[i] is not None:
            rev[i], parts, net = toi[i], (opex[i], allw[i]), True
        else:
            continue
        if opex[i] is not None:
            cost[i] = round(sum(x or 0 for x in parts), 3)
    if not has("revenue") and any(v is not None for v in rev):
        values["revenue"] = rev
        labels["revenue"] = ("computed: total operating income (no gross interest income printed)" if net
                             else "computed: interest income + non-interest income (gross income)")
    if not has("cost_of_sales") and any(v is not None for v in cost):
        values["cost_of_sales"] = cost
        labels["cost_of_sales"] = ("computed: operating expenses + allowances" if net
                                   else "computed: interest expense + operating expenses + allowances (gross expense)")


def _cogs_basis(data: dict, periods: list[dict], factor: float, rules: dict | None) -> dict:
    """How the COGS is made up, as the AI gave it: the industry (the user's rule for the company first,
    else the AI's answer, else what the filing's own words point to), the method, and every expense
    line with its figures ('000), whether it is in COGS and why."""
    rules = rules or industries.for_filing()
    raw = data.get("cost_of_sales_basis") if isinstance(data.get("cost_of_sales_basis"), dict) else {}
    cols = [p["col"] for p in periods]
    lines = []
    for ln in raw.get("lines") or []:
        if isinstance(ln, dict):
            vals = [None if v is None else round(v * factor, 3) for v in _row_values(ln, cols)]
            lines.append({"label": str(ln.get("source_label") or "").strip() or "(unlabelled line)", "values": vals,
                          "in_cogs": bool(ln.get("in_cogs")), "why": str(ln.get("why") or "").strip()})
    words = [str(r.get("source_label") or "") for st in ("income_statement", "balance_sheet")
             for r in (data.get(st) or {}).get("rows") or [] if isinstance(r, dict)]
    hint = industries.guess([*words, *(ln["label"] for ln in lines), str(data.get("company_name") or "")], rules)
    company = rules.get("company") or {}
    given = str(data.get("industry") or "").strip().lower()
    if company.get("industry"):
        iid, evidence, by = company["industry"], f"your rule for {company['match']}", "you"
    elif given in industries.ids(rules):
        iid, evidence, by = given, str(data.get("industry_evidence") or "").strip(), "AI"
    elif hint:
        iid, evidence, by = hint[0], "its lines: " + ", ".join(hint[1]), "the filing's words"
    else:
        iid, evidence, by = industries.FALLBACK, "", "default"
    method = str(raw.get("method") or "").strip().lower()
    basis = {"industry": iid, "industry_name": industries.name(rules, iid), "evidence": evidence, "decided_by": by,
             "rule": industries.rule_used(rules, iid), "method": method if method in COGS_METHODS else "",
             "explanation": str(raw.get("explanation") or "").strip(),
             "periods": [{"period_end": p["period_end"], "months": p["months"], "label": p.get("label", "")} for p in periods],
             "lines": lines}
    if hint and hint[0] != iid and by == "AI":
        basis["also_suggests"] = f"{industries.name(rules, hint[0])} ({', '.join(hint[1])})"
    return basis


WORKED_OUT = ("judged", "computed", "your choice", "formula")
# Depreciation, amortisation and depletion lines. FactSet's cost of revenue includes D&A and shows D&A on
# its own too, so a COGS made of lines says how much of it is D&A and what it is without.
DA_LINE = re.compile(r"depreciat|amorti[sz]|depletion", re.I)


def _apply_cogs(values: dict, labels: dict, n: int, basis: dict, prefix: str = "judged") -> None:
    """Cost of Goods Sold from the lines marked in the basis, where no cost of sales is printed: their
    sum; 0 when the basis says nothing is a cost of earning the revenue."""
    if any(v is not None for v in values.get("cost_of_sales", [])):
        return
    picked = [ln for ln in basis["lines"] if ln["in_cogs"]]
    if picked:
        cogs = [None if all(ln["values"][i] is None for ln in picked)
                else round(sum(ln["values"][i] or 0 for ln in picked), 3) for i in range(n)]
        label = f"{prefix} ({basis['industry_name']}): " + " + ".join(ln["label"] for ln in picked)
    elif basis["method"] == "none":
        cogs = [None if r is None else 0.0 for r in values.get("revenue") or [None] * n]
        label = f"{prefix} ({basis['industry_name']}): no direct cost of earning the revenue"
    else:
        return
    if any(c is not None for c in cogs):
        values["cost_of_sales"], labels["cost_of_sales"] = cogs, label


def _settle_basis(basis: dict, values: dict, labels: dict) -> None:
    """The basis records the COGS it ended in and how, whoever worked it out."""
    cogs = values.get("cost_of_sales")
    label = str(labels.get("cost_of_sales", ""))
    basis["cogs"] = list(cogs) if cogs else None
    basis["cogs_label"] = label
    basis["gross_profit_label"] = str(labels.get("gross_profit", ""))
    n, da = len(cogs or []), None
    if cogs and label.startswith(("judged", "your choice")):
        inside = [ln for ln in basis["lines"] if ln["in_cogs"] and DA_LINE.search(ln["label"])]
        if inside:
            da = [None if all(ln["values"][i] is None for ln in inside)
                  else round(sum(ln["values"][i] or 0 for ln in inside), 3) for i in range(n)]
    elif cogs and label.startswith("computed (no cost of sales reported)"):  # the by-nature sum includes D&A
        da = [None if d is None else -abs(d) for d in values.get("depreciation_amortisation") or [None] * n]
    if da and any(v is not None for v in da):
        basis["da_in_cogs"] = da
        basis["cogs_excl_da"] = [None if c is None else round(c - (d or 0), 3) for c, d in zip(cogs, da)]
    else:
        basis["da_in_cogs"] = basis["cogs_excl_da"] = None
    if cogs and not label.startswith(WORKED_OUT):
        basis["method"] = "printed"
        basis["explanation"] = basis["explanation"] or f"Cost of sales as printed: {label}."
    elif label.startswith("computed"):  # the program's own sum (expenses by nature, a bank's lines)
        basis["method"] = "program"
        basis["explanation"] = basis["explanation"] or ("Worked out by the program: " + label.split(":", 1)[-1].strip() + ".")
    elif not cogs:
        basis["method"] = basis["method"] or "missing"
        basis["explanation"] = basis["explanation"] or "No cost of sales is printed and none could be worked out."


def recompute_cogs(normalised: dict, picks: list, formats: dict | None = None) -> list[str]:
    """A user's correction of which lines are COGS for one filing: added up again, no AI. Worked-out
    cost of sales, gross profit and formula entries are worked out again; printed figures stay.
    Returns the lines now in COGS."""
    block = normalised.get("income_statement") or {}
    basis = block.get("cogs_basis")
    if not isinstance(basis, dict) or not basis.get("lines"):
        raise ValueError("This filing has no expense lines to choose from.")
    if basis.get("method") == "printed":
        raise ValueError("This filing prints its cost of sales; it is used as printed.")
    lines = basis["lines"]
    if not isinstance(picks, list) or len(picks) != len(lines):
        raise ValueError(f"Say in or out for each of the {len(lines)} lines.")
    n = len(block.get("periods") or [])
    values, labels = block.setdefault("values", {}), block.setdefault("labels", {})
    for ln, pick in zip(lines, picks):
        ln["in_cogs"] = bool(pick)
        ln["values"] = (list(ln.get("values") or []) + [None] * n)[:n]
    for k in list(values):
        if str(labels.get(k, "")).startswith(WORKED_OUT) and (k in ("cost_of_sales", "gross_profit")
                                                               or str(labels.get(k, "")).startswith("formula")):
            values.pop(k)
            labels.pop(k, None)
    chosen = [ln["label"] for ln in lines if ln["in_cogs"]]
    basis["method"] = "chosen" if chosen else "none"
    basis["explanation"] = ("Lines chosen by you: " + " + ".join(chosen) + "." if chosen
                            else "You marked no line as a cost of earning the revenue: gross profit equals revenue.")
    basis["rule"], basis["decided_by_cogs"] = "your choice for this filing", "you"
    _apply_cogs(values, labels, n, basis, prefix="your choice")
    rows = T.rows_for("income_statement", block.get("format"), formats)
    _derive_gross_profit(values, labels, n, by_formula=any(r.key == "gross_profit" and r.formula for r in rows))
    _apply_formulas("income_statement", rows, values, labels, n)
    _settle_basis(basis, values, labels)
    basis["method"] = "chosen" if chosen else "none"
    return chosen


def fill_gross_profit(first: dict, second: dict) -> list[str]:
    """When a first reading has no gross profit at all, take what a second reading found (matched by
    period): a printed gross profit, or the gross expense it judged, from which gross profit is worked
    out against the first reading's revenue. Changes `first` in place; returns the lines taken."""
    a, b = first.get("income_statement"), second.get("income_statement")
    if not a or not b or any(v is not None for v in a["values"].get("gross_profit", [])):
        return []
    n, nb = len(a["periods"]), len(b["periods"])
    where = {(p["period_end"], p["months"]): j for j, p in enumerate(b["periods"])}
    printed = not str(b["labels"].get("gross_profit", "")).startswith(("computed", "formula"))
    keys = ("revenue", "cost_of_sales", "gross_profit") if printed else ("revenue", "cost_of_sales")
    took = set()
    for i, p in enumerate(a["periods"]):
        j = where.get((p["period_end"], p["months"]))
        if j is None:
            continue
        for k in keys:
            v = (b["values"].get(k) or [None] * nb)[j]
            if v is not None and (a["values"].get(k) or [None] * n)[i] is None:
                a["values"].setdefault(k, [None] * n)[i] = v
                if k not in took and not any(x is not None for j2, x in enumerate(a["values"][k]) if j2 != i):
                    a["labels"][k] = b["labels"].get(k, k)
                took.add(k)
    if "gross_profit" not in took:
        _derive_gross_profit(a["values"], a["labels"], n)
    if took and b.get("cogs_basis"):
        a["cogs_basis"] = b["cogs_basis"]
        _settle_basis(a["cogs_basis"], a["values"], a["labels"])
    return sorted(took)


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

    def both(a, b):
        return lambda i: None if col(a)[i] is None or col(b)[i] is None else col(a)[i] + col(b)[i]

    if st == "income_statement":
        derive("net_interest_income", "interest income + interest expense", both("interest_income", "interest_expense"))
        derive("total_operating_income", "net interest income + non-interest income",
               lambda i: None if col("net_interest_income")[i] is None
               or (col("fee_income")[i] is None and col("other_non_interest_income")[i] is None)
               else total("net_interest_income", "fee_income", "other_non_interest_income")(i))
        derive("total_operating_expenses", "staff costs + other operating expenses",
               both("staff_costs", "other_operating_expenses_bank"))
        derive("operating_profit_pre_provisions", "total operating income + total operating expenses",
               both("total_operating_income", "total_operating_expenses"))
    if st == "balance_sheet":
        derive("shareholders_equity", "share capital + reserves (+ perpetual securities, treasury shares)",
               lambda i: None if col("share_capital")[i] is None or col("reserves")[i] is None
               else total("share_capital", "reserves", "perpetual_securities", "treasury_shares")(i))
        derive("total_equity", "shareholders' equity + minority interests", total("shareholders_equity", "minority_interests"))
        derive("total_liabilities", "total assets - total equity",
               lambda i: None if col("total_assets")[i] is None or col("total_equity")[i] is None
               else col("total_assets")[i] - col("total_equity")[i])


def _net_bank_income(values: dict, labels: dict, n: int, step: float) -> None:
    """An Indian bank's "Total Income" is interest earned + other income, before interest expended. When
    that gross figure was given as total operating income, replace it with the net figure."""
    toi, ii = values.get("total_operating_income"), values.get("interest_income")
    if not toi or not ii:
        return
    col = lambda k: values.get(k) or [None] * n  # noqa: E731
    ie, nii, fee, oth = col("interest_expense"), col("net_interest_income"), col("fee_income"), col("other_non_interest_income")
    fixed = False
    for i in range(n):
        if toi[i] is None or ii[i] is None:
            continue
        gross = ii[i] + (fee[i] or 0) + (oth[i] or 0)
        net = nii[i] if nii[i] is not None else (ii[i] - abs(ie[i]) if ie[i] is not None else None)
        if net is None or abs(gross - net) <= checks.tolerance([gross, net], step):
            continue
        if abs(toi[i] - gross) <= checks.tolerance([toi[i], gross], step):
            toi[i] = round(net + (fee[i] or 0) + (oth[i] or 0), 3)
            fixed = True
    if fixed:
        labels["total_operating_income"] = ("computed: net interest income + non-interest income (the filing's "
                                            "'Total Income' includes gross interest earned)")


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


def normalise(data: dict, formats: dict | None = None, cogs: dict | None = None) -> dict:
    """Rescale to the template's output unit ('000) and key rows by template key.

    Returns {statement: {"periods": [...], "values": {key: [..]}, "labels": {key: str}, "format": str}};
    the income statement also carries "cogs_basis": the industry and how its COGS was made up.
    `cogs`: the industries and COGS rules for this filing (industries.for_filing).
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
        # A sign read backwards is put right where the printed totals prove it, before anything is
        # derived from it.
        if st == "income_statement":
            _net_bank_income(values, labels, n, factor)
        checks.repair_signs(st, values, labels, n, factor)
        # Derived lines work by meaning (key), whatever the format calls them; each only fills a
        # line the filing didn't print, from parts the filing did print.
        _derive_bank(st, values, labels, n)
        rows = T.rows_for(st, fmt, formats)
        basis = None
        if st == "income_statement":
            # COGS: printed; else the lines the AI marked by the industry's rule; else the program's sums.
            basis = _cogs_basis(data, periods, factor, cogs)
            _apply_cogs(values, labels, n, basis)
            _derive_bank_gross(values, labels, n)
            _derive_gross_profit(values, labels, n, by_formula=any(r.key == "gross_profit" and r.formula for r in rows))
        _apply_formulas(st, rows, values, labels, n)
        if basis:
            _settle_basis(basis, values, labels)
        if st == "cash_flow" and "cf_net_operating" in values:
            capex = values.get("cf_capex", [None] * n)
            values["free_cash_flow"] = [
                None if o is None else round(o + (c or 0), 3)
                for o, c in zip(values["cf_net_operating"], capex)
            ]
            labels["free_cash_flow"] = "computed: net operating cash flow + capex"
        # step: output units per unit of the filing (100 for lakhs), for rounding tolerance in checks
        out[st] = {"periods": periods, "values": values, "labels": labels, "format": fmt, "step": factor}
        if basis:
            out[st]["cogs_basis"] = basis
    return out
