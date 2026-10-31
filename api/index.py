"""Vercel serverless entry point (Flask WSGI app).

Routes (the operation is also accepted as ?op=health|screen|process on any path)
  GET  /api/health                    configuration status
  GET  /api/screen?url=...&company=   list announcements for an SGX page URL (token or browser)
  POST /api/process                   scan (+ extract) one filing, return workbook
On Vercel, /api/browse (api/browse.mjs) runs the headless browser for screening and attachments.

Environment variables (Vercel > Project > Settings > Environment Variables)
  GEMINI_API_KEY      AI key for extraction (or ANTHROPIC_API_KEY to use Claude); set it on
                      THIS Vercel project (not only another project), then redeploy
  APP_PASSWORD        recommended: callers must send it as X-App-Password
  SGX_AUTH_TOKEN      only if api.sgx.com starts rejecting anonymous requests
  SGX_GEMINI_MODEL    optional: pin a Gemini model (CLAUDE_MODEL for Claude)
  AI_PROVIDER         optional: gemini | anthropic, when both keys are set
"""

from __future__ import annotations

import base64
import hmac
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request, send_from_directory

from sgx_fin import extractor
from sgx_fin.excel_writer import workbook_bytes
from sgx_fin.pipeline import attachment_name, choose_financial_pdf, pick_best_pdf, process_pdf
from sgx_fin.sgx_client import Announcement, SGXClient, Screen, is_sgx_url

app = Flask(__name__, static_folder=None)
PUBLIC = Path(__file__).resolve().parent.parent / "public"
app.config["MAX_CONTENT_LENGTH"] = 4_400_000  # Vercel caps request bodies at 4.5 MB
WORK = Path(tempfile.gettempdir()) / "sgx_fin"


@app.errorhandler(Exception)
def _err(e):
    traceback.print_exc()
    code = getattr(e, "code", 500)
    return jsonify(error=f"{type(e).__name__}: {e}"), code if isinstance(code, int) else 500


# One catch-all route instead of per-path routes: depending on how Vercel runs the function it
# hands Flask the browser's path (/api/screen), the rewrite target (/api/index) or the path below
# the function's mount (/screen). The front end therefore also names the operation in ?op=,
# which survives every one of those.
API: dict[str, tuple] = {}


def api(name: str, method: str, public: bool = False):
    def deco(fn):
        API[name] = (fn, method, public)
        return fn
    return deco


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def dispatch(path: str):
    op = request.args.get("op") or path.rstrip("/").rsplit("/", 1)[-1]
    if op in API:
        fn, method, public = API[op]
        if request.method != method:
            return jsonify(error=f"Use {method} for {op}"), 405
        pw = os.environ.get("APP_PASSWORD")
        if not public and pw and not hmac.compare_digest(request.headers.get("X-App-Password", ""), pw):
            return jsonify(error="Wrong or missing password"), 401
        return fn()
    # Vercel serves public/ from its CDN; this covers local runs and deployments where Flask
    # receives the page request itself.
    if request.method == "GET" and not path.startswith("api"):
        name = path or "index.html"
        if (PUBLIC / name).is_file():
            return send_from_directory(PUBLIC, name)
    return jsonify(error=f"No route for {request.method} {request.path} (op={op or '-'})"), 404


@api("health", "GET", public=True)
def health():
    return jsonify(
        ai_key=extractor.provider() is not None,
        provider=extractor.provider(),
        provider_label=extractor.provider_label(),
        password_required=bool(os.environ.get("APP_PASSWORD")),
        sgx_token=bool(os.environ.get("SGX_AUTH_TOKEN")),
        model=extractor.default_model() if extractor.provider() else None,
        deployment=os.environ.get("VERCEL_ENV", "local"),
        project=os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL"),
        ai_vars=extractor.ai_env_names(),
    )


@api("screen", "GET")
def screen():
    url = request.args.get("url", "")
    if not url:
        return jsonify(error="url is required"), 400
    s = Screen.from_url(url)
    client = SGXClient()
    anns = client.screen(s, company=request.args.get("company") or None,
                         attachments=request.args.get("attachments") == "1")
    return jsonify(page_url=s.page_url, count=len(anns), debug=client.last_debug, announcements=[
        {"title": a.title, "issuer": a.issuer, "stock_code": a.stock_code, "url": a.url,
         "broadcast": a.broadcast, "category": a.category, "attachments": a.raw.get("attachments")}
        for a in anns])


