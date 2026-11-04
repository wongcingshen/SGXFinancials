"""MCP connector: lets Claude (claude.ai, the Claude apps, Claude Code) use SGX Financials as a tool.

Served by api/index.py at /api/mcp (MCP Streamable HTTP, stateless, JSON responses). Add it in
Claude under Settings -> Connectors -> Add custom connector with the URL the web app's "Add to
Claude" button copies: https://<your-app>/api/mcp?op=mcp&key=<APP_PASSWORD>.

Tools
  screen_sgx_results   list a date range's financial-results announcements on SGX
  open_filing          download a filing and run the keyword page scan (no AI). When the statements
                       are PICTURES (scans, photos), it returns the cleaned-up page images and the
                       transcription rules instead of calling Gemini: Claude reads them itself.
  submit_statements    Claude's reading, in the extraction JSON -> unit rescaling, sign repair,
                       derived lines, tie-out checks and an Excel download link. Failing checks
                       come back with the lines to read again.
  extract_filing       the server's own pipeline (Gemini / configured AI) for text filings

The user's own standards (layouts learnt from their workbooks in the web app, sgx_fin/standards.py) come as
a token (`standard`, "sgxstd1...."): one company's, from the web app's message, or all of them, pasted by the
user. The tools then read the statements into those lines and write the workbook in that unit, column
order and sign convention.

Nothing is stored on the server: the download link carries the figures (compressed, signed with
APP_PASSWORD) and the workbook is built when it is opened.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import zlib
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

import requests

from . import checks, extractor, industries, pictures
from . import template as T
from .page_scanner import page_texts
from .pipeline import attachment_name, choose_financial_pdf, default_pages, not_results_reason, process_pdf
from .sgx_client import Announcement, SGXClient, Screen, build_page_url, classify_title, is_sgx_url

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "sgx-financials", "title": "SGX Financials", "version": "1.0.0"}
PREVIEW_LIMIT = 16  # small previews offered when Claude still has to find the statement pages
READ_LIMIT = 6  # full-resolution statement pages per open_filing call
PAGE_LONG_SIDE = 1800  # each page is sent as two overlapping halves at this size
TEXT_LIMIT = 8000  # characters of a text page's text layer sent with its images

INSTRUCTIONS = """SGX Financials turns SGX-listed companies' financial results into a standard Excel workbook \
(income statement, balance sheet, cash flow, all in '000).

Workflow:
1. screen_sgx_results for a date range (Singapore dates) to find results announcements, or take an SGX link from the user.
2. open_filing on the announcement. It downloads the results PDF and finds the statement pages without AI.
   - If the statements are PICTURES (scanned or photographed pages), you read them yourself: open_filing \
