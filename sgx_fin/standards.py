"""Learn a statement standard from a finished workbook (Excel / CSV) and say which companies use it.

The user uploads results they already have in the layout they like: one company's workbook, or a whole
listing (one sheet per company, or one table with a company column). For every company found the
program reads the lines of the income statement, balance sheet and cash flow in order, with their
figures; companies laid out alike are grouped into one *standard* ("Standard 1 for X and Y").
Each standard records
  - its lines, their order, headings and totals (a statement format, see template.py), each line
    mapped to the app's own key of the same meaning so checks and derived lines keep working;
  - the unit the figures are in ('000, millions...), from the sheet's own words;
  - the column order (latest period first or last), from the period headers;
  - the sign convention: costs and outflows shown as positive numbers get sign -1, so new figures
    (which this app keeps with costs negative) are written the user's way;
  - lines that are sums of others ("Gross Profit = Revenue - Cost of Sales"), which become formulas
    once the figures in the file prove them.
The AI (when a key is set) names each standard, maps lines the program can't place, says which
lines are costs or outflows and proposes the sums; the program checks everything it says against
the file's figures. Nothing is stored on the server: the web app keeps the standards and sends the
company's one with each extraction.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import zlib
from datetime import date, datetime
from pathlib import Path

from . import formats as F
from . import template as T

GENERIC_SHEET = re.compile(r"^(sheet\s*\d*|data|export|financials?|report|results?|statements?|summary|all)$", re.I)
INTERNAL_SHEETS = {"checks", "source map", "_store", "cogs basis", "transcription", "_cogs", "_tables"}
COMPANY_HEAD = re.compile(r"^(company( name)?|name|issuer|stock( name)?|counter( name)?|security|entity)$", re.I)
CODE_HEAD = re.compile(r"^(stock code|code|ticker|symbol|sgx code|counter code|stock symbol)$", re.I)
STATEMENT_HEAD = re.compile(r"^(statement|report|section|table|sheet)$", re.I)
LABEL_HEAD = re.compile(r"^(line ?item|item|items|account|accounts|description|particulars|field|metric|line|label|caption)$", re.I)
CODE = re.compile(r"^[A-Z0-9]{2,5}(?:\.SI)?$")
NAME_CODE = re.compile(r"^(?P<name>[^()]{3,}?)\s*\((?:SGX\s*[:：]\s*)?(?P<code>[A-Z0-9]{2,5})(?:\.SI)?\)")
CODE_LINE = re.compile(r"\b(?:stock code|ticker|sgx code|symbol|code)\s*[:：]\s*(?P<code>[A-Z0-9]{2,5})\b", re.I)
NAME_LINE = re.compile(r"^(?:company(?: name)?|name|issuer)\s*[:：]\s*(?P<name>.{3,})$", re.I)

UNIT_WORDS = [  # the first that matches the sheet's own words decides
    ("billions", re.compile(r"\bbillions?\b|\bbn\b|\$\s?b\b|'000,000,000", re.I)),
    ("millions", re.compile(r"\bmillions?\b|\bmil\b|\bmn\b|\bmm\b|[$€£¥]\s?m\b|\b(?:sgd|usd|rmb|hkd|myr|s\$)\s?m\b|'000,000|\(m\)", re.I)),
    ("thousands", re.compile(r"'\s?000|’000|\(000\)|\bthousands?\b|\b000s\b|\bk\b|\$\s?k\b|\bin 000\b", re.I)),
    ("units", re.compile(r"\bin units\b|\bunits\b|\bactual\b|\bin (?:sgd|s\$|usd|dollars)\b(?!\s?(?:'|’)?000|\s?m\b)", re.I)),
]
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# Costs and outflows: this app keeps them negative; a workbook that shows them positive gets sign -1.
OUTFLOW = re.compile(
    r"\b(cost|costs|expenses?|expenditure|tax(ation)?|depreciation|amorti[sz]ation|impairment|allowances?|"
    r"provisions? for|finance costs?|interest expense|interest expended|purchases?|acquisitions? of|payments?|paid|"
    r"repayments?|redemption|outflow|losses on|staff|employee benefits?|salaries|wages|raw materials|consumables|"
    r"distributions? to|dividends? paid|capex|capital expenditure)\b", re.I)
NOT_OUTFLOW = re.compile(r"^(total )?(revenue|income(?! tax)|sales|turnover|profit|gross profit|net profit|receipts?)\b|"
                         r"\b(before|after) (tax|taxation)\b|\bproceeds\b|\brefund\b|\bnet cash\b|\bincome tax recoverable\b|"
                         r"\b(payables?|liabilit(y|ies)|provisions?|deferred tax|tax payable|accrued)\b", re.I)
IS_START = re.compile(r"^(total )?(revenue|turnover|sales|gross income|interest income|total income|interest earned)\b", re.I)
BS_START = re.compile(r"^(non[- ]current assets|current assets|assets|total assets|property,? plant|cash and (bank|short)|"
                      r"cash (and|&) cash equivalents$)", re.I)
CF_START = re.compile(r"(operating activities|cash flows? from operat|cash generated from operations|"
                      r"operating cash flow)", re.I)
MATCH_CO = 0.8  # share of lines two companies have in common for one standard


# ------------------------------------------------------------------ reading cells
def _num(v):
    """A cell as a number: 1234, '1,234', '(1,234)', '-1,234', '−5'; None otherwise."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("−", "-").replace(" ", "")
    m = re.fullmatch(r"(\()?([+-])?([\d,]*\.?\d+)(\))?%?", s)
    if not m or not re.search(r"\d", s) or ("," in m.group(3) and not re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", m.group(3))):
        return None
    n = float(m.group(3).replace(",", ""))
    return -n if (m.group(1) and m.group(4)) or m.group(2) == "-" else n


def _period_key(v) -> tuple[int, int] | None:
    """A period header as (year, month): 'FY2024', 'FY24', '1H2025', 'Dec 2024', '31/12/2024', a date."""
    if isinstance(v, (datetime, date)):
        return (v.year, v.month)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (int(v), 12) if 1990 <= v <= 2100 and float(v).is_integer() else None
    s = str(v or "").strip()
    if not s or len(s) > 40:
        return None
    month = next((n for m, n in MONTHS.items() if re.search(rf"\b{m}[a-z]*\b", s, re.I)), 0)
    y = re.search(r"(?<!\d)(19[89]\d|20\d\d)(?!\d)", s)
    if y:
        if not month:
            d = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](19|20)\d\d\b", s)
            month = int(d.group(2)) if d and 1 <= int(d.group(2)) <= 12 else 0
            h = re.search(r"\b([1-4])\s?Q|\bQ\s?([1-4])\b|\b([12])\s?H|\bH\s?([12])\b|\b([39])\s?M\b", s, re.I)
            if h:
                q, q2, hh, h2, nine = h.groups()
                month = 3 * int(q or q2) if (q or q2) else 6 * int(hh or h2) if (hh or h2) else int(nine)
        return (int(y.group(1)), month or 12)
    fy = re.search(r"\b(?:FY|[1-4]Q|Q[1-4]|[12]H|H[12]|[39]M)\s?'?(\d{2})\b", s, re.I)
    if fy:
        return (2000 + int(fy.group(1)), month or 12)
    return None


