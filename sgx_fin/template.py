"""Output layouts ("formats"), in a standard financial-statements layout.

A format is data, not code: formats/*.json are the built-in ones (General, Bank), and formats
imported from a spreadsheet export in the web app (or passed to the API) replace a built-in
with the same id or add a new one (e.g. REIT, Insurance). The AI chooses the format for each
filing from their `applies_to` descriptions, and the workbook is written in that format's rows.

Format file
  {"id": "general", "name": "General", "applies_to": "who it is for",
   "statements": {"income_statement" | "balance_sheet" | "cash_flow":
                  {"title": "Income Statement", "rows": [{"key", "label", "kind", "hint"}, ...]}}}

Row fields
  key    stable id (do not rename once you have saved workbooks). Rows imported without a key get
         the built-in key of the same meaning ("Cost of Sales" -> cost_of_sales), else one made
         from the label, so derived lines and tie-out checks keep working on other sites' labels.
  label  text written in column A of the Excel sheet
  kind   "header" (section title, no numbers), "item" (a reported line) or "total" (bold subtotal)
  hint   synonyms / guidance passed to the AI to map the company's own wording
  formula  optional "A + B - C" over other rows' labels (or keys) of the same statement, and plain
         numbers: the line is calculated when the filing doesn't print it (e.g. EBITDA)
  sign   -1 for a cost or outflow the user's own workbooks show as a positive number: the figures
         are kept with this app's signs (costs negative) and only flipped when written to Excel

A format learnt from the user's workbook (sgx_fin/standards.py) also carries
  conventions  {"unit": "units" | "thousands" | "millions" | "billions", "latest_first": bool}:
               the unit and column order the workbook is written in
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

STATEMENT_IDS = ("income_statement", "balance_sheet", "cash_flow")
STATEMENT_TITLES = {"income_statement": "Income Statement", "balance_sheet": "Balance Sheet", "cash_flow": "Cash Flow"}
FORMATS_DIR = Path(__file__).resolve().parent.parent / "formats"
KINDS = ("header", "item", "total")

# Units used in the workbook. Figures are shown in thousands of the
# reporting currency; EPS stays in cents whatever this is set to.
OUTPUT_UNIT = 1_000
OUTPUT_UNIT_LABEL = "'000"

# Column order of periods: True puts the most recent period in column B.
LATEST_FIRST = True


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    kind: str = "item"
    hint: str = ""
    formula: str = ""
    sign: int = 1


# ------------------------------------------------------------------ loading and checking formats
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "row"


NOISE = re.compile(r"\((?:loss(?:es)?|decrease|expense|expenses|credit|charge|net|cents|income|gain|gains|s\$'?000|'000)\)", re.I)


def normalise_label(label: str) -> str:
    """'Net Increase/(Decrease) in Cash' -> 'net increase in cash'; '&' == 'and'. Meaningful brackets
    stay: 'Lease Liabilities (Current)' and '(Non-Current)' are different lines."""
    s = NOISE.sub(" ", str(label)).lower().replace("&", " and ").replace("’", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s).replace("'s", "s").replace("'", "")
    return re.sub(r"\s+", " ", s).strip()


# Other ways data sites and filings name the lines that derived figures and checks rely on.
SYNONYMS = {
    "income_statement": {
        "revenue": ["turnover", "sales", "total revenue", "revenue"],
        "cost_of_sales": ["cost of sales", "cost of revenue", "cost of goods sold", "cogs"],
        "gross_profit": ["gross profit"],
        "profit_before_tax": ["profit before tax", "profit before taxation", "profit before income tax", "pre tax profit"],
        "tax": ["taxation", "income tax", "tax", "tax expense", "income tax expense"],
        "profit_after_tax": ["profit after tax", "profit after taxation", "net profit", "profit for the year",
                             "profit for the period"],
        "discontinued_ops": ["profit from discontinued operations", "loss from discontinued operations",
                             "profit for the period from discontinued operations",
                             "loss for the period from discontinued operations",
                             "profit for the year from discontinued operations",
                             "loss for the year from discontinued operations",
                             "profit from discontinued operations net of tax"],
        "pat_shareholders": ["profit attributable to shareholders", "net profit attributable to shareholders",
                             "attributable to owners of the company", "patmi", "profit attributable to owners"],
        "pat_minority": ["minority interests", "minority interest", "non controlling interests"],
        "depreciation_amortisation": ["depreciation and amortisation", "depreciation and amortization"],
        "eps_basic": ["eps basic", "basic eps", "earnings per share basic", "eps", "basic earnings per share"],
        "eps_diluted": ["eps diluted", "diluted eps", "earnings per share diluted", "diluted earnings per share"],
        "interest_income": ["interest income", "interest earned"],
        "interest_expense": ["interest expense", "interest expended"],
        "net_interest_income": ["net interest income"],
        "allowances": ["allowances", "provisions", "allowances for credit and other losses",
                       "provisions and contingencies"],
        "operating_profit_pre_provisions": ["operating profit before allowances", "operating profit before provisions"],
        "share_associates": ["share of results of associates", "share of profit of associates",
                             "share of results of associates and joint ventures"],
        "exceptional_items": ["exceptional items", "exceptional item"],
        "raw_materials": ["raw materials consumables used and changes in inventories", "raw materials and consumables used"],
        "employee_benefits": ["employee benefits expenses", "employee benefits expense", "staff costs"],
    },
    "balance_sheet": {
        "total_assets": ["total assets"], "total_liabilities": ["total liabilities"], "total_equity": ["total equity"],
        "shareholders_equity": ["shareholders equity", "total shareholders equity", "shareholders funds",
                                "equity attributable to owners"],
        "share_capital": ["share capital", "capital"], "reserves": ["reserves", "reserves and surplus"],
        "minority_interests": ["minority interests", "minority interest", "non controlling interests"],
        "total_ca": ["total current assets"], "total_nca": ["total non current assets"],
        "total_cl": ["total current liabilities"], "total_ncl": ["total non current liabilities"],
        "perpetual_securities": ["perpetual securities", "perpetual capital securities"],
        "assets_held_for_sale": ["assets held for sale", "assets classified as held for sale",
                                 "assets of disposal group classified as held for sale"],
        "liabilities_held_for_sale": ["liabilities held for sale", "liabilities classified as held for sale",
                                      "liabilities directly associated with assets held for sale",
                                      "liabilities directly associated with assets classified as held for sale",
                                      "liabilities of disposal group classified as held for sale"],
    },
    "cash_flow": {
        "cf_net_operating": ["net cash from operating activities", "net cash generated from operating activities",
                             "net cash flow from operating activities", "cash flow from operations",
                             "net cash used in operating activities"],
        "cf_net_investing": ["net cash from investing activities", "net cash used in investing activities",
                             "net cash flow from investing activities"],
        "cf_net_financing": ["net cash from financing activities", "net cash used in financing activities",
                             "net cash flow from financing activities"],
        "cf_net_change": ["net increase in cash", "net change in cash", "net increase in cash and cash equivalents"],
        "cf_cash_begin": ["cash at beginning of period", "cash at beginning of year",
                          "cash and cash equivalents at beginning of period"],
        "cf_cash_end": ["cash at end of period", "cash at end of year", "cash and cash equivalents at end of period"],
        "cf_fx_effect": ["effect of exchange rate changes", "effect of exchange rate changes on cash"],
        "cf_other_cash_changes": ["other changes in cash", "cash transferred to assets held for sale",
                                  "changes in cash and cash equivalents transferred to assets held for sale",
                                  "cash and cash equivalents classified as held for sale"],
        "cf_capex": ["purchase of property plant and equipment", "capital expenditure", "capex",
                     "purchase of property, plant and equipment"],
        "free_cash_flow": ["free cash flow"],
    },
}


def _read_rows(statement: str, rows: list, prefer: dict | None = None) -> list[Row]:
    """Rows from JSON. A row without a key takes the key of the same label in `prefer` (the format
    being replaced), else the built-in key of the same meaning, else one made from the label."""
    out, used = [], set()
    preferred, preferred_hints = {}, {}
    if prefer:
        preferred = {normalise_label(r.label): r.key for r in prefer["statements"][statement][1]}
        preferred_hints = {r.key: r.hint for r in prefer["statements"][statement][1] if r.hint}
    for r in rows:
        if isinstance(r, str):
            r = {"label": r}
        label = str(r.get("label") or "").strip()
        if not label:
            continue
        kind = r.get("kind") if r.get("kind") in KINDS else "item"
        key = str(r.get("key") or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", key):
            key = preferred.get(normalise_label(label)) or (
                ("h_" + _slug(label)) if kind == "header" else canonical_key(statement, label))
        base, n = key, 2
        while key in used:
            key, n = f"{base}_{n}", n + 1
        used.add(key)
        formula = str(r.get("formula") or "").strip()
        hint = str(r.get("hint") or "") or preferred_hints.get(key) or _builtin_hint(statement, key)
        if formula and not r.get("hint"):
            hint = f"the printed figure if shown; otherwise calculated by the program as {formula}"
        sign = -1 if r.get("sign") in (-1, "-1") and kind != "header" else 1
        out.append(Row(key, label, kind, hint, formula, sign))
    return out


def check_format(raw: dict, prefer: dict | None = None) -> dict:
    """A format dict as loaded (or posted by the web app), cleaned: rows become Row objects."""
    if not isinstance(raw, dict):
        raise ValueError("a format must be a JSON object")
    fid = _slug(str(raw.get("id") or raw.get("name") or ""))
    if not fid or fid == "row":
        raise ValueError("a format needs an id or name")
    statements = {}
    for st in STATEMENT_IDS:
        block = (raw.get("statements") or {}).get(st) or {}
        rows = _read_rows(st, block.get("rows") or [], prefer)
        statements[st] = (str(block.get("title") or STATEMENT_TITLES[st]), rows)
    if not any(r.kind != "header" for _, rows in statements.values() for r in rows):
        raise ValueError(f"format {fid} has no rows")
    out = {"id": fid, "name": str(raw.get("name") or fid.title()),
           "applies_to": str(raw.get("applies_to") or ""), "statements": statements}
    conv = clean_conventions(raw.get("conventions"))
    if conv:
        out["conventions"] = conv
    return out


UNIT_NAMES = ("units", "thousands", "millions", "billions")
UNIT_SIZES = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}
UNIT_LABELS = {"units": "", "thousands": "'000", "millions": "m", "billions": "bn"}


def clean_conventions(raw) -> dict:
    """A format's unit and column order, keeping only what is understood."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    if raw.get("unit") in UNIT_NAMES:
        out["unit"] = raw["unit"]
    if isinstance(raw.get("latest_first"), bool):
        out["latest_first"] = raw["latest_first"]
    return out


