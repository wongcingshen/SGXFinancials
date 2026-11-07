"""Arithmetic tie-outs of the statements (parts add up to their printed total).

Used four ways: to put right a sign the AI read backwards, to pick which lines to read again when
a total doesn't add up, to report PASS / FAIL on the workbook's Checks sheet, and to give each
extraction a verdict: can its figures be relied on?

Each check is (statement, name, parts, total). A part starting with "-" is subtracted; one ending
in "?" may be missing (it then counts as nothing); one ending in "~" may or may not be inside the
total already (perpetual securities shown within or after the owners' equity): the check passes
either way. A check runs when its total and every required
part have a figure; it is skipped entirely when none of its lines exist (a check for the other
format, bank vs general).

A PASS has to prove something. A check whose figures are all 0 fails (0 + 0 = 0 only shows that
nothing was read), and the slack allowed is the rounding of the figures involved plus a sliver of
materiality (0.005% of the largest figure, never more than 10 of the filing's units), so rounding
never fails a check but a misread digit that matters can't hide inside a percentage of a large total.

Checks are lenient about presentation, strict about figures: a line a filing may print inside or
outside a total ("~": exchange differences, perpetual securities, held for sale) passes either way,
and a line two failing checks agree is wrong is put right from them (repair_by_agreement).
"""

from __future__ import annotations

CHECKS = [
    ("income_statement", "Revenue + COGS = Gross Profit", ["revenue", "cost_of_sales"], "gross_profit"),
    ("income_statement", "PBT + Tax (+ discontinued) = PAT", ["profit_before_tax", "tax", "discontinued_ops?"],
     "profit_after_tax"),
    ("income_statement", "Shareholders + MI (+perps) = PAT (+ associates after tax)",
     ["pat_shareholders", "pat_minority?", "perpetual_holders~", "-share_associates_after_tax?"], "profit_after_tax"),
    # Assets / liabilities held for sale are printed inside the current subtotal or after it.
    ("balance_sheet", "NCA + CA = Total Assets", ["total_nca", "total_ca", "assets_held_for_sale~"], "total_assets"),
    ("balance_sheet", "CL + NCL = Total Liabilities", ["total_cl", "total_ncl", "liabilities_held_for_sale~"],
     "total_liabilities"),
    ("balance_sheet", "Total Liabilities + Total Equity = Total Assets", ["total_liabilities", "total_equity"], "total_assets"),
    ("balance_sheet", "Total Assets - Total Liabilities = Net Assets", ["total_assets", "-total_liabilities"], "net_assets"),
    ("balance_sheet", "Shareholders' Equity + MI (+ perps) = Total Equity",
     ["shareholders_equity", "minority_interests?", "perpetual_securities~"], "total_equity"),
    # Exchange differences come after the net change (most SGX filings) or inside it (Japanese, US layouts).
    ("cash_flow", "Operating + Investing + Financing (+ FX) = Net Change",
     ["cf_net_operating", "cf_net_investing", "cf_net_financing", "cf_fx_effect~", "cf_other_cash_changes~"],
     "cf_net_change"),
    ("cash_flow", "Begin + Net Change + FX (+ other) = End",
     ["cf_cash_begin", "cf_net_change", "cf_fx_effect~", "cf_other_cash_changes~"], "cf_cash_end"),
    # banks
    ("income_statement", "Interest Income + Interest Expense = Net Interest Income",
     ["interest_income", "interest_expense"], "net_interest_income"),
    ("income_statement", "Net Interest Income + Non-Interest Income = Total Operating Income",
     ["net_interest_income", "fee_income?", "other_non_interest_income?"], "total_operating_income"),
    ("income_statement", "Staff Costs + Other Operating Expenses = Total Operating Expenses",
     ["staff_costs", "other_operating_expenses_bank"], "total_operating_expenses"),
    ("income_statement", "Total Operating Income + Total Operating Expenses = Operating Profit before Allowances",
     ["total_operating_income", "total_operating_expenses"], "operating_profit_pre_provisions"),
    ("income_statement", "Operating Profit before Allowances + Allowances (+ associates, exceptional) = PBT",
     ["operating_profit_pre_provisions", "allowances?", "share_associates?", "exceptional_items?"], "profit_before_tax"),
    ("balance_sheet", "Bank assets add up to Total Assets",
     ["cash_central_bank", "due_from_banks?", "bank_investments?", "loans_advances", "ppe?", "other_assets?"], "total_assets"),
    ("balance_sheet", "Deposits + Borrowings + Other Liabilities + Equity = Total Assets",
     ["customer_deposits", "due_to_banks?", "bank_borrowings?", "other_liabilities?", "share_capital", "reserves",
      "perpetual_securities?", "treasury_shares?", "minority_interests?"], "total_assets"),
]


