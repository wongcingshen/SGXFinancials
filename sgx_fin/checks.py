"""Arithmetic tie-outs of the statements (parts add up to their printed total).

Used three ways: to put right a sign the AI read backwards, to pick which lines to read again when
a total doesn't add up, and to report PASS / FAIL on the workbook's Checks sheet.

Each check is (statement, name, parts, total). A part starting with "-" is subtracted; one ending
in "?" may be missing (it then counts as nothing). A check runs when its total and every required
part have a figure; it is skipped entirely when none of its lines exist (a check for the other
format, bank vs general).
"""

from __future__ import annotations

CHECKS = [
    ("income_statement", "Revenue + COGS = Gross Profit", ["revenue", "cost_of_sales"], "gross_profit"),
    ("income_statement", "PBT + Tax = PAT (continuing)", ["profit_before_tax", "tax"], "profit_after_tax"),
    ("income_statement", "Shareholders + MI (+perps) = PAT (+ associates after tax)",
     ["pat_shareholders", "pat_minority?", "perpetual_holders?", "-share_associates_after_tax?"], "profit_after_tax"),
    ("balance_sheet", "NCA + CA = Total Assets", ["total_nca", "total_ca"], "total_assets"),
    ("balance_sheet", "CL + NCL = Total Liabilities", ["total_cl", "total_ncl"], "total_liabilities"),
    ("balance_sheet", "Total Liabilities + Total Equity = Total Assets", ["total_liabilities", "total_equity"], "total_assets"),
    ("balance_sheet", "Shareholders' Equity + MI = Total Equity", ["shareholders_equity", "minority_interests?"], "total_equity"),
    ("cash_flow", "Operating + Investing + Financing = Net Change",
     ["cf_net_operating", "cf_net_investing", "cf_net_financing"], "cf_net_change"),
    ("cash_flow", "Begin + Net Change + FX = End", ["cf_cash_begin", "cf_net_change", "cf_fx_effect?"], "cf_cash_end"),
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
    """[(sign, key, optional)] of a check's parts."""
    return [(-1 if p.startswith("-") else 1, p.lstrip("-").rstrip("?"), p.endswith("?")) for p in parts]


def tolerance(total: float, step: float = 1.0) -> float:
    """Rounding slack in output units: 0.2% of the total, at least two of the filing's own units."""
    return max(2.0, 2.0 * step, abs(total) * 0.002)


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
        s = diff = None
        if str(labels.get(total, "")).startswith(("computed", "formula")):
            status = "derived"  # the total was calculated, not printed
        elif get(total) is None or any(get(k) is None for _, k, opt in ts if not opt):
            status = "n/a"
        else:
            s = sum(sign * (get(k) or 0) for sign, k, _ in ts)
            diff = round(s - get(total), 3)
            status = "PASS" if abs(diff) <= tolerance(get(total), step) else "FAIL"
        out.append({"statement": st, "check": name, "status": status, "difference": diff,
                    "parts": [k for k in keys if get(k) is not None], "total": total})
    return out


def _passing(results: list[dict]) -> set[str]:
    return {c["check"] for c in results if c["status"] == "PASS"}


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
        lines.append(f"- {f['statement']}, column \"{f['label'] or f['period_end']}\" ({f['period_end']}, "
                     f"{f['months']} months): {f['check']} fails: {parts} does not equal {f['total']} {show(f['total'])} "
                     f"(off by {_fmt(f['difference'], factor)})")
    return "\n".join(lines)


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
                    if not keys:
                        continue
                    old = {k: get(k) for k in keys}
                    for k in keys:
                        a["values"].setdefault(k, [None] * n)[i] = get_b(k)
                    after = _passing(run(st, get, a["labels"], step))
                    if c["check"] in after and before <= after:
                        fixed.append(f"{st} {p['period_end']} {c['check']}: " + ", ".join(
                            f"{k} {_fmt(old[k], 1)} -> {_fmt(get(k), 1)}" for k in keys))
                        progress = True
                        break
                    for k in keys:
                        a["values"][k][i] = old[k]
                if not progress:
                    break
    return fixed
