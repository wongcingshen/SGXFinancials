"""Offline tests: page scanner on the sample PDF, normalisation, Excel writer + merge, URL screen."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook

from sgx_fin import extractor
from sgx_fin.excel_writer import workbook_bytes
from sgx_fin.page_scanner import scan_pdf
from sgx_fin.sgx_client import Screen

HERE = Path(__file__).parent

# What the model returns for tests/sample_results.pdf (shape enforced by extractor.SCHEMA).
FAKE = {
    "company_name": "ACME HOLDINGS LIMITED", "currency": "SGD", "unit": "thousands", "basis": "Group", "notes": "",
    "income_statement": {"page_numbers": [1], "periods": [
        {"label": "6M 30 Jun 2026", "period_end": "2026-06-30", "months": 6},
        {"label": "6M 30 Jun 2025", "period_end": "2025-06-30", "months": 6}], "rows": [
        {"key": "revenue", "source_label": "Revenue", "values": [120512, 107600]},
        {"key": "cost_of_sales", "source_label": "Cost of sales", "values": [-80412, -72100]},
        {"key": "gross_profit", "source_label": "Gross profit", "values": [40100, 35500]},
        {"key": "profit_before_tax", "source_label": "Profit before tax", "values": [19400, 16100]},
        {"key": "tax", "source_label": "Income tax expense", "values": [-3700, -3050]},
        {"key": "profit_after_tax", "source_label": "Profit for the period", "values": [15700, 13050]},
        {"key": "pat_shareholders", "source_label": "Owners of the Company", "values": [15200, 12700]},
        {"key": "pat_minority", "source_label": "Non-controlling interests", "values": [500, 350]},
        {"key": "eps_basic", "source_label": "Basic", "values": [3.04, 2.54]}]},
    "balance_sheet": {"page_numbers": [2, 3], "periods": [
        {"label": "30 Jun 2026", "period_end": "2026-06-30", "months": 0},
        {"label": "31 Dec 2025", "period_end": "2025-12-31", "months": 0}], "rows": [
        {"key": "total_nca", "source_label": "Total non-current assets", "values": [101300, 98450]},
        {"key": "total_ca", "source_label": "Total current assets", "values": [97700, 90550]},
        {"key": "total_assets", "source_label": "Total assets", "values": [199000, 189000]},
        {"key": "total_cl", "source_label": "Total current liabilities", "values": [44000, 41100]},
        {"key": "total_ncl", "source_label": "Total non-current liabilities", "values": [26000, 28400]},
        {"key": "total_liabilities", "source_label": "Total liabilities", "values": [70000, 69500]},
        {"key": "shareholders_equity", "source_label": "Equity attributable to owners", "values": [125000, 116000]},
        {"key": "minority_interests", "source_label": "Non-controlling interests", "values": [4000, 3500]},
        {"key": "total_equity", "source_label": "Total equity", "values": [129000, 119500]}]},
    "cash_flow": {"page_numbers": [4], "periods": [
        {"label": "6M 2026", "period_end": "2026-06-30", "months": 6},
        {"label": "6M 2025", "period_end": "2025-06-30", "months": 6}], "rows": [
        {"key": "cf_net_operating", "source_label": "Net cash from operating activities", "values": [22000, 18750]},
        {"key": "cf_capex", "source_label": "Purchase of PPE", "values": [-7900, -6500]},
        {"key": "cf_net_investing", "source_label": "Net cash used in investing", "values": [-7700, -6350]},
        {"key": "cf_net_financing", "source_label": "Net cash used in financing", "values": [-10500, -9100]},
        {"key": "cf_net_change", "source_label": "Net increase", "values": [3800, 3300]},
        {"key": "cf_cash_begin", "source_label": "Beginning", "values": [41950, 36000]},
        {"key": "cf_fx_effect", "source_label": "FX", "values": [-450, -200]},
        {"key": "cf_cash_end", "source_label": "End", "values": [45300, 39100]}]},
}


def test_scanner_picks_statement_pages():
    res = scan_pdf(HERE / "sample_results.pdf")
    assert res.selected == {"income_statement": [4], "balance_sheet": [5, 6], "cash_flow": [7]}


def test_screen_url_roundtrip():
    url = "https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17"
    s = Screen.from_url(url)
    assert s.page_url == url
    p = s.api_params(0)
    assert (p["periodstart"], p["periodend"], p["cat"], p["sub"]) == ("20261029_160000", "20261030_155959", "ANNC", "ANNC17")


def test_schema_is_json():
    json.dumps(extractor.SCHEMA)
    assert "revenue" in extractor.SCHEMA["properties"]["income_statement"]["properties"]["rows"]["items"]["properties"]["key"]["enum"]


def test_gemini_schema_converts():
    from google.genai import types

    sch = extractor.gemini_schema(extractor.SCHEMA)
    types.Schema.model_validate(sch)  # what response_schema accepts
    cells = sch["properties"]["balance_sheet"]["properties"]["rows"]["items"]["properties"]["cells"]
    assert cells["items"]["properties"]["col"] == {"type": "integer"}
    assert "bank" in sch["properties"]["format"]["enum"]
    assert "additionalProperties" not in json.dumps(sch)


def test_gross_profit_from_expenses_by_nature():
    by_nature = json.loads(json.dumps(FAKE))
    by_nature["unit"] = "units"
    by_nature["income_statement"]["rows"] = [
        {"key": "revenue", "source_label": "Revenue", "values": [1_000_000, 900_000]},
        {"key": "raw_materials", "source_label": "Raw materials, consumables used and changes in inventories", "values": [-400_000, -380_000]},
        {"key": "employee_benefits", "source_label": "Employee benefits expenses", "values": [-200_000, None]},
        {"key": "depreciation_amortisation", "source_label": "Depreciation and amortisation", "values": [50_000, 45_000]},
    ]
    norm = extractor.normalise(by_nature)
    inc = norm["income_statement"]
    assert inc["values"]["cost_of_sales"] == [-650, -425]  # '000; a missing part counts as nothing
    assert inc["values"]["gross_profit"] == [350, 475]
    assert inc["labels"]["cost_of_sales"].startswith("computed")
    _, checks = workbook_bytes(None, norm, {"company_name": "X"})
    gp = [c for c in checks if c["check"].startswith("Revenue + COGS")]
    assert {c["status"] for c in gp} == {"derived"}

    # A company that reports cost of sales keeps its own figures.
    norm = extractor.normalise(FAKE)
    assert norm["income_statement"]["values"]["cost_of_sales"] == [-80412, -72100]
    assert not norm["income_statement"]["labels"]["cost_of_sales"].startswith("computed")


def test_only_the_financial_statements_pdf_is_used():
    import shutil
    from pypdf import PdfWriter
    from sgx_fin.pipeline import choose_financial_pdf

    out = HERE / "out" / "attachments"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    blank = out / "blank.pdf"
    w = PdfWriter(); w.add_blank_page(612, 792); w.write(blank)
    sources = {"release": blank, "slides": HERE / "sample_results.pdf", "results": HERE / "sample_results.pdf"}

    class FakeClient:
        def __init__(self): self.got = []
        def download(self, url, folder):
            kind = url.rsplit("=", 1)[1]
            self.got.append(kind)
            dest = folder / {"release": "Media Release.pdf", "slides": "Results Presentation.pdf",
                             "results": "Half Yearly Results.pdf"}[kind]
            shutil.copy(sources[kind], dest)
            return dest

    links = [("https://links.sgx.com/FileOpen/a.ashx?k=release", "20201029 - NetLink NBN - Media Release.pdf"),
             ("https://links.sgx.com/FileOpen/b.ashx?k=slides", "Financial Results Presentation.pdf"),
             ("https://links.sgx.com/FileOpen/c.ashx?k=results", "20201029 - NetLink NBN - Half Yearly Results.pdf")]
    client = FakeClient()
    pdf, scan, used, downloaded = choose_financial_pdf(client, links, out)
    assert used.endswith("k=results") and pdf.name == "Half Yearly Results.pdf" and scan.found_all
    assert client.got == ["results"], "stops once the results PDF holds all three statements"

    # No financially named file: falls back to the content scan (presentation has the statements).
    client = FakeClient()
    _, _, used, _ = choose_financial_pdf(client, links[:2], out)
    assert used.endswith("k=slides") and client.got == ["release", "slides"]


def picture_pdf(src, dest):
    """A copy of `src` whose pages are photos: rendered images with no text layer."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(src))
    imgs = [doc[i].render(scale=1.5).to_pil().convert("RGB") for i in range(len(doc))]
    imgs[0].save(dest, save_all=True, append_images=imgs[1:], resolution=108)
    return dest


def test_picture_statements_go_to_the_ai_to_find():
    import shutil
    from sgx_fin.pipeline import choose_financial_pdf, default_pages

    out = HERE / "out" / "pictures"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    pic = picture_pdf(HERE / "sample_results.pdf", out / "photos.pdf")
    scan = scan_pdf(pic)
    assert scan.image_only_pages == list(range(1, 9)) and scan.needs_vision
    assert default_pages(scan) == list(range(1, 9))
    assert not scan_pdf(HERE / "sample_results.pdf").needs_vision  # text PDFs are unchanged

    class FakeClient:
        def __init__(self): self.got = []
        def download(self, url, folder):
            kind = url.rsplit("=", 1)[1]
            self.got.append(kind)
            dest = folder / {"release": "Media Release.pdf", "results": "Half Yearly Results.pdf", "odd": "SESGXFINAL.pdf"}[kind]
            shutil.copy(HERE / "sample_results.pdf" if kind == "release" else pic, dest)
            return dest

    links = [("https://links.sgx.com/FileOpen/a.ashx?k=release", "Media Release.pdf"),
             ("https://links.sgx.com/FileOpen/b.ashx?k=results", "Half Yearly Results.pdf")]
    client = FakeClient()
    pdf, found, used, _ = choose_financial_pdf(client, links, out)
    assert used.endswith("k=results") and found.needs_vision and client.got == ["results"]
    # Unclear name (like SESGXFINAL): still beats a release whose text mentions the statements.
    links = [("https://links.sgx.com/FileOpen/a.ashx?k=release", "Media Release.pdf"),
             ("https://links.sgx.com/FileOpen/c.ashx?k=odd", "SESGXFINAL")]
    _, _, used, _ = choose_financial_pdf(FakeClient(), links, out)
    assert used.endswith("k=odd")


