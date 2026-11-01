"""Filings whose statements are pictures: scans, phone photos, scans with a hidden OCR layer.

Picture filings go wrong in different ways, so each step deals with one of them:

  locate      Many pages, few of them statements; standalone and consolidated sets; pages turned
              sideways. Small previews of every picture page -> the statement pages (consolidated
              preferred, continuation pages kept), their headings, and how far each page is turned.
  prepare     Turned, skewed, dim, shadowed or small pictures. Each page is rendered at the scan's
              own resolution, turned upright, straightened, cropped to the content and given even
              contrast. No AI.
  transcribe  Reading and interpreting at once is where figures slip between rows and columns. The
              tables are first copied exactly as printed (text, not numbers): every row, every
              column, with each column's period. Nothing is mapped yet.
  verify      No AI. What a table says about itself: totals named in labels ("Total Income (1+2)",
              "(7-8-9)", "(a)+(b)+(c)"), and quarter + quarter = half year (or 6M + 3M = 9M) on
              every line. A failure marks the rows involved as doubtful.
  reread      Doubtful rows are read again from enlarged halves of their page; a new reading is
              taken only where it makes the failing relation hold.
  as_text     The checked tables as text, for the same template step text filings use.

Gemini (Flash, then Flash-Lite) does the AI steps, with GLM 5.3 Flash behind it when GLM_FLASH is set.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageFilter, ImageOps

from . import extractor, glm
from . import template as T
from .glm import number

# ------------------------------------------------------------------ locate
LOCATE_SYSTEM = "You look through the pages of a listed company's results filing to find its primary financial statements."
LOCATE_INSTRUCTION = (
    "Each image is one page, labelled with its page number. List every page that shows one of the primary financial "
    "statements: the income statement (statement of profit or loss, or of financial results), the balance sheet "
    "(statement of financial position, or of assets and liabilities) and the cash flow statement. For each page give "
    "its number from the label, its heading as printed, the statements on it, whether they are consolidated (Group) "
    "or standalone (the company or bank alone; 'unknown' when the page doesn't say, e.g. a continuation page), and "
    "how many degrees clockwise the page must be turned for its text to read upright (0 if it already does). Leave "
    "out notes, segment reporting, auditors' reports, press releases, commentary and ratio tables.")
LOCATE_SCHEMA = {
    "type": "object",
    "properties": {"pages": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "the number in the page's label"},
            "heading": {"type": "string"},
            "statements": {"type": "array", "items": {"type": "string", "enum": list(T.STATEMENTS)}},
            "basis": {"type": "string", "enum": ["consolidated", "standalone", "unknown"]},
            "turn": {"type": "string", "enum": ["0", "90", "180", "270"],
                     "description": "degrees clockwise to turn the page so its text reads upright"},
        },
        "required": ["page", "heading", "statements", "basis", "turn"],
        "additionalProperties": False,
    }}},
    "required": ["pages"],
    "additionalProperties": False,
}


@dataclass
class Located:
    pages: list[int]  # original page numbers to read
    found: list[dict]  # every statement page reported: {page, heading, statements, basis, turn}
    basis: str  # consolidated | standalone | unknown
    model: str

    @property
    def turns(self) -> dict[int, int]:
        out = {}
        for f in self.found:
            try:
                out[f["page"]] = int(f.get("turn") or 0) % 360
            except (TypeError, ValueError):
                pass
        return out

    @property
    def context(self) -> str:
        heads = "; ".join(f"page {p}: \"{f['heading']}\"" for p in self.pages
                          for f in self.found if f["page"] == p and f.get("heading"))
        which = {"consolidated": "the consolidated (Group) statements", "standalone": "the standalone statements"}
        return f"They were picked out as {which.get(self.basis, 'the primary statements')}" + (f" ({heads})." if heads else ".")


def pick_located(found: list[dict], allowed: list[int], per_statement: int = 3, cap: int = 6) -> tuple[list[int], str]:
    """The pages to read: every statement from the consolidated pages if the filing has them, else the
    standalone ones. An unlabelled page right after a labelled one is its continuation and shares
    its basis."""
    found = sorted((f for f in found if f.get("page") in allowed), key=lambda f: f["page"])
    basis_of = {f["page"]: f["basis"] for f in found if f.get("basis") in ("consolidated", "standalone")}
    for f in found:
        if f["page"] not in basis_of and f["page"] - 1 in basis_of:
            basis_of[f["page"]] = basis_of[f["page"] - 1]
    overall = "consolidated" if "consolidated" in basis_of.values() else "standalone" if basis_of else "unknown"
    order = [overall, "standalone" if overall == "consolidated" else "consolidated", None]
    chosen = set()
    for st in T.STATEMENTS:
        pages = [f["page"] for f in found if st in (f.get("statements") or [])]
        for basis in order:
            sel = [p for p in pages if basis_of.get(p) == basis]
            if sel:
                chosen |= set(sel[:per_statement])
                break
    return sorted(chosen)[:cap], overall


def _cached(cache_dir: Path | None, pdf: Path, kind: str, *parts) -> Path:
    key = hashlib.sha256(pdf.read_bytes() + json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return (cache_dir or pdf.parent) / f"{pdf.stem}.{key}.{kind}.json"


def _ask(system: str, instruction: str, schema: dict, images: list[tuple[str, bytes]], deadline: float,
         sharp: bool, client=None) -> tuple[dict, str, dict]:
    """One JSON answer about some pictures: Gemini first (Flash for sharp reading, Flash-Lite for
    previews), GLM 5.3 Flash if that fails and there's time."""
    errors = []
    if extractor.provider() == "gemini":
        try:
            ex = extractor._gemini(
                None, instruction, extractor.vision_models()[0] if sharp else extractor.default_model("gemini"),
                "high" if sharp else "low", client, deadline=deadline, system=system, schema_json=schema, images=images,
                candidates=extractor.vision_models() if sharp else None,
                resolution="MEDIA_RESOLUTION_HIGH" if sharp else "MEDIA_RESOLUTION_MEDIUM",
                part_resolution="MEDIA_RESOLUTION_ULTRA_HIGH" if sharp else None)
            return ex.data, ex.model, ex.usage
        except Exception as e:
            errors.append(f"Gemini: {e}")
    if glm.api_key() and deadline - time.monotonic() > 40:
        try:
            shape = "\nReturn only JSON matching this JSON Schema:\n" + json.dumps(schema)
            answer, model, usage = glm.ask(system, instruction + shape, images, deadline)
            return answer, f"{model} via {'NVIDIA' if glm.is_nvidia() else 'Z.ai'}", {
                "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
        except Exception as e:
            errors.append(f"{glm.label()}: {e}")
    raise RuntimeError(" | ".join(errors) or "No AI key for pictures: set GEMINI_API_KEY or GLM_FLASH")


def locate(pdf: Path, pages: list[int], deadline: float | None = None, cache_dir: Path | None = None,
           client=None) -> Located | None:
    """Find the statement pages among picture pages from small previews. None when there's no AI
    for it, or none of the pages holds a statement."""
    if not (extractor.provider() == "gemini" or glm.api_key()):
        return None
    deadline = deadline or time.monotonic() + 120
    cache = _cached(cache_dir, pdf, "locate", pages, LOCATE_INSTRUCTION)
    if cache.exists():
        c = json.loads(cache.read_text())
        return Located(c["pages"], c["found"], c["basis"], c["model"])
    images = [(f"Page {p}:", jpeg(prepare(pdf, p, long_side=1100, enhance=False), 70)) for p in pages]
    answer, model, _ = _ask(LOCATE_SYSTEM, LOCATE_INSTRUCTION, LOCATE_SCHEMA, images, deadline, sharp=False, client=client)
    found = [f for f in answer.get("pages") or [] if isinstance(f, dict) and isinstance(f.get("page"), int)]
    chosen, basis = pick_located(found, pages)
    if not chosen:
        return None
    res = Located(chosen, found, basis, model)
    cache.write_text(json.dumps(res.__dict__, indent=2))
    return res


# ------------------------------------------------------------------ prepare (no AI)
def _native_long_side(page) -> int | None:
    """The long side, in pixels, the page has at its biggest picture's own resolution."""
    w, h = page.get_size()
    best = None
    for o in page.get_objects(max_depth=2):
        if o.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
            continue
        try:
            left, bottom, right, top = (o.get_bounds() if hasattr(o, "get_bounds") else o.get_pos())
            px_w, px_h = o.get_px_size()
        except Exception:
            continue
        placed = max(right - left, top - bottom)
        if placed < 0.3 * max(w, h):
            continue  # a logo or a stamp
        per_point = max(px_w, px_h) / placed
        best = max(best or 0, int(per_point * max(w, h)))
    return best


def _pixels(img: Image.Image) -> list:
    return list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata())