def _sheets_from_xlsx(data: bytes) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True)
    out = []
    for ws in wb.worksheets:
        if ws.sheet_state != "visible" or ws.title.strip().lower() in INTERNAL_SHEETS:
            continue
        rows = []
        for row in ws.iter_rows():
            cells = []
            for c in row:
                if c.value is None or not str(c.value).strip():
                    continue
                fill = c.fill
                filled = bool(fill and fill.fill_type == "solid"
                              and str(getattr(fill.fgColor, "rgb", "") or "").upper() not in ("00000000", "FFFFFFFF"))
                cells.append({"v": c.value, "col": c.column, "bold": bool(c.font and c.font.b), "filled": filled})
            rows.append(cells)
        ours = str(ws["A4"].value or "").strip() == "Period"  # a workbook made by this app
        out.append({"title": ws.title.strip(), "rows": rows, "ours": ours})
    return out


def _sheets_from_csv(data: bytes, title: str) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for raw in csv.reader(io.StringIO(text), dialect):
        rows.append([{"v": v.strip(), "col": i + 1, "bold": False, "filled": False} for i, v in enumerate(raw) if v.strip()])
    return [{"title": title, "rows": rows, "ours": False}]


# ------------------------------------------------------------------ companies in the file
def _company_from_text(texts: list[str]) -> dict:
    out = {"name": "", "code": ""}
    for t in texts:
        t = str(t).strip()
        m = NAME_CODE.match(t)
        if m and not out["code"]:
            out["name"], out["code"] = m.group("name").strip(" -–:"), m.group("code")
        m = CODE_LINE.search(t)
        if m and not out["code"]:
            out["code"] = m.group("code").upper()
        m = NAME_LINE.match(t)
        if m and not out["name"]:
            out["name"] = m.group("name").strip()
    return out


def _company_from_key(key: str) -> dict:
    key = re.sub(r"\.(xlsx|xlsm|csv|txt)$", "", str(key or "").strip(), flags=re.I).strip(" _-–")
    if not key or GENERIC_SHEET.match(key):
        return {"name": "", "code": ""}
    m = NAME_CODE.match(key)
    if m:
        return {"name": m.group("name").strip(), "code": m.group("code")}
    if CODE.match(key.upper()) and re.search(r"\d", key) or re.fullmatch(r"[A-Z]{1,2}\d{1,3}[A-Z]?|[A-Z]\d[A-Z0-9]{1,2}", key):
        return {"name": "", "code": key.upper().removesuffix(".SI")}
    return {"name": key.replace("_", " ").strip(), "code": ""}


def _statement_of(text) -> str | None:
    s = str(text or "").strip()
    st = F._section_of(s)
    if st:
        return st
    low = s.lower()
    if re.fullmatch(r"is|p&l|pnl|p ?& ?l|income|profit (and|&) loss|pl", low):
        return "income_statement"
    if re.fullmatch(r"bs|balance|financial position|sofp", low):
        return "balance_sheet"
    if re.fullmatch(r"cf|cfs|cash ?flows?|cash", low):
        return "cash_flow"
    return None


STATEMENT_WORDS = [
    ("income_statement", re.compile(r"income statements?|profit (?:and|or|&) loss|\bp ?& ?l\b|statements? of (?:comprehensive )?income", re.I)),
    ("balance_sheet", re.compile(r"balance sheets?|financial position", re.I)),
    ("cash_flow", re.compile(r"cash ?flows?(?: statements?)?|statements? of cash ?flows?", re.I)),
    ("income_statement", re.compile(r"\bIS\b|\bPL\b")),
    ("balance_sheet", re.compile(r"\bBS\b")),
    ("cash_flow", re.compile(r"\bCF\b")),
]


