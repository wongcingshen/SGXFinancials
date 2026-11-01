"""Notices of when a company will release its results: the date, the period and the timing.

Read with patterns, no AI, from the announcement page's own text (some notices state the date
there and attach nothing) or from the notice PDF. Examples:
  "...intends to release its financial results for the first half of FY2027 ended 30 September
   2026 on 13 November 2026 (Friday) after the close of trading on the Singapore Exchange."
  "Date of Results Announcement: 13/11/2026"
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import datetime

MONTHS = "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
# 13 November 2026 · 13th Nov, 2026 · 13-Nov-2026 · November 13, 2026 · 13/11/2026 · 13.11.2026 · 2026-11-13
DATE = (rf"(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?[\s-]+(?P<m1>{MONTHS})\.?[\s,-]+(?P<y1>\d{{4}})"
        rf"|(?P<m2>{MONTHS})\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{{4}})"
        rf"|(?P<d3>\d{{1,2}})[./](?P<n3>\d{{1,2}})[./](?P<y3>\d{{4}})"
        rf"|(?P<y4>\d{{4}})-(?P<n4>\d{{2}})-(?P<d4>\d{{2}}))")
WEEKDAY = r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
RELEASE = re.compile(rf"(?:release|announce|publish|report)\w*\b.{{0,220}}?\bon\s+{WEEKDAY}{DATE}", re.I | re.S)
LABELLED = re.compile(rf"(?:results?\s+(?:release|announcement)\s+date|date\s+of\s+(?:the\s+)?(?:results?\s+)?(?:release|announcement)"
                      rf"(?:\s+of\s+(?:the\s+)?(?:financial\s+)?results)?|release\s+date)\s*[:\-–]?\s*{WEEKDAY}{DATE}", re.I | re.S)
PERIOD = re.compile(rf"(?:results|statements)\b.{{0,40}}?\bfor\s+(?:the\s+)?(.{{3,90}}?\s+ended\s+{DATE})", re.I | re.S)
TIMING = re.compile(r"(after (?:the )?(?:close of )?(?:trading|market)[^.,;)]*|after (?:the )?market (?:close|closes)[^.,;)]*"
                    r"|before (?:the )?(?:opening|commencement|market opens?|start of trading)[^.,;)]*"
                    r"|(?:pre|post)[- ]market|during (?:the )?(?:trading|market) hours)", re.I)


def _iso(m: re.Match) -> str | None:
    g = {k: v for k, v in m.groupdict().items() if v}
    try:
        if "y1" in g:
            return datetime.strptime(f"{g['d1']} {g['m1'][:3]} {g['y1']}", "%d %b %Y").strftime("%Y-%m-%d")
        if "y2" in g:
            return datetime.strptime(f"{g['d2']} {g['m2'][:3]} {g['y2']}", "%d %b %Y").strftime("%Y-%m-%d")
        if "y3" in g:  # SGX writes day first
            return datetime(int(g["y3"]), int(g["n3"]), int(g["d3"])).strftime("%Y-%m-%d")
        if "y4" in g:
            return datetime(int(g["y4"]), int(g["n4"]), int(g["d4"])).strftime("%Y-%m-%d")
    except ValueError:
        return None
    return None


def page_text(html: str) -> str:
    """The readable text of an announcement page, without scripts, styles or tags."""
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|td|th)>", "\n", html)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html))


def parse_release_notice(text: str) -> dict:
    """{'release_date': 'YYYY-MM-DD' | None, 'period': str, 'period_end': 'YYYY-MM-DD' | None, 'timing': str}"""
    flat = re.sub(r"\s+", " ", text)
    out = {"release_date": None, "period": "", "period_end": None, "timing": ""}
    period = PERIOD.search(flat)
    if period:
        out["period"] = period.group(1).strip()
        out["period_end"] = _iso(period)
    # The release date: a labelled one ("Date of Results Announcement: ..."), else "release ... on
    # <date>"; never the period's own end date.
    for rx in (LABELLED, RELEASE):
        for m in rx.finditer(flat):
            iso = _iso(m)
            if iso and iso != out["period_end"]:
                out["release_date"] = iso
                timing = TIMING.search(flat, m.end(), m.end() + 160)
                out["timing"] = timing.group(1).strip() if timing else ""
                return out
    return out