def _ink(gray: Image.Image) -> Image.Image:
    small = gray.copy()
    small.thumbnail((900, 900))
    return ImageOps.invert(ImageOps.autocontrast(small, cutoff=1)).point(lambda v: 255 if v > 110 else 0)


def _spread(prof: list) -> float:
    mean = sum(prof) / len(prof)
    return sum((x - mean) ** 2 for x in prof) / len(prof)


def line_ratio(gray: Image.Image) -> float:
    """How much more the ink lines up in rows than in columns: well above 1 for upright (or upside
    down) text, well below 1 for text turned sideways, near 1 when ruled grids make it unclear."""
    ink = _ink(gray)
    rows = _spread(_pixels(ink.resize((1, ink.height), Image.BOX)))
    cols = _spread(_pixels(ink.resize((ink.width, 1), Image.BOX)))
    return rows / max(cols, 1e-9)


def upright_turn(gray: Image.Image, said: int = 0) -> int:
    """The quarter turns to apply: the AI's answer from the previews, checked against the ink. A page
    whose lines clearly run across is not turned sideways; one whose lines clearly run up and down
    is, even if nobody said so (90° clockwise, the usual way a landscape table lies on a portrait
    scan). Upside-down can't be told from the ink, so 180 is taken as said."""
    ratio = line_ratio(gray)
    if said % 180 == 90:
        return 0 if ratio > 1.8 else said
    if ratio < 0.55:
        return (said + 90) % 360
    return said % 360


