"""Read a statement layout ("format") from a ShareInvestor export or from pasted rows.

Accepted input
  - Excel (.xlsx): one sheet per statement (named like "Income Statement", "Balance Sheet",
    "Cash Flow"), or one sheet with those words as section headings. A workbook made by this
    app also works, so its column-A labels can be edited in Excel and imported back.
  - CSV / pasted text (e.g. copied from ShareInvestor's Financials page): the label is the first
    cell or text of each line; "Income Statement" / "Balance Sheet" / "Cash Flow" lines start a
    section, numbers and dates are ignored.
Row kinds are guessed: a line with no figures that is followed by lines is a header, a bold line
or one starting with "Total" is a total; everything else is an item.

The web app shows the result as editable text, one row per line: "[Income Statement]" starts a
statement, "# " marks a header and "= " a total. Text with these markers is read back exactly.
"""

from __future__ import annotations

import csv
import io
import re

from . import template as T

_TITLE = r"^(?:consolidated |group |standalone )?(?:{})(?P<rest>.*)$"
SECTION = {
    "income_statement": re.compile(_TITLE.format(
        r"income statements?|statements? of (?:comprehensive )?income|statements? of profit or loss"
        r"|profit (?:and|or) loss(?: statement| account)?"), re.I),
    "balance_sheet": re.compile(_TITLE.format(
        r"balance sheets?|statements? of financial position|statements? of assets and liabilities"), re.I),
    "cash_flow": re.compile(_TITLE.format(r"cash ?flows?(?: statements?)?|statements? of cash ?flows?"), re.I),
}
# Words that make "Operating Cash Flow Before Working Capital Changes" a line, not a heading.
LINE_WORDS = re.compile(r"\b(before|after|from|operating|activities|net|generated|used|per|attributable)\b", re.I)
NUMBER = re.compile(r"^[\s(+\-−]*[\d,.]+[)%\s]*$|^[-–—]$|^n\.?a\.?$", re.I)
# Lines in exports that are page furniture, not statement rows.
SKIP = re.compile(r"^(period|period ended|months|currency|in (sgd|s\$|usd|rm|rmb)|figures in|units?|"
                  r"fy ?\d{4}|\d{1,2}m \w{3} \d{4}|financials?|annual|quarterly|half[- ]?year(ly)?|source:.*|"
                  r"all figures.*|\(.*'000.*\)|.*'000.*)$", re.I)
TOTAL = re.compile(r"^(total|net )|^(gross profit|operating profit|profit (before|after)|cash at end)", re.I)


def _section_of(text: str) -> str | None:
    """Which statement a heading line starts ('Income Statement (S$'000)', 'Statement of Cash
    Flows'), or None for an ordinary line."""
    s = str(text).strip()
    for st, rx in SECTION.items():
        m = rx.match(s)
        if m and len(m.group("rest").split()) <= 5 and not LINE_WORDS.search(m.group("rest") or ""):
            return st
    return None


def _is_label(cell) -> bool:
    if cell is None:
        return False
    s = str(cell).strip()
    return bool(s) and not NUMBER.match(s) and bool(re.search(r"[A-Za-z]", s))


def _rows_from_lines(lines: list[tuple], default: str | None = None) -> dict[str, list[dict]]:
    """lines: (label, has_figures, bold[, filled]) in order -> {statement: [row dicts]}

    A styled sheet (bold or filled label cells) is read by its styling: filled = header, bold = total.
    Otherwise a line without figures over lines with figures is a header.
    """
    lines = [(l[0], l[1], l[2], l[3] if len(l) > 3 else False) for l in lines]
    out: dict[str, list[dict]] = {}
    current = default
    styled = any(b or f for _, _, b, f in lines)
    with_figures = any(f for _, f, _, _ in lines)  # a labels-only paste can't use the figures rule
    for i, (label, has_figures, bold, filled) in enumerate(lines):
        section = _section_of(label) if not has_figures else None
        if section:
            current = section
            continue
        if current is None or SKIP.match(label):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        plain = T.normalise_label(label)
        if styled:
            kind = "header" if filled else "total" if bold else "item"
        elif with_figures and not has_figures and nxt and not _section_of(nxt[0]):
            kind = "header"  # a heading over figures (bold or not)
        elif not with_figures and (label.endswith(":") or (label.isupper() and len(label) > 3)):
            kind = "header"
        elif bold or TOTAL.search(plain):
            kind = "total"
        else:
            kind = "item"
        out.setdefault(current, []).append({"label": label.rstrip(":").strip(), "kind": kind})
    # A header with nothing under it before the next header is just a line without figures.
    for rows in ([] if styled else out.values()):
        for a, b in zip(rows, rows[1:] + [None]):
            if a["kind"] == "header" and (b is None or b["kind"] == "header"):
                a["kind"] = "item"
    return out


MARKED_SECTION = re.compile(r"^\[(.+)\]$")