def terms(parts: list[str]) -> list[tuple[int, str, bool]]:
    """[(sign, key, optional)] of a check's parts ("?" and "~" parts are both optional)."""
    return [(-1 if p.startswith("-") else 1, p.lstrip("-").rstrip("?~"), p.endswith(("?", "~"))) for p in parts]


def flexible(parts: list[str]) -> set[str]:
    """Parts that may already be inside the total ("~")."""
    return {p.lstrip("-").rstrip("~") for p in parts if p.endswith("~")}


def _sums(parts: list[str], get) -> list[float]:
    """The sum of a check's parts, once for each way its "~" parts may be counted (in or out)."""
    ts, flex = terms(parts), flexible(parts)
    fixed = sum(sign * (get(k) or 0) for sign, k, _ in ts if k not in flex)
    out = [fixed]
    for sign, k, _ in ts:
        if k in flex and get(k):
            out += [x + sign * get(k) for x in out]
    return out


# What usually lies behind a failing check, for the AI to look at first.
CAUSES = {
    "PBT + Tax (+ discontinued) = PAT": "profit_after_tax is the profit for the period from continuing AND discontinued "
    "operations; the discontinued line goes in discontinued_ops",
    "Shareholders + MI (+perps) = PAT (+ associates after tax)": "take pat_shareholders, pat_minority and "
    "perpetual_holders only from the lines under 'attributable to', in the same column; not the discontinued "
    "operations line or a comprehensive income line",
    "Total Liabilities + Total Equity = Total Assets": "total_equity is never the 'Total equity and liabilities' line "
    "(that equals total assets); total_liabilities is never 'Total liabilities and equity'",
    "Shareholders' Equity + MI (+ perps) = Total Equity": "shareholders_equity is the equity attributable to owners "
    "(the subtotal, or share capital + reserves when there is none); total_equity is never 'Total equity and liabilities'",
    "Begin + Net Change + FX (+ other) = End": "a line printed between the net change and the cash at the end "
    "(cash transferred to assets held for sale, cash of a disposal group, bank overdrafts, restricted cash) goes in "
    "cf_other_cash_changes; cf_cash_end is the end of the cash flow statement's roll-forward, before any "
    "reconciliation to the balance sheet below it; cf_cash_begin is this period's opening cash",
    "Operating + Investing + Financing (+ FX) = Net Change": "use the printed net cash totals of each section (after "
    "interest and tax paid), in the same column",
    "NCA + CA = Total Assets": "total_ca / total_nca are the printed subtotals; assets held for sale shown under the "
    "current subtotal are part of total_ca",
    "CL + NCL = Total Liabilities": "total_cl / total_ncl are the printed subtotals; liabilities held for sale go in "
    "liabilities_held_for_sale",
}


ZERO = "every figure is 0"
STATEMENT_NAMES = {"income_statement": "income statement", "balance_sheet": "balance sheet",
                   "cash_flow": "cash flow statement"}


def _decimals(x: float) -> int:
    """Decimal places (0-3) a figure shows, from its value in the filing's own unit."""
    for d in range(4):
        y = x * 10 ** d
        if abs(y - round(y)) <= abs(y) * 1e-12 + 1e-9:
            return d
    return 3


MATERIAL = 0.00005  # 0.005% of the largest figure of a check counts as rounding...
MATERIAL_UNITS = 10  # ...but never more than this many of the filing's printed units