def sheet_statement(title: str) -> tuple[str | None, str]:
    """('D05 Income Statement') -> ('income_statement', 'D05'); ('Income Statement') -> (.., '');
    a sheet not named after a statement -> (None, title)."""
    for st, rx in STATEMENT_WORDS:
        if rx.search(title):
            rest = re.sub(r"\s+", " ", rx.sub(" ", title)).strip(" -–_|:()")
            rest = re.sub(r"^(consolidated|group|statements?|of)\b\s*", "", rest, flags=re.I).strip(" -–_|:()")
            return st, rest
    return None, title


def _listing_header(rows: list[list[dict]]) -> dict | None:
    """A table with a company (or stock code) column: {row, company, code, statement, label} column numbers."""
    for i, cells in enumerate(rows[:15]):
        cols = {"company": None, "code": None, "statement": None, "label": None}
        for c in cells:
            v = str(c["v"]).strip()
            if COMPANY_HEAD.match(v) and cols["company"] is None:
                cols["company"] = c["col"]
            elif CODE_HEAD.match(v) and cols["code"] is None:
                cols["code"] = c["col"]
            elif STATEMENT_HEAD.match(v):
                cols["statement"] = c["col"]
            elif LABEL_HEAD.match(v):
                cols["label"] = c["col"]
        if cols["company"] or cols["code"]:
            return {"row": i, **cols}
    return None


def _period_row(rows: list[list[dict]], limit: int = 15) -> tuple[int, list] | None:
    """The header row with the most period headers among the first rows: (index, [(col, (y, m))])."""
    best = None
    for i, cells in enumerate(rows[:limit]):
        keys = [(c["col"], _period_key(c["v"])) for c in cells]
        keys = [(col, k) for col, k in keys if k]
        if len(keys) >= 2 and (best is None or len(keys) > len(best[1])):
            best = (i, keys)
    return best


def _line(cells: list[dict], skip_cols: set) -> dict | None:
    cells = [c for c in cells if c["col"] not in skip_cols]
    label = next((c for c in cells if F._is_label(c["v"])), None)
    if label is None:
        return None
    values = [(c["col"], _num(c["v"])) for c in cells if c is not label and c["col"] > label["col"]]
    values = [(col, v) for col, v in values if v is not None]
    return {"label": re.sub(r"\s+", " ", str(label["v"])).strip(), "values": values, "bold": label["bold"], "filled": label["filled"]}


def _blocks(sheets: list[dict], filename: str) -> list[dict]:
    """Companies in the file, each {"name", "code", "source", "texts", "periods", "lines": {st or None: [line]}}."""
    stem = Path(filename).stem
    blocks: dict[str, dict] = {}

    def block(key: str, source: str) -> dict:
        return blocks.setdefault(key, {"key": key, "source": source, "texts": [], "periods": [], "lines": []})

    named = {id(sh): sheet_statement(sh["title"]) for sh in sheets}
    any_named = any(st for st, _ in named.values())
    for sh in sheets:
        rows = sh["rows"][7:] if sh["ours"] else sh["rows"]
        head_texts = [str(c["v"]) for cells in sh["rows"][:8] for c in cells if isinstance(c["v"], str)]
        listing = _listing_header(rows)
        if listing:
            hdr = rows[listing["row"]]
            periods = [(c["col"], _period_key(c["v"])) for c in hdr]
            periods = [(col, k) for col, k in periods if k]
            skip = {listing[k] for k in ("company", "code", "statement") if listing[k]}
            for cells in rows[listing["row"] + 1:]:
                by_col = {c["col"]: c["v"] for c in cells}
                name = str(by_col.get(listing["company"], "") or "").strip()
                code = str(by_col.get(listing["code"], "") or "").strip().upper()
                if not (name or code):
                    continue
                b = block(f"{code}|{name}".upper(), f"{sh['title']} (rows for {code or name})")
                b.setdefault("name", name)
                b.setdefault("code", code)
                b["texts"] = head_texts
                b["periods"] = periods
                lab_cells = [c for c in cells if c["col"] not in skip]
                if listing["label"]:
                    lab_cells = [c for c in lab_cells if c["col"] >= listing["label"]]
                ln = _line(lab_cells, set())
                if ln:
                    ln["statement"] = _statement_of(by_col.get(listing["statement"])) if listing["statement"] else None
                    b["lines"].append(ln)
            continue
        st, key = named[id(sh)]
        if any_named and not st:
            continue  # a cover or ratios sheet next to the statement sheets
        if not st and (len(sheets) == 1 or GENERIC_SHEET.match(key)):
            key = ""
        b = block(key.upper(), sh["title"] if key else filename)
        b["texts"] += [sh["title"], *head_texts]
        pr = _period_row(sh["rows"])
        skip_row = pr[0] if pr else None
        if pr and not b["periods"]:
            b["periods"] = pr[1]
        if st:
            b["lines"].append({"label": T.STATEMENT_TITLES[st], "values": [], "bold": False, "filled": False, "section": st})
        for i, cells in enumerate(rows):
            if (i + (7 if sh["ours"] else 0)) == skip_row:
                continue
            ln = _line(cells, set())
            if ln:
                b["lines"].append(ln)

    out = []
    for key, b in blocks.items():
        who = {"name": b.get("name", ""), "code": b.get("code", "")}
        found = _company_from_text(b["texts"])
        keyed = _company_from_key(b["key"] if b["key"] else "") if "|" not in b["key"] else {"name": "", "code": ""}
        fromfile = _company_from_key(stem) if not b["key"] else {"name": "", "code": ""}
        for src in (found, keyed, fromfile):
            for k in ("name", "code"):
                who[k] = who[k] or src[k]
        if who["name"] and not who["code"] and CODE.match(who["name"].upper()) and re.search(r"\d", who["name"]):
            who = {"name": "", "code": who["name"].upper()}
        statements = _sections(b["lines"])
        if sum(len(v) for v in statements.values()) < 5:
            continue
        out.append({"name": who["name"], "code": who["code"], "source": b["source"], "texts": b["texts"][:40],
                    "periods": b["periods"], "statements": statements})
    return out


