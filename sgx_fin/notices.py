"""Notices of when a company will release its results: the date, the period and the timing.

Read from the notice's text with patterns, no AI. Example:
  "...intends to release its financial results for the first half of FY2027 ended 30 September
   2026 on 13 November 2026 (Friday) after the close of trading on the Singapore Exchange."
"""

from __future__ import annotations

import re
from datetime import datetime

MONTHS = "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
DATE = rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})\s*,?\s+(\d{{4}})"
RELEASE = re.compile(rf"(?:release|announce|publish)\w*\b.{{0,220}}?\bon\s+(?:\w+day,?\s+)?{DATE}", re.I | re.S)
PERIOD = re.compile(rf"(?:results|statements)\b.{{0,40}}?\bfor\s+(?:the\s+)?(.{{3,90}}?\s+ended\s+{DATE})", re.I | re.S)
TIMING = re.compile(r"(after the (?:close of )?(?:trading|market)[^.,;]*|before the (?:opening|commencement|market opens)[^.,;]*"
                    r"|(?:pre|post)[- ]market|during (?:the )?(?:trading|market) hours)", re.I)


def _iso(day: str, month: str, year: str) -> str | None:
    try:
        return datetime.strptime(f"{day} {month[:3]} {year}", "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_release_notice(text: str) -> dict:
    """{'release_date': 'YYYY-MM-DD' | None, 'period': str, 'period_end': 'YYYY-MM-DD' | None, 'timing': str}"""
    flat = re.sub(r"\s+", " ", text)
    out = {"release_date": None, "period": "", "period_end": None, "timing": ""}
    period = PERIOD.search(flat)
    if period:
        out["period"] = period.group(1).strip()
        out["period_end"] = _iso(*period.groups()[1:4])
    # The release date: "on <date>" after "release/announce", skipping the period's own end date.
    for m in RELEASE.finditer(flat):
        iso = _iso(*m.groups())
        if iso and iso != out["period_end"]:
            out["release_date"] = iso
            timing = TIMING.search(flat, m.end(), m.end() + 160)
            out["timing"] = timing.group(1).strip() if timing else ""
            break
    return out