def from_marked(text: str) -> dict[str, list[dict]] | None:
    """The editable text form: [Statement] lines, '# ' headers, '= ' totals. None if unmarked."""
    raw = [l.rstrip() for l in text.splitlines() if l.strip()]
    if not any(MARKED_SECTION.match(l.strip()) for l in raw):
        return None
    out: dict[str, list[dict]] = {}
    current = None
    for line in raw:
        line = line.strip()
        m = MARKED_SECTION.match(line)
        if m:
            current = _section_of(m.group(1))
            continue
        if current is None:
            continue
        kind = "header" if line.startswith("# ") else "total" if line.startswith("= ") else "item"
        label = line[2:].strip() if kind != "item" else line
        label, _, formula = label.partition(":=")
        label = label.strip()
        if label:
            row = {"label": label, "kind": kind}
            if formula.strip():
                row["formula"] = formula.strip()
            out.setdefault(current, []).append(row)
    return out


def to_marked(fmt: dict) -> str:
    """A format as editable text (the inverse of from_marked)."""
    parts = []
    for st in T.STATEMENT_IDS:
        rows = (fmt.get("statements") or {}).get(st, {}).get("rows") or []
        parts.append(f"[{T.STATEMENT_TITLES[st]}]")
        parts += [("# " if r.get("kind") == "header" else "= " if r.get("kind") == "total" else "") + r["label"]
                  + (f" := {r['formula']}" if r.get("formula") else "") for r in rows]
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def from_text(text: str) -> dict[str, list[dict]]:
    marked = from_marked(text)
    if marked is not None:
        return marked
    lines = []
    for raw in text.splitlines():
        cells = [c.strip() for c in re.split(r"\t|\s{2,}|\|", raw) if c.strip()]
        if not cells or not _is_label(cells[0]):
            continue
        lines.append((cells[0], any(NUMBER.match(c) and re.search(r"\d", c) for c in cells[1:]), False))
    return _rows_from_lines(lines)


def from_csv(data: bytes) -> dict[str, list[dict]]:
    text = data.decode("utf-8-sig", errors="replace")
    lines = []
    for cells in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in cells]
        label = next((c for c in cells if _is_label(c)), None)
        if not label:
            continue
        rest = cells[cells.index(label) + 1:]
        lines.append((label, any(NUMBER.match(c) and re.search(r"\d", c) for c in rest if c), False))
    return _rows_from_lines(lines)


def from_xlsx(data: bytes) -> dict[str, list[dict]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True)
    out: dict[str, list[dict]] = {}
    for ws in wb.worksheets:
        if ws.sheet_state != "visible" or ws.title.lower() in ("checks", "source map", "_store", "cogs basis", "transcription"):
            continue
        lines = []
        ours = str(ws["A4"].value or "").strip() == "Period"  # a workbook made by this app
        for row in ws.iter_rows(min_row=8 if ours else 1):
            cells = [c for c in row if c.value is not None and str(c.value).strip()]
            label_cell = next((c for c in cells if _is_label(c.value)), None)
            if label_cell is None:
                continue
            others = [c for c in cells if c is not label_cell]
            has_figures = any(isinstance(c.value, (int, float)) or (NUMBER.match(str(c.value)) and re.search(r"\d", str(c.value)))
                              for c in others)
            bold = bool(label_cell.font and label_cell.font.b)
            fill = label_cell.fill
            filled = bool(fill and fill.fill_type == "solid" and str(fill.fgColor.rgb or "").upper() not in ("00000000", "FFFFFFFF"))
            lines.append((str(label_cell.value).strip(), has_figures, bold, filled))
        for st, rows in _rows_from_lines(lines, default=_section_of(ws.title)).items():
            out.setdefault(st, []).extend(rows)
    return out


def build(name: str, applies_to: str, sections: dict[str, list[dict]], base: str | None = None,
          fmt_id: str = "") -> dict:
    """A format from parsed sections. Statements not in the input are taken from `base`."""
    if not any(sections.values()):
        raise ValueError("No statement rows found. Include the Income Statement, Balance Sheet and/or "
                         "Cash Flow headings, or name the sheets after them.")
    fid = T._slug(fmt_id or name) if (fmt_id or name) else "custom"
    base_fmt = T.BUILTIN.get(base or fid) or T.BUILTIN.get("general")
    statements = {}
    for st in T.STATEMENT_IDS:
        if sections.get(st):
            statements[st] = {"title": T.STATEMENT_TITLES[st], "rows": sections[st]}
        elif base_fmt:
            statements[st] = T.to_json(base_fmt)["statements"][st]
    fmt = T.check_format({"id": fid, "name": name or fid.title(), "applies_to": applies_to, "statements": statements},
                         prefer=base_fmt)
    return T.to_json(fmt)


def parse(name: str, applies_to: str, *, text: str = "", filename: str = "", data: bytes = b"",
          base: str | None = None, fmt_id: str = "") -> dict:
    if data:
        lower = filename.lower()
        if lower.endswith((".xlsx", ".xlsm")):
            sections = from_xlsx(data)
        elif lower.endswith((".csv", ".txt")):
            sections = from_csv(data)
        else:
            raise ValueError("Use an .xlsx or .csv file, or paste the rows")
    else:
        sections = from_text(text)
    return build(name, applies_to, sections, base, fmt_id)