def _sections(lines: list[dict]) -> dict[str, list[dict]]:
    """Lines split into statements (by their headings, a statement column, or where each statement's
    first lines start), with kinds (header / item / total) and figures."""
    sections = {}
    current = None
    explicit = any(ln.get("section") or ln.get("statement") or (not ln["values"] and F._section_of(ln["label"]))
                   for ln in lines)
    tagged = []
    for ln in lines:
        st = ln.get("section") or ln.get("statement")
        if not st and not ln["values"]:
            st = F._section_of(ln["label"])
            if st:
                current = st
                continue
        if ln.get("section"):
            current = st
            continue
        if ln.get("statement"):
            current = ln["statement"]
        if not explicit:  # no headings at all: each statement starts at its typical first lines
            order = list(T.STATEMENT_IDS)
            if current is None:
                current = "income_statement"
            if order.index(current) < 1 and BS_START.search(ln["label"]):
                current = "balance_sheet"
            elif order.index(current) < 2 and CF_START.search(ln["label"]):
                current = "cash_flow"
        if current is None or F.SKIP.match(ln["label"]):
            continue
        tagged.append((current, ln))
    for st in T.STATEMENT_IDS:
        mine = [ln for s, ln in tagged if s == st]
        seen, uniq = set(), []
        for ln in mine:  # a long table repeats each line once per period: keep the first, gather the figures
            n = T.normalise_label(ln["label"])
            if n in seen:
                prev = next(u for u in uniq if T.normalise_label(u["label"]) == n)
                prev["values"] += ln["values"]
                continue
            seen.add(n)
            uniq.append({**ln, "values": list(ln["values"])})
        if not uniq:
            continue
        kinds = F._rows_from_lines([(u["label"], bool(u["values"]), u["bold"], u["filled"]) for u in uniq], default=st).get(st, [])
        if len(kinds) != len(uniq):  # rows the shared reader drops (page furniture): keep ours in step
            kinds = [{"label": u["label"], "kind": "total" if u["bold"] else "item"} for u in uniq]
        sections[st] = [{"label": k["label"], "kind": k["kind"], "values": [v for _, v in u["values"]]}
                        for k, u in zip(kinds, uniq)]
    return sections


# ------------------------------------------------------------------ grouping and conventions
def _labels(block: dict) -> set[str]:
    return {f"{st}|{T.normalise_label(r['label'])}" for st, rows in block["statements"].items() for r in rows}


def group(blocks: list[dict]) -> list[dict]:
    """Companies whose lines are (nearly) the same form one standard; its lines are the largest
    member's, with lines only others have put in after the line they follow."""
    clusters: list[dict] = []
    for b in sorted(blocks, key=lambda b: -len(_labels(b))):
        mine = _labels(b)
        best, score = None, 0.0
        for c in clusters:
            j = len(mine & c["set"]) / max(1, len(mine | c["set"]))
            if j > score:
                best, score = c, j
        if best is not None and score >= MATCH_CO:
            best["members"].append(b)
            for st, rows in b["statements"].items():
                have = best["statements"].setdefault(st, [])
                names = [T.normalise_label(r["label"]) for r in have]
                for k, r in enumerate(rows):
                    n = T.normalise_label(r["label"])
                    if n in names:
                        continue
                    prev = T.normalise_label(rows[k - 1]["label"]) if k else None
                    at = names.index(prev) + 1 if prev in names else len(have)
                    have.insert(at, {"label": r["label"], "kind": r["kind"]})
                    names.insert(at, n)
            best["set"] |= mine
        else:
            clusters.append({"members": [b], "set": set(mine),
                             "statements": {st: [{"label": r["label"], "kind": r["kind"]} for r in rows]
                                            for st, rows in b["statements"].items()}})
    return clusters


def _figures(cluster: dict, st: str, label: str) -> list[float]:
    n = T.normalise_label(label)
    return [v for m in cluster["members"] for r in m["statements"].get(st, []) if T.normalise_label(r["label"]) == n
            for v in r["values"]]


def detect_unit(texts: list[str]) -> tuple[str | None, str]:
    """The unit the sheet's own words give, and those words."""
    for t in texts:
        for unit, rx in UNIT_WORDS:
            m = rx.search(str(t))
            if m and len(str(t)) < 120:
                return unit, str(t).strip()
    return None, ""


