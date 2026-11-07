"""Glue: announcement -> best PDF -> keyword page scan -> AI extraction -> normalised figures."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import checks, extractor, industries, pictures, textlayer
from .checks import STATEMENT_NAMES, join_or
from .page_scanner import ScanResult, page_texts, scan_pdf
from .sgx_client import Announcement, SGXClient


class NotResults(Exception):
    """The filing holds no financial statements (e.g. a notice of the results release date)."""


@dataclass
class Result:
    pdf: Path
    scan: ScanResult
    pages: list[int]
    normalised: dict | None = None
    meta: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)


# An announcement often carries several PDFs (results, media release, presentation). Their names
# decide the order they are tried in; presentations and releases are only a last resort.
FINANCIAL_NAME = re.compile(
    r"financial statements?|results|condensed|interim|unaudited|audited|full[\s-]?year|half[\s-]?year"
    r"|quarter|annual report|\b(?:fy|hy|[12]h|[1-4]q|q[1-4])\s?'?\d{0,4}\b", re.I)
NOT_FINANCIAL = re.compile(
    r"presentation|\bslides?\b|\bdeck\b|media release|press release|news ?release|webcast|transcript"
    r"|briefing|infographic|fact ?sheet|poster|speech|letter to|notice of", re.I)


def attachment_name(url: str, name: str = "") -> str:
    """The link text, else the file name SGX puts in the FileOpen URL."""
    if name and name.strip():
        return name.strip()
    return re.sub(r"\.ashx$", "", unquote(Path(urlparse(url).path).name), flags=re.I)


def name_score(name: str) -> int:
    """1 = named like financial statements, 0 = unclear, -1 = presentation / media release etc."""
    if NOT_FINANCIAL.search(name):
        return -1
    return 1 if FINANCIAL_NAME.search(name) else 0


_SCANS: dict[tuple, ScanResult | None] = {}


def cached_scan(pdf: Path) -> ScanResult | None:
    """scan_pdf, remembered for this process (the scan and extract requests read the same file)."""
    st = pdf.stat()
    key = (str(pdf), st.st_size, st.st_mtime_ns)
    if key not in _SCANS:
        try:
            _SCANS[key] = scan_pdf(pdf)
        except Exception:  # corrupt / encrypted attachment
            _SCANS[key] = None
    return _SCANS[key]


def pick_best_pdf(pdfs: list[Path], scans: dict | None = None) -> tuple[Path, ScanResult] | None:
    """The attachment that holds the financial statements: by what its pages contain, preferring
    one that isn't a presentation or release (by name or, for slides, by how it was made and its
    shape), then by how financial its name looks."""
    scans = {} if scans is None else scans
    best = None
    for pdf in pdfs:
        if pdf not in scans:
            scans[pdf] = cached_scan(pdf)
        res = scans[pdf]
        if res is None:
            continue
        found = sum(bool(v) for v in res.selected.values())
        top = sum(max((p.scores[st] for p in res.pages), default=0) for st in res.selected)
        ns = name_score(pdf.name)
        # A results PDF whose statements are pictures counts as a candidate even though the
        # keyword scan found nothing in it; presentations and releases never do.
        rank = (ns >= 0 and not res.presentation and (found > 0 or res.needs_vision), found, ns, top)
        if best is None or rank > best[0]:
            best = (rank, pdf, res)
    return (best[1], best[2]) if best else None


def choose_financial_pdf(client: SGXClient, links: list[tuple[str, str]], folder: Path):
    """Download attachments best-named first and stop at the first one holding all three statements.

    links: (url, name) pairs. Returns (pdf, scan, url used, names of files downloaded) or None.
    """
    ranked = sorted(links, key=lambda link: -name_score(link[1]))  # stable: SGX order within a rank
    scans: dict = {}
    urls: dict[Path, str] = {}
    for url, name in ranked:
        pdf = client.download(url, folder)
        if not pdf:
            continue
        urls[pdf] = url
        res = pick_best_pdf([pdf], scans)
        if res and res[1].presentation:
            continue  # slides: the formal statements may be in another attachment
        if res and res[1].found_all and name_score(name) >= 0 and name_score(pdf.name) >= 0:
            break  # the financial statements are here; leave the other attachments alone
        if res and res[1].needs_vision and name_score(name) == 1:
            break  # a results PDF whose statements are pictures
    best = pick_best_pdf(list(urls), scans)
    if not best:
        return None
    return best[0], best[1], urls[best[0]], [p.name for p in urls]


READ_MAX = 8  # picture pages read in one go; more than this are narrowed down first


def process_pdf(pdf: Path, scan: ScanResult | None = None, pages: list[int] | None = None,
                model: str | None = None, effort: str = "high",
                dry_run: bool = False, meta: dict | None = None,
                cache_dir: Path | None = None, deadline: float | None = None,
                formats: dict | None = None, cogs: dict | None = None) -> Result:
    """Read the statements: the pages given, else the ones the keyword scan picked.

    Pages that are pictures (scans, photos, scans with a hidden OCR layer) go through
    sgx_fin.pictures: the statement pages are found from small previews when the text couldn't
    tell, straightened and cleaned up, copied as printed, checked, and doubtful rows re-read; the
    checked tables then fill the template like a text filing. When parts don't add up to a printed
    total after that, the template step looks once more with the failing checks pointed out.
    `cogs`: the industries and COGS rules for this filing, the user's preferences included
    (industries.for_filing); the built-in ones when not given.
    """
    cogs = cogs or industries.for_filing(None, (meta or {}).get("stock_code", ""), (meta or {}).get("company_name", ""))
    scan = scan or cached_scan(pdf) or scan_pdf(pdf)
    manual = bool(pages)
    pages = pages or default_pages(scan)
    result = Result(pdf, scan, pages, meta=dict(meta or {}))
    if dry_run or not pages:
        return result
    deadline = deadline or time.monotonic() + 280
    vision = any(p in scan.picture_pages for p in pages)
    # The AI finds the statements itself when the keyword scan couldn't, or when pages were chosen by hand.
    locate = vision or not scan.confident or set(pages) != set(scan.all_selected)
    located, reading, turns, context, notes = None, None, {}, "", []
    if vision:
        shortlist = scan.vision_pages(cap=40) if (not manual and scan.needs_vision) else pages if len(pages) > READ_MAX else None
        if shortlist:
            try:
                located = pictures.locate(pdf, shortlist, deadline=min(deadline - 120, time.monotonic() + 90), cache_dir=cache_dir)
            except Exception as e:
                notes.append(f"Finding the statement pages failed ({e}).")
            if located:
                pages, turns, context, locate = located.pages, located.turns, located.context, False
        if located or len(pages) <= READ_MAX:
            try:
                reading = pictures.read(pdf, pages, turns, deadline=deadline - 25, cache_dir=cache_dir)
            except Exception as e:
                notes.append(f"Copying the tables from the pictures failed ({e}); read them directly instead.")
            if reading and not reading.tables:
                notes.append("No tables were copied from the pictures; read them directly instead.")
                reading = None
    result.pages = pages

    def run(recheck: str = "", gaps: str = ""):
        if reading:
            return extractor.extract_text(pdf, reading.pages, pictures.as_text(reading.tables), model=model, effort=effort,
                                          cache_dir=cache_dir, deadline=deadline, formats=formats, recheck=recheck, gaps=gaps,
                                          cogs=cogs)
        return extractor.extract(pdf, pages, model=model, effort=effort, cache_dir=cache_dir, vision=vision,
                                 locate=locate, deadline=deadline, formats=formats, context=context, recheck=recheck,
                                 gaps=gaps, cogs=cogs)

    ex = run()
    normalised = extractor.normalise(ex.data, formats, cogs)
    usage = dict(ex.usage)
    if reading:
        pictures._add_usage(usage, reading.usage)

    def found(st: str) -> list[int]:
        nums = [n for n in (ex.data.get(st) or {}).get("page_numbers", []) if isinstance(n, int)]
        if reading:  # the template step was told the original page numbers
            return sorted({n for n in nums if n in reading.pages})
        return sorted({pages[n - 1] for n in nums if 1 <= n <= len(pages)})

    # Text pages: a figure that doesn't add up is looked for on its line in the PDF's own text first
    # (free, exact digits); one is kept only where it makes the totals add up.
    texts = None

    def from_text() -> list[str]:
        nonlocal texts
        if vision or not checks.failures(normalised):
            return []
        try:
            if texts is None:
                texts = textlayer.read_texts(pdf, {st: found(st) or pages for st in normalised})
            took = checks.try_candidates(normalised, textlayer.candidates(texts, normalised))
        except Exception as e:  # a damaged text layer only costs this shortcut
            notes.append(f"Reading the failing lines from the PDF's text failed ({e}).")
            return []
        if took:
            notes.append("Re-read from the PDF's text: " + "; ".join(took) + ".")
        return took

    from_text()

    # Totals that still don't add up, or gross profit left blank: one more look, pointed at the failing
    # lines (from the copied tables for pictures, which is quick; from the pages otherwise).
    fails = checks.failures(normalised)
    gaps = checks.gross_profit_gaps(normalised)
    rechecked = None
    if (fails or gaps) and deadline - time.monotonic() > (40 if reading else 70):
        factor = extractor.UNIT_FACTORS.get(ex.data.get("unit") or extractor.DEFAULT_UNIT, 1000) / extractor.T.OUTPUT_UNIT
        try:
            ex2 = run(checks.describe(fails, normalised, factor), checks.describe_gaps(gaps, normalised) if gaps else "")
            second = extractor.normalise(ex2.data, formats, cogs)
            fixed = checks.reconcile(normalised, second) + from_text()
            filled = extractor.fill_gross_profit(normalised, second) if gaps else []
            pictures._add_usage(usage, ex2.usage)
            rechecked = {"failed": len(fails), "fixed": fixed, "still_failing": len(checks.failures(normalised)),
                         "gross_profit_blank": len(gaps), "gross_profit_filled": filled,
                         "gross_profit_still_blank": len(checks.gross_profit_gaps(normalised))}
        except Exception as e:
            notes.append(f"A second look at the failing checks failed: {e}")
    corrected = [c for b in normalised.values() for c in b.get("corrected") or []]
    if corrected:
        notes.append("Put right where two printed totals agree: " + "; ".join(corrected) + ".")

    result.normalised = normalised
    result.usage = usage
    result.meta.update({
        "company_name": result.meta.get("company_name") or ex.data.get("company_name", ""),
        "currency": ex.data.get("currency", ""),
        "unit": ex.data.get("unit", ""),
        "basis": ex.data.get("basis", ""),
        "format": result.normalised["income_statement"]["format"],
        "notes": " ".join([ex.data.get("notes", ""), *notes]).strip(),
        "source_pdf": pdf.name,
        "pages": ",".join(map(str, pages)),
        "model": ex.model if not reading or reading.model == ex.model else f"{reading.model} (copied) + {ex.model}",
        "vision": vision,
        "presentation": scan.presentation,
        # The company's industry and how its cost of goods sold was made up (shown in the app and workbook).
        "industry": (normalised["income_statement"].get("cogs_basis") or {}).get("industry_name", ""),
        "cogs_basis": normalised["income_statement"].get("cogs_basis"),
        "located_by_ai": locate or bool(located),
        # Where the AI says each statement was, as page numbers of the original PDF.
        "found_pages": {st: found(st) for st in ("income_statement", "balance_sheet", "cash_flow")},
    })
    if located:
        result.meta["located"] = {"model": located.model, "basis": located.basis, "pages": located.pages,
                                  "found": located.found}
    if reading:
        result.meta["pictures"] = reading.summary()
        result.meta["transcription"] = reading.tables
    if rechecked:
        result.meta["rechecked"] = rechecked
    # Whether the figures can be relied on: the web app shows a failure, not "Excel ready", when not.
    result.meta["verdict"] = checks.verdict(normalised, scan.presentation)
    return result


NOTICE = re.compile(r"intends? to (?:announce|release)|will (?:be )?(?:announc|releas)\w* (?:its|the)? ?(?:unaudited |audited )?"
                    r"(?:financial )?results|date of (?:the )?release of|release of (?:its|the) (?:financial )?results "
                    r"(?:on|for)|notification of results", re.I)


def not_results_reason(pdf: Path, scan: ScanResult) -> str | None:
    """Why a PDF is not a financial results document (a release-date notice, a press release, a
    presentation without the formal statements...), or None. Decided without AI so such filings
    never reach it."""
    if scan.presentation and not scan.formal_statements:
        missing = [STATEMENT_NAMES[st] for st in STATEMENT_NAMES if st not in scan.titled]
        return f"It's a presentation ({scan.presentation}) without the formal financial statements (no {join_or(missing)})."
    if any(scan.selected.values()) or scan.image_only_pages:
        return None  # statements found, or pictures that may hold them
    dense = max((p.numeric_ratio for p in scan.pages), default=0)
    first = " ".join(page_texts(pdf)[:2])
    if NOTICE.search(first) and scan.page_count <= 4:
        return "It's a notice of when the results will be released, not the results."
    if dense < 0.15:
        return "It has no financial statement tables (no statement titles and no number tables)."
    return None


def default_pages(scan: ScanResult) -> list[int]:
    """The pages to send to the AI: the statements the keyword scan found; when it found only some,
    a shortlist for the AI to find them in (also when one was picked without its title heading the
    page, so a guess is never read as the statement); when they are pictures, the picture pages."""
    if scan.needs_vision:
        return scan.vision_pages()
    if not scan.confident:
        return scan.locate_pages()
    return scan.all_selected


def process_announcement(client: SGXClient, ann: Announcement, work_dir: Path, **kw) -> Result | None:
    folder = work_dir / ann.slug
    files = ann.raw.get("attachments") or [{"url": u, "name": ""} for u in client.attachments(ann)]
    picked = choose_financial_pdf(client, [(f["url"], attachment_name(f["url"], f.get("name", ""))) for f in files], folder)
    if not picked:
        return None
    pdf, scan, _, _ = picked
    reason = not_results_reason(pdf, scan)
    if reason:
        raise NotResults(reason)
    meta = {"company_name": ann.issuer, "stock_code": ann.stock_code,
            "filing": f"{ann.broadcast} {ann.title}".strip(), "announcement_url": ann.url}
    return process_pdf(pdf, scan=scan, meta=meta, cache_dir=folder, **kw)