def skew_angle(gray: Image.Image, limit: float = 4.0) -> float:
    """Degrees (counter-clockwise) that make the text lines level: the angle at which the rows of
    ink are sharpest (a projection profile), coarse then fine. 0 for a straight page."""
    ink = _ink(gray)

    def sharpness(a: float) -> float:
        return _spread(_pixels(ink.rotate(a, resample=Image.NEAREST, fillcolor=0).resize((1, ink.height), Image.BOX)))

    coarse = max((k * 0.5 for k in range(int(-limit * 2), int(limit * 2) + 1)), key=sharpness)
    fine = max((coarse + k * 0.1 for k in range(-4, 5)), key=sharpness)
    return round(fine, 1) if abs(fine) >= 0.3 else 0.0


def prepare(pdf: Path, page_no: int, turn: int = 0, long_side: int | None = None, enhance: bool = True) -> Image.Image:
    """A page as a clean greyscale picture: at the scan's own resolution (between 1,800 and 3,000 px
    on the long side, or `long_side`), turned upright, straightened, cropped to the content, with
    the contrast stretched (dim photos, shadows) and the digits lightly sharpened. `turn` is the
    clockwise quarter turn the previews suggested; the turn applied is in .info["turn"]."""
    doc = pdfium.PdfDocument(str(pdf))
    try:
        page = doc[page_no - 1]
        w, h = page.get_size()
        target = long_side or min(3000, max(1800, _native_long_side(page) or 2400))
        img = page.render(scale=target / max(w, h)).to_pil().convert("L")
    finally:
        doc.close()
    if enhance:
        turn = upright_turn(img, turn)
    if turn % 360:
        img = img.rotate(-(turn % 360), expand=True, fillcolor=255)
    if not enhance:
        img.info["turn"] = turn % 360
        return img
    angle = skew_angle(img)
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
    img = ImageOps.autocontrast(img, cutoff=0.5)
    ink = ImageOps.invert(img).point(lambda v: 255 if v > 90 else 0).getbbox()
    if ink:
        pad = int(0.015 * max(img.size))
        l, t, r, b = ink
        box = (max(0, l - pad), max(0, t - pad), min(img.width, r + pad), min(img.height, b + pad))
        if (box[2] - box[0]) * (box[3] - box[1]) > 0.25 * img.width * img.height:
            img = img.crop(box)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))
    img.info["turn"] = turn % 360
    return img