def detect_order(periods: list) -> bool | None:
    """True when the latest period is the leftmost column, False when it is the rightmost."""
    keys = [k for _, k in sorted(periods)]
    if len(keys) < 2:
        return None
    up = sum(1 for a, b in zip(keys, keys[1:]) if b > a)
    down = sum(1 for a, b in zip(keys, keys[1:]) if b < a)
    return None if up == down else down > up


def is_outflow(label: str) -> bool:
    return bool(OUTFLOW.search(label)) and not NOT_OUTFLOW.search(label)


def _signs(cluster: dict, outflow: dict) -> dict:
    """sign -1 for outflow lines the file shows as positive numbers; lines without figures follow
    the other outflow lines of their statement."""
    signs, votes = {}, {}
    for st, rows in cluster["statements"].items():
        if st == "balance_sheet":
            continue
        for r in rows:
            if r["kind"] == "header" or not outflow.get((st, r["label"])):
                continue
            vals = [v for v in _figures(cluster, st, r["label"]) if v]
            if vals:
                pos = sum(v > 0 for v in vals) > len(vals) / 2
                signs[(st, r["label"])] = -1 if pos else 1
                votes.setdefault(st, []).append(pos)
        for r in rows:
            if (st, r["label"]) not in signs and outflow.get((st, r["label"])) and r["kind"] != "header":
                v = votes.get(st) or []
                signs[(st, r["label"])] = -1 if v and sum(v) > len(v) / 2 else 1
    return signs


def _check_formula(cluster: dict, st: str, target: str, formula: str) -> bool:
    """A sum the AI proposed, in the file's own signs, holds wherever the file has all its figures."""
    rows = {T.normalise_label(r["label"]) for r in cluster["statements"].get(st, [])}
    terms = T.formula_terms(formula)
    if not terms or any(not re.fullmatch(r"\d+(\.\d+)?", ref) and T.normalise_label(ref) not in rows for _, ref in terms):
        return False
    held = failed = 0
    for m in cluster["members"]:
        by = {T.normalise_label(r["label"]): r["values"] for r in m["statements"].get(st, [])}
        tv = by.get(T.normalise_label(target)) or []
        for i, t in enumerate(tv):
            total, ok = 0.0, True
            for s, ref in terms:
                if re.fullmatch(r"\d+(\.\d+)?", ref):
                    total += s * float(ref)
                    continue
                vals = by.get(T.normalise_label(ref)) or []
                if i >= len(vals):
                    ok = False
                    break
                total += s * vals[i]
            if not ok:
                continue
            if abs(total - t) <= max(1.0, abs(t) * 0.001) + 0.5 * len(terms):
                held += 1
            else:
                failed += 1
    return held > 0 and failed <= held // 10


def _app_formula(formula: str, st: str, target: str, signs: dict) -> str:
    """A sum in the file's signs, rewritten in this app's (costs negative)."""
    t = signs.get((st, target), 1)
    by_norm = {T.normalise_label(lbl): sg for (s, lbl), sg in signs.items() if s == st}
    parts = []
    for s, ref in T.formula_terms(formula):
        sign = s * t * (1 if re.fullmatch(r"\d+(\.\d+)?", ref) else by_norm.get(T.normalise_label(ref), 1))
        parts.append(("- " if sign < 0 else "+ ") + ref)
    text = " ".join(parts)
    return text[2:] if text.startswith("+ ") else text


# ------------------------------------------------------------------ the AI's review
KIND_TYPES = ("general", "bank", "reit", "insurance", "other")
AI_SYSTEM = """You review financial statement layouts (income statement, balance sheet, cash flow) taken from a user's own
Excel workbooks of SGX-listed companies. Each standard below is the layout several companies share. For each one:
- `name`: a short name for the layout (e.g. "General — expenses by function", "Bank", "REIT"); `applies_to`: which
  companies it suits, in one sentence; `company_type`: general | bank | reit | insurance | other.
- `unit`: the unit the figures are in (units, thousands, millions, billions; unknown when nothing shows it),
  judged from the header text and the size of the figures; `unit_evidence`: the words or reasoning.
- `latest_first`: true when the leftmost period column is the most recent one.
- For every row: `key`, the app key of the same meaning from the list (\"\" when none means the same; never use one
  key twice in a statement); `kind` (header: a title without figures, item, total: a subtotal or total);
  `outflow`: true for costs, expenses, tax, losses, cash paid and other outflows (not for liabilities); `formula`:
  for a total that is the sum of rows above it in the file's own signs, e.g. "Revenue - Cost of sales", using the
  rows' labels exactly and + and - with spaces around them (\"\" otherwise).
- `notes`: anything unusual about the layout, in one sentence.
"""


def _ai_schema() -> dict:
    row = {"type": "object", "properties": {
        "statement": {"type": "string", "enum": list(T.STATEMENT_IDS)}, "row": {"type": "integer"},
        "key": {"type": "string"}, "kind": {"type": "string", "enum": list(T.KINDS)},
        "outflow": {"type": "boolean"}, "formula": {"type": "string"}},
        "required": ["statement", "row", "key", "kind", "outflow", "formula"], "additionalProperties": False}
    std = {"type": "object", "properties": {
        "standard": {"type": "integer"}, "name": {"type": "string"}, "applies_to": {"type": "string"},
        "company_type": {"type": "string", "enum": list(KIND_TYPES)},
        "unit": {"type": "string", "enum": [*T.UNIT_NAMES, "unknown"]}, "unit_evidence": {"type": "string"},
        "latest_first": {"type": "boolean"}, "notes": {"type": "string"},
        "rows": {"type": "array", "items": row}},
        "required": ["standard", "name", "applies_to", "company_type", "unit", "unit_evidence", "latest_first", "notes", "rows"],
        "additionalProperties": False}
    return {"type": "object", "properties": {"standards": {"type": "array", "items": std}},
            "required": ["standards"], "additionalProperties": False}