def tolerance(figures: list[float], step: float = 1.0) -> float:
    """Rounding slack in output units ('000) for figures that should add up.

    Each figure was printed rounded to the filing's unit (`step` output units), or to its decimals
    when every figure shows some (crores with paise), so the figures of a check can disagree by half
    of that each; never less than two of it. On large totals (companies round from unrounded ledgers)
    up to 0.005% of the largest figure is allowed too, capped at 10 printed units. No real share of
    the total: 0.2% of a bank's total assets would let a line be misread by billions."""
    places = min((_decimals(v / step) for v in figures), default=0) if step else 0
    unit = max(step / 10 ** places, 0.001)  # figures are kept to 3 decimals of '000
    largest = max((abs(v) for v in figures), default=0.0)
    return max(2 * unit, unit * len(figures) / 2, min(MATERIAL * largest, MATERIAL_UNITS * unit))


def run(statement: str, get, labels: dict | None = None, step: float = 1.0) -> list[dict]:
    """The checks of one statement for one period. `get(key)` gives a figure or None."""
    labels = labels or {}
    out = []
    for st, name, parts, total in CHECKS:
        if st != statement:
            continue
        ts = terms(parts)
        keys = [k for _, k, _ in ts]
        if all(get(k) is None for k in (*keys, total)):
            continue
        diff = None
        note = ""
        if str(labels.get(total, "")).startswith(("computed", "formula")):
            status = "derived"  # the total was calculated, not printed
        elif get(total) is None or any(get(k) is None for _, k, opt in ts if not opt):
            status = "n/a"
        else:
            figures = [get(k) for k in keys if get(k) is not None] + [get(total)]
            diff = min((round(x - get(total), 3) for x in _sums(parts, get)), key=abs)
            if not any(figures):  # 0 + 0 = 0 proves nothing: lines read as 0 that may not be there at all
                status, note = "FAIL", ZERO
            else:
                status = "PASS" if abs(diff) <= tolerance(figures, step) else "FAIL"
        out.append({"statement": st, "check": name, "status": status, "difference": diff,
                    "parts": [k for k in keys if get(k) is not None], "total": total, "note": note})
    return out


def _passing(results: list[dict]) -> set[str]:
    return {c["check"] for c in results if c["status"] == "PASS"}


def _better(before: list[dict], after: list[dict], wanted: set[str]) -> bool:
    """A change is kept only when the checks it was for now pass, none that passed breaks, and no
    check fails that didn't fail before."""
    failing = lambda res: {c["check"] for c in res if c["status"] == "FAIL"}  # noqa: E731
    return wanted <= _passing(after) and _passing(before) <= _passing(after) and failing(after) <= failing(before)


def column(values: dict, n: int, i: int):
    return lambda k: (values.get(k) or [None] * n)[i]


def repair_signs(statement: str, values: dict, labels: dict, n: int, step: float = 1.0) -> list[str]:
    """Flip the sign of a figure where that, and only that, makes a failing check add up without
    breaking one that passed (a charge read as positive, a minority share printed as 'Less: ...').
    Returns what was changed."""
    changed = []
    for i in range(n):
        get = column(values, n, i)
        for _ in range(4):
            res = run(statement, get, labels, step)
            before = _passing(res)
            fixed = False
            for c in (c for c in res if c["status"] == "FAIL"):
                for k in c["parts"]:
                    v = get(k)
                    if not v:
                        continue
                    values[k][i] = -v
                    after = _passing(run(statement, get, labels, step))
                    if c["check"] in after and before <= after:
                        changed.append(f"{k}[{i}]")
                        if "sign corrected" not in str(labels.get(k, "")):
                            labels[k] = f"{labels.get(k, '')} (sign corrected so the total adds up)".strip()
                        fixed = True
                        break
                    values[k][i] = v
                if fixed:
                    break
            if not fixed:
                break
    return changed


def _implied(parts: list[str], total: str, key: str, get) -> list[float]:
    """The figures for `key` that would make a check add up, the other lines taken as read."""
    ts = terms(parts)
    if key == total:
        return _sums(parts, get)
    sign = next((sg for sg, k, _ in ts if k == key), None)
    if sign is None or get(total) is None:
        return []
    others = [p for p in parts if p.lstrip("-").rstrip("?~") != key]
    return [sign * (get(total) - x) for x in _sums(others, get)]


def _note(labels: dict, key: str, why: str) -> None:
    if why not in str(labels.get(key, "")):
        labels[key] = f"{labels.get(key, key)} ({why})".strip()