def conventions(fmt_id: str | None, formats: dict | None = None) -> dict:
    """Unit, unit label, column order of the format a workbook is written in (the app's defaults
    for the built-in formats)."""
    fmt = (formats or BUILTIN).get(fmt_id or "") or {}
    conv = fmt.get("conventions") or {}
    unit = conv.get("unit") or "thousands"
    return {"unit": unit, "size": UNIT_SIZES[unit], "label": UNIT_LABELS[unit],
            "latest_first": conv.get("latest_first", LATEST_FIRST)}


def _load_builtin() -> dict[str, dict]:
    out = {}
    for path in sorted(FORMATS_DIR.glob("*.json")):
        fmt = check_format(json.loads(path.read_text()))
        out[fmt["id"]] = fmt
    return out


_LOOKUP: dict[str, dict[str, str]] = {}
_HINTS: dict[tuple[str, str], str] = {}
BUILTIN: dict[str, dict] = {}


def _build_lookup() -> None:
    for st, syns in SYNONYMS.items():
        table = _LOOKUP.setdefault(st, {})
        for key, names in syns.items():
            for name in names:
                table.setdefault(normalise_label(name), key)


def canonical_key(statement: str, label: str) -> str:
    """The built-in key for a label of the same meaning, else one made from the label."""
    return _LOOKUP.get(statement, {}).get(normalise_label(label)) or _slug(label)