def _fmt_num(v: float) -> str:
    return f"({abs(v):,.0f})" if v < 0 else f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"


def ai_prompt(clusters: list[dict]) -> str:
    keys = []
    for st in T.STATEMENT_IDS:
        rows = {r.key: r.label for f in T.BUILTIN.values() for r in f["statements"][st][1] if r.kind != "header"}
        keys.append(f"{st}: " + "; ".join(f"{k} ({v})" for k, v in rows.items()))
    parts = ["App keys by statement:\n" + "\n".join(keys)]
    for n, c in enumerate(clusters, 1):
        m = c["members"]
        who = ", ".join(x["code"] or x["name"] or x["source"] for x in m[:8]) + (f" and {len(m) - 8} more" if len(m) > 8 else "")
        head = " | ".join(dict.fromkeys(t for t in m[0]["texts"][:12] if t))[:600]
        periods = " | ".join(f"{y}-{mo:02d}" for _, (y, mo) in sorted(m[0]["periods"]))
        lines = [f"\n# Standard {n} ({len(m)} compan{'y' if len(m) == 1 else 'ies'}: {who})",
                 f"Header text: {head}", f"Period columns left to right: {periods or 'not found'}"]
        for st, rows in c["statements"].items():
            lines.append(f"## {st}")
            for i, r in enumerate(rows[:250]):
                figs = "; ".join(_fmt_num(v) for v in _figures(c, st, r["label"])[:3])
                lines.append(f"{i} | {r['label']} | {r['kind']} | {figs}")
        parts.append("\n".join(lines))
    return "\n".join(parts)


def ai_review(clusters: list[dict], deadline: float | None = None) -> tuple[dict[int, dict], str]:
    """{standard number: the AI's answer} for the first standards (the largest), and the model."""
    from . import extractor as X

    prov = X.provider()
    if not prov or not clusters:
        return {}, ""
    instruction = ai_prompt(clusters) + "\n\nReview every standard above and return the JSON."
    model = X.default_model(prov)
    if prov == "anthropic":
        ex = X._claude(None, instruction, model, "medium", None, system=AI_SYSTEM, schema=_ai_schema())
    else:
        ex = X._gemini(None, instruction, model, "medium", None, deadline=deadline, system=AI_SYSTEM, schema_json=_ai_schema())
    return {int(s.get("standard", 0)): s for s in (ex.data.get("standards") or []) if isinstance(s, dict)}, ex.model


# ------------------------------------------------------------------ the result
def _fid(cluster: dict) -> str:
    sig = json.dumps([[st, [T.normalise_label(r["label"]) for r in rows]] for st, rows in sorted(cluster["statements"].items())])
    return "std_" + hashlib.sha1(sig.encode()).hexdigest()[:8]


