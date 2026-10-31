"""Non-AI page scanner: finds which PDF pages hold the three primary statements.

Every page's text layer is scored against keyword sets for the income
statement, balance sheet and cash flow statement. Title phrases near the top of
a page weigh most, line-item vocabulary and numeric density add to the score,
and narrative/notes pages are penalised. Only the winning pages (plus a
continuation page when a statement spills over) are sent to the AI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium

# Title phrases: strong evidence when they appear in the top part of a page.
TITLES = {
    "income_statement": [
        r"statements? of (consolidated )?profit or loss",
        r"(consolidated )?income statements?",
        r"statements? of (consolidated )?comprehensive income",
        r"profit (or|and) loss (and other comprehensive income )?statement",
        r"statements? of profit or loss and other comprehensive income",
        r"profit and loss accounts?",
    ],
    "balance_sheet": [
        r"statements? of (consolidated )?financial position",
        r"(consolidated )?balance sheets?",
    ],
    "cash_flow": [
        r"statements? of (consolidated )?cash flows?",
        r"(consolidated )?cash flows? statements?",
    ],
}

# Line-item vocabulary that appears inside each statement.
BODY = {
    "income_statement": [
        "revenue", "cost of sales", "gross profit", "other income", "administrative expenses",
        "distribution", "finance costs", "finance income", "profit before tax",
        "income tax expense", "tax expense", "profit for the", "loss for the",
        "earnings per share", "attributable to", "non-controlling interests",
        "share of results of", "share of profit",
    ],
    "balance_sheet": [
        "non-current assets", "current assets", "total assets", "property, plant and equipment",
        "inventories", "trade and other receivables", "cash and cash equivalents",
        "cash and bank balances", "current liabilities", "non-current liabilities",
        "total liabilities", "trade and other payables", "borrowings", "share capital",
        "retained earnings", "accumulated profits", "reserves", "total equity", "net assets",
    ],
    "cash_flow": [
        "operating activities", "investing activities", "financing activities",
        "cash flows from", "net cash", "adjustments for", "depreciation",
        "working capital", "purchase of property", "proceeds from", "dividends paid",
        "interest paid", "income tax paid", "cash and cash equivalents at",
        "beginning of", "end of the", "net increase", "net decrease",
    ],
}

# Signals that a page is narrative, notes or index rather than a primary statement.
NEGATIVE = [
    r"\bnotes? to the (condensed )?(interim )?(consolidated )?financial statements\b",
    r"\btable of contents\b", r"^\s*contents\s*$", r"independent auditor",
    r"review of (group )?performance", r"commentary", r"chairman", r"basis of preparation",
    r"significant accounting policies", r"segment information", r"the board of directors",
]

NUMBER = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?-?\d+\.\d+\)?|\(\d+\)")
WORD = re.compile(r"[A-Za-z]{2,}")


@dataclass
class PageScore:
    page: int  # 1-based
    scores: dict[str, float]
    numeric_ratio: float
    has_text: bool
    title_hits: dict[str, str] = field(default_factory=dict)


@dataclass
class ScanResult:
    pdf: Path
    page_count: int
    pages: list[PageScore]
    selected: dict[str, list[int]]  # statement -> 1-based pages
    image_only_pages: list[int]

    @property
    def all_selected(self) -> list[int]:
        return sorted({p for ps in self.selected.values() for p in ps})

    @property
    def found_all(self) -> bool:
        return all(self.selected.get(k) for k in TITLES)


def page_texts(pdf: Path) -> list[str]:
    doc = pdfium.PdfDocument(str(pdf))
    try:
        texts = []
        for i in range(len(doc)):
            tp = doc[i].get_textpage()
            texts.append(tp.get_text_range())
            tp.close()
        return texts
    finally:
        doc.close()


def score_page(text: str, page_no: int) -> PageScore:
    low = text.lower()
    lines = [l.strip() for l in low.splitlines() if l.strip()]
    top = "\n".join(lines[: max(8, len(lines) // 6)])  # header zone

    words = len(WORD.findall(text))
    nums = len(NUMBER.findall(text))
    numeric_ratio = nums / max(1, words + nums)

    neg = sum(1 for p in NEGATIVE if re.search(p, top, flags=re.M))

    scores, title_hits = {}, {}
    for st in TITLES:
        s = 0.0
        for pat in TITLES[st]:
            if m := re.search(pat, top):
                s += 10
                title_hits[st] = m.group(0)
                break
            if re.search(pat, low):
                s += 2  # mentioned further down (e.g. a cross-reference)
                break
        hits = sum(1 for kw in BODY[st] if kw in low)
        s += hits * 1.5
        s += min(numeric_ratio, 0.6) * 15  # statements are number-dense
        s -= neg * 6
        scores[st] = round(s, 2)

    return PageScore(page_no, scores, round(numeric_ratio, 3), bool(text.strip()), title_hits)


def scan_pdf(pdf: Path, min_score: float = 14.0, max_pages_per_statement: int = 3) -> ScanResult:
    texts = page_texts(pdf)
    pages = [score_page(t, i + 1) for i, t in enumerate(texts)]
    image_only = [p.page for p in pages if not p.has_text]

    selected: dict[str, list[int]] = {}
    for st in TITLES:
        ranked = sorted(pages, key=lambda p: p.scores[st], reverse=True)
        best = ranked[0] if ranked else None
        if not best or best.scores[st] < min_score:
            selected[st] = []
            continue
        chosen = [best.page]

        # A page belongs to this statement if this statement is its top score and
        # no other statement's title heads it.
        def owns(p: PageScore) -> bool:
            return (p.scores[st] >= max(p.scores.values()) and p.scores[st] >= min_score * 0.6
                    and not any(k != st for k in p.title_hits))

        # Statements often spill over two or three pages (e.g. assets, then
        # "Equity and liabilities"); the best-scoring page may be either half.
        lo = hi = best.page
        while len(chosen) < max_pages_per_statement:
            if lo > 1 and owns(pages[lo - 2]):
                lo -= 1
                chosen.insert(0, lo)
            elif hi < len(pages) and owns(pages[hi]):
                hi += 1
                chosen.append(hi)
            else:
                break
        selected[st] = chosen

    return ScanResult(pdf, len(pages), pages, selected, image_only)


def format_report(res: ScanResult, top_n: int = 5) -> str:
    out = [f"{res.pdf.name}: {res.page_count} pages"]
    if res.image_only_pages:
        out.append(f"  pages with no text layer (scanned images): {res.image_only_pages}")
    for st in TITLES:
        ranked = sorted(res.pages, key=lambda p: p.scores[st], reverse=True)[:top_n]
        cand = ", ".join(f"p{p.page}={p.scores[st]}" for p in ranked)
        out.append(f"  {st:17s} -> selected {res.selected[st] or 'NONE'}   (top: {cand})")
    return "\n".join(out)