def repair_by_agreement(statement: str, values: dict, labels: dict, n: int, step: float = 1.0) -> list[dict]:
    """Put right a line that two failing checks each say is wrong, by the same amount: both checks
    give it the same figure from other printed lines, so that figure is not a guess (a total read from
    the line next door, "Total equity and liabilities" taken for total equity). Taken only when both
    checks then pass and none that passed breaks. Returns what was changed ({key, index, old, new, why})."""
    changed = []
    for i in range(n):
        get = column(values, n, i)
        for _ in range(4):
            res = run(statement, get, labels, step)
            fails = [c for c in res if c["status"] == "FAIL" and c["note"] != ZERO]
            spec = {name: (parts, total) for st, name, parts, total in CHECKS if st == statement}
            options: dict[str, list[tuple[str, float]]] = {}
            for c in fails:
                parts, total = spec[c["check"]]
                for k in (*c["parts"], total):
                    if get(k) is None or str(labels.get(k, "")).startswith(("computed", "formula")):
                        continue
                    for v in _implied(parts, total, k, get):
                        options.setdefault(k, []).append((c["check"], round(v, 3)))
            fixed = False
            for k, opts in options.items():
                for name, v in opts:
                    agree = {nm for nm, w in opts if abs(w - v) <= tolerance([v, w], step)}
                    if len(agree) < 2 or abs(v - get(k)) <= tolerance([v, get(k)], step):
                        continue
                    old = get(k)
                    values[k][i] = v
                    if _better(res, run(statement, get, labels, step), agree):
                        changed.append({"key": k, "index": i, "old": old, "new": v, "why": f"{len(agree)} totals agree"})
                        _note(labels, k, f"corrected where {len(agree)} printed totals agree")
                        fixed = True
                        break
                    values[k][i] = old
                if fixed:
                    break
            if not fixed:
                break
    return changed


def repair_known_slips(statement: str, values: dict, labels: dict, n: int, step: float = 1.0) -> list[dict]:
    """Readings that can't be right: total equity (or total liabilities) equal to total assets while
    the other is not nil is the "Total equity and liabilities" line read for it. The figure is worked
    out from the other two printed totals instead."""
    if statement != "balance_sheet":
        return []
    changed = []
    for i in range(n):
        get = column(values, n, i)
        ta, tl, te = get("total_assets"), get("total_liabilities"), get("total_equity")
        if ta is None or tl is None or te is None or not ta:
            continue
        same = lambda a, b: abs(a - b) <= tolerance([a, b], step)  # noqa: E731
        for key, other in (("total_equity", tl), ("total_liabilities", te)):
            if same(get(key), ta) and abs(other) > tolerance([other], step) and not same(other + get(key), ta):
                old = get(key)
                values[key][i] = round(ta - other, 3)
                changed.append({"key": key, "index": i, "old": old, "new": values[key][i],
                                "why": "'Total equity and liabilities' had been read for it"})
                _note(labels, key, "corrected: 'Total equity and liabilities' had been read for it")
                break
    return changed


def try_candidates(normalised: dict, candidates: dict) -> list[str]:
    """For each failing check, try the figures printed on its lines (`candidates[statement][key]` =
    [(figure, printed wording)], from textlayer.candidates) in place of the figure read; one is kept
    only where it makes that check add up without breaking one that passed. A line the reading left
    out (an optional part) may be filled the same way. Changes `normalised` in place; returns what
    was changed."""
    fixed = []
    for st, block in normalised.items():
        cand = candidates.get(st) or {}
        if not cand:
            continue
        periods, values, labels = block["periods"], block["values"], block["labels"]
        n, step = len(periods), block.get("step", 1.0)
        spec = {name: (parts, total) for s, name, parts, total in CHECKS if s == st}
        for i, p in enumerate(periods):
            get = column(values, n, i)
            for _ in range(4):
                res = run(st, get, labels, step)
                progress = False
                for c in (c for c in res if c["status"] == "FAIL" and c["note"] != ZERO):
                    parts, total = spec[c["check"]]
                    for k in [total, *(k for _, k, _ in terms(parts))]:
                        if str(labels.get(k, "")).startswith(("computed", "formula")) or k not in cand:
                            continue
                        had, old = k in values, get(k)
                        for v, printed in cand[k]:
                            if old is not None and abs(v - old) <= tolerance([v, old], step):
                                continue
                            values.setdefault(k, [None] * n)[i] = v
                            if _better(res, run(st, get, labels, step), {c["check"]}):
                                where = p.get("label") or p["period_end"]
                                fixed.append(f"{k} ({where}) {'blank' if old is None else _fmt(old, 1)} -> {_fmt(v, 1)}, "
                                             f"as printed on \"{printed}\"")
                                if old is None and not had:
                                    labels[k] = f"{printed} (re-read from the PDF's text)"
                                else:
                                    _note(labels, k, "re-read from the PDF's text")
                                progress = True
                                break
                            values[k][i] = old
                        if not had and k in values and all(x is None for x in values[k]):
                            values.pop(k)
                        if progress:
                            break
                    if progress:
                        break
                if not progress:
                    break
    return fixed