def build_standard(n: int, cluster: dict, ai: dict | None) -> dict:
    ai = ai or {}
    m = cluster["members"]
    # Unit: the sheets' own words first, then the AI's judgement, else the app's '000.
    found = [detect_unit(x["texts"]) for x in m]
    units = [u for u, _ in found if u]
    unit = max(set(units), key=units.count) if units else (ai.get("unit") if ai.get("unit") in T.UNIT_NAMES else None)
    unit_why = next((w for u, w in found if u == unit and w), "") if units else (ai.get("unit_evidence") or "")
    orders = [o for o in (detect_order(x["periods"]) for x in m) if o is not None]
    latest = (sum(orders) >= len(orders) / 2) if orders else (ai["latest_first"] if isinstance(ai.get("latest_first"), bool) else None)
    order_why = "from the period headers" if orders else ("judged by the AI" if latest is not None else "")

    answers = {(r.get("statement"), r.get("row")): r for r in ai.get("rows") or [] if isinstance(r, dict)}
    builtin_keys = {st: {r.key for f in T.BUILTIN.values() for r in f["statements"][st][1] if r.kind != "header"}
                    for st in T.STATEMENT_IDS}
    outflow, rows_out, formulas = {}, {}, []
    for st, rows in cluster["statements"].items():
        used = set()
        for i, r in enumerate(rows):
            a = answers.get((st, i)) or {}
            kind = a.get("kind") if a.get("kind") in T.KINDS else r["kind"]
            if kind == "header" and _figures(cluster, st, r["label"]):
                kind = "item"
            row = {"label": r["label"], "kind": kind}
            key = str(a.get("key") or "").strip()
            if key in builtin_keys[st] and key not in used and kind != "header":
                row["key"] = key
                used.add(key)
            outflow[(st, r["label"])] = kind != "header" and (a["outflow"] if isinstance(a.get("outflow"), bool)
                                                              else is_outflow(r["label"]))
            row["_formula"] = str(a.get("formula") or "").strip() if kind == "total" else ""
            rows_out.setdefault(st, []).append(row)
    signs = _signs(cluster, outflow)
    for st, rows in rows_out.items():
        for row in rows:
            f = row.pop("_formula")
            if signs.get((st, row["label"])) == -1:
                row["sign"] = -1
            if f and _check_formula(cluster, st, row["label"], f):
                row["formula"] = _app_formula(f, st, row["label"], signs)
                formulas.append(f"{row['label']} = {f}")
    kind = ai.get("company_type") if ai.get("company_type") in KIND_TYPES else guess_type(cluster)
    prefer = T.BUILTIN.get("bank" if kind == "bank" else "general")
    base = T.to_json(prefer, hints=False)
    statements = {st: {"title": T.STATEMENT_TITLES[st], "rows": rows_out[st]} if rows_out.get(st) else base["statements"][st]
                  for st in T.STATEMENT_IDS}
    fid = _fid(cluster)
    name = (ai.get("name") or "").strip() or f"Standard {n}"
    conv = {k: v for k, v in (("unit", unit), ("latest_first", latest)) if v is not None}
    fmt = T.check_format({"id": fid, "name": name, "statements": statements, "conventions": conv,
                          "applies_to": (ai.get("applies_to") or "").strip()
                          or f"The layout of {', '.join(x['code'] or x['name'] for x in m[:3] if x['code'] or x['name']) or 'your workbook'}"},
                         prefer=prefer)
    flipped = sum(1 for _, rows in fmt["statements"].values() for r in rows if r.sign == -1)
    costs = [s for (st, lbl), s in signs.items()]
    expenses = "positive" if costs and all(s == -1 for s in costs) else "negative" if all(s == 1 for s in costs) else "mixed"
    return {
        "id": fid, "name": name, "applies_to": fmt["applies_to"], "company_type": kind,
        "format": T.to_json(fmt, hints=False), "marked": F.to_marked(T.to_json(fmt, hints=False)),
        "conventions": {"unit": unit or "thousands", "unit_found": bool(unit), "unit_evidence": unit_why,
                        "latest_first": T.LATEST_FIRST if latest is None else latest, "order_found": latest is not None,
                        "order_evidence": order_why, "expenses": expenses, "flipped": flipped},
        "rows": {st: len(rows_out.get(st) or []) for st in T.STATEMENT_IDS},
        "missing": [T.STATEMENT_TITLES[st] for st in T.STATEMENT_IDS if not rows_out.get(st)],
        "mapped": sum(1 for st, (_, rows) in fmt["statements"].items() if rows_out.get(st)
                      for r in rows if r.key in builtin_keys[st]),
        "formulas": formulas,
        "companies": [{"name": x["name"], "code": x["code"], "source": x["source"]} for x in m],
        "notes": (ai.get("notes") or "").strip(),
        "token": encode([(T.to_json(fmt, hints=False), [])]),
    }


def guess_type(cluster: dict) -> str:
    """Without the AI: a bank by its lines (interest income and expense, deposits, loans), a REIT by net
    property income, an insurer by premiums; else general."""
    text = " ".join(cluster["set"])
    if re.search(r"interest (income|earned)", text) and re.search(r"deposits|loans and advances|advances", text):
        return "bank"
    if "net property income" in text or "distribution per unit" in text:
        return "reit"
    if re.search(r"premiums?", text) and re.search(r"claims", text):
        return "insurance"
    return "general"


AI_STANDARDS = 8  # the AI reviews this many standards (the largest); the rest are the program's reading


def learn(filename: str, data: bytes, deadline: float | None = None, use_ai: bool = True) -> dict:
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xlsm")):
        sheets = _sheets_from_xlsx(data)
    elif lower.endswith((".csv", ".txt")):
        sheets = _sheets_from_csv(data, Path(filename).stem)
    else:
        raise ValueError("Upload an Excel (.xlsx) or CSV file")
    blocks = _blocks(sheets, filename)
    if not blocks:
        raise ValueError("No statement lines found. The file needs line labels down a column with figures beside them, "
                         "under Income Statement / Balance Sheet / Cash Flow headings or on sheets named after them.")
    clusters = sorted(group(blocks), key=lambda c: -len(c["members"]))
    answers, model, ai_error = {}, "", ""
    if use_ai:
        try:
            answers, model = ai_review(clusters[:AI_STANDARDS], deadline)
        except Exception as e:  # the program's own reading still stands
            ai_error = f"{type(e).__name__}: {e}"
    standards = [build_standard(n, c, answers.get(n)) for n, c in enumerate(clusters, 1)]
    return {"standards": standards, "companies": len(blocks), "model": model, "ai_error": ai_error,
            "reviewed": min(len(clusters), AI_STANDARDS) if model else 0}


def chosen(raw) -> dict | None:
    """The standard the web app sends for a filing's company, as a format (or None)."""
    if not raw:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    fmt = raw.get("format") if isinstance(raw, dict) and "format" in raw else raw
    if not isinstance(fmt, dict):
        raise ValueError("a standard must be a JSON object")
    return T.check_format(fmt)


# ------------------------------------------------------------------ tokens for the Claude connector
# The server stores nothing, so a standard travels with the request: the web app puts the company's token in
# the message it opens Claude with (or the user pastes a token with all their standards into a Claude
# project), and Claude passes it to the connector's tools as `standard`. A token is the format, compacted
# (statements identical to a built-in one by name, keys only where they can't be worked out from the label),
# with the companies it is for, zlib-compressed and base64url-encoded.
TOKEN_PREFIX = "sgxstd1."
KIND_CODES = {"header": "h", "item": "i", "total": "t"}
KIND_NAMES = {v: k for k, v in KIND_CODES.items()}
CO_SUFFIX = re.compile(r"\b(LIMITED|LTD|PLC|INC|INCORPORATED|CORPORATION|CORP|BHD|BERHAD|TBK|PTE|CO|COMPANY|HOLDINGS?|"
                       r"GROUP|THE|REIT|TRUST)\b")