def _builtin_hint(statement: str, key: str) -> str:
    return _HINTS.get((statement, key), "")


_build_lookup()
BUILTIN = _load_builtin()
# Built-in labels count as synonyms, built-in hints guide imported rows; the General format's come
# first where formats share a key (a bank's Revenue is its gross income).
for _fmt in sorted(BUILTIN.values(), key=lambda f: f["id"] != "general"):
    for _st, (_, _rows) in _fmt["statements"].items():
        for _r in _rows:
            if _r.kind != "header":
                _LOOKUP[_st].setdefault(normalise_label(_r.label), _r.key)
                if _r.hint:
                    _HINTS.setdefault((_st, _r.key), _r.hint)


def resolve(custom: list | None = None) -> dict[str, dict]:
    """Built-in formats, with custom ones (from the web app) replacing same-id built-ins or added."""
    out = dict(BUILTIN)
    for raw in custom or []:
        fmt = check_format(raw)
        out[fmt["id"]] = fmt
    return out


def to_json(fmt: dict, hints: bool = True) -> dict:
    out = {"id": fmt["id"], "name": fmt["name"], "applies_to": fmt["applies_to"], "statements": {
        st: {"title": title, "rows": [{k: v for k, v in (("key", r.key), ("label", r.label), ("kind", r.kind),
                                                          ("hint", r.hint if hints else ""), ("formula", r.formula),
                                                          ("sign", -1 if r.sign == -1 else 0)) if v} for r in rows]}
        for st, (title, rows) in fmt["statements"].items()}}
    if fmt.get("conventions"):
        out["conventions"] = dict(fmt["conventions"])
    return out


