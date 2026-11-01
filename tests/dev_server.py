"""Run the web app locally.

  python tests/dev_server.py          # real SGX + Claude (needs ANTHROPIC_API_KEY)
  python tests/dev_server.py --mock   # demo data: fake SGX listing, sample PDF, canned extraction

Then open http://127.0.0.1:5000
"""

import argparse
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

    from sgx_fin import extractor, pipeline
    from sgx_fin.sgx_client import Announcement, SGXClient
    from test_offline import FAKE

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
    ]
    named = {
        "NVA": ["20261030_NVLG_CONSOLIDATED_FS_2026Q3.pdf", "20261030_NVLG_EXPLANATION_CONSOL_FS_2026Q3.pdf"],
        "NVB": ["20261030_NVLG_SEPARATE_FS_2026Q3.pdf", "20261030_NVLG_EXPLANATION_SEP_FS_2026Q3.pdf"],
        "ABA": ["ABC FY2026 Results A.pdf"], "ABB": ["ABC FY2026 Results B.pdf"],
    }
    separate = ROOT / "tests" / "separate_results.pdf"
    notice = ROOT / "tests" / "notice.pdf"

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
        src = notice if ("KSH" in url or "NTC" in url) else separate if ("NVB" in url or "ABB" in url) else sample
        shutil.copy(src, out)
        return out

    def extract(pdf, pages, **kw):
        time.sleep(2.5)
        data = {**FAKE, "notes": "Mock extraction from the sample PDF; figures are illustrative."}
        return extractor.Extraction(data, "mock " + extractor.default_model(), {"input_tokens": 6120, "output_tokens": 2310})

    SGXClient.screen, SGXClient.attachments, SGXClient.download = screen, attachments, download
    pipeline.extractor.extract = extract


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    if a.mock:
        install_mocks()
    load_app().run(host="127.0.0.1", port=a.port, debug=False, threaded=True)
