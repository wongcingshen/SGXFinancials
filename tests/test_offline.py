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
    from sgx_fin import glm

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
    assert "Interest Income" in col_a and "Cost of Goods Sold" not in col_a
    names = {c["check"] for c in checks}
    assert not any(n.startswith("Revenue") for n in names), "general checks skipped for a bank"
    assert any(n.startswith("Interest Income") for n in names)

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