def halves(img: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Top and bottom parts of a page, overlapping so no row is cut in both."""
    h = img.height
    return img.crop((0, 0, img.width, int(h * 0.56))), img.crop((0, int(h * 0.44), img.width, h))


def jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ------------------------------------------------------------------ transcribe
TRANSCRIBE_SYSTEM = ("You copy financial tables from pictures of pages exactly as printed. You never calculate, convert, "
                     "round, reorder, merge or guess figures.")
TRANSCRIBE_INSTRUCTION = """Each image is a page, labelled with its page number. Copy every financial statement table on them (statement of profit or loss / financial results, balance sheet / assets and liabilities, cash flow statement); a page may hold more than one table, each listed separately.

For each table give: the page number; the title as printed; the unit as printed (e.g. "₹ in lakhs", "S$'000"; "" if none); whether it is consolidated or standalone; and its figure columns numbered 1, 2, 3... from left to right, each with its heading as printed (join stacked heading lines with " | "), its period end date (YYYY-MM-DD) and its length in months (3, 6, 9 or 12; 0 for an "as at" date).

Then every row from top to bottom, including headings and sub-items: its reference as printed ("1", "(a)", "iii)"; "" if none), its label in full (the English text if bilingual), and each figure as printed text under its column number. Keep brackets, minus signs, commas and decimal points; write "-" for a dash or nil and "?" for a figure you cannot read; leave out empty cells and note-reference columns.

Follow each row straight across the page so every figure stays on its own row and in its own column. Before answering, check each column top to bottom against the picture."""
_CELL = {"type": "object", "properties": {"col": {"type": "integer"}, "text": {"type": "string"}},
         "required": ["col", "text"], "additionalProperties": False}
TABLES_SCHEMA = {
    "type": "object",
    "properties": {"tables": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "page": {"type": "integer"},
            "title": {"type": "string"},
            "unit": {"type": "string"},
            "basis": {"type": "string", "enum": ["consolidated", "standalone", "unknown"]},
            "columns": {"type": "array", "items": {
                "type": "object",
                "properties": {"col": {"type": "integer"}, "heading": {"type": "string"},
                               "period_end": {"type": "string"}, "months": {"type": "integer"}},
                "required": ["col", "heading", "period_end", "months"], "additionalProperties": False}},
            "rows": {"type": "array", "items": {
                "type": "object",
                "properties": {"ref": {"type": "string"}, "label": {"type": "string"},
                               "cells": {"type": "array", "items": _CELL}},
                "required": ["ref", "label", "cells"], "additionalProperties": False}},
        },
        "required": ["page", "title", "unit", "basis", "columns", "rows"],
        "additionalProperties": False,
    }}},
    "required": ["tables"],
    "additionalProperties": False,
}


def clean_tables(raw: dict, pages: list[int]) -> list[dict]:
    """Tables in a fixed shape: cells as {col: text}, columns with ISO dates."""
    out = []
    for t in raw.get("tables") or []:
        if not isinstance(t, dict):
            continue
        cols = []
        for i, c in enumerate(t.get("columns") or [], 1):
            if isinstance(c, dict):
                try:
                    months = int(c.get("months") or 0)
                except (TypeError, ValueError):
                    months = 0
                cols.append({"col": c.get("col") if isinstance(c.get("col"), int) else i, "heading": str(c.get("heading") or ""),
                             "period_end": extractor.iso_date(c.get("period_end") or ""), "months": months})
        rows = []
        for r in t.get("rows") or []:
            if not isinstance(r, dict):
                continue
            cells = {}
            for c in r.get("cells") or []:
                if isinstance(c, dict) and isinstance(c.get("col"), int) and str(c.get("text", "")).strip():
                    cells[c["col"]] = str(c["text"]).strip()
            rows.append({"ref": str(r.get("ref") or "").strip(), "label": str(r.get("label") or "").strip(), "cells": cells})
        page = t.get("page") if isinstance(t.get("page"), int) else (pages[0] if pages else 0)
        out.append({"page": page, "title": str(t.get("title") or ""), "unit": str(t.get("unit") or ""),
                    "basis": str(t.get("basis") or "unknown"), "columns": cols, "rows": rows})
    return out


# ------------------------------------------------------------------ verify (no AI)
SKIP_ROW = re.compile(r"ratio|%|per share|\beps\b|earnings per|margin|capital\b|reserve|net worth|face value|"
                      r"\bshares?\b|\bnpa\b|weighted|number of|book value|\bnav\b|dividend per|percentage", re.I)
NUM_FORMULA = re.compile(r"\(\s*(\d{1,2}(?:\s*[+\-–]\s*\d{1,2})+)\s*\)")
# Lines that total the lines above them: "Total ...", "Sub total", "Net cash from/used in ... activities".
TOTAL_ROW = re.compile(r"^\s*(?:sub[\s-]?total|total\b|net cash (?:flows? )?(?:from|used|generated|\(used in\)|inflow|outflow))", re.I)
SUB_FORMULA = re.compile(r"((?:\(\s*[a-z]\s*\)\s*[+\-–]\s*)+\(\s*[a-z]\s*\))|\(\s*([a-z](?:\s*[+\-–]\s*[a-z])+)\s*\)", re.I)


def _ref(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _end_minus(end: str, months: int) -> str:
    """The month-end `months` before a period end (2026-09-30, 3 -> 2026-06-30)."""
    try:
        y, m, _ = (int(x) for x in end.split("-"))
    except ValueError:
        return ""
    m -= months
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, calendar.monthrange(y, m)[1]).isoformat()


def _step(*texts: str) -> float:
    """The printed rounding unit of some figures: 1, or 0.01 when they show two decimals."""
    decimals = max((len(t.split(".")[-1]) for t in texts if t and re.search(r"\.\d+\)?-?$", t)), default=0)
    return 10.0 ** -decimals


def _value(row: dict, col: int) -> float | None:
    """A cell's figure; a printed dash counts as nil (0), an empty or unreadable cell as None."""
    text = row["cells"].get(col)
    if text is None:
        return None
    x = number(text)
    if x is None and text.strip() in ("-", "--", "–", "—", "nil", "Nil", "NIL"):
        return 0.0
    return x


def verify(tables: list[dict]) -> list[dict]:
    """Relations the tables state about themselves that don't hold. Each problem names its table,
    the rows involved and why: [{"table", "rows", "cols", "why"}]."""
    problems = []
    for ti, t in enumerate(tables):
        rows, cols = t["rows"], [c["col"] for c in t["columns"]] or sorted({c for r in t["rows"] for c in r["cells"]})
        serial = {}  # main serial number -> row index
        for ri, r in enumerate(rows):
            if re.fullmatch(r"\d{1,2}", _ref(r["ref"])):
                serial.setdefault(_ref(r["ref"]), ri)

        def block(ri: int) -> range:
            """The rows of the main item `ri` belongs to: from its serial row to the next one."""
            starts = sorted(serial.values())
            lo = max([s for s in starts if s <= ri], default=0)
            hi = min([s for s in starts if s > ri], default=len(rows))
            return range(lo, hi)

        for ri, r in enumerate(rows):
            terms = []
            m = NUM_FORMULA.search(r["label"])
            if m:
                for sign, tok in re.findall(r"([+\-–]?)\s*(\d{1,2})", m.group(1)):
                    terms.append((-1 if sign and sign in "-–" else 1, serial.get(tok)))
            else:
                m = SUB_FORMULA.search(r["label"])
                if m:
                    expr = m.group(1) or m.group(2)
                    for sign, tok in re.findall(r"([+\-–]?)\s*\(?\s*([a-z])\s*\)?", expr, re.I):
                        idx = next((j for j in block(ri) if j != ri and _ref(rows[j]["ref"]) == tok.lower()), None)
                        if idx is None:  # "(A)+(B)+(C)" naming lines that end in "(A)", "(B)", "(C)"
                            mark = re.compile(rf"\(\s*{tok}\s*\)\W*$", re.I)
                            idx = next((j for j in range(len(rows)) if j != ri and mark.search(rows[j]["label"])), None)
                        terms.append((-1 if sign and sign in "-–" else 1, idx))
            if not terms:
                problems += _block_total(ti, rows, ri, cols)
                continue
            if any(idx is None or idx == ri for _, idx in terms):
                continue
            for c in cols:
                total, parts = _value(r, c), [(s, _value(rows[j], c)) for s, j in terms]
                if total is None and all(v is None for _, v in parts):
                    continue
                if total is None or any(v is None for _, v in parts):
                    continue  # a figure missing or unreadable: re-read below only if something fails
                got = sum(s * v for s, v in parts)
                step = _step(r["cells"].get(c, ""), *(rows[j]["cells"].get(c, "") for _, j in terms))
                if abs(got - total) > step * (len(parts) + 1) / 2 + 1e-9:
                    problems.append({"table": ti, "rows": [ri, *(j for _, j in terms)], "cols": [c],
                                     "why": f"{r['label']}: the parts give {got:,.2f} but column {c} shows {r['cells'].get(c)}"})

        problems += _cash_roll(ti, rows, cols)

        # Quarter + quarter = half year, half year + quarter = nine months...: line by line, within each
        # run of columns (a table with standalone and consolidated side by side repeats its periods).
        segments, seen = [{}], set()
        for c in t["columns"]:
            key = (c["period_end"], c["months"])
            if not (c["months"] and c["period_end"]):
                continue
            if key in seen:
                segments.append({})
                seen = set()
            seen.add(key)
            segments[-1][key] = c["col"]
        triples = []
        for by in segments:
            for (end, months), whole in by.items():
                for (end2, part), recent in by.items():
                    if end2 == end and 0 < part < months:
                        earlier = by.get((_end_minus(end, part), months - part))
                        if earlier:
                            triples.append((whole, recent, earlier, f"{months}M to {end} = {part}M to {end} + "
                                            f"{months - part}M to {_end_minus(end, part)}"))
        for ri, r in enumerate(rows):
            if SKIP_ROW.search(r["label"]):
                continue
            vals = [_value(r, c) for c in cols]
            if len({v for v in vals if v is not None}) <= 1:
                continue  # a constant line (share capital...) or a single figure
            for whole, recent, earlier, rule in triples:
                a, b, e = _value(r, whole), _value(r, recent), _value(r, earlier)
                if a is None or b is None or e is None:
                    continue
                step = _step(*(r["cells"].get(c, "") for c in (whole, recent, earlier)))
                if abs(a - (b + e)) > step * 1.5 + 1e-9:
                    problems.append({"table": ti, "rows": [ri], "cols": [whole, recent, earlier],
                                     "why": f"{r['label']}: {rule} fails ({r['cells'].get(whole)} vs "
                                            f"{r['cells'].get(recent)} + {r['cells'].get(earlier)})"})
    return problems


CASH_BEGIN = re.compile(r"cash.*(?:beginning|opening|start)|(?:beginning|opening).*cash", re.I)
CASH_END = re.compile(r"cash.*(?:\bend\b|closing)|(?:\bend\b|closing).*cash", re.I)
CASH_NET = re.compile(r"net (?:increase|decrease|change|\(?decrease\)?|\(?increase\)?).*cash", re.I)
CASH_FX = re.compile(r"exchange|translation|foreign currenc", re.I)


def _cash_roll(ti: int, rows: list[dict], cols: list[int]) -> list[dict]:
    """Cash at the beginning + the net change (+ exchange differences) = cash at the end, in every column."""
    find = lambda rx, after=-1: next((i for i, r in enumerate(rows) if i > after and rx.search(r["label"])  # noqa: E731
                                      and _has_figures(r)), None)
    net = find(CASH_NET)
    begin = find(CASH_BEGIN, net if net is not None else -1)
    end = find(CASH_END, begin if begin is not None else -1)
    if None in (net, begin, end):
        return []
    fx = next((i for i in range(net + 1, end) if CASH_FX.search(rows[i]["label"]) and _has_figures(rows[i])), None)
    parts = [net, begin] + ([fx] if fx is not None else [])
    out = []
    for c in cols:
        vals = [_value(rows[i], c) for i in parts]
        total = _value(rows[end], c)
        if total is None or vals[0] is None or vals[1] is None:
            continue
        step = _step(*(rows[i]["cells"].get(c, "") for i in (*parts, end)))
        if abs(sum(v or 0 for v in vals) - total) > step * (len(parts) + 1) / 2 + 1e-9:
            out.append({"table": ti, "rows": [begin, net, end] + ([fx] if fx is not None else []), "cols": [c],
                        "why": f"cash at the beginning + net change doesn't give the cash at the end in column {c} "
                               f"({rows[begin]['cells'].get(c)} + {rows[net]['cells'].get(c)} vs {rows[end]['cells'].get(c)})"})
    return out


def _has_figures(row: dict) -> bool:
    return any(number(v) is not None for v in row["cells"].values())


def _block_total(ti: int, rows: list[dict], ri: int, cols: list[int]) -> list[dict]:
    """A "Total" / "Sub total" / "Total <heading>" line against the lines above it. Where the block
    starts isn't printed, so any of the last three headings or totals above may start it (a sub
    total often includes the line before its "Adjustments for:" heading); it fails only when none adds up."""
    r = rows[ri]
    label = r["label"].strip()
    if not TOTAL_ROW.match(label) or not _has_figures(r):
        return []
    starts, j = [], ri - 1
    while j >= 0 and len(starts) < 3:
        if not _has_figures(rows[j]) or TOTAL_ROW.match(rows[j]["label"].strip()):
            starts.append(j)
        j -= 1
    starts.append(-1)  # the top of the table
    blocks = []
    for b in starts:
        blocks.append([k for k in range(b + 1, ri) if _has_figures(rows[k])])
        if b >= 0 and _has_figures(rows[b]):
            blocks.append([k for k in range(b, ri) if _has_figures(rows[k])])  # a previous total carried on
    blocks = [b for b in blocks if len(b) >= 2]
    if not blocks:
        return []
    out = []
    for c in cols:
        total = _value(r, c)
        if total is None:
            continue
        ok = False
        for b in blocks:
            vals = [_value(rows[k], c) for k in b]
            if any(v is None for v in vals) and all(rows[k]["cells"].get(c) is None for k in b):
                continue
            step = _step(r["cells"].get(c, ""), *(rows[k]["cells"].get(c, "") for k in b))
            if abs(sum(v or 0 for v in vals) - total) <= step * (len(b) + 1) / 2 + 1e-9:
                ok = True
                break
        if not ok:
            out.append({"table": ti, "rows": [ri, *blocks[0]], "cols": [c],
                        "why": f"{label}: the lines above don't add up to column {c}'s {r['cells'].get(c)}"})
    return out


# ------------------------------------------------------------------ reread
REREAD_INSTRUCTION = """These are pages of a results filing: each page whole, then its top and bottom parts enlarged (the parts overlap). Some rows of a first copy of its tables don't add up, so one or more of their figures was misread:

{rows}

Read each listed row again from the enlarged parts, digit by digit and column by column (use the whole page for the column headings), and give all of its figures as printed text under the same column numbers. Keep brackets and minus signs; "-" for a dash; "?" if unreadable."""
REREAD_SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {
        "type": "object",
        "properties": {"table": {"type": "integer"}, "row": {"type": "integer"},
                       "cells": {"type": "array", "items": _CELL}},
        "required": ["table", "row", "cells"], "additionalProperties": False}}},
    "required": ["rows"],
    "additionalProperties": False,
}


