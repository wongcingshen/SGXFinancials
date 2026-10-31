# SGX Financial Results → ShareInvestor-style Excel

Automatically retrieve financial results of SGX-listed companies and turn the three primary
statements (income statement, balance sheet, cash flow) into an Excel workbook laid out like
the ShareInvestor.com Financials pages.

```
SGX page URL ──► announcements ──► PDF attachments ──► keyword page scan (no AI)
 from / to / ANNC17                                        │ picks only the statement pages
                                                           ▼
                     Excel (Income Statement / Balance Sheet / Cash Flow / Checks / Source Map)
                                                           ▲
                                     Claude reads just those pages ──┘
```

## How it works

1. **Screen**: uses the SGX announcements page URL format, e.g.
   `https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17`
   (`from`/`to` = Singapore dates, `ANNC17` = Financial Statements). The page URL is translated
   into the same `api.sgx.com` query the SGX site makes.
2. **Download**: opens each announcement on `links.sgx.com` and downloads its PDF attachments.
3. **Non-AI page scan** (`sgx_fin/page_scanner.py`): scores every page against keyword sets
   (statement titles such as *"statement of financial position"*, line items such as *"total
   assets"*, *"net cash from operating activities"*, plus numeric density) and penalises notes,
   commentary and contents pages. Picks the best attachment and the 1–3 pages for each statement,
   including continuation pages.
4. **AI extraction** (`sgx_fin/extractor.py`): only the selected pages are cut into a small PDF
   and sent to Claude (`claude-opus-5-5`) with a strict JSON schema built from the template, so
   every number lands on a known line. Group (consolidated) figures only, with the company's own label kept for audit.
5. **Excel** (`sgx_fin/excel_writer.py`): rescales to `'000`, writes ShareInvestor-style sheets,
   runs tie-out checks (assets = liabilities + equity, op + inv + fin = net change in cash, …)
   and **merges** new periods into an existing workbook so quarters/half-years/full years accumulate.

The row layout lives in **`sgx_fin/template.py`**. Edit labels, order or add rows there to match
your ShareInvestor view exactly; nothing else needs changing.

## Deploy to Vercel

1. Import this GitHub repo in Vercel (**Add New → Project**). Framework preset: **Other**. No build settings needed.
2. In **Settings → Environment Variables** add:

   | Variable | Required | Purpose |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | yes | Claude API key for extraction |
   | `APP_PASSWORD` | strongly recommended | Anyone with the URL could otherwise spend your API credits |
   | `SGX_AUTH_TOKEN` | only if needed | See *SGX token* below |
   | `CLAUDE_MODEL` | no | Override the model (default `claude-opus-5-5`) |

3. Deploy. Open the site, enter the password, pick a date and click **Screen**, then
   **Scan** (free, keyword scan only) or **Extract** (AI) on any announcement.

Vercel notes: `vercel.json` sets `maxDuration: 300` s for the API (extraction usually takes
1–3 min). Request bodies are capped at 4.5 MB by Vercel, so large PDFs should be given by SGX link
rather than uploaded. Vercel has no storage, so each run returns a workbook to download; upload
your previous workbook in **Options** to merge new periods into it.

## Automatic daily retrieval (GitHub Actions)

`.github/workflows/daily-sgx.yml` runs on weekdays at 23:30 SGT:

1. Add repo secrets: **Settings → Secrets and variables → Actions** → `ANTHROPIC_API_KEY`
   (and `SGX_AUTH_TOKEN` if needed).
2. List the companies to track in `watchlist.txt` (stock codes or name fragments).
3. Each run screens that day's `ANNC17` announcements, extracts watchlist companies and commits
   `output/<CODE>.xlsx`. You can also run it manually (**Actions → Daily SGX results → Run
   workflow**) with any SGX page URL to backfill a date range.

With an empty watchlist it only logs the day's announcements, so it never spends API credits by surprise.

## Command line

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...

# list financial-results announcements for a screen
python -m sgx_fin screen "https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17"

# full pipeline for a date range, one company
python -m sgx_fin run --from 2026-10-01 --to 2026-10-31 --company "DBS GROUP"

# keyword scan only (no AI cost) — see which pages would be sent
python -m sgx_fin run --date 2026-10-30 --dry-run

# a PDF you already have
python -m sgx_fin pdf results.pdf --code D05 [--pages 7,8,9,10]
```

Workbooks go to `output/`. Extractions are cached per PDF excerpt, so re-runs are free.

## SGX token

`api.sgx.com` is the backend of the public SGX website. If it starts answering 401/403, it wants
the `authorizationtoken` header the website sends. Either:

- copy it from your browser (open the SGX announcements page → DevTools → Network → the request
  to `api.sgx.com/announcements` → Request Headers → `authorizationtoken`) into `SGX_AUTH_TOKEN`, or
- for the CLI/GitHub Action, `pip install playwright && playwright install chromium`; the tool
  then opens the page URL headlessly and captures the token automatically.

SGX's terms of use apply to the data you retrieve; keep request volumes modest.

## Output workbook

| Sheet | Contents |
|---|---|
| Income Statement / Balance Sheet / Cash Flow | ShareInvestor-style rows, one column per period (latest first), `'000` of reporting currency, EPS in cents |
| Checks | Arithmetic tie-outs per period: PASS / FAIL / n/a |
| Source Map | For every figure: the company's own line label, filing, PDF, pages and model |

## Tests

```bash
python tests/test_offline.py               # scanner, URL mapping, schema, Excel merge
NODE_PATH=$(npm root -g) node tests/make_sample_pdf.js   # regenerate the sample PDF (needs playwright)
```

## Limits

- Scanned (image-only) PDFs have no text layer, so the keyword scan cannot locate pages; the scan
  reports them and you can pass `--pages` / "Override pages" manually.
- Banks, insurers and REITs use different statement formats; figures map to the closest lines and
  the AI notes the format in the **notes** field. Check the **Checks** sheet before relying on numbers.
