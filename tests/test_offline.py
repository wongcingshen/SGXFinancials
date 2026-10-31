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
    vals = sch["properties"]["balance_sheet"]["properties"]["rows"]["items"]["properties"]["values"]
    assert vals == {"type": "array", "items": {"type": "number", "nullable": True}}
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