def _apply_reread(tables: list[dict], answer: dict) -> tuple[list[dict], list[dict]]:
    """Take re-read rows that remove problems without adding any. Returns (corrections, problems left)."""
    corrections = []
    problems = verify(tables)
    for item in answer.get("rows") or []:
        ti, ri = item.get("table"), item.get("row")
        if not (isinstance(ti, int) and isinstance(ri, int) and 0 <= ti < len(tables) and 0 <= ri < len(tables[ti]["rows"])):
            continue
        row = tables[ti]["rows"][ri]
        new = {c["col"]: str(c["text"]).strip() for c in item.get("cells") or []
               if isinstance(c, dict) and isinstance(c.get("col"), int) and str(c.get("text", "")).strip()}
        if not new or new == row["cells"]:
            continue
        old = row["cells"]
        row["cells"] = {**old, **new}
        after = verify(tables)
        if len(after) < len(problems):
            for col in sorted(set(old) | set(new)):
                if old.get(col) != row["cells"].get(col):
                    corrections.append({"page": tables[ti]["page"], "table": ti, "row": ri, "label": row["label"],
                                        "col": col, "was": old.get(col), "now": row["cells"].get(col)})
            problems = after
        else:
            row["cells"] = old
    return corrections, problems


# ------------------------------------------------------------------ the whole reading
@dataclass
class Reading:
    pages: list[int]
    tables: list[dict]
    model: str
    usage: dict = field(default_factory=dict)
    corrections: list[dict] = field(default_factory=list)  # figures changed by the re-read
    doubtful: list[dict] = field(default_factory=list)  # problems still open after it
    turned: dict = field(default_factory=dict)  # page -> degrees turned

    def summary(self) -> dict:
        return {"pages": self.pages, "tables": len(self.tables), "model": self.model, "turned": self.turned,
                "corrections": self.corrections, "doubtful": [p["why"] for p in self.doubtful],
                "doubtful_rows": sorted({(p["table"], r) for p in self.doubtful if "table" in p for r in p.get("rows", [])})}


