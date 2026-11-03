"""GLM 5.3 Flash for filings whose statements are pictures (scans, photos).

Pages are rendered to JPEG and sent to an OpenAI-compatible chat completions endpoint: NVIDIA's API
catalog for an nvapi- key (the free tier), else Z.ai. A filing with up to GLM_MAX_IMAGES picture
pages is read in one request; a longer one is first skimmed at low resolution to find the statement
pages, then only those are read in full. locate() does the skimming alone, for the two-step reading
in extractor.

Environment
  GLM_FLASH       the API key (nvapi-... for NVIDIA; GLM_API_KEY / ZAI_API_KEY also work)
  GLM_MODEL       pin the model id; by default the GLM 5.3 Flash id is looked up in the key's model list
  GLM_BASE_URL    override the endpoint (default: NVIDIA for nvapi- keys, else https://api.z.ai/api/paas/v4)
  GLM_MAX_IMAGES  pictures per request (default 8)
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pypdfium2 as pdfium
import requests

from . import template as T

KEY_VARS = ("GLM_FLASH", "GLM_API_KEY", "ZAI_API_KEY", "ZHIPUAI_API_KEY", "NVIDIA_API_KEY")
NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
ZAI_URL = "https://api.z.ai/api/paas/v4"
ZAI_MODELS = ["glm-5.3-flash", "glm-4.6v-flash"]  # the second is Z.ai's free vision model
# Tried on NVIDIA when the model list can't be read; the catalog's exact id varies by listing.
NVIDIA_GUESSES = ["z-ai/glm-5.3-flash", "zai-org/glm-5.3-flash", "z-ai/glm-5-3-flash", "glm-5-3-flash"]
GLM53_FLASH = re.compile(r"glm[-_]?5[._-]?3[-_]?flash", re.I)
_listed: dict[str, list[str]] = {}
UNITS = ("units", "thousands", "lakhs", "millions", "crores", "billions")
STATEMENTS = tuple(T.STATEMENTS)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def api_key() -> str | None:
    for name in KEY_VARS:
        value = _env(name)
        if value and not value.lower().startswith("glm-"):  # GLM_FLASH may hold a model id instead
            return value
    return None


def base_url() -> str:
    if _env("GLM_BASE_URL"):
        return _env("GLM_BASE_URL").rstrip("/")
    return NVIDIA_URL if (api_key() or "").startswith("nvapi-") else ZAI_URL


def is_nvidia() -> bool:
    return base_url() == NVIDIA_URL


def _listed_models() -> list[str]:
    """Model ids the key can use (GET /models), cached per endpoint."""
    url = base_url()
    if url not in _listed:
        try:
            r = requests.get(f"{url}/models", headers={"Authorization": f"Bearer {api_key()}"}, timeout=20)
            _listed[url] = [m.get("id", "") for m in r.json().get("data", [])] if r.ok else []
        except (requests.RequestException, ValueError):
            _listed[url] = []
    return _listed[url]


def models() -> list[str]:
    """Models to try in order: GLM_MODEL, then GLM 5.3 Flash as the endpoint names it."""
    pinned = _env("GLM_MODEL")
    if is_nvidia():
        found = [m for m in _listed_models() if GLM53_FLASH.search(m)]
        found.sort(key=lambda m: ("free" in m.lower(), len(m)))  # plain id before variants
        candidates = found or NVIDIA_GUESSES
    else:
        candidates = ZAI_MODELS
    ordered = ([pinned] if pinned else []) + candidates
    return list(dict.fromkeys(ordered))


def label() -> str:
    return "GLM 5.3 Flash" + (" (NVIDIA)" if is_nvidia() else "")


def max_images() -> int:
    try:
        return max(1, int(_env("GLM_MAX_IMAGES") or 8))
    except ValueError:
        return 8


# ------------------------------------------------------------------ images
def render(pdf: Path, pages: list[int], long_side: int) -> list[str]:
    """data: URLs of the given 1-based pages as JPEG, scaled so the longer side is `long_side` px."""
    doc = pdfium.PdfDocument(str(pdf))
    try:
        out = []
        for p in pages:
            page = doc[p - 1]
            w, h = page.get_size()
            img = page.render(scale=long_side / max(w, h)).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=75)
            out.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())
        return out
    finally:
        doc.close()


# ------------------------------------------------------------------ API
def _post(model: str, system: str, content: list, json_mode: bool = True, deadline: float | None = None) -> tuple[str, dict]:
    left = 240.0 if deadline is None else deadline - time.monotonic()
    if left < 10:
        raise GLMError(408, "out of time")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "temperature": 0.1,
        "max_tokens": 32768,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{base_url()}/chat/completions",
                      headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
                      json=body, timeout=min(240.0, left))
    if r.status_code >= 400:
        raise GLMError(r.status_code, r.text[:400])
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise GLMError(200, "output limit reached before the JSON was complete")
    msg = choice.get("message") or {}
    return msg.get("content") or "", data.get("usage") or {}


class GLMError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"GLM HTTP {status}: {detail}")
        self.status = status


def _call(model: str, system: str, content: list, deadline: float | None = None) -> tuple[dict, dict]:
    try:
        text, usage = _post(model, system, content, deadline=deadline)
    except GLMError as e:
        if e.status != 400:
            raise
        text, usage = _post(model, system, content, json_mode=False, deadline=deadline)  # no JSON mode
    except requests.Timeout as e:
        raise GLMError(408, "timed out") from e
    return parse_json(text), usage


def parse_json(text: str) -> dict:
    """The JSON object in the reply, ignoring <think>…</think> reasoning and ``` fences."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("GLM did not return JSON")
    return json.loads(text[start:end + 1])


def _pages_content(urls: list[str], instruction: str, labels: list[int] | None = None) -> list:
    content = []
    for i, url in enumerate(urls, 1):
        content.append({"type": "text", "text": f"Page {labels[i - 1] if labels else i}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": instruction})
    return content


SKELETON = (
    'Return only one JSON object, no prose, shaped like:\n'
    '{"company_name": str, "format": "general|bank", "currency": "SGD", '
    '"unit": "units|thousands|lakhs|millions|crores|billions", "basis": "Group", '
    '"industry": <industry id>, "industry_evidence": str, '
    '"notes": str, "income_statement": BLOCK, "balance_sheet": BLOCK, "cash_flow": BLOCK, '
    '"cost_of_sales_basis": {"method": "printed|by_nature|judged|none", "lines": [{"source_label": str, '
    '"cells": [{"col": int, "value": number}], "in_cogs": bool, "why": str}], "explanation": str}}\n'
    'where BLOCK = {"page_numbers": [int], "periods": [{"col": int, "label": str, "period_end": "YYYY-MM-DD", '
    '"months": int}], "rows": [{"key": <template key>, "source_label": str, "cells": [{"col": int, "value": number}]}]}'
)

LOCATE = ("Each image is one page of a company's results filing, numbered as labelled. Which pages hold the "
          "primary financial statements of the Group? Return only JSON: "
          '{"income_statement": [page numbers], "balance_sheet": [...], "cash_flow": [...]} '
          "using the labels' numbers, [] when a statement is not on these pages. Ignore notes, commentary "
          "and segment tables.")


LOCATE_JSON = ('\nReturn only JSON: {"pages": [{"page": <number from the label>, "heading": str, '
               '"statements": ["income_statement" | "balance_sheet" | "cash_flow"], '
               '"basis": "consolidated" | "standalone" | "unknown"}]}')


def locate(pdf: Path, pages: list[int], system: str, instruction: str,
           deadline: float | None = None) -> tuple[list[dict], str]:
    """Statement pages among `pages` from small previews, labelled with their own page numbers and
    skimmed in parallel batches. Returns ([{page, heading, statements, basis}], model)."""
    size = max_images() * 2
    batches = [pages[i:i + size] for i in range(0, len(pages), size)]
    errors = []
    for model in models():
        def skim(batch):
            return _call(model, system, _pages_content(render(pdf, batch, 900), instruction + LOCATE_JSON, batch), deadline)[0]
        try:
            found = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                for hits in pool.map(skim, batches):
                    found += [h for h in hits.get("pages") or [] if isinstance(h, dict)]
            return found, model
        except GLMError as e:
            errors.append(f"{model}: {e}")
            if e.status in (401, 403, 408):
                break
        except (ValueError, KeyError, TypeError) as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("; ".join(errors))


def ask(system: str, instruction: str, images: list[tuple[str, bytes]] | None = None,
        deadline: float | None = None) -> tuple[dict, str, dict]:
    """One JSON question with optional labelled JPEG images. Returns (answer, model, usage)."""
    errors = []
    for model in models():
        content = []
        for label_text, jpg in images or []:
            content.append({"type": "text", "text": label_text})
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}})
        content.append({"type": "text", "text": instruction})
        try:
            answer, usage = _call(model, system, content, deadline)
            return answer, model, usage
        except GLMError as e:
            errors.append(f"{model}: {e}")
            if e.status in (401, 403, 408):
                break
        except (ValueError, KeyError, TypeError) as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("; ".join(errors))


# ------------------------------------------------------------------ extraction
def extract(pdf: Path, pages: list[int], system: str, instruction: str,
            deadline: float | None = None, formats: dict | None = None) -> tuple[dict, str, dict]:
    """Read the statements from picture pages. Returns (data, model used, usage).

    `pages` are original page numbers; data[...]["page_numbers"] come back as 1-based positions in
    `pages`, the same convention as the PDF-excerpt providers.
    """
    errors = []
    for model in models():
        try:
            data, usage = _extract_with(model, pdf, pages, system, instruction, deadline, formats)
            return data, model, usage
        except GLMError as e:
            errors.append(f"{model}: {e}")
            if e.status in (401, 403, 408):
                break  # bad key, or out of time: other models won't help
        except (ValueError, KeyError, TypeError) as e:
            errors.append(f"{model}: {e}")
    raise RuntimeError("; ".join(errors))


def _extract_with(model: str, pdf: Path, pages: list[int], system: str, instruction: str,
                  deadline: float | None = None, formats: dict | None = None) -> tuple[dict, dict]:
    limit = max_images()
    use = pages
    usage_total: dict = {}
    if len(pages) > limit:
        # Skim small previews in batches (in parallel) to find the statement pages, then read only those.
        batches = [pages[i:i + limit * 2] for i in range(0, len(pages), limit * 2)]

        def skim(batch):
            return batch, _call(model, system, _pages_content(render(pdf, batch, 900), LOCATE), deadline)

        found: set[int] = set()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for batch, (hits, usage) in pool.map(skim, batches):
                _add(usage_total, usage)
                for st in STATEMENTS:
                    found |= {batch[n - 1] for n in hits.get(st) or [] if isinstance(n, int) and 1 <= n <= len(batch)}
        use = sorted(found)[:limit] or pages[:limit]
    raw, usage = _call(model, system + "\n\n" + SKELETON, _pages_content(render(pdf, use, 1600), instruction), deadline)
    _add(usage_total, usage)
    data = coerce(raw, formats)
    for st in STATEMENTS:  # positions in `use` -> positions in `pages`
        data[st]["page_numbers"] = [pages.index(use[n - 1]) + 1 for n in data[st]["page_numbers"] if 1 <= n <= len(use)]
    return data, usage_total


def _add(total: dict, usage: dict) -> None:
    for k in ("prompt_tokens", "completion_tokens"):
        total[k] = total.get(k, 0) + int(usage.get(k) or 0)


# ------------------------------------------------------------------ cleaning GLM's JSON
NUM = re.compile(r"^\(?-?[\d,]*\.?\d+\)?-?$")
NIL = {"", "-", "--", "–", "—", "n/a", "na", "nil", "?", "x"}


def number(v):
    """A printed figure as a float, or None for blanks, dashes and unreadable cells.

    12,345 · (12,345) · −12,345 · 12,345- · 12,34,567 (Indian grouping) · 12 345 · ₹ 1,234 · S$1,234
    · 1.234.567 or 1.234,5 (dots for thousands) · "-" / "nil" / "?" -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("−", "-").replace("–", "-").replace("\u2009", "").replace("\xa0", "")
    s = re.sub(r"^(?:[A-Z]{0,3}\$|₹|Rs\.?|RMB|US\$|S\$|¥|€|£)\s*", "", s, flags=re.I).replace(" ", "")
    if s.lower() in NIL:
        return None
    if re.fullmatch(r"\(?-?\d{1,3}(?:\.\d{3}){2,}(?:,\d+)?\)?|\(?-?\d{1,3}(?:\.\d{3})+,\d+\)?", s):
        s = s.replace(".", "").replace(",", ".")  # 1.234.567 / 1.234,56
    if not NUM.match(s):
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-") or (s.endswith("-") and len(s) > 1)
    s = s.strip("()").strip("-").replace(",", "")
    try:
        x = float(s)
    except ValueError:
        return None
    return -abs(x) if neg else x


def coerce(raw: dict, formats: dict | None = None) -> dict:
    """Shape GLM's answer like the schema-checked answers of the other providers."""
    formats = formats or T.BUILTIN
    unit = str(raw.get("unit") or "").lower()
    out = {
        "company_name": str(raw.get("company_name") or ""),
        "format": raw.get("format") if raw.get("format") in formats else "general",
        "currency": str(raw.get("currency") or ""),
        "unit": unit if unit in UNITS else "thousands",
        "basis": str(raw.get("basis") or "Group"),
        "notes": str(raw.get("notes") or ""),
    }
    if unit not in UNITS:
        out["notes"] = (out["notes"] + " Unit not stated; figures taken as thousands.").strip()
    for st in STATEMENTS:
        block = raw.get(st) if isinstance(raw.get(st), dict) else {}
        keys = {r.key for r in T.data_rows(st, formats=formats)}
        periods = []
        for i, p in enumerate(block.get("periods") or [], 1):
            if isinstance(p, dict):
                try:
                    months = int(p.get("months") or 0)
                except (TypeError, ValueError):
                    months = 0
                col = p.get("col") if isinstance(p.get("col"), int) else i
                periods.append({"col": col, "label": str(p.get("label") or ""),
                                "period_end": str(p.get("period_end") or ""), "months": months})
        rows = []
        for r in block.get("rows") or []:
            if not isinstance(r, dict) or r.get("key") not in keys:
                continue
            if isinstance(r.get("cells"), list):
                cells = [{"col": c.get("col"), "value": number(c.get("value"))} for c in r["cells"]
                         if isinstance(c, dict) and isinstance(c.get("col"), int)]
                rows.append({"key": r["key"], "source_label": str(r.get("source_label") or ""),
                             "cells": [c for c in cells if c["value"] is not None]})
            else:  # a plain list in column order
                vals = r.get("values") if isinstance(r.get("values"), list) else []
                rows.append({"key": r["key"], "source_label": str(r.get("source_label") or ""),
                             "values": [number(v) for v in vals]})
        nums = [n for n in block.get("page_numbers") or [] if isinstance(n, int)]
        out[st] = {"page_numbers": nums, "periods": periods, "rows": rows}
    out["industry"] = str(raw.get("industry") or "").strip().lower()
    out["industry_evidence"] = str(raw.get("industry_evidence") or "")
    basis = raw.get("cost_of_sales_basis") if isinstance(raw.get("cost_of_sales_basis"), dict) else {}
    out["cost_of_sales_basis"] = {
        "method": str(basis.get("method") or "").strip().lower(), "explanation": str(basis.get("explanation") or ""),
        "lines": [{"source_label": str(ln.get("source_label") or ""), "in_cogs": bool(ln.get("in_cogs")), "why": str(ln.get("why") or ""),
                   "cells": [{"col": c["col"], "value": number(c.get("value"))} for c in ln.get("cells") or []
                             if isinstance(c, dict) and isinstance(c.get("col"), int) and number(c.get("value")) is not None]}
                  for ln in basis.get("lines") or [] if isinstance(ln, dict)]}
    return out
