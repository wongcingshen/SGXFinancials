"""Glue: announcement -> best PDF -> keyword page scan -> AI extraction -> normalised figures."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import extractor
from .page_scanner import ScanResult, scan_pdf
from .sgx_client import Announcement, SGXClient


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


def pick_best_pdf(pdfs: list[Path], scans: dict | None = None) -> tuple[Path, ScanResult] | None:
    """The attachment that holds the financial statements: by what its pages contain, preferring
    one that isn't a presentation or release, then by how financial its name looks."""
    scans = {} if scans is None else scans
    best = None
    for pdf in pdfs:
        if pdf not in scans:
            try:
                scans[pdf] = scan_pdf(pdf)
            except Exception:  # corrupt / encrypted attachment
                scans[pdf] = None
        res = scans[pdf]
        if res is None:
            continue
        found = sum(bool(v) for v in res.selected.values())
        top = sum(max((p.scores[st] for p in res.pages), default=0) for st in res.selected)
        ns = name_score(pdf.name)
        # A results PDF whose statements are pictures counts as a candidate even though the
        # keyword scan found nothing in it; presentations and releases never do.
        rank = (ns >= 0 and (found > 0 or res.needs_vision), found, ns, top)
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
        if res and res[1].found_all and name_score(name) >= 0 and name_score(pdf.name) >= 0:
            break  # the financial statements are here; leave the other attachments alone
        if res and res[1].needs_vision and name_score(name) == 1:
            break  # a results PDF whose statements are pictures
    best = pick_best_pdf(list(urls), scans)
    if not best:
        return None
    return best[0], best[1], urls[best[0]], [p.name for p in urls]


def process_pdf(pdf: Path, scan: ScanResult | None = None, pages: list[int] | None = None,
                model: str | None = None, effort: str = "high",
                dry_run: bool = False, meta: dict | None = None,
                cache_dir: Path | None = None) -> Result:
    scan = scan or scan_pdf(pdf)
    pages = pages or default_pages(scan)
    result = Result(pdf, scan, pages, meta=dict(meta or {}))
    if dry_run or not pages:
        return result
    vision = any(p in scan.image_only_pages for p in pages)
    ex = extractor.extract(pdf, pages, model=model, effort=effort, cache_dir=cache_dir, vision=vision)
    result.normalised = extractor.normalise(ex.data)
    result.usage = ex.usage
    result.meta.update({
        "company_name": result.meta.get("company_name") or ex.data.get("company_name", ""),
        "currency": ex.data.get("currency", ""),
        "basis": ex.data.get("basis", ""),
        "notes": ex.data.get("notes", ""),
        "source_pdf": pdf.name,
        "pages": ",".join(map(str, pages)),
        "model": ex.model,
        "vision": vision,
        # Where the AI says each statement was, as page numbers of the original PDF.
        "found_pages": {st: sorted({pages[i - 1] for i in (ex.data.get(st) or {}).get("page_numbers", [])
                                    if isinstance(i, int) and 1 <= i <= len(pages)})
                        for st in ("income_statement", "balance_sheet", "cash_flow")},
    })
    return result


def default_pages(scan: ScanResult) -> list[int]:
    """The pages to send to the AI: the statements the keyword scan found or, when the statements
    are pictures it can't read, the picture pages for the AI to look through."""
    return scan.vision_pages() if scan.needs_vision else scan.all_selected


def process_announcement(client: SGXClient, ann: Announcement, work_dir: Path, **kw) -> Result | None:
    folder = work_dir / ann.slug
    files = ann.raw.get("attachments") or [{"url": u, "name": ""} for u in client.attachments(ann)]
    picked = choose_financial_pdf(client, [(f["url"], attachment_name(f["url"], f.get("name", ""))) for f in files], folder)
    if not picked:
        return None
    pdf, scan, _, _ = picked
    meta = {"company_name": ann.issuer, "stock_code": ann.stock_code,
            "filing": f"{ann.broadcast} {ann.title}".strip(), "announcement_url": ann.url}
    return process_pdf(pdf, scan=scan, meta=meta, cache_dir=folder, **kw)
