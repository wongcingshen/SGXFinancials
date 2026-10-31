"""Vercel serverless entry point (Flask WSGI app).

Routes
  GET  /api/health                    configuration status
  GET  /api/screen?url=...&company=   list announcements for an SGX page URL
  POST /api/process                   scan (+ extract) one filing, return workbook

Environment variables (Vercel > Project > Settings > Environment Variables)
  ANTHROPIC_API_KEY   required for extraction
  APP_PASSWORD        recommended: callers must send it as X-App-Password
  SGX_AUTH_TOKEN      only if api.sgx.com starts rejecting anonymous requests
  CLAUDE_MODEL        optional override (default claude-opus-5-5)
"""

from __future__ import annotations

import base64
import hmac
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, abort, jsonify, request, send_from_directory

from sgx_fin import extractor
from sgx_fin.excel_writer import workbook_bytes
from sgx_fin.pipeline import pick_best_pdf, process_pdf
from sgx_fin.sgx_client import Announcement, SGXClient, Screen, is_sgx_url

app = Flask(__name__, static_folder=None)
PUBLIC = Path(__file__).resolve().parent.parent / "public"
app.config["MAX_CONTENT_LENGTH"] = 4_400_000  # Vercel caps request bodies at 4.5 MB
WORK = Path(tempfile.gettempdir()) / "sgx_fin"


@app.before_request
def _auth():
    if not request.path.startswith("/api/") or request.path.endswith("/health"):
        return None
    pw = os.environ.get("APP_PASSWORD")
    if pw and not hmac.compare_digest(request.headers.get("X-App-Password", ""), pw):
        return jsonify(error="Wrong or missing password"), 401
    return None


@app.errorhandler(Exception)
def _err(e):
    traceback.print_exc()
    code = getattr(e, "code", 500)
    return jsonify(error=f"{type(e).__name__}: {e}"), code if isinstance(code, int) else 500


# Vercel serves public/ from its CDN; these routes cover local runs (`flask --app api/index run`)
# and any deployment where Flask receives the page request itself.
@app.get("/")
def home():
    return send_from_directory(PUBLIC, "index.html")


@app.get("/<path:name>")
def static_file(name: str):
    if name.startswith("api/") or not (PUBLIC / name).is_file():
        abort(404)
    return send_from_directory(PUBLIC, name)


@app.get("/api/health")
def health():
    return jsonify(
        anthropic_key=bool(os.environ.get("ANTHROPIC_API_KEY")),
        password_required=bool(os.environ.get("APP_PASSWORD")),
        sgx_token=bool(os.environ.get("SGX_AUTH_TOKEN")),
        model=os.environ.get("CLAUDE_MODEL", extractor.DEFAULT_MODEL),
    )


@app.get("/api/screen")
def screen():
    url = request.args.get("url", "")
    if not url:
        return jsonify(error="url is required"), 400
    s = Screen.from_url(url)
    anns = SGXClient().screen(s, company=request.args.get("company") or None)
    return jsonify(page_url=s.page_url, count=len(anns), announcements=[
        {"title": a.title, "issuer": a.issuer, "stock_code": a.stock_code,
         "url": a.url, "broadcast": a.broadcast, "category": a.category} for a in anns])


@app.post("/api/process")
def process():
    f = request.form
    dry_run = f.get("dry_run") == "true"
    pages = [int(x) for x in f.get("pages", "").replace(" ", "").split(",") if x] or None
    meta = {"company_name": f.get("issuer", ""), "stock_code": f.get("stock_code", ""),
            "filing": f.get("title", "")}
    WORK.mkdir(parents=True, exist_ok=True)
    client = SGXClient()

    # 1. Get candidate PDFs: an uploaded file, a direct PDF link, or an announcement page.
    pdfs: list[Path] = []
    if "pdf" in request.files and request.files["pdf"].filename:
        up = request.files["pdf"]
        path = WORK / Path(up.filename).name
        up.save(path)
        pdfs = [path]
        meta["filing"] = meta["filing"] or up.filename
    else:
        url = f.get("announcement_url") or f.get("pdf_url") or ""
        if not is_sgx_url(url):
            return jsonify(error="Provide a PDF upload or an https://*.sgx.com link"), 400
        meta["announcement_url"] = url
        ann = Announcement(meta["filing"], meta["company_name"], meta["stock_code"], url, "", "", {})
        folder = WORK / ann.slug
        pdfs = [p for u in client.attachments(ann) if (p := client.download(u, folder))]
    if not pdfs:
        return jsonify(error="No PDF attachment found"), 404

    # 2. Non-AI scan picks the attachment and the statement pages.
    picked = pick_best_pdf(pdfs)
    if not picked:
        return jsonify(error="Could not read any of the PDFs"), 422
    pdf, scan = picked
    scan_info = {
        "pdf": pdf.name, "page_count": scan.page_count, "selected": scan.selected,
        "image_only_pages": scan.image_only_pages,
        "candidates": {st: sorted(({"page": p.page, "score": p.scores[st]} for p in scan.pages),
                                  key=lambda x: -x["score"])[:5] for st in scan.selected},
        "attachments": [p.name for p in pdfs],
    }
    chosen = pages or scan.all_selected
    if dry_run or not chosen:
        return jsonify(scan=scan_info, pages=chosen, extracted=False,
                       message=None if chosen else "No statement pages found; pass pages manually.")

    # 3. AI extraction on the selected pages only, then merge into the uploaded workbook (if any).
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify(error="ANTHROPIC_API_KEY is not configured on the server"), 500
    res = process_pdf(pdf, scan=scan, pages=chosen, meta=meta,
                      model=os.environ.get("CLAUDE_MODEL", extractor.DEFAULT_MODEL),
                      effort=f.get("effort", "high"), cache_dir=WORK)
    existing = request.files["workbook"].read() if request.files.get("workbook") else None
    data, checks = workbook_bytes(existing, res.normalised, res.meta)
    name = (res.meta.get("stock_code") or res.meta.get("company_name") or "financials").replace(" ", "_")
    return jsonify(
        scan=scan_info, pages=chosen, extracted=True, meta=res.meta, usage=res.usage, checks=checks,
        filename=f"{name}.xlsx", workbook_b64=base64.b64encode(data).decode(),
    )