def failures(normalised: dict) -> list[dict]:
    """Every failing check of an extraction, with its period."""
    out = []
    for st, block in normalised.items():
        n = len(block["periods"])
        for i, p in enumerate(block["periods"]):
            for c in run(st, column(block["values"], n, i), block["labels"], block.get("step", 1.0)):
                if c["status"] == "FAIL":
                    out.append({**c, "index": i, "period_end": p["period_end"], "months": p["months"],
                                "label": p.get("label", "")})
    return out


def describe(fails: list[dict], normalised: dict, factor: float = 1.0) -> str:
    """The failing checks as lines for the AI to look at again, in the filing's own unit."""
    lines = []
    for f in fails:
        vals = normalised[f["statement"]]["values"]
        show = lambda k: _fmt((vals.get(k) or [None])[f["index"]], factor)  # noqa: E731
        parts = " + ".join(f"{k} {show(k)}" for k in f["parts"])
        where = (f"- {f['statement']}, column \"{f['label'] or f['period_end']}\" ({f['period_end']}, "
                 f"{f['months']} months): {f['check']}")
        if f.get("note") == ZERO:
            lines.append(f"{where}: every figure is 0 ({parts}, {f['total']} {show(f['total'])}), which proves nothing. "
                         "Give these lines' printed figures; a line that isn't printed is left out, not written as 0.")
        else:
            cause = CAUSES.get(f["check"])
            lines.append(f"{where} fails: {parts} does not equal {f['total']} {show(f['total'])} "
                         f"(off by {_fmt(f['difference'], factor)})" + (f". Look first at: {cause}" if cause else ""))
    return "\n".join(lines)


def gross_profit_gaps(normalised: dict) -> list[int]:
    """Income statement columns with figures but no gross profit (printed or worked out)."""
    block = normalised.get("income_statement") or {}
    vals, n = block.get("values") or {}, len(block.get("periods") or [])
    gp = vals.get("gross_profit") or [None] * n
    return [i for i in range(n) if gp[i] is None
            and any((v or [None] * n)[i] is not None for k, v in vals.items() if not k.startswith("eps"))]


def describe_gaps(gaps: list[int], normalised: dict) -> str:
    """Gross profit gaps as a line for the AI to act on."""
    periods = normalised["income_statement"]["periods"]
    cols = ", ".join(f"\"{periods[i].get('label') or periods[i]['period_end']}\"" for i in gaps)
    return (f"- income_statement, column(s) {cols}: gross profit is blank. It must not be: give gross_profit if printed, "
            "else cost_of_sales, the gross expense: the cost of sales line if printed, otherwise use your judgement and "
            "add up the expense lines that are the cost of earning the revenue (for a bank, finance or insurance company "
            "e.g. financial cost associated with financial income + provision for credit losses + insurance service "
            "expenses + operating costs), negative, source labels joined with \" + \". The program then works out "
            "gross profit as revenue + cost_of_sales.")


def join_or(items: list[str]) -> str:
    """['a', 'b', 'c'] -> 'a, b or c'."""
    return " or ".join([", ".join(items[:-1]), items[-1]] if len(items) > 2 else items)


def has_figures(block: dict | None) -> bool:
    return any(v is not None for vals in ((block or {}).get("values") or {}).values() for v in vals)