# ------------------------------------------------------------------ formulas
OPERATOR = re.compile(r"(?:^|\s)([+\-−])\s")


def formula_terms(formula: str) -> list[tuple[int, str]]:
    """'Revenue + Cost of Goods Sold - 5' -> [(1, 'Revenue'), (1, 'Cost of Goods Sold'), (-1, '5')].
    Operators need spaces around them, so hyphens inside labels ('Non-Current') are safe."""
    terms, sign, pos = [], 1, 0
    text = " " + formula.strip()
    for m in OPERATOR.finditer(text):
        part = text[pos:m.start()].strip()
        if part:
            terms.append((sign, part))
        sign = -1 if m.group(1) in "-−" else 1
        pos = m.end()
    part = text[pos:].strip()
    if part:
        terms.append((sign, part))
    return terms


def formula_refs(statement: str, rows: list[Row]) -> dict[str, str]:
    """label (normalised) / key -> key, for resolving formula references within a statement."""
    refs = {r.key: r.key for r in rows}
    refs.update({normalise_label(r.label): r.key for r in rows})
    return refs


def formula_problems(fmt: dict) -> list[str]:
    """References in formulas that don't name a row of the same statement."""
    problems = []
    for st, (_, rows) in fmt["statements"].items():
        refs = formula_refs(st, rows)
        for r in rows:
            for _, ref in formula_terms(r.formula) if r.formula else []:
                if not re.fullmatch(r"\d+(?:\.\d+)?", ref) and ref not in refs and normalise_label(ref) not in refs:
                    problems.append(f"{STATEMENT_TITLES[st]}: “{r.label}” uses “{ref}”, which isn't a row of that statement")
    return problems


# ------------------------------------------------------------------ lookups used by the pipeline
# Back-compatible views of the built-ins: FORMATS[id][statement] = (title, rows)
FORMATS = {fid: fmt["statements"] for fid, fmt in BUILTIN.items()}
STATEMENTS = FORMATS.get("general") or next(iter(FORMATS.values()))


def data_rows(statement: str, fmt: str | None = None, formats: dict | None = None) -> list[Row]:
    """Rows that hold numbers; with no format, every format's rows (unique keys)."""
    formats = formats or BUILTIN
    ids = [fmt] if fmt in formats else list(formats)
    rows = {r.key: r for f in ids for r in formats[f]["statements"][statement][1] if r.kind != "header"}
    return list(rows.values())


def rows_for(statement: str, fmt: str | None, formats: dict | None = None) -> list[Row]:
    formats = formats or BUILTIN
    chosen = formats.get(fmt or "general") or formats.get("general") or next(iter(formats.values()))
    return chosen["statements"][statement][1]


def title_for(statement: str, fmt: str | None, formats: dict | None = None) -> str:
    formats = formats or BUILTIN
    chosen = formats.get(fmt or "general") or formats.get("general") or next(iter(formats.values()))
    return chosen["statements"][statement][0]


def labels(formats: dict | None = None) -> dict[str, str]:
    formats = formats or BUILTIN
    return {r.key: r.label for f in formats.values() for _, rows in f["statements"].values() for r in rows}
