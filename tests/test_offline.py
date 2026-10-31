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
