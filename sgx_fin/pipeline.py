"""Glue: announcement -> best PDF -> keyword page scan -> AI extraction -> normalised figures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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


def pick_best_pdf(pdfs: list[Path]) -> tuple[Path, ScanResult] | None:
    """Among an announcement's attachments, the one that most looks like it holds all three statements."""
    best = None
    for pdf in pdfs:
        try:
            res = scan_pdf(pdf)
        except Exception:  # corrupt / encrypted attachment
            continue
        top = sum(max((p.scores[st] for p in res.pages), default=0) for st in res.selected)
        rank = (sum(bool(v) for v in res.selected.values()), top)
        if best is None or rank > best[0]:
            best = (rank, pdf, res)
    return (best[1], best[2]) if best else None


def process_pdf(pdf: Path, scan: ScanResult | None = None, pages: list[int] | None = None,
                model: str | None = None, effort: str = "high",
                dry_run: bool = False, meta: dict | None = None,
                cache_dir: Path | None = None) -> Result:
    scan = scan or scan_pdf(pdf)
    pages = pages or scan.all_selected
    result = Result(pdf, scan, pages, meta=dict(meta or {}))
    if dry_run or not pages:
        return result
    ex = extractor.extract(pdf, pages, model=model, effort=effort, cache_dir=cache_dir)
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
    })
    return result


def process_announcement(client: SGXClient, ann: Announcement, work_dir: Path, **kw) -> Result | None:
    folder = work_dir / ann.slug
    pdfs = [p for url in client.attachments(ann) if (p := client.download(url, folder))]
    picked = pick_best_pdf(pdfs)
    if not picked:
        return None
    pdf, scan = picked
    meta = {"company_name": ann.issuer, "stock_code": ann.stock_code,
            "filing": f"{ann.broadcast} {ann.title}".strip(), "announcement_url": ann.url}
    return process_pdf(pdf, scan=scan, meta=meta, cache_dir=folder, **kw)
