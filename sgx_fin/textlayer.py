"""A free second reading for text filings: the figures printed on a line, read from the PDF's own
text layer (no AI), for the lines of checks that fail.

The AI sometimes takes a figure from the row next door, or maps a line to the wrong key ("Loss from
discontinued operations" for the minority's share). The text layer has every printed figure exactly;
what it lacks is which column is which. So it only offers candidates: each figure printed on the line
the AI named, or on a line whose wording means that key, is tried by checks.try_candidates, and kept
only where it makes the failing check add up without breaking one that passed.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import template as T

NUMBER = re.compile(r"\(?[-−–]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?[-−–]?\d+(?:\.\d+)?\)?")
NIL = re.compile(r"[-−–—*@]|n\.?\s?m\.?|n/m|nil|n\.a\.?|na", re.I)
OUR_NOTES = re.compile(r"\s*\((?:sign corrected|corrected|re-read)[^)]*\)\s*$", re.I)


def _number(token: str) -> float | None:
    t = token.strip()
    if not NUMBER.fullmatch(t):
        return None
    neg = t.startswith("(") and t.endswith(")") or t.lstrip("(").startswith(("-", "−", "–"))
    v = float(re.sub(r"[^\d.]", "", t) or "nan")
    return -v if neg else v


def parse_line(line: str) -> tuple[str, list[float]]:
    """'Non-controlling interests - (1,336) N.M.' -> ('Non-controlling interests', [-1336.0]).
    Figures are taken from the right until the first word; nil marks and percentages are skipped."""
    tokens = line.split()
    nums: list[float] = []
    while tokens:
        t = tokens[-1]
        if t.endswith("%") and _number(t[:-1]) is not None:
            tokens.pop()
        elif (v := _number(t)) is not None:
            nums.append(v)
            tokens.pop()
        elif NIL.fullmatch(t.strip("()")):
            tokens.pop()
        else:
            break
    return " ".join(tokens), nums[::-1]


def lines_with_figures(texts: list[str]) -> list[tuple[str, list[float]]]:
    """(normalised label, figures) for every line with figures. A label wrapped over the line before
    (which has no figures of its own) is joined to it, and both versions are offered; figures wrapped
    onto a line of their own belong to the label above them."""
    out = []
    for text in texts:
        prev, last = "", ""
        for raw in text.splitlines():
            label, nums = parse_line(raw.strip())
            if not nums:
                prev = label if label else ""
                last = T.normalise_label(label) if label else last
                continue
            name = T.normalise_label(label)
            if name:
                out.append((name, nums))
                last = name
            elif last:  # '- (4,304)' under 'Changes in cash ... held for sale 3'
                out.append((last, nums))
            if prev:
                out.append((T.normalise_label(f"{prev} {label}"), nums))
            prev = ""
    return out


def names_for(statement: str, key: str, source_label: str = "") -> set[str]:
    """The wordings that mean `key`: the AI's own source label (when it names one line), the
    built-in label and the synonyms."""
    names = {name for name, k in T._LOOKUP.get(statement, {}).items() if k == key}
    label = OUR_NOTES.sub("", str(source_label or ""))
    while OUR_NOTES.search(label):
        label = OUR_NOTES.sub("", label)
    if label and " + " not in label and not label.startswith(("computed", "formula", "judged", "your choice")):
        names.add(T.normalise_label(label))
    names.discard("")
    return names


def candidates(texts: dict[str, list[str]], normalised: dict, keys: dict[str, set[str]] | None = None) -> dict:
    """{statement: {key: [(figure in '000, printed wording)]}} from the statement's own pages' text.
    `texts`: {statement: [page text, ...]}; `keys`: only these keys per statement (all checked keys if None)."""
    from .checks import CHECKS, terms

    out: dict[str, dict[str, list[tuple[float, str]]]] = {}
    for st, block in normalised.items():
        lines = lines_with_figures(texts.get(st) or [])
        if not lines:
            continue
        step = block.get("step", 1.0) or 1.0
        wanted = keys.get(st, set()) if keys is not None else {
            k for s, _, parts, total in CHECKS if s == st for k in [*(t[1] for t in terms(parts)), total]}
        for key in wanted:
            names = names_for(st, key, (block.get("labels") or {}).get(key, ""))
            seen, found = set(), []
            for name, nums in lines:
                if name in names:
                    for v in nums:
                        x = round(v * step, 3)
                        if x not in seen:
                            seen.add(x)
                            found.append((x, name))
            if found:
                out.setdefault(st, {})[key] = found
    return out


def read_texts(pdf: Path, pages_by_statement: dict[str, list[int]]) -> dict[str, list[str]]:
    """{statement: [text of each of its pages]} (1-based pages) from the PDF's text layer."""
    from .page_scanner import page_texts

    texts = page_texts(pdf)
    return {st: [texts[p - 1] for p in pages if 1 <= p <= len(texts)] for st, pages in pages_by_statement.items()}
