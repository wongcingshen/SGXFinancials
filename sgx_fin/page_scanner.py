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
import pypdfium2.raw as pdfium_c

# Title phrases: strong evidence when they appear in the top part of a page.
TITLES = {
    "income_statement": [
        r"statements? of (consolidated )?profit or loss",
        r"(consolidated )?income statements?",
        r"statements? of (consolidated )?comprehensive income",
        r"profit (or|and) loss (and other comprehensive income )?statement",
        r"statements? of profit or loss and other comprehensive income",
        r"profit and loss accounts?",
        # Banks / Indian listings: "Statement of Standalone Audited Financial Results"
        r"statements? of (?:(?:audited|unaudited|reviewed|standalone|consolidated)\s+)*financial results",
        r"(?:standalone|consolidated) (?:(?:audited|unaudited|reviewed)\s+)?financial results",
    ],
    "balance_sheet": [
        r"statements? of (consolidated )?financial position",
        r"(consolidated )?balance sheets?",
        r"statements? of (?:(?:standalone|consolidated)\s+)?assets and liabilities",
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
        # banks
        "interest earned", "interest expended", "net interest income", "interest income",
        "operating profit before provisions", "provisions and contingencies", "total income",
        "total expenditure", "net profit",
    ],
    "balance_sheet": [
        "non-current assets", "current assets", "total assets", "property, plant and equipment",
        "inventories", "trade and other receivables", "cash and cash equivalents",
        "cash and bank balances", "current liabilities", "non-current liabilities",
        "total liabilities", "trade and other payables", "borrowings", "share capital",
        "retained earnings", "accumulated profits", "reserves", "total equity", "net assets",
        # banks
        "deposits", "advances", "capital and liabilities", "other liabilities and provisions",
        "balances with banks", "balances with reserve bank",
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
    image_only_pages: list[int]  # pictures with (almost) no text layer: scans and photos
    basis: str = "unknown"  # consolidated | separate | unknown, from the statement headings
    # Scans that carry a hidden OCR text layer: the keywords can be read, the figures are read from
    # the picture (OCR layers often get digits wrong).
    scanned_pages: list[int] = field(default_factory=list)

    @property
    def picture_pages(self) -> list[int]:
        return sorted({*self.image_only_pages, *self.scanned_pages})

    @property
    def all_selected(self) -> list[int]:
        return sorted({p for ps in self.selected.values() for p in ps})

    @property
    def found_all(self) -> bool:
        return all(self.selected.get(k) for k in TITLES)

    @property
    def needs_vision(self) -> bool:
        """The statements are probably pictures the keyword scan can't read.

        True when there are picture pages and either a statement wasn't found in the text, or most
        of the document is pictures (then the text "hits" are likely a cover or notes page).
        """
        if not self.image_only_pages:
            return False
        if not any(self.selected.values()):
            return True  # no statement in the text at all
        return len(self.image_only_pages) >= max(3, 0.5 * self.page_count)  # mostly pictures

    @property
    def needs_ai_locate(self) -> bool:
        """A text PDF where the deciding words for some statement weren't found."""
        return not self.needs_vision and not self.found_all

    @property
    def missing(self) -> list[str]:
        return [st for st in TITLES if not self.selected.get(st)]

    def locate_pages(self, cap: int = 24) -> list[int]:
        """Shortlist for the AI to find the statements in, when the keywords fell short: a short
        document whole; otherwise what the scan found plus the most number-heavy pages (statement
        tables are mostly numbers), skipping contents and commentary pages."""
        if self.page_count <= cap:
            return list(range(1, self.page_count + 1))
        pages = list(self.all_selected)
        dense = sorted((p for p in self.pages if p.has_text), key=lambda p: -p.numeric_ratio)
        for p in dense:
            if len(pages) >= cap:
                break
            if p.page not in pages and p.numeric_ratio >= 0.15:
                pages.append(p.page)
        return sorted(pages)

    def vision_pages(self, cap: int = 24) -> list[int]:
        """Pages for the AI to look through when the statements are pictures: whatever the text scan
        found, then picture pages from the front (where results filings put their statements)."""
        pages = list(self.all_selected)
        for p in self.image_only_pages:
            if len(pages) >= cap:
                break
            if p not in pages:
                pages.append(p)
        return sorted(pages)


def page_texts(pdf: Path) -> list[str]:
    return [r[0] for r in read_pages(pdf)]


def _layers(page) -> tuple[bool, bool]:
    """(has a picture, is a scan whose text is a hidden OCR layer).

    A scan: one picture covering most of the page, with the text either invisible (render mode 3,
    how OCR tools write it) or drawn underneath the picture. A text page on a full-page background
    picture draws its text visibly on top, so it doesn't count."""
    w, h = page.get_size()
    has_image, cover, texts, hidden, under = False, 0.0, 0, 0, 0
    for o in page.get_objects(max_depth=2):
        if o.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            has_image = True
            try:
                left, bottom, right, top = (o.get_bounds() if hasattr(o, "get_bounds") else o.get_pos())
                area = max(0.0, min(right, w) - max(left, 0)) * max(0.0, min(top, h) - max(bottom, 0)) / (w * h or 1)
            except Exception:
                area = 0.0
            if area >= 0.6:
                under, cover = under + texts, max(cover, area)  # text drawn before a full-page picture is hidden by it
        elif o.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
            texts += 1
            try:
                if pdfium_c.FPDFTextObj_GetTextRenderMode(o.raw) == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE:
                    hidden += 1
            except Exception:
                pass
    scan = cover >= 0.6 and texts > 0 and (hidden + under) >= 0.6 * texts
    return has_image, scan


def read_pages(pdf: Path) -> list[tuple[str, bool, bool]]:
    """(text layer, has a picture, is a scan with a hidden OCR layer) for every page."""
    doc = pdfium.PdfDocument(str(pdf))
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            text = tp.get_text_range()
            tp.close()
            try:
                has_image, scan = _layers(page)
            except Exception:
                has_image, scan = False, False
            out.append((text, has_image, scan))
        return out
    finally:
        doc.close()


# A page with a picture and fewer characters than this is treated as a scan / photo.
PICTURE_PAGE_MAX_CHARS = 200


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
        if "consolidated" in top:
            s += 2  # filings with both prefer the Group (consolidated) statements
        elif "standalone" in top:
            s -= 2
        scores[st] = round(s, 2)

    return PageScore(page_no, scores, round(numeric_ratio, 3), bool(text.strip()), title_hits)


def scan_pdf(pdf: Path, min_score: float = 14.0, max_pages_per_statement: int = 3) -> ScanResult:
    read = read_pages(pdf)
    pages = [score_page(t, i + 1) for i, (t, _, _) in enumerate(read)]
    image_only = [i + 1 for i, (t, img, _) in enumerate(read)
                  if not t.strip() or (img and len(t.strip()) < PICTURE_PAGE_MAX_CHARS)]
    scanned = [i + 1 for i, (t, _, scan) in enumerate(read) if scan and i + 1 not in image_only]

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

    result = ScanResult(pdf, len(pages), pages, selected, image_only, scanned_pages=scanned)
    result.basis = statement_basis([read[p - 1][0] for p in (result.all_selected or range(1, min(len(read), 6) + 1))])
    return result


# Whether statements are the group's (consolidated) or the parent company's alone (separate /
# standalone). Some issuers file each set as its own announcement; only the consolidated one is used.
CONSOLIDATED = re.compile(r"\bconsolidated\b|\bhợp nhất\b|\bhop nhat\b", re.I)
SEPARATE = re.compile(r"\bseparate (?:financial )?statements?\b|\bseparate (?:statement|balance)|\bstandalone\b|"
                      r"\bstand-alone\b|\bcompany[- ]only\b|\bparent company\b|\briêng\b", re.I)


def statement_basis(texts: list[str]) -> str:
    """'consolidated', 'separate' or 'unknown', judged from the top of the statement pages."""
    cons = sep = 0
    for text in texts:
        lines = [l for l in text.splitlines() if l.strip()]
        top = "\n".join(lines[: max(8, len(lines) // 5)])
        cons += len(CONSOLIDATED.findall(top))
        sep += len(SEPARATE.findall(top))
    if cons > sep:
        return "consolidated"
    if sep > cons:
        return "separate"
    return "unknown"


def format_report(res: ScanResult, top_n: int = 5) -> str:
    out = [f"{res.pdf.name}: {res.page_count} pages"]
    if res.image_only_pages:
        out.append(f"  pages with no text layer (scanned images): {res.image_only_pages}")
    if res.scanned_pages:
        out.append(f"  scans with a hidden OCR text layer: {res.scanned_pages}")
    for st in TITLES:
        ranked = sorted(res.pages, key=lambda p: p.scores[st], reverse=True)[:top_n]
        cand = ", ".join(f"p{p.page}={p.scores[st]}" for p in ranked)
        out.append(f"  {st:17s} -> selected {res.selected[st] or 'NONE'}   (top: {cand})")
    return "\n".join(out)
