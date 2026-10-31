"""Output layout, modelled on the ShareInvestor.com "Financials" pages.

Each statement is a list of rows in display order. Edit labels or add/remove
rows here to match your ShareInvestor export exactly. The pipeline does not
hard-code any of these names anywhere else.

Row fields
  key    stable id the AI fills in (do not rename once you have saved workbooks)
  label  text written in column A of the Excel sheet
  kind   "header" (section title, no numbers), "item" (a reported line),
         or "total" (a reported subtotal, written in bold)
  hint   synonyms / guidance passed to the AI to map the company's own wording
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    key: str
    label: str
    kind: str = "item"
    hint: str = ""


INCOME_STATEMENT = [
    Row("revenue", "Revenue", "total", "Revenue, turnover, sales, total income (banks: total income)"),
    Row("cost_of_sales", "Cost of Goods Sold", "item", "Cost of sales, cost of revenue; negative. Leave out when the statement has no such line"),
    Row("raw_materials", "Raw Materials & Consumables Used", "item", "Expenses by nature: raw materials, consumables used and changes in inventories, as one figure; negative"),
    Row("employee_benefits", "Employee Benefits Expenses", "item", "Expenses by nature: employee benefits expense / staff costs on the face of the statement; negative"),
    Row("gross_profit", "Gross Profit", "total", "Gross profit as printed. Leave out when not printed"),
    Row("other_income", "Other Income", "item", "Other income, other operating income, other gains (net)"),
    Row("selling_expenses", "Distribution & Selling Expenses", "item", "Distribution, selling, marketing expenses; negative"),
    Row("admin_expenses", "Administrative Expenses", "item", "Administrative / general and administrative expenses; negative"),
    Row("other_expenses", "Other Operating Expenses", "item", "Other operating expenses, other losses, impairment losses on financial assets (not the raw materials, employee benefits or D&A lines captured separately); negative"),
    Row("operating_profit", "Operating Profit", "total", "Operating profit / results from operating activities / EBIT, if reported"),
    Row("finance_income", "Finance Income", "item", "Finance income, interest income (non-bank)"),
    Row("finance_costs", "Finance Costs", "item", "Finance costs, finance expenses, interest expense (non-bank); negative"),
    Row("share_associates", "Share of Results of Associates", "item", "Share of profit/(loss) of associates, net of tax"),
    Row("share_jv", "Share of Results of Joint Ventures", "item", "Share of profit/(loss) of joint ventures, net of tax"),
    Row("exceptional_items", "Exceptional Items", "item", "Exceptional / non-recurring items shown separately on the face of the statement"),
    Row("profit_before_tax", "Profit Before Tax", "total", "Profit/(loss) before tax / before income tax"),
    Row("tax", "Taxation", "item", "Income tax expense / tax credit; expense negative"),
    Row("profit_after_tax", "Profit After Tax", "total", "Profit/(loss) for the year/period from continuing + discontinued operations"),
    Row("discontinued_ops", "Profit from Discontinued Operations", "item", "Profit/(loss) from discontinued operations, net of tax"),
    Row("pat_shareholders", "Profit Attributable to Shareholders", "total", "Profit attributable to owners/equity holders of the Company"),
    Row("pat_minority", "Minority Interests", "item", "Profit attributable to non-controlling interests"),
    Row("perpetual_holders", "Attributable to Perpetual Securities Holders", "item", "Attributable to perpetual securities / capital securities holders, if any"),
    Row("eps_basic", "EPS - Basic (cents)", "item", "Basic earnings per share; convert to cents if stated in dollars"),
    Row("eps_diluted", "EPS - Diluted (cents)", "item", "Diluted earnings per share; convert to cents if stated in dollars"),
    Row("depreciation_amortisation", "Depreciation & Amortisation", "item", "Depreciation and amortisation: the income statement line(s) when shown, else the total from the cash flow statement adjustments; positive"),
]

BALANCE_SHEET = [
    Row("h_nca", "Non-Current Assets", "header"),
    Row("ppe", "Property, Plant & Equipment", "item", "PPE, including right-of-use assets if not shown separately"),
    Row("rou_assets", "Right-of-Use Assets", "item", "Right-of-use assets, if shown separately"),
    Row("investment_properties", "Investment Properties", "item"),
    Row("intangibles", "Intangible Assets & Goodwill", "item", "Intangible assets, goodwill"),
    Row("associates", "Associated Companies", "item", "Investments in associates"),
    Row("joint_ventures", "Joint Ventures", "item", "Investments in joint ventures"),
    Row("lt_investments", "Long-Term Investments", "item", "Financial assets at FVOCI/FVTPL/amortised cost (non-current), other investments"),
    Row("deferred_tax_assets", "Deferred Tax Assets", "item"),
    Row("other_nca", "Other Non-Current Assets", "item", "Other receivables, prepayments, derivatives, etc. (non-current)"),
    Row("total_nca", "Total Non-Current Assets", "total"),
    Row("h_ca", "Current Assets", "header"),
    Row("inventories", "Inventories", "item", "Inventories / stocks; include development properties if that is the main stock"),
    Row("trade_receivables", "Trade & Other Receivables", "item", "Trade and other receivables, contract assets"),
    Row("st_investments", "Short-Term Investments", "item", "Financial assets (current), short-term investments, fixed deposits > 3 months"),
    Row("cash", "Cash & Cash Equivalents", "item", "Cash and bank balances, cash and cash equivalents"),
    Row("assets_held_for_sale", "Assets Held for Sale", "item"),
    Row("other_ca", "Other Current Assets", "item", "Prepayments, derivatives, tax recoverable, other current assets"),
    Row("total_ca", "Total Current Assets", "total"),
    Row("total_assets", "Total Assets", "total"),
    Row("h_cl", "Current Liabilities", "header"),
    Row("trade_payables", "Trade & Other Payables", "item", "Trade and other payables, accruals, contract liabilities"),
    Row("st_borrowings", "Short-Term Borrowings", "item", "Loans and borrowings (current), bank overdrafts, current lease liabilities if bundled"),
    Row("st_lease_liabilities", "Lease Liabilities (Current)", "item", "Current lease liabilities, if shown separately"),
    Row("current_tax", "Current Tax Payable", "item", "Income tax payable / provision for taxation"),
    Row("other_cl", "Other Current Liabilities", "item", "Provisions, derivatives, liabilities held for sale, other current liabilities"),
    Row("total_cl", "Total Current Liabilities", "total"),
    Row("h_ncl", "Non-Current Liabilities", "header"),
    Row("lt_borrowings", "Long-Term Borrowings", "item", "Loans and borrowings (non-current), bonds, notes"),
    Row("lt_lease_liabilities", "Lease Liabilities (Non-Current)", "item", "Non-current lease liabilities, if shown separately"),
    Row("deferred_tax_liabilities", "Deferred Tax Liabilities", "item"),
    Row("other_ncl", "Other Non-Current Liabilities", "item", "Provisions, other payables, derivatives (non-current)"),
    Row("total_ncl", "Total Non-Current Liabilities", "total"),
    Row("total_liabilities", "Total Liabilities", "total"),
    Row("net_assets", "Net Assets", "total", "Total assets minus total liabilities"),
    Row("h_eq", "Shareholders' Equity", "header"),
    Row("share_capital", "Share Capital", "item"),
    Row("treasury_shares", "Treasury Shares", "item", "Negative"),
    Row("reserves", "Reserves", "item", "Retained earnings + all other reserves combined (if reported separately, sum them)"),
    Row("perpetual_securities", "Perpetual Securities", "item", "Perpetual / capital securities classified as equity"),
    Row("shareholders_equity", "Total Shareholders' Equity", "total", "Equity attributable to owners of the Company"),
    Row("minority_interests", "Minority Interests", "item", "Non-controlling interests"),
    Row("total_equity", "Total Equity", "total"),
]

CASH_FLOW = [
    Row("h_op", "Operating Activities", "header"),
    Row("cf_pbt", "Profit Before Tax", "item", "Starting line of the indirect method (profit before tax or profit for the period)"),
    Row("cf_dep_amort", "Depreciation & Amortisation", "item", "Sum of depreciation and amortisation add-backs"),
    Row("cf_other_adj", "Other Non-Cash Adjustments", "item", "All other adjustments (interest, impairments, gains/losses, share of associates...) combined"),
    Row("cf_op_before_wc", "Operating Cash Flow Before Working Capital Changes", "total"),
    Row("cf_wc_changes", "Working Capital Changes", "item", "Total of changes in inventories, receivables, payables, etc."),
    Row("cf_cash_from_ops", "Cash Generated from Operations", "total"),
    Row("cf_interest_paid_op", "Interest Paid", "item", "Only if classified in operating activities; negative"),
    Row("cf_interest_recv_op", "Interest Received", "item", "Only if classified in operating activities"),
    Row("cf_tax_paid", "Income Tax Paid", "item", "Negative"),
    Row("cf_net_operating", "Net Cash from Operating Activities", "total"),
    Row("h_inv", "Investing Activities", "header"),
    Row("cf_capex", "Purchase of Property, Plant & Equipment", "item", "Capex incl. investment properties and intangibles if combined; negative"),
    Row("cf_disposal_ppe", "Proceeds from Disposal of PPE", "item"),
    Row("cf_acquisitions", "Acquisition of Subsidiaries / Investments", "item", "Acquisition of subsidiaries net of cash, investments in associates/JVs/financial assets; negative"),
    Row("cf_disposal_inv", "Proceeds from Disposal of Investments", "item"),
    Row("cf_dividends_recv", "Dividends Received", "item"),
    Row("cf_interest_recv_inv", "Interest Received (Investing)", "item"),
    Row("cf_other_investing", "Other Investing Activities", "item"),
    Row("cf_net_investing", "Net Cash from Investing Activities", "total"),
    Row("h_fin", "Financing Activities", "header"),
    Row("cf_borrowings_proceeds", "Proceeds from Borrowings", "item"),
    Row("cf_borrowings_repaid", "Repayment of Borrowings", "item", "Negative"),
    Row("cf_lease_payments", "Payment of Lease Liabilities", "item", "Principal portion of lease payments; negative"),
    Row("cf_interest_paid_fin", "Interest Paid (Financing)", "item", "Negative"),
    Row("cf_dividends_paid", "Dividends Paid", "item", "To owners and non-controlling interests combined; negative"),
    Row("cf_share_issue", "Proceeds from Issue of Shares", "item"),
    Row("cf_share_buyback", "Purchase of Treasury Shares", "item", "Negative"),
    Row("cf_other_financing", "Other Financing Activities", "item"),
    Row("cf_net_financing", "Net Cash from Financing Activities", "total"),
    Row("h_net", "Net Change in Cash", "header"),
    Row("cf_net_change", "Net Increase/(Decrease) in Cash", "total"),
    Row("cf_cash_begin", "Cash at Beginning of Period", "item"),
    Row("cf_fx_effect", "Effect of Exchange Rate Changes", "item"),
    Row("cf_cash_end", "Cash at End of Period", "total"),
    Row("free_cash_flow", "Free Cash Flow", "item", "Leave null; computed by the program as net operating cash flow + capex"),
]

# Banks report interest income, deposits and loans rather than revenue, inventories and payables,
# so they get their own income statement and balance sheet. The cash flow layout is shared.
BANK_INCOME_STATEMENT = [
    Row("interest_income", "Interest Income", "item", "Interest earned / interest income on advances, investments and balances with banks; positive"),
    Row("interest_expense", "Interest Expense", "item", "Interest expended / interest expense on deposits and borrowings; negative"),
    Row("net_interest_income", "Net Interest Income", "total", "Net interest income as printed; leave out if not printed"),
    Row("fee_income", "Fee & Commission Income", "item", "Net fee and commission income"),
    Row("other_non_interest_income", "Other Non-Interest Income", "item", "Trading, investment, insurance and other income (a bank's 'Other income'), excluding fees if shown separately"),
    Row("total_operating_income", "Total Operating Income", "total", "Total operating income NET of interest expense, if printed. Not an Indian bank's 'Total Income', which includes gross interest earned"),
    Row("staff_costs", "Staff Costs", "item", "Employee costs / payments to and provisions for employees; negative"),
    Row("other_operating_expenses_bank", "Other Operating Expenses", "item", "Other operating expenses (premises, depreciation, other); negative"),
    Row("total_operating_expenses", "Total Operating Expenses", "total", "Operating expenses total; negative"),
    Row("operating_profit_pre_provisions", "Operating Profit before Allowances", "total", "Operating profit before provisions and contingencies"),
    Row("allowances", "Allowances / Provisions for Credit Losses", "item", "Provisions (other than tax) and contingencies, allowances for credit and other losses; negative"),
    Row("share_associates", "Share of Results of Associates", "item", "Share of profit/(loss) of associates and joint ventures"),
    Row("exceptional_items", "Exceptional Items", "item", "Exceptional / extraordinary items on the face of the statement"),
    Row("profit_before_tax", "Profit Before Tax", "total", "Profit/(loss) before tax"),
    Row("tax", "Taxation", "item", "Tax expense; negative"),
    Row("profit_after_tax", "Profit After Tax", "total", "Net profit for the period"),
    Row("pat_shareholders", "Profit Attributable to Shareholders", "total", "Attributable to owners / equity holders of the bank (Group)"),
    Row("pat_minority", "Minority Interests", "item", "Attributable to non-controlling / minority interest"),
    Row("eps_basic", "EPS - Basic (cents)", "item", "Basic EPS in the currency's minor unit (cents, paise: x100 if printed in dollars / rupees)"),
    Row("eps_diluted", "EPS - Diluted (cents)", "item", "Diluted EPS in the currency's minor unit"),
]

BANK_BALANCE_SHEET = [
    Row("h_assets", "Assets", "header"),
    Row("cash_central_bank", "Cash & Balances with Central Banks", "item", "Cash in hand and balances with the central bank (e.g. Reserve Bank of India, MAS)"),
    Row("due_from_banks", "Due from Banks / Placements", "item", "Balances with banks, money at call and short notice, placements with banks"),
    Row("bank_investments", "Investments & Securities", "item", "Investments, government and other securities"),
    Row("loans_advances", "Loans & Advances to Customers", "item", "Advances / loans and advances to customers, net of allowances"),
    Row("ppe", "Fixed Assets", "item", "Fixed assets / property and equipment"),
    Row("other_assets", "Other Assets", "item", "Other assets, derivatives, goodwill and intangibles"),
    Row("total_assets", "Total Assets", "total", "Total assets (an Indian bank's 'Total' of assets)"),
    Row("h_liabilities", "Liabilities", "header"),
    Row("customer_deposits", "Deposits from Customers", "item", "Deposits"),
    Row("due_to_banks", "Due to Banks", "item", "Deposits and balances of banks, if separate from customer deposits"),
    Row("bank_borrowings", "Borrowings & Debt Issued", "item", "Borrowings, debt securities, subordinated debt"),
    Row("other_liabilities", "Other Liabilities & Provisions", "item", "Other liabilities and provisions, derivatives"),
    Row("total_liabilities", "Total Liabilities", "total", "Printed total; leave out if not printed"),
    Row("h_equity", "Shareholders' Equity", "header"),
    Row("share_capital", "Share Capital", "item", "Capital"),
    Row("reserves", "Reserves", "item", "Reserves and surplus / retained earnings + other reserves"),
    Row("perpetual_securities", "Perpetual Securities", "item", "Additional tier 1 / perpetual capital securities in equity"),
    Row("shareholders_equity", "Total Shareholders' Equity", "total", "Equity attributable to owners; leave out if not printed"),
    Row("minority_interests", "Minority Interests", "item", "Non-controlling / minority interest"),
    Row("total_equity", "Total Equity", "total", "Total equity; leave out if not printed"),
]

FORMATS = {
    "general": {
        "income_statement": ("Income Statement", INCOME_STATEMENT),
        "balance_sheet": ("Balance Sheet", BALANCE_SHEET),
        "cash_flow": ("Cash Flow", CASH_FLOW),
    },
    "bank": {
        "income_statement": ("Income Statement", BANK_INCOME_STATEMENT),
        "balance_sheet": ("Balance Sheet", BANK_BALANCE_SHEET),
        "cash_flow": ("Cash Flow", CASH_FLOW),
    },
}
STATEMENTS = FORMATS["general"]  # statement ids and order are the same in every format

# Units used in the workbook. ShareInvestor shows figures in thousands of the
# reporting currency; EPS stays in cents whatever this is set to.
OUTPUT_UNIT = 1_000
OUTPUT_UNIT_LABEL = "'000"

# Column order of periods: True puts the most recent period in column B.
LATEST_FIRST = True


def data_rows(statement: str, fmt: str | None = None) -> list[Row]:
    """Rows that hold numbers; with no format, every format's rows (unique keys)."""
    formats = [fmt] if fmt else list(FORMATS)
    rows = {r.key: r for f in formats for r in FORMATS[f][statement][1] if r.kind != "header"}
    return list(rows.values())


def rows_for(statement: str, fmt: str | None) -> list[Row]:
    return FORMATS.get(fmt or "general", FORMATS["general"])[statement][1]


def labels() -> dict[str, str]:
    return {r.key: r.label for f in FORMATS.values() for _, rows in f.values() for r in rows}
