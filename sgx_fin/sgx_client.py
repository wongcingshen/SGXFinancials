"""Screen SGX company announcements and download their PDF attachments.

Screening is driven by the public SGX page URL, e.g.

    https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17

`from` / `to` are Singapore dates (YYYYMMDD) and `ANNC=ANNC17` is the
"Financial Statements" category. That page is a JavaScript app whose listing
comes from api.sgx.com, which refuses requests without the token the page
generates. So by default the page is opened in a headless browser
(scraper/cli.mjs, Node + puppeteer) and its results are read from there; with
SGX_AUTH_TOKEN set, the API is queried directly instead. Announcement pages
and attachments on links.sgx.com are fetched directly, with the browser as a
fallback.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

PAGE_URL = "https://www.sgx.com/stock-exchange/company-announcements"
API_URL = "https://api.sgx.com/announcements/v1.1/"
FINANCIAL_RESULTS_CATEGORY = "ANNC17"
ROOT = Path(__file__).resolve().parent.parent
SGT = timezone(timedelta(hours=8))
PAGE_SIZE = 250

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.sgx.com",
    "Referer": "https://www.sgx.com/",
}


@dataclass
class Screen:
    """One screen of the SGX announcements page: a date range and categories."""

    date_from: date
    date_to: date
    categories: dict[str, list[str]] = field(
        default_factory=lambda: {"ANNC": [FINANCIAL_RESULTS_CATEGORY]}
    )

    @classmethod
    def from_url(cls, url: str) -> "Screen":
        qs = parse_qs(urlparse(url).query)
        try:
            d_from = datetime.strptime(qs["from"][0], "%Y%m%d").date()
            d_to = datetime.strptime(qs.get("to", qs["from"])[0], "%Y%m%d").date()
        except (KeyError, ValueError) as e:
            raise ValueError(f"URL needs from=YYYYMMDD (and to=YYYYMMDD): {url}") from e
        cats = {
            k: [c for v in vals for c in v.split(",") if c]
            for k, vals in qs.items()
            if k not in ("from", "to", "value", "exactsearch")
        }
        return cls(d_from, d_to, cats or {"ANNC": [FINANCIAL_RESULTS_CATEGORY]})

    @property
    def page_url(self) -> str:
        params = {"from": self.date_from.strftime("%Y%m%d"), "to": self.date_to.strftime("%Y%m%d")}
        params.update({k: ",".join(v) for k, v in self.categories.items()})
        return f"{PAGE_URL}?{urlencode(params, safe=',')}"

    def api_params(self, page: int) -> dict[str, str]:
        # The SGX site converts Singapore calendar days (UTC+8) to a UTC window:
        # 30 Oct SGT = 29 Oct 16:00:00 UTC .. 30 Oct 15:59:59 UTC.
        start = datetime.combine(self.date_from, datetime.min.time()) - timedelta(hours=8)
        end = datetime.combine(self.date_to, datetime.min.time()) + timedelta(hours=16, seconds=-1)
        params = {
            "periodstart": start.strftime("%Y%m%d_%H%M%S"),
            "periodend": end.strftime("%Y%m%d_%H%M%S"),
            "pagestart": str(page),
            "pagesize": str(PAGE_SIZE),
        }
        params["cat"] = ",".join(self.categories)
        params["sub"] = ",".join(c for v in self.categories.values() for c in v)
        return params


@dataclass
class Announcement:
    title: str
    issuer: str
    stock_code: str
    url: str
    broadcast: str
    category: str
    raw: dict

    @property
    def slug(self) -> str:
        base = self.stock_code or self.issuer
        return re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")[:40] or "UNKNOWN"


def _first(d: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return str(v)
    return default


def _parse_announcement(item: dict) -> Announcement:
    code = _first(item, "stock_code", "security_code", "securityCode", "issuer_code")
    if not code:
        for nested in ("issuers", "securities", "issuer"):
            sub = item.get(nested)
            if isinstance(sub, list) and sub and isinstance(sub[0], dict):
                code = _first(sub[0], "stock_code", "security_code", "code")
            elif isinstance(sub, dict):
                code = _first(sub, "stock_code", "security_code", "code")
            if code:
                break
    broadcast = _first(item, "broadcast_date_time", "submission_date_time", "submission_date")
    if broadcast.isdigit() and len(broadcast) >= 12:  # epoch milliseconds
        broadcast = datetime.fromtimestamp(int(broadcast) / 1000, SGT).strftime("%Y-%m-%d %H:%M")
    return Announcement(
        title=_first(item, "title", "announcement_title"),
        issuer=_first(item, "issuer_name", "security_name", "company_name"),
        stock_code=code,
        url=_first(item, "url", "link"),
        broadcast=broadcast,
        category=_first(item, "category_name", "sub_category_name", "cat"),
        raw=item,
    )


class SGXClient:
    def __init__(self, token: str | None = None, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout
        self.token = token or os.environ.get("SGX_AUTH_TOKEN")
        self.last_debug: dict = {}

    # ---------------------------------------------------------------- screen
    def screen(self, screen: Screen, company: str | None = None) -> list[Announcement]:
        """All announcements matching the page URL, optionally filtered by company name/code."""
        anns = None
        if self.token:
            try:
                anns = [_parse_announcement(i) for i in self._fetch_all(screen)]
            except PermissionError:
                anns = None  # token expired: fall back to the browser
        if anns is None:
            out = run_browser("screen", screen.page_url)
            self.last_debug = out.get("debug", {})
            anns = [Announcement(a["title"], a["issuer"], a["stock_code"], a["url"], a["broadcast"],
                                 a["category"], a) for a in out["announcements"]]
        if company:
            c = company.lower()
            anns = [a for a in anns if c in a.issuer.lower() or c == a.stock_code.lower()]
        return anns

    def _fetch_all(self, screen: Screen) -> list[dict]:
        items, page = [], 0
        while True:
            headers = {"authorizationtoken": self.token} if self.token else {}
            r = self.session.get(API_URL, params=screen.api_params(page), headers=headers, timeout=self.timeout)
            if r.status_code in (401, 403):
                raise PermissionError(f"SGX API returned {r.status_code}")
            r.raise_for_status()
            body = r.json()
            data = body.get("data") or []
            items.extend(data)
            total = int((body.get("meta") or {}).get("totalItems") or len(items))
            if not data or len(items) >= total:
                return items
            page += 1

    # ----------------------------------------------------------- attachments
    def attachments(self, ann: Announcement) -> list[str]:
        """PDF attachment URLs listed on the announcement's links.sgx.com page."""
        if ann.url.lower().endswith(".pdf"):
            return [ann.url]
        urls: list[str] = []
        try:
            r = self.session.get(ann.url, timeout=self.timeout)
            r.raise_for_status()
            for h in re.findall(r'href=["\']([^"\']+)["\']', r.text, flags=re.I):
                if "fileopen" in h.lower() or h.lower().split("?")[0].endswith(".pdf"):
                    full = urljoin(r.url, h.replace("&amp;", "&"))
                    if is_sgx_url(full) and full not in urls:
                        urls.append(full)
        except requests.RequestException:
            pass
        if not urls and browser_available():
            urls = [f["url"] for f in run_browser("attachments", ann.url)["files"]]
        return urls

    def download(self, url: str, dest_dir: Path) -> Path | None:
        """Download one attachment; returns None if it is not a PDF."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        r = self.session.get(url, timeout=self.timeout * 4)
        r.raise_for_status()
        if not r.content.startswith(b"%PDF"):
            return None
        name = _filename(r, url)
        path = dest_dir / name
        path.write_bytes(r.content)
        time.sleep(0.5)  # be polite to links.sgx.com
        return path


def _filename(resp: requests.Response, url: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    name = m.group(1) if m else Path(urlparse(url).path).name
    name = re.sub(r"\.ashx$", "", name, flags=re.I)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "attachment"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def browser_available() -> bool:
    return bool(shutil.which("node")) and (ROOT / "node_modules" / "puppeteer-core").exists()


def run_browser(op: str, url: str) -> dict:
    """Run scraper/cli.mjs (headless browser) and return its JSON output."""
    if not browser_available():
        raise RuntimeError(
            "SGX's announcement list only loads in a browser. Install Node.js and run `npm install` "
            "in the project (set CHROME_PATH to a local Chrome when not on Linux x64), or set "
            "SGX_AUTH_TOKEN to query the SGX API directly."
        )
    out = subprocess.run([shutil.which("node"), str(ROOT / "scraper" / "cli.mjs"), op, url],
                         capture_output=True, text=True, timeout=300, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(f"Browser step failed: {out.stderr.strip()[-600:]}")
    return json.loads(out.stdout)


def is_sgx_url(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == "sgx.com" or host.endswith(".sgx.com"))


def build_page_url(date_from: date, date_to: date, category: str = FINANCIAL_RESULTS_CATEGORY) -> str:
    return Screen(date_from, date_to, {"ANNC": [category]}).page_url


__all__ = ["Screen", "Announcement", "SGXClient", "build_page_url", "browser_available", "is_sgx_url", "run_browser"]