def _default_key(st: str, label: str, kind: str) -> str:
    return ("h_" + T._slug(label)) if kind == "header" else T.canonical_key(st, label)


def _compact(fmt: dict) -> dict:
    """A format (as to_json gives it) in as few characters as can be read back exactly."""
    out = {"i": fmt["id"], "n": fmt.get("name", ""), "a": fmt.get("applies_to", ""), "s": {}}
    if fmt.get("conventions"):
        out["c"] = fmt["conventions"]
    for st in T.STATEMENT_IDS:
        rows = (fmt.get("statements") or {}).get(st, {}).get("rows") or []
        sig = [(r.get("label"), r.get("kind", "item"), r.get("formula", ""), r.get("sign", 0)) for r in rows]
        same = next((bid for bid, b in T.BUILTIN.items()
                     if sig == [(r.label, r.kind, r.formula, 0) for r in b["statements"][st][1]]), None)
        if same:
            out["s"][st] = "=" + same
            continue
        packed = []
        for r in rows:
            kind = r.get("kind", "item")
            key = r.get("key", "")
            row = [r["label"], KIND_CODES.get(kind, "i"), "" if key == _default_key(st, r["label"], kind) else key,
                   r.get("formula", ""), -1 if r.get("sign") == -1 else 0]
            while row[-1] in ("", 0) and len(row) > 2:
                row.pop()
            packed.append(row)
        out["s"][st] = packed
    return out


def _expand(c: dict) -> dict:
    statements = {}
    for st in T.STATEMENT_IDS:
        rows = (c.get("s") or {}).get(st)
        if isinstance(rows, str) and rows.startswith("="):
            base = T.BUILTIN.get(rows[1:]) or T.BUILTIN["general"]
            statements[st] = T.to_json(base, hints=False)["statements"][st]
            continue
        out = []
        for r in rows or []:
            label, kind, key, formula, sign = (list(r) + ["", "i", "", "", 0][len(r):])[:5]
            row = {"label": label, "kind": KIND_NAMES.get(kind, "item")}
            if key:
                row["key"] = key
            if formula:
                row["formula"] = formula
            if sign == -1:
                row["sign"] = -1
            out.append(row)
        statements[st] = {"title": T.STATEMENT_TITLES[st], "rows": out}
    return {"id": c.get("i") or "std", "name": c.get("n") or "Your standard", "applies_to": c.get("a") or "",
            "conventions": c.get("c") or {}, "statements": statements}


def encode(entries: list) -> str:
    """entries: [(format JSON, [{"code", "name"}, ...]), ...] -> a token. With no companies, the standard is
    for whichever company the token is used with."""
    raw = {"p": [[_compact(fmt), [[c.get("code", ""), c.get("name", "")] for c in companies or []]]
                 for fmt, companies in entries]}
    data = zlib.compress(json.dumps(raw, separators=(",", ":"), ensure_ascii=False).encode(), 9)
    return TOKEN_PREFIX + base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode(token: str) -> list[tuple[dict, list[dict]]]:
    """A token -> [(checked format, companies)]. ValueError when it isn't one or was copied wrongly."""
    token = re.sub(r"\s+", "", str(token or ""))
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError(f"a standard token starts with {TOKEN_PREFIX}")
    body = token[len(TOKEN_PREFIX):]
    try:
        raw = json.loads(zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))
    except Exception:
        raise ValueError("the standard token is damaged (part of it is missing or was copied wrongly)")
    out = []
    for fmt, companies in raw.get("p") or []:
        out.append((T.check_format(_expand(fmt)), [{"code": c[0], "name": c[1]} for c in companies]))
    if not out:
        raise ValueError("the standard token holds no standard")
    return out


def _co_key(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]+", " ", str(s or "").upper())
    return re.sub(r"\s+", " ", CO_SUFFIX.sub(" ", s)).strip()


def same_company(c: dict, code: str = "", name: str = "") -> bool:
    """'DBS GROUP HOLDINGS LTD' is 'DBS Group Holdings Limited'; stock codes decide when both have one."""
    code = str(code or "").strip().upper()
    if c.get("code") and code:
        return c["code"].upper() == code
    x, y = _co_key(c.get("name")), _co_key(name)
    return len(x) >= 2 and len(y) >= 2 and (x == y or y.startswith(x + " ") or x.startswith(y + " "))


def for_company(token: str, code: str = "", name: str = "") -> tuple[dict | None, str]:
    """The company's standard in a token, and a word on what was found. A standard without companies is for
    any company; with several, the one listing this company."""
    entries = decode(token)
    for fmt, companies in entries:
        if not companies or any(same_company(c, code, name) for c in companies):
            return fmt, f"your standard “{fmt['name']}”"
    who = code or name or "this company"
    return None, (f"Your standards don't include {who}: the built-in layout is used. "
                  "(Pass stock_code or company_name so the right standard is picked.)" if not (code or name) else
                  f"Your standards don't include {who}: the built-in layout is used.")