def test_picture_filings_read_by_glm_on_nvidia():
    import os
    import shutil
    from sgx_fin import glm

    out = HERE / "out" / "glm"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    pic = picture_pdf(HERE / "sample_results.pdf", out / "photos.pdf")
    saved = {k: os.environ.pop(k, None) for k in ("GLM_FLASH", "GLM_MODEL", "GLM_MAX_IMAGES", "GLM_BASE_URL", "GLM_API_KEY",
                                                 "ZAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")}
    os.environ["GLM_FLASH"] = "nvapi-test"
    glm._listed.clear()
    answer = json.loads(json.dumps(FAKE))
    answer["income_statement"]["rows"][0]["values"] = ["120,512", "(1,000)"]  # strings, as GLM may send them
    for st, nums in (("income_statement", [1]), ("balance_sheet", [2, 3]), ("cash_flow", [4])):
        answer[st]["page_numbers"] = nums
    calls = []

    class Resp:
        def __init__(self, body, status=200): self.body, self.status_code, self.ok, self.text = body, status, status < 400, json.dumps(body)
        def json(self): return self.body

    def get(url, **kw):
        calls.append(("GET", url))
        return Resp({"data": [{"id": "meta/llama-3.1-8b-instruct"}, {"id": "z-ai/glm-5.3-flash"}]})

    def post(url, json=None, **kw):
        content = json["messages"][1]["content"]
        images = [c for c in content if c["type"] == "image_url"]
        calls.append(("POST", url, json["model"], len(images)))
        if "Which pages hold" in content[-1]["text"]:  # locating step for long filings
            return Resp({"choices": [{"message": {"content": '{"income_statement": [4], "balance_sheet": [5, 6], "cash_flow": [7]}'}}]})
        text = "<think>the {statements} are on pages...</think>```json\n" + __import__("json").dumps(answer) + "\n```"
        return Resp({"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9000, "completion_tokens": 900}})

    real_get, real_post = glm.requests.get, glm.requests.post
    glm.requests.get, glm.requests.post = get, post
    try:
        assert glm.is_nvidia() and glm.models()[0] == "z-ai/glm-5.3-flash"
        ex = extractor.extract(pic, [4, 5, 6, 7], cache_dir=out, vision=True)
        assert ex.model == "z-ai/glm-5.3-flash via NVIDIA"
        assert calls[-1] == ("POST", "https://integrate.api.nvidia.com/v1/chat/completions", "z-ai/glm-5.3-flash", 4)
        assert ex.data["income_statement"]["rows"][0]["values"] == [120512.0, -1000.0]
        assert ex.data["balance_sheet"]["page_numbers"] == [2, 3]  # positions in the pages sent

        # Long picture filing: skim previews to find the statements, then read only those pages.
        os.environ["GLM_MAX_IMAGES"] = "3"
        calls.clear()
        ex = extractor.extract(pic, list(range(1, 9)), cache_dir=out, vision=True)
        posts = [c for c in calls if c[0] == "POST"]
        assert sorted(c[3] for c in posts[:2]) == [2, 6] and posts[2][3] == 3, posts  # 2 skims in parallel, then 3 read
        assert ex.data["income_statement"]["page_numbers"] == [4]  # page 4 of the 8 sent

        # GLM failing: Gemini takes over (no Gemini key here, so the error lists both).
        glm.requests.post = lambda url, **kw: Resp({"error": "busy"}, 503)
        try:
            extractor.extract(pic, [1, 2], cache_dir=out, vision=True)
            raise AssertionError("expected failure")
        except RuntimeError as e:
            assert "GLM 5.3 Flash" in str(e)
    finally:
        glm.requests.get, glm.requests.post = real_get, real_post
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        glm._listed.clear()


def test_ai_finds_the_statements_when_keywords_are_missing():
    from pathlib import Path as P
    from sgx_fin.page_scanner import PageScore, ScanResult, score_page
    from sgx_fin.pipeline import default_pages

    def page(n, ratio, text=True):
        return PageScore(n, {"income_statement": 0, "balance_sheet": 0, "cash_flow": 0}, ratio, text)

    # 30 pages; only the balance sheet's words were found (p5). Pages 10-29 are number-heavy tables.
    pages = [page(n, 0.4 if 10 <= n <= 29 else 0.05) for n in range(1, 31)]
    scan = ScanResult(P("x.pdf"), 30, pages, {"income_statement": [], "balance_sheet": [5], "cash_flow": []}, [])
    assert scan.needs_ai_locate and not scan.needs_vision and scan.missing == ["income_statement", "cash_flow"]
    chosen = default_pages(scan)
    assert chosen == [5] + list(range(10, 30))  # found page + number-heavy pages; narrative pages left out
    short = ScanResult(P("y.pdf"), 6, pages[:6], {"income_statement": [], "balance_sheet": [], "cash_flow": []}, [])
    assert default_pages(short) == [1, 2, 3, 4, 5, 6]  # a short document goes whole
    assert "find the income statement" in extractor._instruction(P("y.pdf"), [1, 2], locate=True)

    # Bank / Indian-format titles are recognised by the keyword scan.
    assert score_page("STATEMENT OF STANDALONE AUDITED FINANCIAL RESULTS\nInterest earned 1,000 900\n"
                      "Interest expended 600 500\nNet Profit 300 250", 1).title_hits.get("income_statement")
    assert score_page("Statement of Assets and Liabilities\nDeposits 9,000\nAdvances 7,000", 1).title_hits.get("balance_sheet")


def test_bank_in_crores_with_explicit_columns():
    from sgx_fin import checks as checks_mod, glm

    cols = [(1, "Quarter ended 30.09.2026", "30.09.2026", 3), (2, "Quarter ended 30.06.2026", "30.06.2026", 3),
            (3, "Quarter ended 30.09.2025", "30.09.2025", 3), (4, "Half year ended 30.09.2026", "30.09.2026", 6),
            (5, "Half year ended 30.09.2025", "30.09.2025", 6), (6, "Year ended 31.03.2026", "31.03.2026", 12)]
    periods = [{"col": c, "label": l, "period_end": d, "months": m} for c, l, d, m in cols]
    cells = lambda *v: [{"col": i, "value": x} for i, x in enumerate(v, 1) if x is not None]  # noqa: E731
    bank = {
        "company_name": "BANK OF BARODA", "format": "bank", "currency": "INR", "unit": "crores", "basis": "Group", "notes": "",
        "income_statement": {"page_numbers": [1], "periods": periods, "rows": [
            {"key": "interest_income", "source_label": "Interest earned", "cells": cells(31000, 30000, 28000, 61000, 55000, 112000)},
            {"key": "interest_expense", "source_label": "Interest expended", "cells": cells(-19000, -18500, -17000, -37500, -33000, -68000)},
            # a blank cell in column 2: the rest must stay in their own columns
            {"key": "other_non_interest_income", "source_label": "Other income", "cells": cells(4000, None, 3500, 7600, 6800, 14000)},
            {"key": "profit_before_tax", "source_label": "Profit before tax", "cells": cells(6000, 5800, 5200, 11800, 10100, 21000)},
        ]},
        "balance_sheet": {"page_numbers": [2], "periods": [
            {"col": 1, "label": "As at 30.09.2026", "period_end": "30.09.2026", "months": 6},  # months must become 0
            {"col": 2, "label": "As at 31.03.2026", "period_end": "31.03.2026", "months": 0}], "rows": [
            {"key": "customer_deposits", "source_label": "Deposits", "cells": cells(1300000, 1250000)},
            {"key": "share_capital", "source_label": "Capital", "cells": cells(1034, 1034)},
            {"key": "reserves", "source_label": "Reserves and surplus", "cells": cells(120000, 115000)},
            {"key": "total_assets", "source_label": "Total", "cells": cells(1600000, 1550000)},
        ]},
        "cash_flow": {"page_numbers": [], "periods": [], "rows": []},
    }
    norm = extractor.normalise(bank)
    inc, bs = norm["income_statement"], norm["balance_sheet"]
    assert inc["values"]["interest_income"][0] == 310_000_000  # 31,000 crore = 310,000,000 thousand
    assert inc["values"]["other_non_interest_income"] == [40_000_000, None, 35_000_000, 76_000_000, 68_000_000, 140_000_000]
    assert inc["values"]["net_interest_income"][0] == 120_000_000 and inc["labels"]["net_interest_income"].startswith("computed")
    assert [p["period_end"] for p in inc["periods"]][:2] == ["2026-09-30", "2026-06-30"]
    assert [p["months"] for p in bs["periods"]] == [0, 0]
    assert bs["values"]["total_liabilities"][0] == 16_000_000_000 - 1_210_340_000

    data, checks = workbook_bytes(None, norm, {"company_name": "BANK OF BARODA", "currency": "INR", "format": "bank"})
    wb = load_workbook(io.BytesIO(data))
    col_a = [c.value for c in wb["Income Statement"]["A"] if c.value]
    assert "Interest Income" in col_a and "Gross Profit" in col_a and "Cost of Goods Sold" not in col_a
    names = {c["check"] for c in checks}
    assert any(n.startswith("Interest Income") for n in names)
    # Gross income is added up from the bank's lines; with no operating expenses read there is no gross
    # expense, so gross profit stays blank and the verdict says so.
    assert inc["values"]["revenue"][0] == 350_000_000 and inc["labels"]["revenue"].startswith("computed")
    assert "gross profit is blank" in checks_mod.verdict(norm)["reasons"]

    # A unit that isn't stated is taken as thousands.
    assert glm.coerce({"unit": ""})["unit"] == "thousands"
    no_unit = json.loads(json.dumps(FAKE)); no_unit.pop("unit")
    assert extractor.normalise(no_unit)["income_statement"]["values"]["revenue"][0] == 120512


# Bank of Baroda's consolidated results (₹ lakhs) as a first reading got them wrong: the gross "Total
# Income" as total operating income, an exceptional charge and the minority share with the wrong sign,
# and one digit misread in the cash flow (16,082,312 read as 18,082,312).
BOB_COLS = [(1, "Quarter ended 30.09.2026", "30.09.2026", 3), (2, "Quarter ended 30.06.2026", "30.06.2026", 3)]


def bob_reading(cash_begin=18082312):
    cells = lambda *v: [{"col": i, "value": x} for i, x in enumerate(v, 1) if x is not None]  # noqa: E731
    row = lambda k, label, *v: {"key": k, "source_label": label, "cells": cells(*v)}  # noqa: E731
    return {
        "company_name": "BANK OF BARODA", "format": "bank", "currency": "INR", "unit": "lakhs", "basis": "Group", "notes": "",
        "income_statement": {"page_numbers": [1], "periods": [
            {"col": c, "label": l, "period_end": d, "months": m} for c, l, d, m in BOB_COLS], "rows": [
            row("interest_income", "Interest earned", 3702059, 3600000),
            row("other_non_interest_income", "Other Income", 876312, 800000),
            row("total_operating_income", "Total Income (1+2)", 4578371, 4400000),
            row("interest_expense", "Interest Expended", -2266048, -2200000),
            row("staff_costs", "Employees cost", -496808, -480000),
            row("other_operating_expenses_bank", "Other operating expenses", -548098, -520000),
            row("total_operating_expenses", "Operating Expenses (i)+(ii)", -1044906, -1000000),
            row("operating_profit_pre_provisions", "Operating Profit before Provisions and Contingencies", 1267417, 1200000),
            row("allowances", "Provisions (other than tax) and Contingencies", -340689, -300000),
            row("exceptional_items", "Exceptional items", None, 568023),
            row("profit_before_tax", "Profit from Ordinary Activities before tax (7-8-9)", 926728, 331977),
            row("tax", "Tax expense", -262823, -90000),
            row("profit_after_tax", "Net Profit for the period", 663905, 241977),
            row("share_associates_after_tax", "Share in Profit of Associates", 15332, 14000),
            row("pat_minority", "Minority Interest", -5509, -5000),
            row("pat_shareholders", "Consolidated Net Profit attributable to the Group", 673728, 250977),
            row("eps_basic", "Basic EPS (Rs)", 1303, 485),
        ]},
        "balance_sheet": {"page_numbers": [2], "periods": [{"col": 1, "label": "30.09.2026", "period_end": "30.09.2026", "months": 0}], "rows": [
            row("cash_central_bank", "Cash and Balances with Reserve Bank of India", 5631446),
            row("due_from_banks", "Balances with Banks and Money at Call and Short Notice", 7747689),
            row("bank_investments", "Investments", 50221440),
            row("loans_advances", "Advances", 152454715),
            row("ppe", "Fixed Assets", 1201365),
            row("other_assets", "Other Assets + Goodwill on Consolidation", 5890068),
            row("total_assets", "Total", 223146723),
            row("customer_deposits", "Deposits", 177509644),
            row("bank_borrowings", "Borrowings", 18347136),
            row("other_liabilities", "Other Liabilities and Provisions", 9652886),
            row("share_capital", "Capital", 103553),
            row("reserves", "Reserves and Surplus", 17374569),
            row("minority_interests", "Minority Interest", 158935),
        ]},
        "cash_flow": {"page_numbers": [2], "periods": [{"col": 1, "label": "Half year ended 30.09.2026", "period_end": "30.09.2026", "months": 6}], "rows": [
            row("cf_pbt", "Net Profit before taxes", 1178175),
            row("cf_net_operating", "Net cash flow from operating activities", -3048906),
            row("cf_net_investing", "Net cash used in investing activities", -130000),
            row("cf_borrowings_proceeds", "Proceeds from issue of bonds", 1012899),
            row("cf_interest_paid_fin", "Interest paid on bonds", -107055),
            row("cf_dividends_paid", "Dividend paid", -439566),
            row("cf_net_financing", "Net cash flow from financing activities", 475729),
            row("cf_net_change", "Net increase/(decrease) in cash", -2703177),
            row("cf_cash_begin", "Cash and cash equivalents at the beginning", cash_begin),
            row("cf_cash_end", "Cash and cash equivalents at the end", 13379135),
        ]},
    }


def test_indian_bank_layout_signs_and_checks():
    from sgx_fin import checks

    norm = extractor.normalise(bob_reading())
    inc, bs = norm["income_statement"]["values"], norm["balance_sheet"]["values"]
    # 'Total Income' includes gross interest earned: the net figure replaces it (₹ lakhs -> '000 is x100).
    assert inc["total_operating_income"] == [231_232_300, 220_000_000]
    assert norm["income_statement"]["labels"]["total_operating_income"].startswith("computed")
    assert inc["exceptional_items"] == [None, -56_802_300]  # a charge, as PBT (7-8-9) shows
    assert inc["pat_minority"] == [550_900, 500_000]  # 'Less: minority interest' is the minority's share
    assert "sign corrected" in norm["income_statement"]["labels"]["exceptional_items"]
    assert inc["net_interest_income"] == [143_601_100, 140_000_000]
    assert bs["total_equity"] == [1_763_705_700] and bs["total_liabilities"] == [20_550_966_600]
    fails = checks.failures(norm)
    assert [(f["statement"], f["check"]) for f in fails] == [("cash_flow", "Begin + Net Change + FX = End")], fails
    text = checks.describe(fails, norm, factor=100)
    assert "cf_cash_begin 18,082,312" in text and "off by 2,000,000" in text  # in the filing's own unit

    # A second reading fixes the digit; only lines of failing checks are taken from it.
    second = extractor.normalise(bob_reading(cash_begin=16082312))
    second["income_statement"]["values"]["interest_income"][0] = 1  # a worse figure elsewhere is not taken
    fixed = checks.reconcile(norm, second)
    assert norm["cash_flow"]["values"]["cf_cash_begin"] == [1_608_231_200] and len(fixed) == 1, fixed
    assert norm["income_statement"]["values"]["interest_income"][0] == 370_205_900
    assert not checks.failures(norm)

    data, results = workbook_bytes(None, norm, {"company_name": "BANK OF BARODA", "currency": "INR", "format": "bank"})
    statuses = {(c["period"], c["check"]): c["status"] for c in results}
    assert all(s in ("PASS", "derived", "n/a") for s in statuses.values()), [k for k, s in statuses.items() if s == "FAIL"]
    for name in ("PBT + Tax = PAT (continuing)", "Shareholders + MI (+perps) = PAT (+ associates after tax)",
                 "Operating Profit before Allowances + Allowances (+ associates, exceptional) = PBT",
                 "Staff Costs + Other Operating Expenses = Total Operating Expenses"):
        assert statuses[("3M Sep 2026", name)] == "PASS", name
    assert statuses[("30 Sep 2026", "Bank assets add up to Total Assets")] == "PASS"
    assert statuses[("30 Sep 2026", "Deposits + Borrowings + Other Liabilities + Equity = Total Assets")] == "PASS"
    col_a = [c.value for c in load_workbook(io.BytesIO(data))["Income Statement"]["A"] if c.value]
    assert col_a.index("Share of Results of Associates (after tax)") == col_a.index("Profit After Tax") + 1


# Page 14 of Bank of Baroda's filing (consolidated results, ₹ lakhs), as printed.
BOB_P14 = [
    ("1", "Interest earned (a)+(b)+(c)+(d)", "3702059 3511452 3331824 7213511 6618455 13429812"),
    ("(a)", "Interest /discount on advances / bills", "2765708 2639979 2435325 5405687 4882288 9939628"),
    ("(b)", "Income on investments", "829154 755227 728440 1584381 1450704 2877332"),
    ("(c)", "Interest on balances with Reserve Bank of India and other inter bank funds", "72195 70660 75680 142855 141611 275183"),
    ("(d)", "Others", "35002 45586 92379 80588 143852 337669"),
    ("2", "Other Income", "876312 620219 511586 1496531 1065371 2252732"),
    ("3", "Total Income (1 + 2)", "4578371 4131671 3843410 8710042 7683826 15682544"),
    ("4", "Interest Expended", "2266048 2130113 2019135 4396161 4049789 8185993"),
    ("5", "Operating Expenses (a)+ (b)", "1044906 943164 974997 1988070 1926196 3871744"),
    ("(a)", "Employees cost", "496808 461134 441150 957942 908336 1720852"),
    ("(b)", "Other operating expenses", "548098 482030 533847 1030128 1017860 2150892"),
    ("6", "Total Expenditure (4+5) excluding provisions and contingencies", "3310954 3073277 2994132 6384231 5975985 12057737"),
    ("7", "Operating Profit (3-6) before Provisions and Contingencies", "1267417 1058394 849278 2325811 1707841 3624807"),
    ("8", "Provisions (other than tax) and Contingencies", "340689 269817 179781 610506 521378 1130338"),
    ("9", "Exceptional Items [refer note 13]", "- 568023 - 568023 - -"),
    ("10", "Profit (+) / Loss (-) from Ordinary Activities before tax (7-8-9)", "926728 220554 669497 1147282 1186463 2494469"),
    ("11", "Provision for Taxes", "262823 63295 162467 326118 344245 546753"),
    ("12", "Net Profit (+) / Loss (-) from Ordinary Activities after tax (10-11)", "663905 157259 507030 821164 842218 1947716"),
    ("13", "Less: Minority Interest", "5509 5597 4707 11106 9509 22384"),
    ("14", "Add: Share of earnings in Associates", "15332 26667 11033 41999 27563 59310"),
    ("15", "Extraordinary items (net of tax expenses)", "- - - - - -"),
    ("16", "Net Profit (+) / Loss (-) for the period (12-13+14+15)", "673728 178329 513356 852057 860272 1984642"),
    ("17", "Paid-up equity share capital (Face Value of ₹ 2 each)", "103553 103553 103553 103553 103553 103553"),
    ("(a)", "Basic EPS before and after Extraordinary items (not annualized) [in ₹]", "13.03 3.45 9.93 16.48 16.64 38.38"),
]
BOB_P14_COLS = [("Quarter Ended | 30.09.2026 | Un-Audited", "2026-09-30", 3), ("Quarter Ended | 30.06.2026 | Un-Audited", "2026-06-30", 3),
                ("Quarter Ended | 30.09.2025 | Un-Audited", "2025-09-30", 3), ("Half Year Ended | 30.09.2026 | Un-Audited", "2026-09-30", 6),
                ("Half Year Ended | 30.09.2025 | Un-Audited", "2025-09-30", 6), ("Year Ended | 31.03.2026 | Audited", "2026-03-31", 12)]


def bob_tables(misread=True):
    """What a transcription returns for page 14; misread: row 5 slips to row (b) above it in two columns,
    the way the first extraction did (H1 1,584,381 and FY 2,877,332 are 'Income on investments')."""
    rows = []
    for ref, label, nums in BOB_P14:
        texts = nums.split()
        if misread and label.startswith("Operating Expenses"):
            texts[3], texts[5] = "1584381", "2877332"
        rows.append({"ref": ref, "label": label, "cells": [{"col": i, "text": t} for i, t in enumerate(texts, 1)]})
    return {"tables": [{
        "page": 5, "title": "Un-Audited Consolidated Financial Results for the Quarter / Half year Ended 30th September 2026",
        "unit": "₹ in lakhs", "basis": "consolidated",
        "columns": [{"col": i, "heading": h, "period_end": d, "months": m} for i, (h, d, m) in enumerate(BOB_P14_COLS, 1)],
        "rows": rows}]}


def test_picture_tables_checked_against_themselves():
    from sgx_fin import pictures

    good = pictures.clean_tables(bob_tables(misread=False), [5])
    assert pictures.verify(good) == []  # labels' own formulas and quarter + quarter = half year all hold
    bad = pictures.clean_tables(bob_tables(), [5])
    problems = pictures.verify(bad)
    row5 = next(i for i, r in enumerate(bad[0]["rows"]) if r["label"].startswith("Operating Expenses"))
    why = " / ".join(p["why"] for p in problems)
    assert all(row5 in p["rows"] for p in problems), why
    assert "Operating Expenses (a)+ (b): the parts give 3,871,744.00 but column 6 shows 2877332" in why
    assert "Total Expenditure (4+5)" in why and "6M to 2026-09-30 = 3M to 2026-09-30 + 3M to 2026-06-30" in why
    assert not any("capital" in p["why"] for p in problems)  # a constant line isn't additive

    # The re-read is taken only where it makes the rows add up.
    wrong = {"rows": [{"table": 0, "row": row5 - 1, "cells": [{"col": 1, "text": "9"}]}]}  # a worse reading elsewhere
    fixed = {"rows": [{"table": 0, "row": row5, "cells": [{"col": i, "text": t} for i, t in
                                                         enumerate("1044906 943164 974997 1988070 1926196 3871744".split(), 1)]}]}
    corrections, left = pictures._apply_reread(bad, {"rows": wrong["rows"] + fixed["rows"]})
    assert left == [] and [(c["col"], c["was"], c["now"]) for c in corrections] == [(4, "1584381", "1988070"), (6, "2877332", "3871744")]
    assert bad[0]["rows"][row5 - 1]["cells"][1] == "2266048"  # Interest Expended kept as first read

    text = pictures.as_text(bad)
    assert "[1] Quarter Ended | 30.09.2026 | Un-Audited (period end 2026-09-30, 3 months)" in text
    assert "5 Operating Expenses (a)+ (b) | [1] 1044906 | [2] 943164" in text and "9 Exceptional Items [refer note 13] | [1] - | [2] 568023" in text

    # Page 16: assets & liabilities and the cash flow have no formulas in their labels; their TOTAL and
    # Sub total lines, the (A)+(B)+(C) line and the cash roll-forward are checked instead.
    p16 = json.loads((HERE / "bob_p16.json").read_text())
    assert pictures.verify(pictures.clean_tables(p16, [16])) == []
    for table, row, col, misread, expect in [
            (1, 7, 0, "662638", "Sub total"),  # Provision for Other items 862,638: a 6 for an 8
            (0, 4, 1, "152620392", "TOTAL"),  # Deposits 152,520,392
            (1, 33, 0, "18082312", "cash at the beginning"),  # the misread the first extraction made
            (1, 24, 0, "(139000)", "Net cash used in investing activities (B)"),  # "(130000)": a total read wrong
            (1, 13, 2, "(20952445)", "Net cash from operating activities (A)")]:  # an advances figure, FY column
        bad = json.loads(json.dumps(p16))
        bad["tables"][table]["rows"][row]["cells"][col]["text"] = misread
        found = pictures.verify(pictures.clean_tables(bad, [16]))
        assert found and all(row in f["rows"] for f in found), (misread, found)
        assert any(expect in f["why"] for f in found), (misread, [f["why"] for f in found])

    # Standalone and consolidated side by side repeat their periods: each half is checked on its own.
    side = pictures.clean_tables(bob_tables(misread=False), [5])
    t = side[0]
    t["columns"] += [{**c, "col": c["col"] + 6} for c in t["columns"]]
    for r in t["rows"]:
        r["cells"].update({c + 6: v for c, v in r["cells"].items()})
    assert pictures.verify(side) == []


def test_pictures_are_straightened_before_reading():
    from PIL import Image
    from sgx_fin import pictures
    import pypdfium2 as pdfium

    out = HERE / "out" / "straighten"
    out.mkdir(parents=True, exist_ok=True)
    page = pdfium.PdfDocument(str(HERE / "sample_results.pdf"))[3].render(scale=2).to_pil().convert("L")
    assert pictures.skew_angle(page) == 0.0
    tilted = page.rotate(-2.0, resample=Image.BICUBIC, expand=True, fillcolor=255)  # 2° clockwise, as a careless scan
    assert abs(pictures.skew_angle(tilted) - 2.0) <= 0.3
    sideways = page.rotate(90, expand=True, fillcolor=255)  # a landscape table photographed sideways
    sideways.convert("RGB").save(out / "sideways.pdf", resolution=144)
    assert pictures.line_ratio(page) > 2 and pictures.line_ratio(sideways) < 0.5
    # Turned as the previews said; and turned even when nobody said, because the lines run up and down.
    for said in (90, 0):
        upright = pictures.prepare(out / "sideways.pdf", 1, turn=said)
        assert upright.info["turn"] == 90 and upright.mode == "L" and pictures.line_ratio(upright) > 1
        assert abs(pictures.skew_angle(upright)) <= 0.3
    # An upright page the previews wrongly called sideways stays as it is.
    page.convert("RGB").save(out / "upright.pdf", resolution=144)
    assert pictures.prepare(out / "upright.pdf", 1, turn=270).info["turn"] == 0
    top, bottom = pictures.halves(upright)
    assert top.height + bottom.height > upright.height  # the halves overlap


def test_scans_with_a_hidden_text_layer_are_read_as_pictures():
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    out = HERE / "out" / "ocr"
    out.mkdir(parents=True, exist_ok=True)
    pic = picture_pdf(HERE / "sample_results.pdf", out / "photos.pdf")
    writer = PdfWriter(clone_from=PdfReader(str(pic)))
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for i, page in enumerate(writer.pages):
        words = ["Condensed interim consolidated statement of cash flows", "Cash flows from operating activities",
                 "Net cash from operating activities 22,000 18,750", "Net cash used in investing activities",
                 "Cash and cash equivalents at end of the period"] if i == 5 else ["Notes and commentary"]
        lines = " ".join(f"({w}) Tj 0 -14 Td" for w in words)
        stream = DecodedStreamObject()
        stream.set_data(f"BT 3 Tr /OCR 10 Tf 40 700 Td {lines} ET".encode())  # 3 Tr: invisible, as OCR tools write it
        page[NameObject("/Resources")].setdefault(NameObject("/Font"), DictionaryObject())[NameObject("/OCR")] = writer._add_object(font)
        contents = page.get_contents()
        contents.set_data(contents.get_data() + b"\n" + stream.get_data())
        page.replace_contents(contents)
    writer.write(out / "searchable.pdf")
    scan = scan_pdf(out / "searchable.pdf")
    # Page 6 has a full OCR layer: a scan. The others' layers are a few words: pictures, as before.
    assert scan.scanned_pages == [6] and scan.image_only_pages == [1, 2, 3, 4, 5, 7, 8], (scan.image_only_pages, scan.scanned_pages)
    assert scan.selected["cash_flow"] == [6] and 6 in scan.picture_pages  # keywords from the OCR layer, figures from the picture
    assert scan_pdf(HERE / "sample_results.pdf").scanned_pages == []  # a text PDF is not a scan


def test_picture_filing_end_to_end():
    import os
    import shutil
    from sgx_fin import pictures, pipeline

    out = HERE / "out" / "located"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(HERE / "sample_results.pdf"))
    imgs = [doc[i].render(scale=1.5).to_pil().convert("RGB") for i in range(len(doc))]
    imgs[5] = imgs[5].rotate(90, expand=True)  # page 6 photographed sideways
    pic = out / "SESGXFINAL.pdf"
    imgs[0].save(pic, save_all=True, append_images=imgs[1:], resolution=108)
    scan = scan_pdf(pic)
    saved = {k: os.environ.pop(k, None) for k in ("GLM_FLASH", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "AI_PROVIDER",
                                                 "SGX_GEMINI_MODEL", "SGX_VISION_MODEL")}
    os.environ["GEMINI_API_KEY"] = "test"
    calls = []

    def fake_gemini(pdf_bytes, instruction, model, effort, client, vision=False, deadline=None, system="",
                    schema_json=None, images=None, candidates=None, resolution=None, part_resolution=None):
        step = {id(pictures.LOCATE_SCHEMA): "locate", id(pictures.TABLES_SCHEMA): "copy",
                id(pictures.REREAD_SCHEMA): "reread"}.get(id(schema_json), "template")
        calls.append({"step": step, "model": model, "labels": [l for l, _ in images or []], "candidates": candidates,
                      "resolution": resolution, "part": part_resolution, "instruction": instruction, "pdf": pdf_bytes,
                      "sizes": [Image.open(io.BytesIO(j)).size for _, j in images or []]})
        if step == "locate":  # standalone statements on 2-3, consolidated on 5-7, page 6 photographed sideways
            pages = [{"page": 2, "heading": "Standalone Financial Results", "statements": ["income_statement"], "basis": "standalone", "turn": "0"},
                     {"page": 3, "heading": "Standalone Assets and Liabilities", "statements": ["balance_sheet", "cash_flow"], "basis": "standalone", "turn": "0"},
                     {"page": 5, "heading": "Consolidated Financial Results", "statements": ["income_statement"], "basis": "consolidated", "turn": "0"},
                     {"page": 6, "heading": "Consolidated Statement of Assets and Liabilities", "statements": ["balance_sheet"], "basis": "consolidated", "turn": "90"},
                     {"page": 7, "heading": "", "statements": ["balance_sheet", "cash_flow"], "basis": "unknown", "turn": "0"},
                     {"page": 99, "heading": "made up", "statements": ["cash_flow"], "basis": "consolidated", "turn": "0"}]
            return extractor.Extraction({"pages": pages}, model, {})
        if step == "copy":
            return extractor.Extraction(bob_tables(), model, {"input_tokens": 5000, "output_tokens": 3000})
        if step == "reread":
            row5 = next(i for i, r in enumerate(BOB_P14) if r[1].startswith("Operating Expenses"))
            return extractor.Extraction({"rows": [{"table": 0, "row": row5, "cells": [
                {"col": i, "text": t} for i, t in enumerate(BOB_P14[row5][2].split(), 1)]}]}, model, {"input_tokens": 4000, "output_tokens": 200})
        return extractor.Extraction(bob_reading(16082312 if "did not add up" in instruction else 18082312), model,
                                    {"input_tokens": 1000, "output_tokens": 100})

    from PIL import Image
    real = extractor._gemini
    extractor._gemini = fake_gemini
    try:
        res = pipeline.process_pdf(pic, scan=scan, cache_dir=out)
        assert [c["step"] for c in calls] == ["locate", "copy", "reread", "template", "template"], [c["step"] for c in calls]
        locate, copy, reread, first, second = calls
        assert locate["labels"] == [f"Page {p}:" for p in range(1, 9)] and locate["resolution"] == "MEDIA_RESOLUTION_MEDIUM"
        assert locate["model"] == "gemini-3.5-flash-lite" and max(locate["sizes"][0]) == 1100
        # Consolidated pages (and the unlabelled continuation page), never the standalone ones or a made-up page.
        assert res.pages == [5, 6, 7] and res.meta["pages"] == "5,6,7" and res.meta["located"]["basis"] == "consolidated"
        assert copy["labels"] == ["Page 5:", "Page 6:", "Page 7:"] and copy["part"] == "MEDIA_RESOLUTION_ULTRA_HIGH"
        assert copy["model"] == "gemini-3.5-flash" and copy["candidates"][-1] == "gemini-flash-lite-latest"
        assert res.meta["pictures"]["turned"] == {6: 90}  # page 6 turned upright, as the preview said
        assert reread["labels"] == ["Page 5, whole:", "Page 5, top part enlarged:", "Page 5, bottom part enlarged:"]
        assert "Operating Expenses (a)+ (b) — first read as col 1: 1044906" in reread["instruction"]
        assert [(c["col"], c["now"]) for c in res.meta["pictures"]["corrections"]] == [(4, "1988070"), (6, "3871744")]
        assert res.meta["pictures"]["doubtful"] == []
        # The template step gets the checked tables as text: no pictures, no PDF.
        assert first["pdf"] is None and not first["labels"] and "[6] 3871744" in first["instruction"]
        assert "cf_cash_begin 18,082,312" in second["instruction"]
        assert res.meta["rechecked"]["still_failing"] == 0
        assert res.normalised["cash_flow"]["values"]["cf_cash_begin"] == [1_608_231_200]
        assert res.meta["found_pages"]["income_statement"] == [] or all(p in (5, 6, 7) for p in res.meta["found_pages"]["income_statement"])
        assert res.usage["input_tokens"] == 5000 + 4000 + 1000 + 1000

        data, _ = workbook_bytes(None, res.normalised, {**res.meta, "company_name": "BANK OF BARODA"})
        wb = load_workbook(io.BytesIO(data))
        sheet = wb["Transcription"]
        text = [" ".join(str(c.value) for c in row if c.value is not None) for row in sheet.iter_rows()]
        assert any("Operating Expenses (a)+ (b)" in t and "3871744" in t for t in text), text[:12]
        assert any("re-read" in str(c.comment.text if c.comment else "") for row in sheet.iter_rows() for c in row)

        # A second run reads from the cache, column numbers intact.
        calls.clear()
        again = pipeline.process_pdf(pic, scan=scan, cache_dir=out)
        assert calls == [] and again.meta["pictures"]["corrections"] == res.meta["pictures"]["corrections"]
        assert again.normalised["cash_flow"]["values"]["cf_cash_begin"] == [1_608_231_200]

        # Pinned to Flash-Lite: picture filings use it too.
        os.environ["SGX_VISION_MODEL"] = "gemini-3.5-flash-lite"
        assert extractor.vision_models() == ["gemini-3.5-flash-lite"]
    finally:
        extractor._gemini = real
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_accuracy_scorer_and_answer_key():
    """A perfect reading of Bank of Baroda scores 100% against the answer key; the old run's
    workbook pattern (gross Total Income, a row slip) is scored as wrong."""
    import tempfile
    from accuracy import score

    key = json.loads((HERE / "answers" / "bank_of_baroda_2026q2.json").read_text())
    data = {"company_name": "BANK OF BARODA", "format": "bank", "currency": "INR", "unit": "lakhs", "basis": "Group", "notes": ""}
    for st in ("income_statement", "balance_sheet", "cash_flow"):
        lines = [l for l in key["lines"] if l["statement"] == st]
        periods = list(dict.fromkeys((l["period_end"], l["months"]) for l in lines))
        rows = {}
        for l in lines:
            if l["value"] is not None:
                rows.setdefault(l["key"].split("+")[0], {})[periods.index((l["period_end"], l["months"])) + 1] = l["value"]
        data[st] = {"page_numbers": [1], "periods": [{"col": i, "label": e, "period_end": e, "months": m} for i, (e, m) in enumerate(periods, 1)],
                    "rows": [{"key": k, "source_label": k, "cells": [{"col": c, "value": v} for c, v in cells.items()]} for k, cells in rows.items()]}
    with tempfile.TemporaryDirectory() as d:
        perfect = Path(d) / "perfect.xlsx"
        perfect.write_bytes(workbook_bytes(None, extractor.normalise(data), {"company_name": "BANK OF BARODA", "format": "bank"})[0])
        s = score(perfect, key)
        assert s["counts"]["correct"] == len(key["lines"]), [r for r in s["results"] if r["status"] != "correct"][:3]

        # The old mistakes: gross 'Total Income' as total operating income (now netted by the program),
        # and Operating Expenses taken from the row above (still wrong: only a re-read fixes that).
        for r in data["income_statement"]["rows"]:
            if r["key"] == "total_operating_income":
                r["cells"] = [{"col": c["col"], "value": c["value"] + abs(next(x for x in data["income_statement"]["rows"] if x["key"] == "interest_expense")["cells"][i]["value"])}
                              for i, c in enumerate(r["cells"])]
            if r["key"] == "total_operating_expenses":
                r["cells"][5]["value"] = -2877332
        old = Path(d) / "old.xlsx"
        old.write_bytes(workbook_bytes(None, extractor.normalise(data), {"company_name": "BANK OF BARODA", "format": "bank"})[0])
        wrong = [(r["key"], r["months"]) for r in score(old, key)["results"] if r["status"] != "correct"]
        assert wrong == [("total_operating_expenses", 12)], wrong


def test_gemini_steps_down_from_settings_a_model_rejects():
    from google.genai import errors

    tried = []

    class Models:
        def generate_content(self, model, contents, config):
            fine = any(getattr(p, "media_resolution", None) for c in contents if hasattr(c, "parts") for p in c.parts)
            tried.append((model, fine, config.media_resolution, bool(config.thinking_config)))
            if model == "gemini-3.5-flash":
                raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
            if fine:
                raise errors.ClientError(400, {"error": {"code": 400, "message": "no ultra", "status": "INVALID_ARGUMENT"}})

            class R:
                text = json.dumps({"pages": []})
                candidates = [type("C", (), {"finish_reason": type("F", (), {"name": "STOP"})()})()]
                usage_metadata = None
                model_version = model
            return R()

    client = type("Client", (), {"models": Models()})()
    ex = extractor._gemini(None, "read", "gemini-3.5-flash", "high", client, images=[("Page 1:", b"\xff\xd8jpeg")],
                           candidates=extractor.vision_models(), resolution="MEDIA_RESOLUTION_HIGH",
                           part_resolution="MEDIA_RESOLUTION_ULTRA_HIGH", schema_json={"type": "object"})
    assert ex.model == "gemini-flash-latest"
    assert tried == [("gemini-3.5-flash", True, None, True),  # quota used up: next model
                     ("gemini-flash-latest", True, None, True),  # ultra-high refused: plain high
                     ("gemini-flash-latest", False, "MEDIA_RESOLUTION_HIGH", True)], tried


def test_statement_formats_import_edit_and_formulas():
    from sgx_fin import formats as F, template as T

    # A workbook made by the app, imported back, reproduces its format exactly (labels, kinds, keys).
    for fid in ("general", "bank"):
        data, _ = workbook_bytes(None, extractor.normalise({**FAKE, "format": fid}), {"company_name": "A", "format": fid})
        back = F.parse(T.BUILTIN[fid]["name"], "", filename="a.xlsx", data=data)
        want = T.to_json(T.BUILTIN[fid])
        for st in T.STATEMENT_IDS:
            assert [(r["label"], r["kind"], r.get("key")) for r in back["statements"][st]["rows"]] == \
                   [(r["label"], r["kind"], r.get("key")) for r in want["statements"][st]["rows"]], (fid, st)

    # Rows pasted from a statements page: labels mapped by meaning, figures ignored.
    pasted = F.parse("General", "", text="Income Statement\nRevenue\t1,000\nCost of Sales\t(600)\n"
                                         "Gross Profit\t400\nTaxation\t(50)\nPATMI\t300\n")
    keys = [r["key"] for r in pasted["statements"]["income_statement"]["rows"]]
    assert keys == ["revenue", "cost_of_sales", "gross_profit", "tax", "pat_shareholders"]

    # Editable text with a formula entry; the text form reads back unchanged.
    text = ("[Income Statement]\nRevenue\nCost of Goods Sold\n= Gross Profit := Revenue + Cost of Goods Sold\n"
            "Depreciation & Amortisation\nProfit Before Tax\n= EBITDA := Profit Before Tax + Depreciation & Amortisation\n"
            "Bad := Revnue - 1\n")
    fmt = F.parse("General", "everyone", text=text, fmt_id="general")
    assert F.parse("General", "everyone", text=F.to_marked(fmt), fmt_id="general") == fmt
    formats = T.resolve([fmt])
    assert any("Revnue" in p for p in T.formula_problems(formats["general"]))
    d = json.loads(json.dumps(FAKE))
    d["income_statement"]["rows"] = [r for r in d["income_statement"]["rows"] if r["key"] != "gross_profit"] + [
        {"key": "depreciation_amortisation", "source_label": "D&A", "values": [6000, 5650]}]
    inc = extractor.normalise(d, formats)["income_statement"]
    assert inc["values"]["gross_profit"] == [40100, 35500] and inc["labels"]["gross_profit"].startswith("formula")
    assert inc["values"]["ebitda"] == [25400, 21750]
    assert "bad" not in inc["values"]  # an unresolved formula leaves the entry blank
    printed = extractor.normalise(FAKE, formats)["income_statement"]
    assert not printed["labels"]["gross_profit"].startswith("formula")  # a printed figure is kept
    data, checks = workbook_bytes(None, extractor.normalise(d, formats), {"company_name": "A", "format": "general"}, formats)
    labels_in_sheet = [c.value for c in load_workbook(io.BytesIO(data))["Income Statement"]["A"]]
    assert "EBITDA" in labels_in_sheet and "Other Income" not in labels_in_sheet  # the edited format's rows


def test_notices_and_releases_are_not_extracted():
    import importlib.util
    import shutil
    from sgx_fin.pipeline import not_results_reason
    from sgx_fin.sgx_client import classify_title

    fs = "Financial Statements and Related Announcement::"
    assert classify_title(fs + "Half Yearly Results") == "results"
    assert classify_title(fs + "Full Yearly Results - Amendment") == "results"
    for t in (fs + "Notification of Results Release", "DATE OF RELEASE OF FINANCIAL RESULTS FOR FIRST HALF OF FY2027"):
        assert classify_title(t) == "release_date", t
    for t in (fs + "Third Quarter Business Update", fs + "Response to SGX Queries", "Results Presentation 1H FY2027"):
        assert classify_title(t) == "other", t

    notice = HERE / "notice.pdf"
    assert "notice" in not_results_reason(notice, scan_pdf(notice))
    assert not_results_reason(HERE / "sample_results.pdf", scan_pdf(HERE / "sample_results.pdf")) is None
    out = HERE / "out" / "notice"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    pic = picture_pdf(HERE / "sample_results.pdf", out / "photos.pdf")
    assert not_results_reason(pic, scan_pdf(pic)) is None  # pictures may hold the statements

    # The web API stops before any AI call.
    spec = importlib.util.spec_from_file_location("vapp", HERE.parent / "api" / "index.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    called = []
    real = extractor.extract
    extractor.extract = lambda *a, **k: called.append(1)
    try:
        r = app.app.test_client().post("/api/process?op=process", content_type="multipart/form-data",
                                       data={"pdf": (io.BytesIO(notice.read_bytes()), "notice.pdf")})
    finally:
        extractor.extract = real
    assert r.status_code == 422 and r.json["not_results"] and not called


def test_release_date_notices_and_separate_statements():
    import importlib.util
    from sgx_fin.notices import parse_release_notice
    from sgx_fin.page_scanner import page_texts
    from sgx_fin.sgx_client import SGXClient

    info = parse_release_notice(" ".join(page_texts(HERE / "notice.pdf")))
    assert info == {"release_date": "2026-11-13", "period": "first half of FY2027 ended 30 September 2026",
                    "period_end": "2026-09-30", "timing": "after the close of trading on the Singapore Exchange"}
    other = parse_release_notice("the unaudited results of the Group for the third quarter ended 30 September 2026 "
                                 "will be released on Thursday, 12 November 2026, before the market opens.")
    assert other["release_date"] == "2026-11-12" and other["period_end"] == "2026-09-30"

    # Consolidated vs separate (company-only) statements, from the statement headings.
    assert scan_pdf(HERE / "sample_results.pdf").basis == "consolidated"
    assert scan_pdf(HERE / "separate_results.pdf").basis == "separate"

    # Notices that state the date on the announcement page itself (no PDF).
    page = ("<html><script>var d='on 1 Jan 2020'</script><table><tr><td>Announcement Title</td><td>Notification of "
            "Results Release</td></tr><tr><td>Date &amp; Time of Broadcast</td><td>30-Oct-2026 17:48:12</td></tr><tr>"
            "<td>Description</td><td>The Company will be releasing its unaudited financial results for the third quarter "
            "ended 30 September 2026 on 12 November 2026 (Thursday) after market close.</td></tr></table></html>")
    from sgx_fin.notices import page_text
    got = parse_release_notice(page_text(page))
    assert got["release_date"] == "2026-11-12" and got["period_end"] == "2026-09-30" and got["timing"] == "after market close"
    assert parse_release_notice("Date of Results Announcement: 13/11/2026")["release_date"] == "2026-11-13"
    assert parse_release_notice("will announce its results on Friday, 13-Nov-2026")["release_date"] == "2026-11-13"

    # The API reads a notice's date without AI.
    spec = importlib.util.spec_from_file_location("vapp", HERE.parent / "api" / "index.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    real = SGXClient.attachments, SGXClient.download
    SGXClient.attachments = lambda self, ann: ["https://links.sgx.com/FileOpen/notice.ashx?App=Announcement&FileID=1"]
    SGXClient.download = lambda self, url, folder: HERE / "notice.pdf"
    class Page:  # the announcement page: no date in its text, so the PDF is read
        status_code, text, url = 200, "<html><body>Notification of Results Release. Please see attached.</body></html>", ""
        def raise_for_status(self): pass
    real_get = __import__("requests").Session.get
    __import__("requests").Session.get = lambda self, url, **kw: Page()
    try:
        client = app.app.test_client()
        r = client.get("/api/release_date?op=release_date&url=https://links.sgx.com/1.0.0/corporate-announcements/K/1")
        Page.text = page  # now the page states the date: no PDF needed
        r2 = client.get("/api/release_date?op=release_date&url=https://links.sgx.com/1.0.0/corporate-announcements/K/2")
        r3 = client.post("/api/release_text?op=release_text", data={"text": page_text(page)})
    finally:
        SGXClient.attachments, SGXClient.download = real
        __import__("requests").Session.get = real_get
    assert r.status_code == 200 and r.json["release_date"] == "2026-11-13" and r.json["source"] == "notice PDF"
    assert r2.json["release_date"] == "2026-11-12" and r2.json["source"] == "announcement page"
    assert r3.json["release_date"] == "2026-11-12"


def highlights():
    """What a presentation's highlights slide gives: revenue and profit, nothing a printed total confirms."""
    d = json.loads(json.dumps(FAKE))
    d["income_statement"]["rows"] = [r for r in d["income_statement"]["rows"] if r["key"] in ("revenue", "pat_shareholders")]
    d["balance_sheet"] = d["cash_flow"] = {"page_numbers": [], "periods": [], "rows": []}
    return d


def test_checks_that_prove_nothing_fail():
    from sgx_fin import checks

    good = extractor.normalise(FAKE)
    v = checks.verdict(good)
    assert v["ok"] and v["pass"] == 18 and v["fail"] == 0 and v["reasons"] == []

    # 0 + 0 = 0 is not a pass: the lines may not be there at all.
    zeros = json.loads(json.dumps(FAKE))
    for st in ("income_statement", "balance_sheet", "cash_flow"):
        for r in zeros[st]["rows"]:
            r["values"] = [0, 0]
    nz = extractor.normalise(zeros)
    fails = checks.failures(nz)
    assert fails and all(f["note"] == checks.ZERO for f in fails)
    assert "every figure is 0" in checks.describe(fails[:1], nz) and "left out, not written as 0" in checks.describe(fails[:1], nz)
    v = checks.verdict(nz)
    assert not v["ok"] and v["pass"] == 0 and "0 checks pass" in v["summary"]
    # A second reading that leaves those lines out (not printed) is taken: n/a beats a false 0.
    second = json.loads(json.dumps(zeros))
    second["cash_flow"]["rows"] = [r for r in second["cash_flow"]["rows"] if r["key"] not in ("cf_cash_begin", "cf_cash_end", "cf_fx_effect", "cf_net_change")]
    fixed = checks.reconcile(nz, extractor.normalise(second))
    assert any("left out (not printed)" in f for f in fixed), fixed
    assert not any(f["check"].startswith("Begin + Net Change") for f in checks.failures(nz))

    # The slack is the rounding of the figures, not 0.2% of the total: a 300 misread in 199,000 fails.
    misread = json.loads(json.dumps(FAKE))
    misread["balance_sheet"]["rows"][2]["values"][0] += 300  # total assets
    assert {f["check"] for f in checks.failures(extractor.normalise(misread))} == {
        "NCA + CA = Total Assets", "Total Liabilities + Total Equity = Total Assets"}
    assert checks.tolerance([100, 50, 150], 1) == 2  # thousands: two of the filing's units
    assert checks.tolerance([123_456_700, 2_000, 123_458_700], 10_000) == 2_000  # crores printed to 0.1 crore
    assert checks.tolerance([1_234.567, 1.0, 1_235.567], 0.001) == 0.002  # units: two dollars, not two thousand
    assert checks.tolerance([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 1) == 5  # ten rounded figures

    # Highlights only: nothing passes, statements and gross profit missing -> not verified, and why.
    v = checks.verdict(extractor.normalise(highlights()), "16:9 slides")
    assert not v["ok"] and v["pass"] == v["fail"] == 0
    assert v["reasons"] == ["no tie-out check passes, so nothing confirms the figures",
                            "no balance sheet or cash flow statement found", "gross profit is blank",
                            "the PDF is a presentation (16:9 slides), not the formal financial statements"]
    assert v["summary"] == "0 checks pass · no balance sheet or cash flow · gross profit blank · slides only"

    # The workbook says so where the figures are, and on the Checks sheet with the verdict first.
    meta = {"company_name": "ACME", "filing": "1H2026 Results", "verdict": v}
    data, results = workbook_bytes(None, extractor.normalise(highlights()), meta)
    wb = load_workbook(io.BytesIO(data))
    assert wb["Income Statement"]["A3"].value.startswith("Not verified: 1H2026 Results: no tie-out check passes")
    assert wb["Checks"]["A1"].value.startswith("NOT VERIFIED — 1H2026 Results (6M Jun 2026, 6M Jun 2025)")
    data, _ = workbook_bytes(None, nz, {"company_name": "ACME", "filing": "zeros", "verdict": checks.verdict(nz)})
    sheet = load_workbook(io.BytesIO(data))["Checks"]
    head = next(r for r in range(1, 6) if sheet.cell(r, 1).value == "Statement")
    assert sheet.cell(head, 6).value == "Note" and sheet.cell(head + 1, 4).value == "FAIL"
    assert sheet.cell(head + 1, 6).value == "every figure is 0"
    data, _ = workbook_bytes(None, good, {"company_name": "ACME", "filing": "FY", "verdict": checks.verdict(good)})
    wb = load_workbook(io.BytesIO(data))
    assert wb["Income Statement"]["A3"].value is None and wb["Checks"]["A1"].value.startswith("OK — FY")

    # The web API sends this filing's verdict with the workbook, so the card can say "Check failed".
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("vapp_verdict", HERE.parent / "api" / "index.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    saved, real = os.environ.get("GEMINI_API_KEY"), extractor.extract
    os.environ["GEMINI_API_KEY"] = "test"
    try:
        for reading, ok in ((FAKE, True), (highlights(), False)):
            extractor.extract = lambda *a, _r=reading, **k: extractor.Extraction(_r, "fake", {})
            r = app.app.test_client().post("/api/process?op=process", content_type="multipart/form-data",
                                           data={"pdf": (io.BytesIO((HERE / "sample_results.pdf").read_bytes()), "acme.pdf")})
            assert r.status_code == 200 and r.json["verdict"]["ok"] is ok, r.json.get("verdict")
        assert r.json["verdict"]["summary"].startswith("0 checks pass")
    finally:
        extractor.extract = real
        os.environ.pop("GEMINI_API_KEY", None)
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved


def test_gross_profit_is_never_blank():
    from sgx_fin import checks, pipeline

    # The rules ask for judgement when no cost of sales is printed, with the lines a financial company uses.
    assert "Gross profit must never end up blank" in extractor.SYSTEM and "Materials always go in" in extractor.SYSTEM
    assert "- finance (Finance, leasing & securities;" in extractor.SYSTEM
    assert "financial cost associated with financial income" in extractor.SYSTEM and "insurance service expenses" in extractor.SYSTEM

    # A finance company read with the general format: the gross expense judged (sign forgotten), gross profit worked out.
    fin = json.loads(json.dumps(FAKE))
    fin["income_statement"]["rows"] = [
        {"key": "revenue", "source_label": "Total income", "values": [50_000, 46_000]},
        {"key": "cost_of_sales", "source_label": "Financial cost associated with financial income + Provision for credit "
                                                  "losses + Insurance service expenses + Operating cost", "values": [38_000, 35_500]},
        {"key": "profit_before_tax", "source_label": "Profit before tax", "values": [12_000, 10_500]},
        {"key": "tax", "source_label": "Tax", "values": [-2_000, -1_800]},
        {"key": "profit_after_tax", "source_label": "Profit for the year", "values": [10_000, 8_700]}]
    inc = extractor.normalise(fin)["income_statement"]
    assert inc["values"]["cost_of_sales"] == [-38_000, -35_500] and "sign corrected" in inc["labels"]["cost_of_sales"]
    assert inc["values"]["gross_profit"] == [12_000, 10_500] and inc["labels"]["gross_profit"].startswith("computed")

    # A bank: gross income, gross expense (interest + operating expenses + allowances) and gross profit from its lines.
    bob = extractor.normalise(bob_reading(16082312))
    v, labels = bob["income_statement"]["values"], bob["income_statement"]["labels"]
    assert v["revenue"] == [457_837_100, 440_000_000]  # 'Total Income (1+2)' 4,578,371 lakhs
    assert v["cost_of_sales"][0] == -(226_604_800 + 104_490_600 + 34_068_900) and "allowances" in labels["cost_of_sales"]
    assert v["gross_profit"][0] == v["profit_before_tax"][0] == 92_672_800  # no exceptional item that quarter
    assert checks.verdict(bob)["ok"]
    data, _ = workbook_bytes(None, bob, {"company_name": "BANK OF BARODA", "format": "bank"})
    col_a = [c.value for c in load_workbook(io.BytesIO(data))["Income Statement"]["A"] if c.value]
    assert col_a.index("Gross Income") < col_a.index("Gross Expense") < col_a.index("Gross Profit") < col_a.index("Interest Income")
    sources = load_workbook(io.BytesIO(data))["Source Map"]
    assert any(r[2] == "Gross Expense" for r in sources.iter_rows(values_only=True)), "the bank's own row names"

    # Left blank by a first reading: a second look asks for the gross expense, and its judgement is taken.
    blank = json.loads(json.dumps(FAKE))
    blank["income_statement"]["rows"] = [r for r in blank["income_statement"]["rows"] if r["key"] not in ("cost_of_sales", "gross_profit")]
    judged = json.loads(json.dumps(FAKE))
    judged["income_statement"]["rows"] = [r for r in judged["income_statement"]["rows"] if r["key"] != "gross_profit"]
    judged["income_statement"]["rows"][1]["source_label"] = "Cost of services + Depreciation"
    asked = []

    def fake_extract(pdf, pages, gaps="", recheck="", **kw):
        asked.append(gaps)
        return extractor.Extraction(judged if gaps else blank, "fake", {"input_tokens": 1, "output_tokens": 1})

    real = pipeline.extractor.extract
    pipeline.extractor.extract = fake_extract
    try:
        out = HERE / "out" / "gross"
        out.mkdir(parents=True, exist_ok=True)
        res = pipeline.process_pdf(HERE / "sample_results.pdf", cache_dir=out)
    finally:
        pipeline.extractor.extract = real
    assert len(asked) == 2 and asked[0] == "" and "gross profit is blank" in asked[1] and "judgement" in asked[1]
    inc = res.normalised["income_statement"]
    assert inc["values"]["cost_of_sales"] == [-80_412, -72_100] and inc["labels"]["cost_of_sales"] == "Cost of services + Depreciation"
    assert inc["values"]["gross_profit"] == [40_100, 35_500] and inc["labels"]["gross_profit"].startswith("computed")
    assert res.meta["rechecked"]["gross_profit_filled"] == ["cost_of_sales"] and res.meta["rechecked"]["gross_profit_still_blank"] == 0
    assert res.meta["verdict"]["ok"], res.meta["verdict"]


def test_presentations_are_not_results():
    import importlib.util
    import shutil
    from pypdf import PdfReader, PdfWriter
    from sgx_fin import mcp_server
    from sgx_fin.page_scanner import slide_deck
    from sgx_fin.pipeline import choose_financial_pdf, not_results_reason

    # A results announcement that carries only slides: highlights, no formal statements. Set aside without AI.
    deck = HERE / "deck_results.pdf"
    scan = scan_pdf(deck)
    assert scan.presentation == "16:9 slides" and scan.titled == ["income_statement", "balance_sheet"]
    assert not_results_reason(deck, scan) == ("It's a presentation (16:9 slides) without the formal financial "
                                              "statements (no cash flow statement).")
    assert not scan_pdf(HERE / "sample_results.pdf").presentation  # a results PDF is not a deck

    # Told apart by shape and words too (4:3 decks), but a landscape results PDF dense with figures is not slides.
    sparse = ["Agenda\nKey highlights\nOutlook", "Revenue up 12%", "Thank you"]
    assert slide_deck("", [(720, 540)] * 3, sparse) == "landscape slides"
    assert slide_deck("", [(842, 595)] * 3, ["Statement of financial position " + "Total assets 1,234 " * 80] * 3) == ""
    assert slide_deck("Microsoft® Word for Microsoft 365", [(595, 842)] * 3, sparse) == ""

    # Made in PowerPoint: a deck even on A4 pages. One that holds the formal statements isn't set aside, but a
    # results PDF among the attachments is preferred to it.
    out = HERE / "out" / "deck"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    w = PdfWriter(clone_from=PdfReader(str(HERE / "sample_results.pdf")))
    w.add_metadata({"/Creator": "Microsoft® PowerPoint® for Microsoft 365", "/Producer": "Microsoft® PowerPoint® for Microsoft 365"})
    w.write(out / "pptx.pdf")
    ppt = scan_pdf(out / "pptx.pdf")
    assert ppt.presentation == "made in PowerPoint" and ppt.formal_statements and not_results_reason(out / "pptx.pdf", ppt) is None

    class FakeClient:
        def __init__(self): self.got = []
        def download(self, url, folder):
            kind = url.rsplit("=", 1)[1]
            self.got.append(kind)
            dest = folder / {"deck": "1H FY2026 Results.pdf", "results": "Financial Statements 1H2026.pdf"}[kind]
            shutil.copy(out / "pptx.pdf" if kind == "deck" else HERE / "sample_results.pdf", dest)
            return dest

    links = [("https://links.sgx.com/FileOpen/a.ashx?k=deck", "1H FY2026 Results.pdf"),
             ("https://links.sgx.com/FileOpen/b.ashx?k=results", "Financial Statements 1H2026.pdf")]
    client = FakeClient()
    pdf, found, used, _ = choose_financial_pdf(client, links, out)
    assert used.endswith("k=results") and not found.presentation and client.got == ["deck", "results"]

    # The web API stops before any AI call and says it's slides.
    spec = importlib.util.spec_from_file_location("vapp_deck", HERE.parent / "api" / "index.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    called = []
    real = extractor.extract
    extractor.extract = lambda *a, **k: called.append(1)
    try:
        r = app.app.test_client().post("/api/process?op=process", content_type="multipart/form-data",
                                       data={"pdf": (io.BytesIO(deck.read_bytes()), "1H FY2026 Results.pdf")})
    finally:
        extractor.extract = real
    assert r.status_code == 422 and r.json["not_results"] and r.json["scan"]["presentation"] == "16:9 slides" and not called

    # Claude, through the connector, is told the same, and can still look at the slides itself.
    old = mcp_server.load_filing
    mcp_server.load_filing = lambda args, ctx: (deck, scan_pdf(deck), None, [deck.name])
    try:
        ctx = mcp_server.Context("https://app.test", out)
        res = mcp_server.call_tool("open_filing", {"announcement_url": "https://links.sgx.com/1.0.0/x"}, ctx)
        text = res["content"][0]["text"]
        assert "Not a results filing: It's a presentation (16:9 slides)" in text and "as_images=true" in text
        res = mcp_server.call_tool("open_filing", {"announcement_url": "https://links.sgx.com/1.0.0/x", "as_images": True}, ctx)
        assert sum(c["type"] == "image" for c in res["content"]) == 7 and "no formal financial statements" in res["content"][0]["text"]
    finally:
        mcp_server.load_filing = old


def by_nature(industry="manufacturing", lines=None, method="by_nature"):
    """A reading with no cost of sales line: the expense lines listed, the COGS ones marked."""
    d = json.loads(json.dumps(FAKE))
    d["industry"], d["industry_evidence"] = industry, "precision parts; raw materials consumed"
    d["income_statement"]["rows"] = [r for r in d["income_statement"]["rows"] if r["key"] not in ("cost_of_sales", "gross_profit")]
    cell = lambda a, b: [{"col": 1, "value": a}, {"col": 2, "value": b}]  # noqa: E731
    d["cost_of_sales_basis"] = {"method": method, "explanation": "Materials, the change in inventories and subcontracting; "
                                "staff and depreciation are mostly head office.", "lines": lines if lines is not None else [
        {"source_label": "Raw materials and consumables used", "cells": cell(-60_000, -54_000), "in_cogs": True, "why": "materials"},
        {"source_label": "Changes in inventories", "cells": cell(1_500, -800), "in_cogs": True, "why": "goods made but unsold"},
        {"source_label": "Subcontract costs", "cells": cell(-9_000, -8_000), "in_cogs": True, "why": "outsourced production"},
        {"source_label": "Employee benefits expense", "cells": cell(-20_000, -18_500), "in_cogs": False, "why": "mostly head office"},
        {"source_label": "Depreciation", "cells": cell(-6_000, -5_650), "in_cogs": False, "why": "office and factory together"}]}
    return d


def test_industries_and_cogs_rules():
    from sgx_fin import industries as I

    # The filing's own words point to its industry; generic lines point nowhere.
    cases = {
        "manufacturing": ["Raw materials and consumables used", "Changes in inventories of finished goods and work in progress"],
        "trading": ["Purchases of goods for resale", "Changes in inventories", "Distribution costs", "Retail stores"],
        "reit": ["Gross revenue", "Property expenses", "Net property income", "Trustee's fee", "Distribution to unitholders"],
        "bank": ["Interest earned", "Interest expended", "Net interest income", "Deposits", "Advances"],
        "insurance": ["Insurance revenue", "Insurance service expenses", "Net expenses from reinsurance contracts held"],
        "finance": ["Financial cost associated with financial income", "Provision for credit losses", "Hire purchase receivables"],
        "construction": ["Contract revenue", "Contract costs", "Subcontract costs"],
        "transport": ["Fuel costs", "Vessel operating costs", "Charter hire", "Port charges"],
        "hospitality": ["Room revenue", "Gaming revenue", "Casino", "Resort operating costs"],
        "resources": ["Sales of crude palm oil", "Plantation upkeep", "Harvesting costs"],
        "investment_holding": ["Dividend income", "Investment income", "Fair value gain on financial assets"],
    }
    for want, words in cases.items():
        assert (I.guess(words) or [None])[0] == want, (want, I.guess(words))
    assert I.guess(["Revenue", "Other income", "Administrative expenses", "Finance costs"]) is None
    assert not I._hit("gas", "gaskets and seals") and not I._hit("mine", "mineral water") and I._hit("manufactur", "manufacturing")

    # A user's preferences: their version of a rule, a new industry, and a company's own rule.
    custom = {"industries": [{"id": "manufacturing", "name": "Manufacturing", "cogs": "Materials only."},
                             {"name": "Aquaculture", "applies_to": "Fish farms", "keywords": "fish feed, hatchery", "cogs": "Feed + fry + farm staff"}],
              "companies": [{"match": "F13", "industry": "manufacturing", "cogs": "Raw materials + changes in inventories only"},
                            {"match": "BARODA", "industry": "bank", "cogs": ""}]}
    rules = I.for_filing(custom, "F13", "FU YU CORPORATION LIMITED")
    tags = {i["id"]: i["source"] for i in rules["industries"]}
    assert tags["manufacturing"] == "your version" and tags["aquaculture"] == "yours" and tags["bank"] == "built-in"
    assert rules["company"]["match"] == "F13" and I.rule_used(rules, "manufacturing") == "your rule for F13"
    assert I.for_filing(custom, "2V6B", "BANK OF BARODA")["company"]["industry"] == "bank"  # by a fragment of its name
    assert I.rule_used(I.for_filing(custom), "manufacturing") == "your Manufacturing rule"
    assert I.rule_used(I.for_filing(), "reit") == "the built-in REITs & business trusts rule"
    text = I.prompt(rules)
    assert "- aquaculture (Aquaculture; Fish farms). COGS (the user's own rule): Feed + fry + farm staff" in text
    assert "The user's own rule for this company (\"F13\")" in text and "Raw materials + changes in inventories only" in text
    assert "aquaculture" in extractor.build_schema(None, rules)["properties"]["industry"]["enum"]
    # Industrial rules put D&A in, as FactSet's cost of revenue does; REITs, banks and insurers keep their own.
    fs = "Depreciation and amortisation on the face of the statement go in too, as in FactSet's COGS including D&A."
    built = {i["id"]: i["cogs"] for i in I.BUILTIN}
    assert all(fs in built[i] for i in ("manufacturing", "trading", "transport", "technology", "services", "other"))
    assert not any(fs in built[i] for i in ("reit", "bank", "finance", "insurance", "investment_holding"))
    assert "Raw materials + changes in inventories only" in extractor.build_system(None, rules)


def test_cogs_is_judged_line_by_line_and_explained():
    import base64
    import importlib.util
    import os
    from sgx_fin import checks, glm, industries as I

    # No cost of sales printed: the marked lines are added up by the program, and the basis says how.
    n = extractor.normalise(by_nature(), None, I.for_filing())
    inc, b = n["income_statement"], n["income_statement"]["cogs_basis"]
    assert inc["values"]["cost_of_sales"] == [-67_500, -62_800]  # -60,000 + 1,500 - 9,000 ('000)
    assert inc["labels"]["cost_of_sales"] == ("judged (Manufacturing & industrials): Raw materials and consumables used + "
                                              "Changes in inventories + Subcontract costs")
    assert inc["values"]["gross_profit"] == [53_012, 44_800]
    assert (b["industry"], b["decided_by"], b["method"], b["rule"]) == (
        "manufacturing", "AI", "by_nature", "the built-in Manufacturing & industrials rule")
    assert b["evidence"] == "precision parts; raw materials consumed" and b["explanation"].startswith("Materials")
    assert [ln["in_cogs"] for ln in b["lines"]] == [True, True, True, False, False] and b["lines"][3]["values"] == [-20_000, -18_500]
    assert b["da_in_cogs"] is None and b["cogs_excl_da"] is None  # no D&A among the lines marked in

    # FactSet's convention: cost of revenue including D&A, with D&A also shown on its own. A COGS that
    # takes in the depreciation line says how much of it is D&A, and what it is without.
    with_da = by_nature()
    with_da["cost_of_sales_basis"]["lines"][4]["in_cogs"] = True
    b = extractor.normalise(with_da)["income_statement"]["cogs_basis"]
    assert b["cogs"] == [-73_500, -68_450] and b["da_in_cogs"] == [-6_000, -5_650] and b["cogs_excl_da"] == [-67_500, -62_800]
    data, _ = workbook_bytes(None, extractor.normalise(with_da), {"company_name": "FU YU", "filing": "1H2026"})
    sheet = [[c for c in r if c is not None] for r in load_workbook(io.BytesIO(data))["COGS Basis"].iter_rows(values_only=True)]
    assert ["of which depreciation & amortisation", -6_000, -5_650] in sheet
    assert sheet[sheet.index(["of which depreciation & amortisation", -6_000, -5_650]) + 1][:3] == [
        "Cost of goods sold excluding D&A", -67_500, -62_800]

    # The user's rule for the company decides the industry; with no answer, the filing's own words do.
    rules = I.for_filing({"companies": [{"match": "ACME", "industry": "trading", "cogs": "Purchases only"}]}, "", "ACME HOLDINGS")
    b = extractor.normalise(by_nature(), None, rules)["income_statement"]["cogs_basis"]
    assert (b["industry"], b["decided_by"], b["rule"]) == ("trading", "you", "your rule for ACME")
    nameless = by_nature(industry="")
    b = extractor.normalise(nameless)["income_statement"]["cogs_basis"]
    assert b["industry"] == "manufacturing" and b["decided_by"] == "the filing's words" and "raw materials" in b["evidence"]
    # An answer the lines contradict is kept, but flagged.
    b = extractor.normalise(by_nature(industry="bank"))["income_statement"]["cogs_basis"]
    assert b["industry"] == "bank" and b["also_suggests"].startswith("Manufacturing & industrials (raw materials")

    # Nothing is a cost of earning the revenue (an investment holding's dividends): COGS 0, gross profit = revenue.
    none = by_nature(industry="investment_holding", method="none", lines=[
        {"source_label": "Administrative expenses", "cells": [{"col": 1, "value": -900}], "in_cogs": False, "why": "head office"}])
    inc = extractor.normalise(none)["income_statement"]
    assert inc["values"]["cost_of_sales"] == [0, 0] and inc["values"]["gross_profit"] == [120_512, 107_600]
    assert "no direct cost" in inc["labels"]["cost_of_sales"]

    # Printed cost of sales: used as printed, and said so. The program's own sums are explained too.
    b = extractor.normalise(FAKE)["income_statement"]["cogs_basis"]
    assert b["method"] == "printed" and b["explanation"] == "Cost of sales as printed: Cost of sales."
    old = json.loads(json.dumps(FAKE))
    old["income_statement"]["rows"] = [r for r in old["income_statement"]["rows"] if r["key"] not in ("cost_of_sales", "gross_profit")] + [
        {"key": "raw_materials", "source_label": "Raw materials", "values": [-50_000, -45_000]}]
    old["income_statement"]["rows"].append({"key": "depreciation_amortisation", "source_label": "D&A", "values": [-5_000, -4_000]})
    b = extractor.normalise(old)["income_statement"]["cogs_basis"]  # a reading from before bases: the by-nature sum
    assert b["method"] == "program" and b["explanation"].startswith("Worked out by the program: raw materials")
    assert b["cogs"] == [-55_000, -49_000] and b["da_in_cogs"] == [-5_000, -4_000] and b["cogs_excl_da"] == [-50_000, -45_000]
    assert extractor.normalise(FAKE)["income_statement"]["cogs_basis"]["da_in_cogs"] is None  # printed: the D&A inside isn't shown

    # GLM's free-form answer keeps the industry and the basis (numbers as printed text too).
    raw = by_nature()
    raw["cost_of_sales_basis"]["lines"][0]["cells"][0]["value"] = "(60,000)"
    got = glm.coerce(raw)
    assert got["industry"] == "manufacturing" and got["cost_of_sales_basis"]["lines"][0]["cells"][0]["value"] == -60_000.0

    # A correction: other lines ticked, added up again without AI; printed figures can't be re-ticked.
    n = extractor.normalise(by_nature())
    chosen = extractor.recompute_cogs(n, [True, True, True, True, False])
    inc = n["income_statement"]
    assert chosen[-1] == "Employee benefits expense" and inc["values"]["cost_of_sales"] == [-87_500, -81_300]
    assert inc["values"]["gross_profit"] == [33_012, 26_300] and inc["labels"]["cost_of_sales"].startswith("your choice")
    assert inc["cogs_basis"]["method"] == "chosen" and inc["cogs_basis"]["explanation"].startswith("Lines chosen by you")
    extractor.recompute_cogs(n, [False] * 5)
    assert inc["values"]["cost_of_sales"] == [0, 0] and inc["cogs_basis"]["method"] == "none"
    for bad, picks in ((extractor.normalise(FAKE), []), (extractor.normalise(by_nature()), [True])):
        try:
            extractor.recompute_cogs(bad, picks)
            raise AssertionError("expected a refusal")
        except ValueError:
            pass

    # The workbook shows how: a COGS Basis sheet (one entry per filing, a re-run replaces it) and the industry.
    n = extractor.normalise(by_nature())
    meta = {"company_name": "FU YU", "stock_code": "F13", "currency": "SGD", "filing": "1H2026", "industry": "Manufacturing & industrials"}
    data, _ = workbook_bytes(None, n, meta)
    wb = load_workbook(io.BytesIO(data))
    assert wb["Income Statement"]["A2"].value.endswith("· Industry: Manufacturing & industrials")
    rows = [[c for c in r if c is not None] for r in wb["COGS Basis"].iter_rows(values_only=True)]
    assert rows[2] == ["1H2026 — industry: Manufacturing & industrials (precision parts; raw materials consumed)"]
    assert rows[3] == ["Cost of goods sold: judged from expenses by nature, by the built-in Manufacturing & industrials rule"]
    assert ["Raw materials and consumables used", -60_000, -54_000, "Yes", "materials"] in rows
    assert ["Employee benefits expense", -20_000, -18_500, "No", "mostly head office"] in rows
    assert rows[-1][:3] == ["Cost of goods sold", -67_500, -62_800]
    extractor.recompute_cogs(n, [True, True, True, True, False])
    data2, _ = workbook_bytes(data, n, meta)  # the correction replaces the filing's entry
    later, _ = workbook_bytes(data2, extractor.normalise(by_nature()), {**meta, "filing": "FY2026"})
    titles = [r[0].value for r in load_workbook(io.BytesIO(later))["COGS Basis"].iter_rows() if r[0].value and " — industry:" in str(r[0].value)]
    assert titles == ["FY2026 — industry: Manufacturing & industrials (precision parts; raw materials consumed)",
                      "1H2026 — industry: Manufacturing & industrials (precision parts; raw materials consumed)"]
    assert any(r[0].value == "Employee benefits expense" and r[3].value == "Yes" for r in load_workbook(io.BytesIO(data2))["COGS Basis"].iter_rows())
    from sgx_fin import formats as F
    back = F.parse("General", "", filename="a.xlsx", data=data)  # the COGS Basis sheet isn't taken for a statement
    assert "Raw materials and consumables used" not in [r["label"] for r in back["statements"]["income_statement"]["rows"]]

    # The web API: rules in, the basis and the figures out; a correction rebuilds the workbook without AI.
    spec = importlib.util.spec_from_file_location("vapp_cogs", HERE.parent / "api" / "index.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    client = app.app.test_client()
    assert {i["id"] for i in client.get("/api/industries?op=industries").json["industries"]} >= {"manufacturing", "reit", "bank"}
    saved, real, seen = os.environ.get("GEMINI_API_KEY"), extractor.extract, {}
    os.environ["GEMINI_API_KEY"] = "test"

    def fake_extract(*a, cogs=None, **k):
        seen["cogs"] = cogs
        return extractor.Extraction(by_nature(), "fake", {})

    extractor.extract = fake_extract
    try:
        rules = {"companies": [{"match": "F13", "industry": "manufacturing", "cogs": "Materials and subcontracting only"}]}
        r = client.post("/api/process?op=process", content_type="multipart/form-data", data={
            "pdf": (io.BytesIO((HERE / "sample_results.pdf").read_bytes()), "fuyu.pdf"), "stock_code": "F13",
            "issuer": "FU YU CORPORATION LIMITED", "cogs_rules": json.dumps(rules)})
    finally:
        extractor.extract = real
        os.environ.pop("GEMINI_API_KEY", None)
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved
    assert r.status_code == 200 and seen["cogs"]["company"]["cogs"] == "Materials and subcontracting only"
    assert r.json["meta"]["cogs_basis"]["rule"] == "your rule for F13" and r.json["meta"]["industry"] == "Manufacturing & industrials"
    picks = [True, True, True, True, False]
    r2 = client.post("/api/recompute?op=recompute", content_type="multipart/form-data", data={
        "normalised": json.dumps(r.json["normalised"]), "meta": json.dumps(r.json["meta"]), "in_cogs": json.dumps(picks),
        "workbook": (io.BytesIO(base64.b64decode(r.json["workbook_b64"])), "F13.xlsx")})
    assert r2.status_code == 200 and r2.json["chosen"][-1] == "Employee benefits expense"
    assert r2.json["normalised"]["income_statement"]["values"]["cost_of_sales"] == [-87_500, -81_300]
    wb = load_workbook(io.BytesIO(base64.b64decode(r2.json["workbook_b64"])))
    gp_row = next(r for r in wb["Income Statement"].iter_rows(values_only=True) if r[0] == "Gross Profit")
    assert gp_row[1:3] == (33_012, 26_300) and checks.verdict(r2.json["normalised"])["ok"] == r2.json["verdict"]["ok"]
    bad = client.post("/api/recompute?op=recompute", content_type="multipart/form-data",
                      data={"normalised": json.dumps(r.json["normalised"]), "meta": "{}", "in_cogs": "[true]"})
    assert bad.status_code == 400 and "Say in or out for each of the 5 lines" in bad.json["error"]


def test_workbook_and_merge():
    meta = {"company_name": "ACME HOLDINGS LIMITED", "stock_code": "ACME", "currency": "SGD", "filing": "1H2026"}
    norm = extractor.normalise(FAKE)
    assert norm["cash_flow"]["values"]["free_cash_flow"] == [14100, 12250]
    data, checks = workbook_bytes(None, norm, meta)
    assert not [c for c in checks if c["status"] == "FAIL"], checks

    # A later filing (FY2026) merges in as a new column; old columns stay.
    fy = json.loads(json.dumps(FAKE))
    for st in ("income_statement", "cash_flow"):
        fy[st]["periods"] = [{"label": "FY2026", "period_end": "2026-12-31", "months": 12}]
        for r in fy[st]["rows"]:
            r["values"] = [v * 2 for v in r["values"][:1]]
    fy["balance_sheet"]["periods"] = [{"label": "31 Dec 2026", "period_end": "2026-12-31", "months": 0}]
    for r in fy["balance_sheet"]["rows"]:
        r["values"] = r["values"][:1]
    data2, _ = workbook_bytes(data, extractor.normalise(fy), {**meta, "filing": "FY2026"})
    wb = load_workbook(io.BytesIO(data2))
    hdr = [c.value for c in wb["Income Statement"][4]][1:]
    assert hdr == ["FY Dec 2026", "6M Jun 2026", "6M Jun 2025"], hdr
    assert wb["Balance Sheet"]["A1"].value == "ACME HOLDINGS LIMITED (ACME)"
    (HERE / "out").mkdir(exist_ok=True)
    (HERE / "out" / "ACME.xlsx").write_bytes(data2)


def test_mcp_connector():
    """The Claude connector: handshake, picture pages handed to Claude, its reading checked and
    turned into a signed Excel link."""
    import copy
    import importlib.util
    import os
    import shutil
    from urllib.parse import urlparse
    from sgx_fin import mcp_server

    out = HERE / "out" / "mcp"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    pic = picture_pdf(HERE / "sample_results.pdf", out / "photos.pdf")
    spec = importlib.util.spec_from_file_location("vercel_app_mcp", HERE.parent / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = mod.app.test_client()
    old_pw, old_load = os.environ.get("APP_PASSWORD"), mcp_server.load_filing
    os.environ["APP_PASSWORD"] = "s3cret"
    current = {"pdf": pic}
    mcp_server.load_filing = lambda args, ctx: (current["pdf"], scan_pdf(current["pdf"]), None, [current["pdf"].name])
    try:
        def rpc(method, params=None, key="s3cret", mid=1):
            msg = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {}), "params": params or {}}
            return client.post(f"/api/mcp?op=mcp&key={key}", json=msg)

        assert rpc("initialize", key="wrong").status_code == 401
        init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}).get_json()
        assert init["result"]["protocolVersion"] == "2025-06-18" and "pictures" in init["result"]["instructions"].lower()
        assert rpc("notifications/initialized", mid=None).status_code == 202
        names = [t["name"] for t in rpc("tools/list").get_json()["result"]["tools"]]
        assert names == ["screen_sgx_results", "open_filing", "submit_statements", "extract_filing"]
        assert client.get("/api/mcp?op=mcp&key=s3cret").status_code == 405  # no SSE stream

        call = lambda name, args: rpc("tools/call", {"name": name, "arguments": args}).get_json()["result"]  # noqa: E731
        url = "https://links.sgx.com/1.0.0/corporate-announcements/TEST"
        # A photographed filing: Claude first gets small previews to find the statement pages...
        res = call("open_filing", {"announcement_url": url})
        images = [c for c in res["content"] if c["type"] == "image"]
        assert len(images) == 8 and "previews" in res["content"][0]["text"] and not res.get("isError")
        # ...then the pages it picked, cleaned up, as two halves each, with the rules.
        res = call("open_filing", {"announcement_url": url, "pages": [1, 2]})
        assert sum(c["type"] == "image" for c in res["content"]) == 4
        text = res["content"][0]["text"]
        assert "TRANSCRIPTION RULES" in text and "submit_statements" in text and "revenue" in text
        assert call("open_filing", {"announcement_url": url, "pages": [99]}).get("isError")
        # A text filing goes to the server's AI unless Claude asks for images.
        current["pdf"] = HERE / "sample_results.pdf"
        res = call("open_filing", {"announcement_url": url})
        assert "TEXT" in res["content"][0]["text"] and len(res["content"]) == 1

        # Claude's reading, in the extraction JSON (cells by column).
        data = copy.deepcopy(FAKE)
        data["format"] = "general"
        for st in ("income_statement", "balance_sheet", "cash_flow"):
            for i, p in enumerate(data[st]["periods"], 1):
                p["col"] = i
            for row in data[st]["rows"]:
                row["cells"] = [{"col": i, "value": v} for i, v in enumerate(row.pop("values"), 1) if v is not None]
        bad = copy.deepcopy(data)
        bad["income_statement"]["rows"][0]["key"] = "turnover_typo"
        assert call("submit_statements", {"extraction": bad}).get("isError")
        res = call("submit_statements", {"extraction": data, "stock_code": "ACME", "source_pdf": "photos.pdf", "pages": [1, 2, 3]})
        text = res["content"][0]["text"]
        assert not res.get("isError") and "Checks:" in text and "| Revenue |" in text and "120,512" in text
        link = text.split("Excel workbook")[1].split(": ", 1)[1].split()[0]
        u = urlparse(link)
        r = client.get(f"{u.path}?{u.query}")
        assert r.status_code == 200 and r.headers["Content-Disposition"].endswith('ACME.xlsx"')
        wb = load_workbook(io.BytesIO(r.data))
        assert wb["Income Statement"]["B1"].value is not None or wb["Income Statement"]["A1"].value
        assert client.get(f"{u.path}?{u.query.replace('&s=', '&s=0')}").status_code == 403  # tampered signature

        # A misread total comes back with the lines to read again.
        wrong = copy.deepcopy(data)
        for row in wrong["balance_sheet"]["rows"]:
            if row["key"] == "total_assets":
                row["cells"][0]["value"] += 900
        text = call("submit_statements", {"extraction": wrong})["content"][0]["text"]
        assert "FAIL" in text and "total_assets" in text and "Verdict: NOT VERIFIED: 2 tie-out checks fail" in text

        # A good reading is confirmed; highlights are not, and Claude is told what to do about it.
        text = call("submit_statements", {"extraction": data, "filing": "1H2026"})["content"][0]["text"]
        assert "Verdict: OK" in text
        link = text.split("Excel workbook")[1].split(": ", 1)[1].split()[0]
        u = urlparse(link)
        checks_sheet = load_workbook(io.BytesIO(client.get(f"{u.path}?{u.query}").data))["Checks"]
        assert checks_sheet["A1"].value.startswith("OK — 1H2026")  # the verdict travels in the link
        thin = copy.deepcopy(data)
        thin["income_statement"]["rows"] = [r for r in thin["income_statement"]["rows"] if r["key"] in ("revenue", "pat_shareholders")]
        thin["balance_sheet"] = thin["cash_flow"] = {"page_numbers": [], "periods": [], "rows": []}
        text = call("submit_statements", {"extraction": thin})["content"][0]["text"]
        assert "Verdict: NOT VERIFIED: no tie-out check passes" in text and "Gross profit is blank" in text
        assert "No tie-out check passes" in text and "No balance sheet or cash flow statement was read" in text

        # The industry and how COGS was made up come back line by line; the user's rule travels as cogs_rule.
        tools = {t["name"]: t for t in rpc("tools/list").get_json()["result"]["tools"]}
        assert all("cogs_rule" in tools[n]["inputSchema"]["properties"] for n in ("open_filing", "submit_statements", "extract_filing"))
        nature = by_nature()
        nature["format"] = "general"
        for st in ("income_statement", "balance_sheet", "cash_flow"):
            for i, p in enumerate(nature[st]["periods"], 1):
                p["col"] = i
            for row in nature[st]["rows"]:
                row["cells"] = [{"col": i, "value": v} for i, v in enumerate(row.pop("values"), 1) if v is not None]
        rule = {"match": "F13", "industry": "manufacturing", "cogs": "Materials and subcontracting only"}
        text = call("submit_statements", {"extraction": nature, "stock_code": "F13", "filing": "1H2026", "cogs_rule": rule})["content"][0]["text"]
        assert "Industry: Manufacturing & industrials (your rule for F13)" in text
        assert "Cost of goods sold: judged (Manufacturing & industrials): Raw materials and consumables used + Changes" in text
        assert "  - IN  Raw materials and consumables used: materials" in text and "  - out Employee benefits expense: mostly head office" in text
        link = text.split("Excel workbook")[1].split(": ", 1)[1].split()[0]
        u = urlparse(link)
        cogs_sheet = load_workbook(io.BytesIO(client.get(f"{u.path}?{u.query}").data))["COGS Basis"]
        assert any("by your rule for F13" in str(r[0].value) for r in cogs_sheet.iter_rows())
        broken = copy.deepcopy(nature)
        broken["cost_of_sales_basis"]["lines"][0]["cells"] = [{"col": 9, "value": 1}]
        assert "income statement's columns" in call("submit_statements", {"extraction": broken})["content"][0]["text"]

        # A text filing read as images comes with its text layer, digits exact.
        current["pdf"] = HERE / "sample_results.pdf"
        res = call("open_filing", {"announcement_url": url, "as_images": True})
        layers = [c["text"] for c in res["content"] if c["type"] == "text" and "text layer" in c["text"]]
        assert len(layers) == 4 and "120,512" in layers[0]
    finally:
        mcp_server.load_filing = old_load
        if old_pw is None:
            os.environ.pop("APP_PASSWORD", None)
        else:
            os.environ["APP_PASSWORD"] = old_pw


def listing_workbook() -> bytes:
    """A whole listing: three companies laid out alike (S$ million, oldest year first, costs positive)
    and a bank laid out its own way ('000, latest first, costs negative)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    wb.remove(wb.active)
    for name, code, k, extra in (("Alpha Ltd", "A11", 1, False), ("Beta Holdings", "B22", 2, True), ("Gamma Corp", "C33", 3, False)):
        ws = wb.create_sheet(code)
        ws.append([f"{name} ({code})"])
        ws.append(["All figures in S$ million"])
        ws.append(["", "FY2023", "FY2024"])
        ws.append(["Income Statement"])
        for label, a, b in (("Revenue", 1000, 1200), ("Cost of sales", 600, 700), ("Gross profit", 400, 500),
                            ("Administrative expenses", 100, 120), ("Profit before tax", 300, 380),
                            ("Income tax expense", 50, 60), ("Profit for the year", 250, 320)):
            ws.append([label, a * k, b * k])
            if label.startswith(("Gross", "Profit")):
                ws.cell(ws.max_row, 1).font = Font(bold=True)
        if extra:
            ws.append(["Other gains", 5, 6])
        ws.append(["Balance Sheet"])
        for label in ("Total assets", "Total liabilities", "Total equity"):
            ws.append([label, 5000, 5500])
            ws.cell(ws.max_row, 1).font = Font(bold=True)
        ws.append(["Cash Flow"])
        ws.append(["Net cash from operating activities", 300, 350])
        ws.append(["Purchase of property, plant and equipment", 80, 90])
    ws = wb.create_sheet("D05")
    ws.append(["DBS Group Holdings Ltd (D05)"])
    ws.append(["S$'000", "FY2024", "FY2023"])
    ws.append(["Income Statement"])
    for label, v in (("Interest income", 900), ("Interest expense", -400), ("Net interest income", 500),
                     ("Operating expenses", -250), ("Allowances", -50), ("Profit before tax", 200), ("Tax", -30),
                     ("Net profit", 170)):
        ws.append([label, v, v * 0.9])
    ws.append(["Balance Sheet"])
    for label in ("Cash and balances with central banks", "Loans and advances", "Total assets", "Deposits", "Total liabilities"):
        ws.append([label, 100, 90])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_standards_learnt_from_the_users_workbooks():
    from sgx_fin import standards as S, template as T

    # The AI's review: names, a key, the outflows and a sum; the program checks the sum against the figures.
    def review(clusters, deadline=None):
        rows = [{"statement": "income_statement", "row": 2, "key": "gross_profit", "kind": "total", "outflow": False,
                 "formula": "Revenue - Cost of sales"},
                {"statement": "income_statement", "row": 0, "key": "revenue", "kind": "total", "outflow": False,
                 "formula": "Cost of sales + 3"}]  # doesn't add up: dropped
        return {1: {"name": "Expenses by function", "applies_to": "Industrials", "company_type": "general", "unit": "millions",
                    "unit_evidence": "", "latest_first": True, "notes": "", "rows": rows},
                2: {"name": "Bank", "applies_to": "Banks", "company_type": "bank", "unit": "unknown", "unit_evidence": "",
                    "latest_first": True, "notes": "", "rows": []}}, "mock"

    old = S.ai_review
    S.ai_review = review
    try:
        res = S.learn("listing.xlsx", listing_workbook())
    finally:
        S.ai_review = old
    assert res["companies"] == 4 and len(res["standards"]) == 2, res
    one, bank = res["standards"]
    assert sorted(c["code"] for c in one["companies"]) == ["A11", "B22", "C33"]  # standard 1 for A11, B22 and C33
    assert [c["code"] for c in bank["companies"]] == ["D05"] and bank["companies"][0]["name"] == "DBS Group Holdings Ltd"
    c = one["conventions"]
    assert (c["unit"], c["latest_first"], c["expenses"]) == ("millions", False, "positive"), c  # the file's words and headers win
    assert one["formulas"] == ["Gross profit = Revenue - Cost of sales"]
    rows = {r["label"]: r for r in one["format"]["statements"]["income_statement"]["rows"]}
    assert rows["Cost of sales"] == {"key": "cost_of_sales", "label": "Cost of sales", "kind": "item", "sign": -1}
    assert rows["Gross profit"]["formula"] == "Revenue + Cost of sales"  # in the app's signs (costs negative)
    assert rows["Income tax expense"]["key"] == "tax" and rows["Other gains"]["kind"] == "item"  # only Beta has it: kept
    assert [r["label"] for r in one["format"]["statements"]["cash_flow"]["rows"]][1] == "Purchase of property, plant and equipment"
    assert (bank["company_type"], bank["conventions"]["unit"], bank["conventions"]["latest_first"],
            bank["conventions"]["expenses"]) == ("bank", "thousands", True, "negative")
    assert bank["missing"] == ["Cash Flow"]  # filled from the built-in bank lines
    assert S.learn("listing.xlsx", listing_workbook(), use_ai=False)["standards"][0]["id"] == one["id"]  # same layout, same id

    # A CSV listing with a company column.
    csv_text = "Stock Code,Company,Statement,Line item,FY2025,FY2024\n" + "".join(
        f"{code},{name},Income Statement,{label},{v},{v}\n"
        for code, name in (("X11", "Ex Ltd"), ("Y22", "Why Ltd"))
        for label, v in (("Turnover", 10), ("Cost of sales", -6), ("Gross profit", 4), ("Taxation", -1), ("Net profit", 3)))
    res = S.learn("all.csv", csv_text.encode(), use_ai=False)
    (std,) = res["standards"]
    assert sorted(c["code"] for c in std["companies"]) == ["X11", "Y22"]
    assert std["conventions"]["latest_first"] is True and std["conventions"]["expenses"] == "negative"
    keys = [r.get("key") for r in std["format"]["statements"]["income_statement"]["rows"]]
    assert keys[:2] == ["revenue", "cost_of_sales"], keys

    # A workbook this app made is read back too (its company from A1).
    made, _ = workbook_bytes(None, extractor.normalise(FAKE), {"company_name": "ACME HOLDINGS LIMITED", "stock_code": "ACME",
                                                                "currency": "SGD"})
    (mine,) = S.learn("ACME.xlsx", made, use_ai=False)["standards"]
    assert mine["companies"][0]["code"] == "ACME" and mine["conventions"]["unit"] == "thousands"
    assert mine["conventions"]["latest_first"] is True and mine["conventions"]["expenses"] == "negative"

    # A new filing of A11 comes out in its standard: millions, oldest first, costs positive, the file's labels.
    fmt = S.chosen(json.dumps(one["format"]))
    formats = {fmt["id"]: fmt}
    norm = extractor.normalise(FAKE, formats)
    assert norm["income_statement"]["format"] == one["id"]
    data, checks = workbook_bytes(None, norm, {"company_name": "Alpha Ltd", "stock_code": "A11", "currency": "SGD"}, formats)
    assert not [c for c in checks if c["status"] == "FAIL"], checks  # checks still run on the app's own figures
    ws = load_workbook(io.BytesIO(data))["Income Statement"]
    assert "SGD m" in ws["A2"].value
    assert [c.value for c in ws[4]][1:] == ["6M Jun 2025", "6M Jun 2026"]  # oldest first
    got = {r[0].value: [c.value for c in r[1:]] for r in ws.iter_rows(min_row=8) if r[0].value}
    assert got["Revenue"] == [107.6, 120.512] and got["Cost of sales"] == [72.1, 80.412], got  # costs positive
    assert got["Income tax expense"] == [3.05, 3.7] and got["Gross profit"] == [35.5, 40.1], got
    assert ws["B8"].number_format.startswith("#,##0.0")
    # Built-in formats are unchanged: '000, latest first, costs negative.
    plain = load_workbook(io.BytesIO(workbook_bytes(None, extractor.normalise(FAKE), {"company_name": "A"})[0]))["Income Statement"]
    assert "'000" in plain["A2"].value and plain["C9"].value == -72100

    # The web API: upload -> standards; the company's standard sent with a recompute writes in it.
    import importlib.util
    spec = importlib.util.spec_from_file_location("vercel_app_std", HERE.parent / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = mod.app.test_client()
    S.ai_review, old = (lambda clusters, deadline=None: ({}, "")), S.ai_review
    try:
        r = client.post("/api/learn_standard?op=learn_standard", data={"file": (io.BytesIO(listing_workbook()), "listing.xlsx")})
    finally:
        S.ai_review = old
    assert r.status_code == 200 and len(r.get_json()["standards"]) == 2, r.get_json()
    bad = client.post("/api/learn_standard?op=learn_standard", data={"file": (io.BytesIO(b"x"), "notes.pdf")})
    assert bad.status_code == 400
    nat = extractor.normalise(by_nature(), formats)
    r = client.post("/api/recompute?op=recompute", data={
        "normalised": json.dumps(nat), "meta": json.dumps({"company_name": "Alpha Ltd", "currency": "SGD", "format": one["id"]}),
        "in_cogs": "[true, true, false, false, false]", "standard": json.dumps(one["format"])})
    assert r.status_code == 200, r.get_json()
    import base64
    ws = load_workbook(io.BytesIO(base64.b64decode(r.get_json()["workbook_b64"])))["Income Statement"]
    assert "SGD m" in ws["A2"].value


def test_connector_uses_the_users_standards():
    """The Claude connector writes in the user's standard when the call carries its token: one company's
    (the web app's message) or a pack of them all (pasted by the user)."""
    import copy
    import importlib.util
    import os
    from urllib.parse import urlparse
    from sgx_fin import mcp_server, standards as S

    found = S.learn("listing.xlsx", listing_workbook(), use_ai=False)["standards"]
    one = found[0]  # A11, B22, C33: millions, oldest first, costs positive
    spec = importlib.util.spec_from_file_location("vercel_app_std_mcp", HERE.parent / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = mod.app.test_client()
    old_pw, old_load = os.environ.get("APP_PASSWORD"), mcp_server.load_filing
    os.environ.pop("APP_PASSWORD", None)
    pdf = HERE / "sample_results.pdf"
    mcp_server.load_filing = lambda args, ctx: (pdf, scan_pdf(pdf), None, [pdf.name])
    try:
        # The web app gets the tokens from the server: each standard's own, and a pack with the companies.
        r = client.post("/api/standards_token?op=standards_token",
                        json={"standards": [{"format": x["format"], "companies": x["companies"]} for x in found]})
        tokens, pack = r.get_json()["tokens"], r.get_json()["pack"]
        assert tokens[0] == one["token"] and pack.startswith("sgxstd1.")

        def call(name, args):
            msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            return client.post("/api/mcp?op=mcp", json=msg).get_json()["result"]

        props = {t["name"]: t["inputSchema"]["properties"] for t in mcp_server.TOOLS}
        assert all("standard" in props[n] for n in ("open_filing", "submit_statements", "extract_filing"))
        assert "sgxstd1." in mcp_server.INSTRUCTIONS
        url = "https://links.sgx.com/1.0.0/corporate-announcements/TEST"
        # Read into the user's lines: the rules name that format only.
        res = call("open_filing", {"announcement_url": url, "as_images": True, "standard": pack, "stock_code": "A11"})
        text = res["content"][0]["text"]
        assert f"# format {one['id']}" in text and "# format general" not in text, text[-3000:]
        assert "Written in your standard" in text and "millions, oldest period first" in text

        data = copy.deepcopy(FAKE)
        data["format"] = one["id"]
        for st in ("income_statement", "balance_sheet", "cash_flow"):
            for i, p in enumerate(data[st]["periods"], 1):
                p["col"] = i
            for row in data[st]["rows"]:
                row["cells"] = [{"col": i, "value": v} for i, v in enumerate(row.pop("values"), 1) if v is not None]
        # minority / owners lines aren't in this user's standard: the format's own keys only
        bad = call("submit_statements", {"extraction": data, "standard": pack, "stock_code": "A11"})
        assert bad.get("isError") and "is not a key of the" in bad["content"][0]["text"]
        keys = {r.get("key") for st in one["format"]["statements"].values() for r in st["rows"]}
        for st in ("income_statement", "balance_sheet", "cash_flow"):
            data[st]["rows"] = [r for r in data[st]["rows"] if r["key"] in keys]
        res = call("submit_statements", {"extraction": data, "standard": pack, "stock_code": "A11", "company_name": "Alpha Ltd"})
        text = res["content"][0]["text"]
        assert not res.get("isError") and "your standard “Standard 1”" in text, text
        link = text.split("Excel workbook")[1].split(": ", 1)[1].split()[0]
        u = urlparse(link)
        ws = load_workbook(io.BytesIO(client.get(f"{u.path}?{u.query}").data))["Income Statement"]
        assert "SGD m" in ws["A2"].value and [c.value for c in ws[4]][1:] == ["6M Jun 2025", "6M Jun 2026"]
        got = {row[0].value: [c.value for c in row[1:]] for row in ws.iter_rows(min_row=8) if row[0].value}
        assert got["Cost of sales"] == [72.1, 80.412], got  # costs positive, the user's labels

        # One company's token (from the web app's message) needs no stock code.
        res = call("submit_statements", {"extraction": data, "standard": one["token"]})
        assert "your standard" in res["content"][0]["text"]
        # A company the pack doesn't list keeps the built-in layout, and Claude is told so.
        plain = copy.deepcopy(FAKE)
        plain["format"] = "general"
        for st in ("income_statement", "balance_sheet", "cash_flow"):
            for i, p in enumerate(plain[st]["periods"], 1):
                p["col"] = i
            for row in plain[st]["rows"]:
                row["cells"] = [{"col": i, "value": v} for i, v in enumerate(row.pop("values"), 1) if v is not None]
        text = call("submit_statements", {"extraction": plain, "standard": pack, "stock_code": "Z74"})["content"][0]["text"]
        assert "don't include Z74" in text and "general format" in text
        # A token copied wrongly is refused with what to do.
        res = call("submit_statements", {"extraction": plain, "standard": pack[:-12]})
        assert res.get("isError") and "Copy the token again exactly" in res["content"][0]["text"]
    finally:
        mcp_server.load_filing = old_load
        if old_pw is not None:
            os.environ["APP_PASSWORD"] = old_pw


def test_only_listed_companies_are_screened():
    """Results filed by issuers of bonds and notes alone (not listed companies) are left out."""
    from sgx_fin import listed
    from sgx_fin.sgx_client import _parse_announcement

    rows = listed.bundled()
    codes = {c for c, _ in rows}
    assert len(rows) > 600 and {"D05", "O39", "Z74", "1E3", "5E2", "N33"} <= codes  # sheet codes as typed, fixed
    assert not any("E+" in c for c in codes)
    companies = listed.load(live=False)
    fs = "Financial Statements and Related Announcement::"
    item = lambda issuer, *c: {"title": fs + "Half Yearly Results", "issuer_name": issuer, "url": "https://links.sgx.com/a",
                               "issuers": [{"stock_code": x, "issuer_name": issuer} for x in c]}
    anns = [_parse_announcement(i) for i in [
        item("MIYOSHI LIMITED", "M03"), item("BANK OF BARODA", "2V6B"), item("GRAB HOLDINGS LIMITED", "52WB"),
        item("NOMURA HOLDINGS, INC", "HRMB", "N33"),  # shares and bonds in one filing: the share code is shown
        item("NO VA LAND INVESTMENT GROUP CORPORATION"), item("KSH HOLDINGS LIMITED")]]  # no code: by name
    kept, dropped = listed.split(anns, companies)
    assert [(a.issuer, a.stock_code) for a in kept] == [
        ("MIYOSHI LIMITED", "M03"), ("NOMURA HOLDINGS, INC", "N33"), ("KSH HOLDINGS LIMITED", "")]
    assert [a.issuer for a in dropped] == ["BANK OF BARODA", "GRAB HOLDINGS LIMITED", "NO VA LAND INVESTMENT GROUP CORPORATION"]
    # A new listing (IPO) is kept once SGX's live list carries its code.
    ipo = _parse_announcement(item("NEWCO HOLDINGS LIMITED", "ZZ9"))
    assert not listed.is_listed(ipo, companies)
    assert listed.is_listed(ipo, listed.Listed(companies.codes | {"ZZ9"}, companies.names, "test"))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
