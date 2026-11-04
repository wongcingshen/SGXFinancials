"""Run the web app locally.

  python tests/dev_server.py          # real SGX + Claude (needs ANTHROPIC_API_KEY)
  python tests/dev_server.py --mock   # demo data: fake SGX listing, sample PDF, canned extraction

Then open http://127.0.0.1:5000
"""

import argparse
import json
import re
import importlib.util
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def load_app():
    spec = importlib.util.spec_from_file_location("vercel_app", ROOT / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.app


def install_mocks():
    import os

    from sgx_fin import extractor, pictures, pipeline
    from sgx_fin.sgx_client import Announcement, SGXClient
    from test_offline import FAKE, bob_reading, bob_tables, picture_pdf

    if not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("GEMINI_API_KEY", "mock")
    sample = ROOT / "tests" / "sample_results.pdf"
    issuers = [
        ("D05", "DBS GROUP HOLDINGS LTD", "18:02", "Financial Statements and Related Announcement::Third Quarter Results"),
        ("Z74", "SINGAPORE TELECOMMUNICATIONS LIMITED", "17:45", "Financial Statements and Related Announcement::Half Yearly Results"),
        ("C6L", "SINGAPORE AIRLINES LIMITED", "17:30", "Financial Statements and Related Announcement::Half Yearly Results"),
        ("U11", "UNITED OVERSEAS BANK LIMITED", "07:05", "Financial Statements and Related Announcement::Third Quarter Results"),
        ("BN4", "KEPPEL LTD.", "07:00", "Financial Statements and Related Announcement::Third Quarter Business Update"),
        ("5JK", "HIAP HOE LIMITED", "21:14", "Financial Statements and Related Announcement::Half Yearly Results"),
        ("F13", "FU YU CORPORATION LIMITED", "19:40", "Financial Statements and Related Announcement::Third Quarter Results"),
        # a notice of the results date: set aside by its title
        ("KSH", "KSH HOLDINGS LIMITED", "17:48", "Financial Statements and Related Announcement::Notification of Results Release"),
        # titled as results, but the PDF is a notice: caught by the content check before any AI call
        ("NTC", "NOTICE CO LTD", "08:00", "Financial Statements and Related Announcement::Half Yearly Results"),
        # the same quarter filed twice: consolidated and separate (company-only) statements
        ("NVA", "NO VA LAND INVESTMENT GROUP CORPORATION", "21:18", "Financial Statements and Related Announcement::Third Quarter Results"),
        ("NVB", "NO VA LAND INVESTMENT GROUP CORPORATION", "21:16", "Financial Statements and Related Announcement::Third Quarter Results"),
        # a pair whose file names don't say which is which: the statement headings decide
        ("ABA", "ABC HOLDINGS LTD", "20:00", "Financial Statements and Related Announcement::Full Yearly Results"),
        ("ABB", "ABC HOLDINGS LTD", "20:01", "Financial Statements and Related Announcement::Full Yearly Results"),
        # statements that are scanned pictures: found, copied, checked and re-read
        ("2V6B", "BANK OF BARODA", "19:39", "Financial Statements and Related Announcement::Second Quarter and/ or Half Yearly Results"),
        # a results announcement whose only PDF is a slide deck: set aside before any AI call
        ("PPT", "PRESENTATION ONLY LTD", "18:30", "Financial Statements and Related Announcement::Half Yearly Results"),
        # an extraction nothing confirms (highlights only, no balance sheet or cash flow): Check failed
        ("HLT", "HIGHLIGHTS HOLDINGS LTD", "18:40", "Financial Statements and Related Announcement::Third Quarter Results"),
    ]
    named = {
        "NVA": ["20261030_NVLG_CONSOLIDATED_FS_2026Q3.pdf", "20261030_NVLG_EXPLANATION_CONSOL_FS_2026Q3.pdf"],
        "NVB": ["20261030_NVLG_SEPARATE_FS_2026Q3.pdf", "20261030_NVLG_EXPLANATION_SEP_FS_2026Q3.pdf"],
        "ABA": ["ABC FY2026 Results A.pdf"], "ABB": ["ABC FY2026 Results B.pdf"],
        "PPT": ["1H FY2026 Results.pdf"],
    }
    separate = ROOT / "tests" / "separate_results.pdf"
    notice = ROOT / "tests" / "notice.pdf"
    deck = ROOT / "tests" / "deck_results.pdf"
    (ROOT / "tests" / "out").mkdir(exist_ok=True)
    scanned = picture_pdf(sample, ROOT / "tests" / "out" / "mock_SESGXFINAL.pdf")

    def screen(self, s, company=None, attachments=False):
        time.sleep(0.8)
        day = s.date_to.strftime("%Y-%m-%d")
        def files(c):
            if not attachments:
                return None
            names = named.get(c) or ([f"{c} Results.pdf"] if c != "D05" else [  # one filing with several attachments
                "20261029 - Media Release.pdf", "20261029 Financial Results Presentation.pdf",
                "20261029 - Half Yearly Results.pdf", "29/10/2026 18:24:53"])
            return [{"name": n, "url": f"https://links.sgx.com/FileOpen/{c}_{i}.ashx?App=Announcement&FileID={i}",
                     "from": "announcement-attachment-list"} for i, n in enumerate(names)]
        anns = [Announcement(t, n, c, f"https://links.sgx.com/1.0.0/corporate-announcements/MOCK{c}", f"{day} {h}", "ANNC17",
                             {"attachments": files(c)})
                for c, n, h, t in issuers]
        if company:
            anns = [a for a in anns if company.lower() in a.issuer.lower() or company.lower() == a.stock_code.lower()]
        return anns

    def attachments(self, ann):
        return [ann.url + "/results.pdf"]

    def download(self, url, dest_dir):
        time.sleep(0.6)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / (re.sub(r"[^A-Za-z0-9_.-]+", "_", url.split("/FileOpen/")[-1].split(".ashx")[0])[-60:] + ".pdf")
        src = (notice if ("KSH" in url or "NTC" in url) else separate if ("NVB" in url or "ABB" in url)
               else scanned if "2V6B" in url else deck if "PPT" in url else sample)
        shutil.copy(src, out)
        return out

    def extract(pdf, pages, **kw):
        time.sleep(2.5)
        data = json.loads(json.dumps({**FAKE, "notes": "Mock extraction from the sample PDF; figures are illustrative."}))
        if "F13" in pdf.name:  # total assets misread by 300: two checks fail, on the second look too
            data["balance_sheet"]["rows"][2]["values"][0] += 300
        code = pdf.name.split("_")[0]
        data["industry"] = {"D05": "bank", "U11": "bank", "Z74": "technology", "C6L": "transport", "5JK": "property",
                            "NVA": "property", "ABA": "trading"}.get(code, "manufacturing")
        data["industry_evidence"] = "mock: chosen by stock code"
        data["cost_of_sales_basis"] = {"method": "printed", "lines": [], "explanation": "Cost of sales as printed."}
        if code == "C6L":  # expenses by nature, no cost of sales: the AI judges the COGS line by line, the program adds it up
            data["industry_evidence"] = "passenger and cargo flights; fuel, handling and aircraft maintenance"
            data["income_statement"]["rows"] = [r for r in data["income_statement"]["rows"] if r["key"] not in ("cost_of_sales", "gross_profit")]
            cell = lambda a, b: [{"col": 1, "value": a}, {"col": 2, "value": b}]  # noqa: E731
            data["cost_of_sales_basis"] = {"method": "by_nature", "explanation": (
                "No cost of sales is printed. The costs of flying are in: fuel, crew and operating staff, aircraft depreciation, "
                "handling, maintenance and inflight costs; sales costs and head-office administration are left out."), "lines": [
                {"source_label": "Fuel costs", "cells": cell(-28000, -25500), "in_cogs": True, "why": "fuel for the flights"},
                {"source_label": "Staff costs", "cells": cell(-21000, -19000), "in_cogs": True, "why": "crew and operating staff"},
                {"source_label": "Depreciation", "cells": cell(-12000, -11500), "in_cogs": True, "why": "mostly aircraft"},
                {"source_label": "Handling charges", "cells": cell(-6500, -6000), "in_cogs": True, "why": "airport handling"},
                {"source_label": "Aircraft maintenance and overhaul costs", "cells": cell(-5500, -5000), "in_cogs": True, "why": "fleet upkeep"},
                {"source_label": "Inflight meals and other passenger costs", "cells": cell(-3200, -2900), "in_cogs": True, "why": "direct service cost"},
                {"source_label": "Sales costs", "cells": cell(-4100, -3800), "in_cogs": False, "why": "selling, not flying"},
                {"source_label": "Administrative expenses", "cells": cell(-2800, -2600), "in_cogs": False, "why": "head office"}]}
        if "HLT" in pdf.name:  # highlights only: revenue and profit, nothing a printed total confirms
            data["income_statement"]["rows"] = [r for r in data["income_statement"]["rows"]
                                                if r["key"] in ("revenue", "pat_shareholders", "eps_basic")]
            data["balance_sheet"] = data["cash_flow"] = {"page_numbers": [], "periods": [], "rows": []}
        return extractor.Extraction(data, "mock " + extractor.default_model(), {"input_tokens": 6120, "output_tokens": 2310})

    def locate(pdf, pages, **kw):
        time.sleep(1.2)
        found = [{"page": 5, "heading": "Un-Audited Consolidated Financial Results", "statements": ["income_statement"], "basis": "consolidated", "turn": "0"},
                 {"page": 6, "heading": "Statement of Assets & Liabilities", "statements": ["balance_sheet", "cash_flow"], "basis": "consolidated", "turn": "90"}]
        return pictures.Located([5, 6], found, "consolidated", "mock gemini-3.5-flash-lite")

    def read(pdf, pages, turns=None, **kw):
        time.sleep(2.0)
        tables = pictures.clean_tables(bob_tables(misread=False), pages)
        row = next(i for i, r in enumerate(tables[0]["rows"]) if r["label"].startswith("Operating Expenses"))
        fixes = [{"page": 5, "table": 0, "row": row, "label": tables[0]["rows"][row]["label"], "col": c, "was": was, "now": now}
                 for c, was, now in ((4, "1584381", "1988070"), (6, "2877332", "3871744"))]
        return pictures.Reading(pages, tables, "mock gemini-3.5-flash", {"input_tokens": 9000, "output_tokens": 4000},
                                corrections=fixes, turned={6: 90})

    def extract_text(pdf, pages, tables, **kw):
        time.sleep(1.0)
        data = bob_reading(16082312)
        for st, nums in (("income_statement", [5]), ("balance_sheet", [6]), ("cash_flow", [6])):
            data[st]["page_numbers"] = nums  # original page numbers, as the template step is told
        return extractor.Extraction(data, "mock gemini-3.5-flash-lite", {"input_tokens": 3000, "output_tokens": 900})

    def review(clusters, deadline=None):  # the AI's review of uploaded standards: names and types only
        time.sleep(1.0)
        from sgx_fin.standards import guess_type
        return {n: {"name": f"{guess_type(c).title()} layout", "company_type": guess_type(c), "rows": [],
                    "notes": "Mock review: the program's own reading of the lines."}
                for n, c in enumerate(clusters, 1)}, "mock " + extractor.default_model()

    from sgx_fin import standards
    standards.ai_review = review
    SGXClient.screen, SGXClient.attachments, SGXClient.download = screen, attachments, download
    pipeline.extractor.extract = extract
    pictures.locate, pictures.read, extractor.extract_text = locate, read, extract_text


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    if a.mock:
        install_mocks()
    load_app().run(host="127.0.0.1", port=a.port, debug=False, threaded=True)
