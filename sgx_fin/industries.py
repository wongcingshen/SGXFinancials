"""Industries, and how each one's cost of goods sold (COGS) is made up when a filing prints none.

The AI names the company's industry from this list (by what it sells and how it earns its
revenue). When the income statement has no cost of sales line, it lists the expense lines on its
face and marks which are COGS by that industry's rule; the program adds the marked lines up and
keeps the list, each line with its figures and why it is in or out, so the workbook shows how the
COGS was made up. The industry is cross-checked against the filing's own words (`guess`).
Depreciation and amortisation follow FactSet's convention (cost of revenue including D&A, with D&A
also shown on its own), as its Fundamentals feed does in the EDGARtoCSV repository.

Built-in rules live in industries/cogs.json. In the web app (Settings > Cost of goods sold) a user
can change an industry's rule, add industries, and give a company its own industry and rule. Those
preferences travel with each extraction as `cogs_rules`:
  {"industries": [{"id", "name", "applies_to", "keywords", "cogs"}, ...],  # an id of a built-in replaces it
   "companies": [{"match": "D05" or a name fragment, "industry": "bank", "cogs": "rule text"}, ...]}
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "industries" / "cogs.json"
FALLBACK = "other"
_ID = re.compile(r"[a-z][a-z0-9_]{0,40}")


def _slug(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")[:40]


def clean(raw) -> dict | None:
    """One industry as given (the built-in file or a user's), or None without an id or name."""
    if not isinstance(raw, dict):
        return None
    iid = str(raw.get("id") or "").strip().lower()
    if not _ID.fullmatch(iid):
        iid = _slug(raw.get("name"))
    if not _ID.fullmatch(iid or ""):
        return None
    words = raw.get("keywords") or []
    if isinstance(words, str):
        words = words.split(",")
    return {"id": iid, "name": str(raw.get("name") or iid.replace("_", " ").title()).strip()[:80],
            "applies_to": str(raw.get("applies_to") or "").strip()[:400],
            "keywords": [w for w in (str(k).strip().lower() for k in words) if w][:40],
            "cogs": str(raw.get("cogs") or "").strip()[:1500]}


def _load() -> list[dict]:
    data = json.loads(FILE.read_text(encoding="utf-8"))
    out = [i for i in (clean(r) for r in data.get("industries") or []) if i]
    if not any(i["id"] == FALLBACK for i in out):
        raise ValueError(f"{FILE} needs an industry with id '{FALLBACK}'")
    return out


BUILTIN = _load()


def resolve(custom: dict | None = None) -> dict:
    """The built-in industries with a user's changes (an industry with a built-in's id replaces it as
    "your version"; a new id is added as "yours"), and the user's company rules."""
    custom = custom if isinstance(custom, dict) else {}
    mine = {i["id"]: i for i in (clean(r) for r in (custom.get("industries") or [])[:60]) if i}
    out = [{**mine[b["id"]], "source": "your version"} if b["id"] in mine else {**b, "source": "built-in"}
           for b in BUILTIN]
    builtin = {b["id"] for b in BUILTIN}
    out += [{**i, "source": "yours"} for i in mine.values() if i["id"] not in builtin]
    known = {i["id"] for i in out}
    companies = []
    for c in (custom.get("companies") or [])[:200]:
        if isinstance(c, dict) and str(c.get("match") or "").strip():
            industry = str(c.get("industry") or "").strip().lower()
            companies.append({"match": str(c["match"]).strip()[:80], "industry": industry if industry in known else "",
                              "cogs": str(c.get("cogs") or "").strip()[:1500]})
    return {"industries": out, "companies": companies}


def company_rule(rules: dict, stock_code: str = "", name: str = "") -> dict | None:
    """The user's rule for this company: by its stock code, or a fragment (3+ letters) of its name."""
    code, company = (stock_code or "").strip().upper(), (name or "").strip().upper()
    for c in rules.get("companies") or []:
        m = c["match"].upper()
        if (code and m == code) or (company and len(m) >= 3 and m in company):
            return c
    return None


def for_filing(custom: dict | None = None, stock_code: str = "", name: str = "") -> dict:
    """The rules that apply to one filing: every industry, and the company's own rule if one matches."""
    rules = resolve(custom)
    rules["company"] = company_rule(rules, stock_code, name)
    return rules


def ids(rules: dict | None = None) -> list[str]:
    return [i["id"] for i in (rules or {"industries": BUILTIN})["industries"]]


def get(rules: dict | None, iid: str) -> dict | None:
    return next((i for i in (rules or {"industries": BUILTIN})["industries"] if i["id"] == iid), None)


def name(rules: dict | None, iid: str) -> str:
    i = get(rules, iid)
    return i["name"] if i else (iid or "").replace("_", " ").title()


def prompt(rules: dict | None = None) -> str:
    """The industries, their COGS rules and the company's own rule, as the AI is given them."""
    rules = rules or for_filing()
    lines = ["Industries: choose `industry` by what the company sells and how it earns its revenue. Each line "
             "gives the industry's COGS rule, used when no cost of sales line is printed:"]
    for i in rules["industries"]:
        mark = " (the user's own rule)" if i.get("source", "built-in") != "built-in" else ""
        lines.append(f"- {i['id']} ({i['name']}; {i['applies_to']}). COGS{mark}: {i['cogs']}")
    c = rules.get("company")
    if c:
        lines.append(f"\nThe user's own rule for this company (\"{c['match']}\"), which comes before the industry rule:"
                     + (f" its industry is {c['industry']}." if c["industry"] else "")
                     + (f" COGS: {c['cogs']}" if c["cogs"] else ""))
    return "\n".join(lines)


def _hit(word: str, text: str) -> bool:
    # Short words must stand alone ("gas" is not "gaskets", "mine" is not "mineral"); longer ones
    # may be stems ("manufactur", "outsourc").
    tail = r"(?![a-z])" if len(word) <= 4 else ""
    return re.search(r"(?<![a-z])" + re.escape(word) + tail, text) is not None


def guess(texts: list[str], rules: dict | None = None) -> tuple[str, list[str]] | None:
    """The industry a filing's own words point to (its line labels and company name), with the words
    found; None when nothing stands out: fewer than two words, or two industries level."""
    rules = rules or for_filing()
    text = " | ".join(str(t).lower() for t in texts if t)
    scored = []
    for i in rules["industries"]:
        found = [w for w in i.get("keywords") or [] if _hit(w, text)]
        if found:
            scored.append((len(found), i["id"], found))
    scored.sort(key=lambda s: -s[0])
    if not scored or scored[0][0] < 2 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1], scored[0][2]


def rule_used(rules: dict | None, iid: str) -> str:
    """Which rule decided the COGS, in words: the user's company rule, the user's industry rule or the built-in."""
    rules = rules or for_filing()
    c = rules.get("company")
    if c and c.get("cogs"):
        return f"your rule for {c['match']}"
    i = get(rules, iid)
    return f"{'your' if i and i.get('source', 'built-in') != 'built-in' else 'the built-in'} {name(rules, iid)} rule"