def verdict(normalised: dict, presentation: str = "") -> dict:
    """Whether an extraction's figures can be relied on: {"ok", "pass", "fail", "reasons", "summary"
    (a few words for a card), "failing" (its failing checks)}.

    Good only when at least one tie-out passes, none fails, each statement has figures and gross
    profit isn't blank. Nothing passing counts as a failure too: figures that no printed total
    confirms (a presentation's highlights, a page read as zeros) are not a result."""
    results = []
    for st, block in normalised.items():
        periods = block.get("periods") or []
        for i, p in enumerate(periods):
            period = f"{p['months']}M to {p['period_end']}" if p.get("months") else p["period_end"]
            results += [{**c, "period": period} for c in
                        run(st, column(block.get("values") or {}, len(periods), i), block.get("labels"), block.get("step", 1.0))]
    passed = sum(c["status"] == "PASS" for c in results)
    failed = [c for c in results if c["status"] == "FAIL"]
    reasons, short = [], []  # in full, and in a few words for a card
    if failed:
        zero = sum(c["note"] == ZERO for c in failed)
        reasons.append(f"{len(failed)} tie-out check{'s fail' if len(failed) > 1 else ' fails'}"
                       + (f" ({zero} with every figure 0)" if zero else ""))
        short.append(f"{len(failed)} check{'s fail' if len(failed) > 1 else ' fails'}")
    if not passed:
        reasons.append("no tie-out check passes, so nothing confirms the figures")
        short.append("0 checks pass")
    missing = [st for st in STATEMENT_NAMES if not has_figures(normalised.get(st))]
    if missing:
        reasons.append(f"no {join_or([STATEMENT_NAMES[st] for st in missing])} found")
        short.append(f"no {join_or([STATEMENT_NAMES[st].replace(' statement', '') for st in missing])}")
    if "income_statement" not in missing and gross_profit_gaps(normalised):
        reasons.append("gross profit is blank")
        short.append("gross profit blank")
    if reasons and presentation:
        reasons.append(f"the PDF is a presentation ({presentation}), not the formal financial statements")
        short.append("slides only")
    failing = [{k: c[k] for k in ("statement", "period", "check", "difference", "note")} for c in failed[:12]]
    return {"ok": not reasons, "pass": passed, "fail": len(failed), "reasons": reasons, "summary": " · ".join(short),
            "failing": failing}


def _fmt(v, factor: float) -> str:
    if v is None:
        return "?"
    x = v / factor if factor else v
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def reconcile(first: dict, second: dict) -> list[str]:
    """Take a second reading's figures for the lines of each failing check of the first, where they
    make that check add up without breaking another. Periods are matched by end date and length.
    Changes `first` in place; returns what was fixed."""
    fixed = []
    for st, a in first.items():
        b = second.get(st) or {}
        n, nb = len(a["periods"]), len(b.get("periods") or [])
        where = {(p["period_end"], p["months"]): j for j, p in enumerate(b.get("periods") or [])}
        for i, p in enumerate(a["periods"]):
            j = where.get((p["period_end"], p["months"]))
            if j is None:
                continue
            get, get_b = column(a["values"], n, i), column(b["values"], nb, j)
            step = a.get("step", 1.0)
            for _ in range(4):
                res = run(st, get, a["labels"], step)
                before = _passing(res)
                progress = False
                for c in (c for c in res if c["status"] == "FAIL"):
                    keys = [k for k in (*c["parts"], c["total"]) if get_b(k) is not None and get_b(k) != get(k)]
                    if not keys and c["note"] == ZERO and all(get_b(k) is None for k in (*c["parts"], c["total"])):
                        keys = [*c["parts"], c["total"]]  # zeros the second reading leaves out: not printed
                    if not keys:
                        continue
                    old = {k: get(k) for k in keys}
                    for k in keys:
                        a["values"].setdefault(k, [None] * n)[i] = get_b(k)
                    res_after = run(st, get, a["labels"], step)
                    after = _passing(res_after)
                    cleared = c["note"] == ZERO and all(x["check"] != c["check"] or x["status"] != "FAIL" for x in res_after)
                    if (c["check"] in after or cleared) and before <= after:
                        fixed.append(f"{st} {p['period_end']} {c['check']}: " + ", ".join(
                            f"{k} {_fmt(old[k], 1)} -> {'left out (not printed)' if get(k) is None else _fmt(get(k), 1)}"
                            for k in keys))
                        progress = True
                        break
                    for k in keys:
                        a["values"][k][i] = old[k]
                if not progress:
                    break
    return fixed