@api("attachments", "GET")
def attachment_links():
    # On Vercel /api/browse is the Node browser function; this covers local runs of this app.
    url = request.args.get("url", "")
    if not is_sgx_url(url):
        return jsonify(error="Provide an https://*.sgx.com announcement link"), 400
    ann = Announcement("", "", "", url, "", "", {})
    return jsonify(url=url, files=[{"name": Path(u).name, "url": u} for u in SGXClient().attachments(ann)])


@api("process", "POST")
def process():
    f = request.form
    dry_run = f.get("dry_run") == "true"
    pages = [int(x) for x in f.get("pages", "").replace(" ", "").split(",") if x] or None
    meta = {"company_name": f.get("issuer", ""), "stock_code": f.get("stock_code", ""),
            "filing": f.get("title", "")}
    WORK.mkdir(parents=True, exist_ok=True)
    client = SGXClient()

    # 1. The PDF to read: an uploaded file, or the attachment of an announcement that holds the
    #    financial statements (chosen by file name, then by a keyword scan of its pages).
    used_url, names, downloaded = None, [], []
    if "pdf" in request.files and request.files["pdf"].filename:
        up = request.files["pdf"]
        path = WORK / Path(up.filename).name
        up.save(path)
        meta["filing"] = meta["filing"] or up.filename
        picked = pick_best_pdf([path])
        names = downloaded = [path.name]
    else:
        pdf_urls = [u for u in f.getlist("pdf_url") if u]
        pdf_names = f.getlist("pdf_name")
        url = f.get("announcement_url") or (pdf_urls[0] if pdf_urls else "")
        if not url or not all(is_sgx_url(u) for u in [url, *pdf_urls]):
            return jsonify(error="Provide a PDF upload or an https://*.sgx.com link"), 400
        meta["announcement_url"] = url
        ann = Announcement(meta["filing"], meta["company_name"], meta["stock_code"], url, "", "", {})
        folder = WORK / ann.slug
        # PDF links found by the browser step, else read the announcement page's attachments.
        try:
            urls = pdf_urls or client.attachments(ann)
        except Exception as e:
            return jsonify(error=f"Could not open the announcement page: {e}", need_browser=True), 502
        if not urls:
            return jsonify(error="No attachments found on the announcement page", need_browser=True), 404
        links = [(u, attachment_name(u, pdf_names[i] if i < len(pdf_names) else "")) for i, u in enumerate(urls)]
        names = [n for _, n in links]
        try:
            chosen_pdf = choose_financial_pdf(client, links, folder)
        except Exception as e:
            return jsonify(error=f"Could not download the attachment: {e}"), 502
        if not chosen_pdf:
            return jsonify(error="None of the attachments is a readable PDF"), 404
        pdf, scan, used_url, downloaded = chosen_pdf
        picked = (pdf, scan)
    if not picked:
        return jsonify(error="Could not read the PDF"), 422

    # 2. Non-AI scan result: the statement pages.
    pdf, scan = picked
    scan_info = {
        "pdf": pdf.name, "page_count": scan.page_count, "selected": scan.selected,
        "image_only_pages": scan.image_only_pages,
        "candidates": {st: sorted(({"page": p.page, "score": p.scores[st]} for p in scan.pages),
                                  key=lambda x: -x["score"])[:5] for st in scan.selected},
        "attachments": names, "downloaded": downloaded, "used_url": used_url,
    }
    chosen = pages or scan.all_selected
    if dry_run or not chosen:
        return jsonify(scan=scan_info, pages=chosen, extracted=False,
                       message=None if chosen else "No statement pages found; pass pages manually.")

    # 3. AI extraction on the selected pages only, then merge into the uploaded workbook (if any).
    if not extractor.provider():
        where = os.environ.get("VERCEL_ENV", "local")
        seen = ", ".join(extractor.ai_env_names()) or "none"
        return jsonify(error=(
            f"This deployment ({where}) can't see GEMINI_API_KEY (AI variables it sees: {seen}). "
            "In Vercel open THIS project → Settings → Environment Variables, add GEMINI_API_KEY "
            f"with {where.title()} ticked, then Deployments → Redeploy.")), 500
    res = process_pdf(pdf, scan=scan, pages=chosen, meta=meta,
                      effort=f.get("effort", "high"), cache_dir=WORK)
    existing = request.files["workbook"].read() if request.files.get("workbook") else None
    data, checks = workbook_bytes(existing, res.normalised, res.meta)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", res.meta.get("stock_code") or res.meta.get("company_name") or "").strip("_") or "financials"
    return jsonify(
        scan=scan_info, pages=chosen, extracted=True, meta=res.meta, usage=res.usage, checks=checks,
        filename=f"{name}.xlsx", workbook_b64=base64.b64encode(data).decode(),
    )