def _add_usage(total: dict, usage: dict) -> None:
    for k in ("input_tokens", "output_tokens"):
        total[k] = (total.get(k) or 0) + (usage.get(k) or 0)


def read(pdf: Path, pages: list[int], turns: dict[int, int] | None = None, deadline: float | None = None,
         cache_dir: Path | None = None, client=None) -> Reading:
    """Transcribe the statement tables on `pages`, check them, and re-read the rows that don't add up."""
    deadline = deadline or time.monotonic() + 200
    turns = {p: t for p, t in (turns or {}).items() if p in pages and t}
    cache = _cached(cache_dir, pdf, "tables", pages, turns, TRANSCRIBE_INSTRUCTION, REREAD_INSTRUCTION)
    if cache.exists():
        c = json.loads(cache.read_text())
        c["turned"] = {int(k): v for k, v in (c.get("turned") or {}).items()}
        for t in c["tables"]:  # JSON keeps column numbers as text
            for r in t["rows"]:
                r["cells"] = {int(k): v for k, v in r["cells"].items()}
        return Reading(**c)
    images = {p: prepare(pdf, p, turns.get(p, 0)) for p in pages}
    turns = {p: img.info.get("turn", 0) for p, img in images.items() if img.info.get("turn")}
    answer, model, usage = _ask(TRANSCRIBE_SYSTEM, TRANSCRIBE_INSTRUCTION, TABLES_SCHEMA,
                                [(f"Page {p}:", jpeg(images[p])) for p in pages], deadline - 50, sharp=True, client=client)
    tables = clean_tables(answer, pages)
    reading = Reading(pages, tables, model, dict(usage or {}), turned=turns)
    problems = verify(tables)
    if problems and deadline - time.monotonic() > 110:
        wanted: dict[int, set[int]] = {}
        for p in problems:
            wanted.setdefault(p["table"], set()).update(p["rows"])
        lines, used_pages = [], []
        for ti, rows in sorted(wanted.items()):
            t = tables[ti]
            heads = "; ".join(f"{c['col']} = \"{c['heading']}\"" for c in t["columns"])
            lines.append(f"Table {ti} on page {t['page']} (\"{t['title']}\"), columns: {heads}")
            for ri in sorted(rows)[:30]:
                r = t["rows"][ri]
                cells = ", ".join(f"col {c}: {v}" for c, v in sorted(r["cells"].items()))
                lines.append(f"  row {ri}: {r['ref']} {r['label']} — first read as {cells or 'nothing'}")
            used_pages.append(t["page"])
        why = "\n".join(f"  ({p['why']})" for p in problems[:20])
        parts = []
        for p in dict.fromkeys(pg for pg in used_pages if pg in images):
            top, bottom = halves(images[p])
            parts += [(f"Page {p}, whole:", jpeg(images[p], 75)), (f"Page {p}, top part enlarged:", jpeg(top)),
                      (f"Page {p}, bottom part enlarged:", jpeg(bottom))]
        if parts:
            try:
                again, _, usage2 = _ask(TRANSCRIBE_SYSTEM, REREAD_INSTRUCTION.format(rows="\n".join(lines) + "\n" + why),
                                        REREAD_SCHEMA, parts, deadline - 45, sharp=True, client=client)
                _add_usage(reading.usage, usage2 or {})
                reading.corrections, problems = _apply_reread(tables, again)
            except Exception as e:
                reading.doubtful.append({"why": f"Re-reading the rows that don't add up failed: {e}"})
    reading.doubtful += problems
    cache.write_text(json.dumps(reading.__dict__, indent=2, default=str))
    return reading


# ------------------------------------------------------------------ for the template step
def as_text(tables: list[dict]) -> str:
    """The tables as text: column numbers in [brackets] on every figure, so none can shift column."""
    out = []
    for i, t in enumerate(tables, 1):
        out.append(f"Table {i} — page {t['page']}: \"{t['title']}\" ({t['basis']}; unit: {t['unit'] or 'not stated'})")
        out.append("Columns: " + "; ".join(
            f"[{c['col']}] {c['heading']} (period end {c['period_end'] or '?'}, {c['months']} months)" for c in t["columns"]))
        for r in t["rows"]:
            cells = " | ".join(f"[{c}] {v}" for c, v in sorted(r["cells"].items()))
            out.append(f"{r['ref']} {r['label']}".strip() + (f" | {cells}" if cells else ""))
        out.append("")
    return "\n".join(out)
