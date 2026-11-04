"""Which announcements come from companies listed on SGX, and which from issuers of debt only.

The Financial Statements category also carries results filed by foreign banks and companies whose
only SGX listing is a bond ("BANK OF BARODA" under 2V6B, "GRAB HOLDINGS" under 52WB), and by
issuers with no stock code at all. Only listed companies are kept:
  1. listings/companies.csv: the SGX companies list (from the Google Sheet), and a snapshot of
     SGX's own list of equity securities for listed companies the sheet doesn't have.
  2. SGX's live list of securities (api.sgx.com/securities, no token needed), so a company that
     lists after the file was made (a new IPO) is kept from its first day of trading.
An announcement is kept when one of its stock codes is listed, or, when it carries no stock code,
when its issuer name matches a listed company's name. Keep in step with scraper/listed.mjs.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

FILE = Path(__file__).resolve().parent.parent / "listings" / "companies.csv"
LIVE_URL = "https://api.sgx.com/securities/v1.1?params=nc,n,type"
EQUITY_TYPES = {"stocks", "reits", "businesstrusts", "adrs"}
# Company-form words, and the trading-counter tags in SGX's short names ("AEM USD", "Nomura Yen1k").
SUFFIX = {"LIMITED", "LTD", "PLC", "INC", "CORP", "CORPORATION", "CO", "BHD", "BERHAD",
          "SGD", "USD", "HKD", "RMB", "YEN1K", "OV"}


def words(name: str) -> list[str]:
    return [w for w in re.split(r"[^A-Z0-9]+", str(name or "").upper().replace("&", " AND ")) if w and w not in SUFFIX]


def _abbreviates(w: str, v: str) -> bool:
    if v.startswith(w):
        return True
    if w[0] != v[0]:
        return False
    it = iter(v)
    return all(ch in it for ch in w)


def name_matches(short: str, issuer: str) -> bool:
    """'Hotung Inv' ~ 'HOTUNG INVESTMENT HOLDINGS LIMITED', 'TheHourGlass' ~ 'THE HOUR GLASS LIMITED',
    'KSH' ~ 'KSH HOLDINGS LIMITED' (3 letters: whole words only)."""
    a, b = words(short), words(issuer)
    ca, cb = "".join(a), "".join(b)
    if len(ca) < 3 or not cb:
        return False
    if len(ca) == 3:
        return len(a) <= len(b) and a == b[:len(a)]
    if ca == cb or (len(ca) >= 5 and cb.startswith(ca)):
        return True
    return len(a) <= len(b) and all(_abbreviates(w, v) for w, v in zip(a, b))


@dataclass
class Listed:
    codes: set[str]
    names: list[str]
    source: str
    error: str = ""
    _seen: dict = field(default_factory=dict, repr=False)


def bundled() -> list[tuple[str, str]]:
    with FILE.open(encoding="utf-8", newline="") as f:
        return [(r["code"].strip().upper(), r["name"].strip()) for r in csv.DictReader(f) if r.get("code", "").strip()]


def _live(timeout: int = 20) -> list[tuple[str, str]]:
    r = requests.get(LIVE_URL, timeout=timeout, headers={
        "Accept": "application/json", "Origin": "https://www.sgx.com", "Referer": "https://www.sgx.com/",
        "User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    prices = ((r.json() or {}).get("data") or {}).get("prices") or []
    out = [(str(p["nc"]).upper(), p.get("n") or "") for p in prices
           if isinstance(p, dict) and p.get("nc") and p.get("type") in EQUITY_TYPES]
    if len(out) < 100:
        raise ValueError(f"only {len(out)} equities listed")
    return out


_cached: Listed | None = None


def load(live: bool = True) -> Listed:
    """The listed companies: the bundled list, plus SGX's live list when it can be read."""
    global _cached
    if _cached and live:
        return _cached
    rows, source, error = bundled(), "bundled", ""
    if live:
        try:
            rows += _live()
            source = "bundled + SGX live"
        except (requests.RequestException, ValueError) as e:
            error = str(e)
    out = Listed({c for c, _ in rows}, list(dict.fromkeys(n for _, n in rows if n)), source, error)
    if live:
        _cached = out
    return out


def codes_of(ann) -> list[str]:
    codes = ann.raw.get("stock_codes") if isinstance(ann.raw, dict) else None
    return [c for c in (str(x or "").strip().upper() for x in (codes or [ann.stock_code])) if c]


def is_listed(ann, listed: Listed) -> bool:
    """True for an announcement from a listed company; sets ann.stock_code to its share code."""
    codes = codes_of(ann)
    code = next((c for c in codes if c in listed.codes), None)
    if code:
        ann.stock_code = code
        return True
    if codes:
        return False
    key = ann.issuer.upper()
    if key not in listed._seen:
        listed._seen[key] = any(name_matches(n, ann.issuer) for n in listed.names)
    return listed._seen[key]


def split(anns: list, listed: Listed | None = None) -> tuple[list, list]:
    listed = listed or load()
    kept, dropped = [], []
    for a in anns:
        (kept if is_listed(a, listed) else dropped).append(a)
    return kept, dropped


__all__ = ["Listed", "load", "split", "is_listed", "name_matches", "bundled"]