returns the cleaned-up page images and the transcription rules. If it returns small previews first, \
pick the consolidated (Group) income statement, balance sheet and cash flow pages and call open_filing \
again with those pages. Then read every figure digit by digit, keeping each under its own column, and \
call submit_statements with the JSON described in the rules.
   - If the statements are text, call extract_filing (the server's AI reads them), or open_filing with \
as_images=true to read them yourself the same way.
3. submit_statements checks that parts add up to printed totals and gives a verdict. If it is NOT VERIFIED \
(checks fail, none passes, a statement or gross profit is missing), do what it says: re-read the lines it names, \
find the missing statement, or judge the gross expense, and submit the whole JSON again. Give the user the \
download link it returns, and say plainly if the figures are still not verified.

Re-checks: when the user asks you to re-check a filing whose checks failed with the server's AI, read it yourself \
(open_filing with as_images=true, then submit_statements); don't call extract_filing, which repeats that reading. \
Some results announcements carry only a presentation (slides) and no formal financial statements: say so rather \
than submitting highlights as statements.

The user's own standards: when the user (or a message from the web app) gives you a standard token, a string \
starting "sgxstd1.", pass it unchanged as `standard` to open_filing, extract_filing and submit_statements for every \
filing, with the stock_code and company_name (a token can hold several companies' standards; the server picks the \
company's). The statements are then read into the user's own lines and the workbook is written in their layout \
(unit, column order, signs). Copy the token exactly, every character."""

# ------------------------------------------------------------------ tool definitions
_URLS = {
    "announcement_url": {"type": "string", "description": "SGX announcement link (https://links.sgx.com/... or www.sgx.com)"},
    "pdf_url": {"type": "string", "description": "Direct link to one PDF attachment on links.sgx.com (optional)"},
}
_META = {
    "company_name": {"type": "string"},
    "stock_code": {"type": "string", "description": "SGX stock code, e.g. D05"},
    "filing": {"type": "string", "description": "Announcement title"},
}
_STANDARD = {"standard": {
    "type": "string", "description": "The user's own standard token (starts with \"sgxstd1.\"), when the user or the web "
                                     "app's message gave one: pass it unchanged, with stock_code / company_name"}}
_COGS_RULE = {"cogs_rule": {
    "type": "object", "description": "The user's own cost of goods sold rule for this company, when the user gave one "
                                     "(the web app puts it in its message): {match, industry, cogs}",
    "properties": {"match": {"type": "string"}, "industry": {"type": "string"}, "cogs": {"type": "string"}}}}

TOOLS = [
    {
        "name": "screen_sgx_results",
        "title": "Screen SGX results announcements",
        "description": "List SGX 'Financial Statements' (ANNC17) announcements broadcast between two Singapore dates, "
                       "with their links and whether each is a results filing. Takes 10-40 s (it reads sgx.com in a browser).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "YYYY-MM-DD (Singapore date)"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD; defaults to from_date"},
                "company": {"type": "string", "description": "Optional company name fragment or stock code"},
            },
            "required": ["from_date"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "open_filing",
        "title": "Open a results filing",
        "description": "Download a results filing and find its statement pages (keyword scan, no AI). For statements that "
                       "are pictures (scans/photos), returns the cleaned-up page images plus the transcription rules so YOU "
                       "read the figures, then call submit_statements. Pass `pages` to choose pages yourself.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_URLS,
                "pages": {"type": "array", "items": {"type": "integer"},
                          "description": "1-based pages of the PDF to return as full-resolution images"},
                "as_images": {"type": "boolean", "description": "Return images even when the statements are text"},
                **_META, **_COGS_RULE, **_STANDARD,
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "submit_statements",
        "title": "Check statements and build the Excel workbook",
        "description": "Submit the statements you read (JSON in the format open_filing describes). The server rescales to "
                       "'000, repairs signs the totals prove wrong, derives missing totals, runs the tie-out checks and "
                       "returns the figures, any failing checks to re-read, and an Excel download link.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "extraction": {"type": "object", "description": "company_name, format, currency, unit, basis, "
                               "income_statement, balance_sheet, cash_flow (each {page_numbers, periods, rows}), notes"},
                **_URLS, **_META, **_COGS_RULE, **_STANDARD,
                "source_pdf": {"type": "string"},
                "pages": {"type": "array", "items": {"type": "integer"}, "description": "PDF pages the figures came from"},
            },
            "required": ["extraction"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "extract_filing",
        "title": "Extract with the server's AI",
        "description": "Run the web app's own pipeline (scan, AI extraction with the server's configured model, checks) on "
                       "a filing and return the figures, checks and an Excel download link. Takes 1-3 minutes.",
        "inputSchema": {
            "type": "object",
            "properties": {**_URLS, **_META, **_COGS_RULE, **_STANDARD,
                           "pages": {"type": "array", "items": {"type": "integer"}, "description": "Optional 1-based pages"}},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
]


class ToolError(Exception):
    """Reported to Claude as a tool result with isError, so it can fix the call."""


class Context:
    """What a request knows about the deployment it runs in."""

    def __init__(self, base_url: str, work: Path, password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.work = work
        self.password = password

    @property
    def on_vercel(self) -> bool:
        return bool(os.environ.get("VERCEL"))

    def browse(self, op: str, **params) -> dict:
        """The Node browser function (/api/browse) of this same deployment."""
        r = requests.get(f"{self.base_url}/api/browse", params={"op": op, **params},
                         headers={"X-App-Password": self.password}, timeout=290)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not r.ok:
            raise ToolError(body.get("error") or f"Browser step failed: HTTP {r.status_code}")
        return body


# ------------------------------------------------------------------ JSON-RPC
def handle(message, ctx: Context):
    """One JSON-RPC message (or a batch). Returns the response, or None for notifications."""
    if isinstance(message, list):
        out = [r for r in (handle(m, ctx) for m in message) if r is not None]
        return out or None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    mid, method, params = message.get("id"), message["method"], message.get("params") or {}
    if "id" not in message:  # notification (notifications/initialized, cancelled...)
        return None
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _ok(mid, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            return _ok(mid, call_tool(params.get("name", ""), params.get("arguments") or {}, ctx))
        if method in ("resources/list", "prompts/list"):
            return _ok(mid, {method.split("/")[0]: []})
        return _error(mid, -32601, f"Method not found: {method}")
    except Exception as e:  # a bug, not a tool failure
        return _error(mid, -32603, f"{type(e).__name__}: {e}")


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def call_tool(name: str, args: dict, ctx: Context) -> dict:
    fn: Callable | None = {"screen_sgx_results": screen_tool, "open_filing": open_tool,
                           "submit_statements": submit_tool, "extract_filing": extract_tool}.get(name)
    if not fn:
        return _text(f"Unknown tool: {name}", error=True)
    try:
        return fn(args, ctx)
    except ToolError as e:
        return _text(str(e), error=True)
    except Exception as e:
        return _text(f"{type(e).__name__}: {e}", error=True)


def _text(text: str, error: bool = False, extra: list | None = None) -> dict:
    out = {"content": [{"type": "text", "text": text}, *(extra or [])]}
    if error:
        out["isError"] = True
    return out


def _image(data: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/jpeg"}


# ------------------------------------------------------------------ screen
def screen_tool(args: dict, ctx: Context) -> dict:
    from datetime import date
    try:
        d1 = date.fromisoformat(str(args.get("from_date", "")).strip())
        d2 = date.fromisoformat(str(args.get("to_date") or args["from_date"]).strip())
    except (KeyError, ValueError):
        raise ToolError("from_date / to_date must be YYYY-MM-DD")
    url = build_page_url(min(d1, d2), max(d1, d2))
    company = (args.get("company") or "").strip()
    if ctx.on_vercel:  # the Python function has no browser; the Node one does
        anns = ctx.browse("screen", url=url, company=company)["announcements"]
    else:
        anns = [{"title": a.title, "issuer": a.issuer, "stock_code": a.stock_code, "url": a.url,
                 "broadcast": a.broadcast, "kind": a.raw.get("kind") or classify_title(a.title)}
                for a in SGXClient().screen(Screen.from_url(url), company=company or None)]
    if not anns:
        return _text(f"No financial-statements announcements on SGX for {d1} to {d2}"
                     + (f" matching '{company}'" if company else "") + f".\nPage: {url}")
    lines = [f"{len(anns)} announcement(s), {d1} to {d2}" + (f", '{company}'" if company else "") + f". Page: {url}", ""]
    for a in anns:
        kind = a.get("kind") or classify_title(a.get("title", ""))
        tag = {"results": "RESULTS", "release_date": "release-date notice"}.get(kind, "other")
        lines.append(f"- [{tag}] {a.get('issuer', '')} ({a.get('stock_code', '')}) · {a.get('broadcast', '')} · "
                     f"{a.get('title', '')}\n  {a.get('url', '')}")
    lines.append("\nCall open_filing with a RESULTS announcement's link to read it.")
    return _text("\n".join(lines))


# ------------------------------------------------------------------ open a filing
def _attachments(url: str, ctx: Context, client: SGXClient) -> list[tuple[str, str]]:
    try:
        urls = client.attachments(Announcement("", "", "", url, "", "", {}))
    except Exception:
        urls = []
    if urls:
        return [(u, attachment_name(u)) for u in urls]
    if ctx.on_vercel:
        files = ctx.browse("attachments", url=url).get("files") or []
        return [(f["url"], attachment_name(f["url"], f.get("name", ""))) for f in files if is_sgx_url(f.get("url", ""))]
    return []


def load_filing(args: dict, ctx: Context):
    """(pdf, scan, used_url, attachment names) for an announcement / PDF link."""
    url = (args.get("announcement_url") or args.get("pdf_url") or "").strip()
    pdf_url = (args.get("pdf_url") or "").strip()
    if not url or not is_sgx_url(url) or (pdf_url and not is_sgx_url(pdf_url)):
        raise ToolError("Give an https://*.sgx.com announcement_url or pdf_url (screen_sgx_results lists them).")
    client = SGXClient()
    links = [(pdf_url, attachment_name(pdf_url))] if pdf_url else _attachments(url, ctx, client)
    if not links:
        raise ToolError("No PDF attachments found on that announcement page.")
    folder = ctx.work / Announcement("", "", "", url, "", "", {}).slug
    picked = choose_financial_pdf(client, links, folder)
    if not picked:
        raise ToolError("None of the attachments is a readable PDF.")
    return picked[0], picked[1], picked[2], [n for _, n in links]


def cogs_rules(args: dict, stock_code: str = "", company: str = "") -> dict:
    """The industries and COGS rules for a call: the built-in ones, with the user's rule for the company
    when the call carries one."""
    rule = args.get("cogs_rule")
    if isinstance(rule, dict) and (rule.get("cogs") or rule.get("industry")):
        rules = industries.resolve({"companies": [{**rule, "match": rule.get("match") or stock_code or company or "this company"}]})
        rules["company"] = rules["companies"][0]
        return rules
    return industries.for_filing(None, stock_code, company)


def standard_formats(args: dict, stock_code: str = "", company: str = "") -> tuple[dict, str]:
    """The formats for a call: the user's standard for the company alone when the call carries a token that
    has one (so the filing is read into its lines), else the built-in ones; and a line saying which."""
    from . import standards
    token = args.get("standard")
    if not token:
        return T.BUILTIN, ""
    try:
        fmt, note = standards.for_company(token, stock_code or args.get("stock_code", ""),
                                          company or args.get("company_name", ""))
    except ValueError as e:
        raise ToolError(f"The `standard` argument can't be used: {e}. Copy the token again exactly as the user gave it "
                        "(every character), or leave `standard` out to use the built-in layout.")
    if not fmt:
        return T.BUILTIN, note
    unit = T.conventions(fmt["id"], {fmt["id"]: fmt})
    order = "latest period first" if unit["latest_first"] else "oldest period first"
    return {fmt["id"]: fmt}, f"Written in {note} ({unit['unit']}, {order})."


def _pages_arg(args: dict, page_count: int) -> list[int]:
    pages = sorted({int(p) for p in args.get("pages") or [] if str(p).lstrip("-").isdigit()})
    bad = [p for p in pages if not 1 <= p <= page_count]
    if bad:
        raise ToolError(f"Pages {bad} are outside the PDF (1-{page_count}).")
    return pages


def _scan_summary(pdf: Path, scan, used_url: str | None, names: list[str]) -> list[str]:
    found = ", ".join(f"{st.replace('_', ' ')}: {ps or '—'}" for st, ps in scan.selected.items())
    out = [f"PDF: {pdf.name} ({scan.page_count} pages)" + (f" — {used_url}" if used_url else ""),
           f"Attachments: {', '.join(names)}",
           f"Keyword scan (pages): {found}",
           f"Statement basis from headings: {scan.basis}"]
    if scan.picture_pages:
        out.append(f"Picture pages (scans/photos): {scan.picture_pages}")
    return out


def open_tool(args: dict, ctx: Context) -> dict:
    formats, std_note = standard_formats(args)
    pdf, scan, used_url, names = load_filing(args, ctx)
    chosen = _pages_arg(args, scan.page_count)
    lines = _scan_summary(pdf, scan, used_url, names)
    source = {"announcement_url": args.get("announcement_url") or used_url, "source_pdf": pdf.name}

    if not chosen:
        reason = not_results_reason(pdf, scan)
        if reason and not args.get("as_images"):
            return _text("\n".join([*lines, "", f"Not a results filing: {reason}",
                                    "To look at its pages yourself anyway, call open_filing again with as_images=true."]))
        if reason:
            lines += ["", f"Set aside without AI as not a results filing: {reason} Look for the Group's formal "
                          "income statement, balance sheet and cash flow statement; if there are none, tell the user "
                          "this filing has no formal financial statements instead of submitting highlights."]
    pictures_route = bool(args.get("as_images")) or scan.needs_vision or any(p in scan.picture_pages for p in chosen)
    if not pictures_route:
        lines += ["", "The statements are TEXT. Call extract_filing with the same link to have the server's AI read "
                      f"pages {default_pages(scan)}, or call open_filing again with as_images=true to read them yourself."]
        return _text("\n".join(lines))

    if not chosen:
        candidates = default_pages(scan)
        if scan.found_all and not scan.needs_vision and len(scan.all_selected) <= READ_LIMIT:
            chosen = scan.all_selected  # text filing read as images: the scan already knows the pages
        elif len(candidates) <= 3:
            chosen = candidates
        else:  # Claude finds the statement pages from small previews first
            shown = candidates[:PREVIEW_LIMIT]
            previews = extractor.render_jpegs(pdf, shown, long_side=900, quality=60)
            lines += ["", f"The statements are pictures. Below are small previews of pages {shown}"
                      + (f" (of {len(candidates)} candidates)" if len(candidates) > len(shown) else "") + ", in that order.",
                      "Find the GROUP / consolidated income statement (or 'financial results'), balance sheet "
                      "('statement of assets and liabilities') and cash flow pages, including continuation pages, "
                      f"then call open_filing again with pages=[...] (up to {READ_LIMIT}) to get them at full resolution."]
            extra = []
            for p, img in zip(shown, previews):
                extra += [{"type": "text", "text": f"Page {p}:"}, _image(img)]
            return _text("\n".join(lines), extra=extra)

    if len(chosen) > READ_LIMIT:
        raise ToolError(f"Choose at most {READ_LIMIT} pages per call (got {len(chosen)}); the statements and their "
                        "continuation pages are usually 3-6 pages.")
    extra = []
    texts = page_texts(pdf) if any(p not in scan.picture_pages for p in chosen) else []
    for p in chosen:
        img = pictures.prepare(pdf, p)  # upright, straightened, cropped, contrast-stretched
        img.thumbnail((PAGE_LONG_SIDE, PAGE_LONG_SIDE))
        top, bottom = pictures.halves(img)
        extra += [{"type": "text", "text": f"Page {p}, top half:"}, _image(pictures.jpeg(top, 82)),
                  {"type": "text", "text": f"Page {p}, bottom half (overlaps the top half):"}, _image(pictures.jpeg(bottom, 82))]
        if texts and p not in scan.picture_pages and texts[p - 1].strip():  # a text page: its digits exactly
            extra.append({"type": "text", "text": f"Page {p} text layer (exact digits; the images show which column "
                                                  f"each belongs to):\n{texts[p - 1].strip()[:TEXT_LIMIT]}"})
    lines += ["", f"Pages {chosen} follow, each as two overlapping halves (cleaned up: turned upright, straightened, "
                  "contrast stretched). Read them yourself following the rules below, then call submit_statements with "
                  f"extraction=<the JSON>, plus announcement_url, source_pdf={json.dumps(pdf.name)}, pages={chosen}"
                  " and the company_name / stock_code if you know them.",
              *([f"{std_note} Use only that format's keys, and pass the same `standard` to submit_statements."] if std_note else []),
              "", reading_rules(formats, cogs=cogs_rules(args)), "", f"Source for submit_statements: {json.dumps({**source, 'pages': chosen})}"]
    return _text("\n".join(lines), extra=extra)


def reading_rules(formats: dict | None = None, cogs: dict | None = None) -> str:
    """The same rules and template the server's own AI gets, plus the JSON shape to submit."""
    shape = {
        "company_name": "…", "format": " | ".join(formats or T.BUILTIN), "industry": "manufacturing",
        "industry_evidence": "sale of precision parts; raw materials and consumables used",
        "currency": "SGD", "unit": "thousands", "basis": "Group (consolidated)",
        "income_statement": {"page_numbers": [7], "periods": [
            {"col": 1, "label": "3 months ended 30.09.2026", "period_end": "2026-09-30", "months": 3}],
            "rows": [{"key": "revenue", "source_label": "Revenue", "cells": [{"col": 1, "value": 12345}]}]},
        "balance_sheet": {"page_numbers": [8], "periods": ["… months 0 …"], "rows": ["…"]},
        "cash_flow": {"page_numbers": [9], "periods": ["…"], "rows": ["…"]},
        "cost_of_sales_basis": {"method": "printed | by_nature | judged | none", "lines": [
            {"source_label": "Raw materials and consumables used", "cells": [{"col": 1, "value": -4567}],
             "in_cogs": True, "why": "materials"}], "explanation": "what the COGS is made of and what was left out"},
        "notes": "caveats, unreadable figures",
    }
    rules = extractor.build_system(formats, cogs).replace(
        "- `page_numbers` are the page numbers within the attached excerpt (1-based) the statement came from.",
        "- `page_numbers` are the PDF page numbers (as labelled above the images) the statement came from.")
    return ("TRANSCRIPTION RULES\n" + rules + "\n\nSubmit exactly this JSON shape (keys from the chosen format only; "
            "`cells` hold only columns that have a figure):\n" + json.dumps(shape, ensure_ascii=False, indent=1) +
            "\nIf a figure is too unclear to read with confidence, leave it out and say so in notes.")


# ------------------------------------------------------------------ results
def _validate(data: dict, formats: dict) -> list[str]:
    problems = []
    if not isinstance(data, dict):
        return ["extraction must be a JSON object"]
    fmt = data.get("format") if data.get("format") in formats else ("general" if "general" in formats else next(iter(formats)))
    if data.get("unit") and data["unit"] not in extractor.UNIT_FACTORS:
        problems.append(f"unit must be one of {list(extractor.UNIT_FACTORS)}")
    if data.get("industry") and data["industry"] not in industries.ids():
        problems.append(f"industry must be one of {industries.ids()}")
    basis = data.get("cost_of_sales_basis")
    if basis is not None:
        cols = {p.get("col") for p in (data.get("income_statement") or {}).get("periods") or [] if isinstance(p, dict)}
        if not isinstance(basis, dict) or not isinstance(basis.get("lines", []), list):
            problems.append("cost_of_sales_basis must be {method, lines: [...], explanation}")
        else:
            for ln in basis.get("lines") or []:
                if not isinstance(ln, dict) or "source_label" not in ln:
                    problems.append("cost_of_sales_basis lines need source_label, cells, in_cogs and why")
                    continue
                for c in ln.get("cells") or []:
                    if not isinstance(c, dict) or c.get("col") not in cols or not isinstance(c.get("value"), (int, float)):
                        problems.append(f"cost_of_sales_basis/{ln['source_label']}: cell {c} must be "
                                        f"{{col: one of the income statement's columns {sorted(cols)}, value: number}}")
    for st in T.STATEMENT_IDS:
        block = data.get(st)
        if block is None:
            continue
        if not isinstance(block, dict):
            problems.append(f"{st} must be an object")
            continue
        keys = {r.key for r in T.data_rows(st, fmt, formats)}
        cols = {p.get("col") for p in block.get("periods") or [] if isinstance(p, dict)}
        for row in block.get("rows") or []:
            if not isinstance(row, dict) or "key" not in row:
                problems.append(f"{st}: every row needs key, source_label and cells")
                continue
            row.setdefault("source_label", row["key"])
            if row["key"] not in keys:
                problems.append(f"{st}: '{row['key']}' is not a key of the {fmt} format")
            for c in row.get("cells") or []:
                if not isinstance(c, dict) or c.get("col") not in cols or not isinstance(c.get("value"), (int, float)):
                    problems.append(f"{st}/{row['key']}: cell {c} must be {{col: one of {sorted(cols)}, value: number}}")
    return problems


def _figures_table(normalised: dict, formats: dict) -> str:
    out = []
    for st in T.STATEMENT_IDS:
        block = normalised.get(st) or {}
        periods, values = block.get("periods") or [], block.get("values") or {}
        if not periods or not values:
            continue
        names = {**T.labels(formats), **{r.key: r.label for r in T.rows_for(st, block.get("format"), formats)}}
        heads = [f"{p['period_end']}" + (f" ({p['months']}M)" if p.get("months") else "") for p in periods]
        out.append(f"\n{T.STATEMENT_TITLES[st]} ('000; EPS in cents)\n| Line | " + " | ".join(heads) + " |\n|---|"
                   + "---:|" * len(heads))
        order = [r.key for r in T.rows_for(st, block.get("format"), formats) if r.key in values]
        for key in order + [k for k in values if k not in order]:
            cells = ["" if v is None else f"{v:,.3f}".rstrip("0").rstrip(".") for v in values[key]]
            out.append(f"| {names.get(key, key)} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def _sign(payload: str) -> str:
    key = (os.environ.get("APP_PASSWORD") or "").encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]


def download_link(ctx: Context, normalised: dict, meta: dict, formats: dict | None = None) -> str:
    """A link that rebuilds the workbook when opened: the figures travel in the link itself (and the user's
    standard, when the filing is written in one)."""
    small = {st: {k: b[k] for k in ("periods", "values", "labels", "format", "step", "cogs_basis") if k in b}
             for st, b in normalised.items()}
    keep = ("company_name", "stock_code", "filing", "announcement_url", "currency", "unit", "basis", "format",
            "notes", "source_pdf", "pages", "model", "verdict", "presentation", "industry")
    payload = {"n": small, "m": {k: meta.get(k) for k in keep if meta.get(k)}}
    fid = meta.get("format") or (normalised.get("income_statement") or {}).get("format")
    if formats and fid in formats and fid not in T.BUILTIN:
        from . import standards
        payload["f"] = standards._compact(T.to_json(formats[fid], hints=False))
    raw = json.dumps(payload, separators=(",", ":"))
    d = base64.urlsafe_b64encode(zlib.compress(raw.encode(), 9)).decode().rstrip("=")
    return f"{ctx.base_url}/api/download?" + urlencode({"op": "download", "d": d, "s": _sign(d)})


def decode_download(d: str, s: str) -> tuple[dict, dict, dict | None]:
    """(normalised, meta, formats): formats is the user's standard when the link carries one, else None."""
    if not hmac.compare_digest(_sign(d), s or ""):
        raise PermissionError("This download link is not valid for this app (the app password changed?).")
    raw = json.loads(zlib.decompress(base64.urlsafe_b64decode(d + "=" * (-len(d) % 4))))
    formats = None
    if raw.get("f"):
        from . import standards
        fmt = T.check_format(standards._expand(raw["f"]))
        formats = {fmt["id"]: fmt}
    return raw["n"], raw.get("m") or {}, formats


def results_text(ctx: Context, normalised: dict, meta: dict, formats: dict, header: list[str]) -> dict:
    from .excel_writer import merge_records, run_checks
    fails = checks.failures(normalised)
    verdict = meta.get("verdict") or checks.verdict(normalised, meta.get("presentation", ""))
    meta = {**meta, "verdict": verdict}
    summary = run_checks(merge_records([], normalised, meta))
    counts = {s: sum(c["status"] == s for c in summary) for s in ("PASS", "FAIL", "derived", "n/a")}
    link = download_link(ctx, normalised, meta, formats)
    basis = (normalised.get("income_statement") or {}).get("cogs_basis") or {}
    if basis:
        header = [*header, f"Industry: {basis.get('industry_name')}" + (f" ({basis['evidence']})" if basis.get("evidence") else ""),
                  "Cost of goods sold: " + (basis.get("cogs_label") or "blank") + (f". {basis['explanation']}" if basis.get("explanation") else "")
                  + "".join(f"\n  - {'IN ' if ln['in_cogs'] else 'out'} {ln['label']}" + (f": {ln['why']}" if ln.get("why") else "")
                            for ln in basis.get("lines") or [])]
        if basis.get("da_in_cogs"):
            show = lambda vals: ", ".join("—" if v is None else f"{v:,.0f}" for v in vals)  # noqa: E731
            header.append(f"Of which depreciation & amortisation ('000, by period): {show(basis['da_in_cogs'])}; "
                          f"cost of goods sold excluding D&A: {show(basis['cogs_excl_da'])} (FactSet shows both).")
        if basis.get("also_suggests"):
            header.append(f"The filing's lines also suggest: {basis['also_suggests']}; check the industry.")
    lines = [*header, f"Checks: {counts['PASS']} pass, {counts['FAIL']} fail, {counts['derived']} derived, {counts['n/a']} n/a.",
             "Verdict: " + ("OK, the figures are confirmed by their printed totals." if verdict["ok"] else
                            "NOT VERIFIED: " + "; ".join(verdict["reasons"]) + ". Don't present these figures as "
                            "checked; fix what is listed below, or tell the user what is wrong.")]
    if fails:
        factor = next((b.get("step", 1.0) for b in normalised.values()), 1.0)
        lines += ["", "These tie-outs FAIL (figures in the filing's own unit). Re-read the lines involved on the page "
                      "images — digit, column, sign, and whether a total was taken for a sub-total — then call "
                      "submit_statements again with the whole corrected JSON:", checks.describe(fails, normalised, factor)]
    gaps = checks.gross_profit_gaps(normalised)
    if gaps and checks.has_figures(normalised.get("income_statement")):
        lines += ["", "Gross profit is blank. Never leave it blank:", checks.describe_gaps(gaps, normalised),
                  "Then call submit_statements again with the whole JSON."]
    if not verdict["pass"] and not fails:
        lines += ["", "No tie-out check passes: the totals that confirm the figures (gross profit, profit before tax, "
                      "tax and profit after tax, total assets / liabilities / equity, cash at the beginning and end) "
                      "weren't read. Read them from the statements; if the filing has no formal financial statements "
                      "(only a presentation or highlights), tell the user instead."]
    missing = [name for st, name in checks.STATEMENT_NAMES.items() if not checks.has_figures(normalised.get(st))]
    if missing:
        it = "it" if len(missing) == 1 else "them"
        lines += ["", f"No {checks.join_or(missing)} was read. Find {it} in the filing (open_filing with pages=[...] "
                      f"shows any page) and submit again, or tell the user the filing doesn't have {it}."]
    lines += [_figures_table(normalised, formats), "", f"Excel workbook (Income Statement, Balance Sheet, Cash Flow, "
              f"Checks, Source Map): {link}",
              "To add these periods to an earlier workbook, upload it in the web app under Settings → Merge."]
    return _text("\n".join(lines))


def submit_tool(args: dict, ctx: Context) -> dict:
    data = args.get("extraction")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            raise ToolError("extraction must be a JSON object")
    formats, std_note = standard_formats(args, args.get("stock_code", ""), args.get("company_name") or
                                         (data.get("company_name", "") if isinstance(data, dict) else ""))
    problems = _validate(data, formats)
    if problems:
        raise ToolError("Fix these and submit again:\n- " + "\n- ".join(problems[:30]))
    normalised = extractor.normalise(data, formats, cogs_rules(args, args.get("stock_code", ""),
                                                               args.get("company_name") or data.get("company_name", "")))
    pages = args.get("pages") or []
    meta = {
        "company_name": args.get("company_name") or data.get("company_name", ""),
        "stock_code": args.get("stock_code", ""),
        "filing": args.get("filing", ""),
        "announcement_url": args.get("announcement_url") or args.get("pdf_url") or "",
        "currency": data.get("currency", ""), "unit": data.get("unit", ""), "basis": data.get("basis", ""),
        "format": normalised["income_statement"]["format"], "notes": data.get("notes", ""),
        "source_pdf": args.get("source_pdf", ""), "pages": ",".join(map(str, pages)),
        "model": "Claude (via MCP connector)", "vision": True,
    }
    meta["verdict"] = checks.verdict(normalised)
    meta["cogs_basis"] = normalised["income_statement"].get("cogs_basis")
    meta["industry"] = (meta["cogs_basis"] or {}).get("industry_name", "")
    head = [f"{meta['company_name'] or 'Filing'} — {_format_name(meta['format'], formats)}, {meta['currency']} "
            f"(read in {meta['unit'] or extractor.DEFAULT_UNIT}, shown in '000), basis: {meta['basis'] or '?'}"]
    if std_note:
        head.append(std_note)
    return results_text(ctx, normalised, meta, formats, head)


def _format_name(fid: str, formats: dict) -> str:
    return f"your standard “{formats[fid]['name']}”" if fid in formats and fid not in T.BUILTIN else f"{fid} format"


def extract_tool(args: dict, ctx: Context) -> dict:
    from . import glm
    started = time.monotonic()
    pdf, scan, used_url, names = load_filing(args, ctx)
    pages = _pages_arg(args, scan.page_count) or None
    if not pages:
        reason = not_results_reason(pdf, scan)
        if reason:
            raise ToolError(f"Not a results filing: {reason}")
    if not extractor.provider() and not (scan.needs_vision and glm.api_key()):
        raise ToolError("This deployment has no AI key (GEMINI_API_KEY / ANTHROPIC_API_KEY). Use open_filing with "
                        "as_images=true and read the statements yourself, then submit_statements.")
    meta = {"company_name": args.get("company_name", ""), "stock_code": args.get("stock_code", ""),
            "filing": args.get("filing", ""), "announcement_url": args.get("announcement_url") or used_url}
    formats, std_note = standard_formats(args, meta["stock_code"], meta["company_name"])
    res = process_pdf(pdf, scan=scan, pages=pages, meta=meta, cache_dir=ctx.work, deadline=started + 280,
                      formats=formats, cogs=cogs_rules(args, meta["stock_code"], meta["company_name"]))
    if not res.normalised:
        raise ToolError("No statement pages found; call open_filing to look at the pages and pass pages=[...].")
    head = _scan_summary(pdf, scan, used_url, names) + [
        f"Read by {res.meta.get('model')} from pages {res.meta.get('pages')} — {_format_name(res.meta.get('format'), formats)}, "
        f"{res.meta.get('currency')}, basis: {res.meta.get('basis')}"]
    if std_note:
        head.append(std_note)
    if res.meta.get("notes"):
        head.append(f"Notes: {res.meta['notes']}")
    return results_text(ctx, res.normalised, res.meta, formats, head)


def connector_url(base_url: str, password: str = "") -> str:
    # op= names the operation whichever path Vercel hands the function (see api/index.py)
    url = f"{base_url.rstrip('/')}/api/mcp?op=mcp"
    return f"{url}&key={quote(password)}" if password else url
