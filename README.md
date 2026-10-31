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
                         Gemini (or Claude) reads just those pages ──┘
```

## How it works

1. **Screen** (`scraper/sgx.mjs`): a headless browser opens the SGX announcements page URL, e.g.
   `https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17`
   (`from`/`to` = Singapore dates, `ANNC17` = Financial Statements), exactly as a person would,
   and reads the result list: the data the page loads for itself (paged through with the page's
   own token), or else the links in the result table. SGX's `api.sgx.com` refuses direct requests
   without that page-generated token, which is why a browser is used.
2. **Open every link under "Title"** (clicking titles that aren't plain links) and list the files
   inside the announcement page's `announcement-attachment-list` element.
3. **Pick the financial statements PDF** (no AI): attachments are tried by name, results /
   financial statements first and presentations, slides and media releases last, and the first
   one whose pages hold all three statements is used; the others are never downloaded. The card
   shows which PDF was extracted.
4. **Non-AI page scan** (`sgx_fin/page_scanner.py`): scores every page against keyword sets
   (statement titles such as *"statement of financial position"*, line items such as *"total
   assets"*, *"net cash from operating activities"*, plus numeric density) and penalises notes,
   commentary and contents pages. Picks the best attachment and the 1–3 pages for each statement,
   including continuation pages.
5. **AI extraction** (`sgx_fin/extractor.py`): only the selected pages are cut into a small PDF
   and sent to the AI with a strict JSON schema built from the template, so
   every number lands on a known line. Group (consolidated) figures only, with the company's own label kept for audit.
6. **Excel** (`sgx_fin/excel_writer.py`): rescales to `'000`, writes ShareInvestor-style sheets,
   runs tie-out checks (assets = liabilities + equity, op + inv + fin = net change in cash, …)
   and **merges** new periods into an existing workbook so quarters/half-years/full years accumulate.

**No cost of sales line?** Companies that present expenses by nature report no cost of sales or
gross profit. The AI then extracts *Raw materials, consumables used and changes in inventories*,
*Employee benefits expenses* and *Depreciation and amortisation*, and the program derives
**Cost of Goods Sold** = the sum of those three and **Gross Profit** = Revenue − Cost of Goods Sold.
Both are marked "computed" in the Source Map, and the gross-profit check shows "derived".

The row layout lives in **`sgx_fin/template.py`**. Edit labels, order or add rows there to match
your ShareInvestor view exactly; nothing else needs changing.

## Web app

`public/` (`index.html`, `styles.css`, `app.js`) is a dependency-free page in the SideTrek design
language (tokens from `TravelFriend/docs/design/tokens.css`: ivory background, Ember coral, Sun
yellow, Fraunces + Outfit, rounded cards, one accent colour per company):

- **Screen**: From / To dates (default, Singapore time: Friday–Sunday on Saturday, Sunday and
  Monday; otherwise yesterday) and an optional company. `/?from=YYYYMMDD&to=YYYYMMDD` (or
  `/?url=<SGX page URL>`) deep-links straight into a screen.
- **Announcement cards**: one per filing, with links to the PDFs in its attachment list and a
  single **Extract** button; progress shows on the card, then the button becomes **Excel**.
  Click a company's name for the pages chosen, the tie-out checks and "Wrong pages?" re-runs.
- **Extract all** works through every card (two at a time) and becomes **Download all** (zip).
- **Choose download folder** (Chrome / Edge): every workbook is saved straight into that folder,
  one file per company, and a new filing is merged into the company's file already there.
- **Process a filing** (header): an SGX announcement/PDF link or a PDF upload.
- **Settings**: app password, how hard the AI reads, and a workbook to merge the next
  extraction into.

Preview it locally without SGX access or an API key:

```bash
pip install -r requirements.txt
python tests/dev_server.py --mock      # demo data; drop --mock for the real thing
# open http://127.0.0.1:5000
```

## Deploy to Vercel

1. Import this GitHub repo in Vercel (**Add New → Project**). Framework preset: **Other**. No build settings needed.
2. In **Settings → Environment Variables** add:

   | Variable | Required | Purpose |
   |---|---|---|
   | `GEMINI_API_KEY` | yes (or `ANTHROPIC_API_KEY`) | Gemini API key for extraction. Variables belong to one Vercel project: add it to this one (Production ticked) even if another project has it, then **Redeploy** |
   | `APP_PASSWORD` | strongly recommended | Anyone with the URL could otherwise spend your API credits |
   | `SGX_AUTH_TOKEN` | no | Optional shortcut, see *SGX token* below |
   | `SGX_GEMINI_MODEL` | no | Pin a model. Default is Flash-Lite (largest free daily allowance): latest `gemini-3.5-flash-lite`, then `gemini-3.1-flash-lite`, then `gemini-flash-lite-latest` if one is unavailable or out of quota. Each filing uses one request. Try `gemini-3.1-pro-preview` for hard filings |
   | `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | no | Use Claude (`claude-opus-5-5`) instead; with both keys set, `AI_PROVIDER=gemini\|anthropic` picks |

3. Deploy. Open the site, enter the password, pick a date and click **Screen**, then
   **Scan** (free, keyword scan only) or **Extract** (AI) on any announcement.

Vercel notes: two functions are deployed. `api/browse.mjs` (Node) runs Chromium from
`@sparticuz/chromium` for screening and attachments (`maxDuration: 120` s; a screen takes 10–40 s).
`api/index.py` (Python) downloads, scans and extracts (`maxDuration: 300` s; extraction usually
takes 1–3 min). Request bodies are capped at 4.5 MB by Vercel, so large PDFs should be given by SGX link
rather than uploaded. Vercel has no storage, so each run returns a workbook to download; upload
your previous workbook in **Options** to merge new periods into it.

## Automatic daily retrieval (GitHub Actions)

`.github/workflows/daily-sgx.yml` runs on weekdays at 23:30 SGT:

1. Add repo secrets: **Settings → Secrets and variables → Actions** → `GEMINI_API_KEY`
   (and `SGX_AUTH_TOKEN` if needed).
2. List the companies to track in `watchlist.txt` (stock codes or name fragments).
3. Each run screens that day's `ANNC17` announcements, extracts watchlist companies and commits
   `output/<CODE>.xlsx`. You can also run it manually (**Actions → Daily SGX results → Run
   workflow**) with any SGX page URL to backfill a date range.

With an empty watchlist it only logs the day's announcements, so it never spends API credits by surprise.

## Command line

```bash
pip install -r requirements.txt
npm install                      # the headless browser used to read sgx.com
export GEMINI_API_KEY=...
export CHROME_PATH=/path/to/chrome   # only off Linux x64 (macOS/Windows): use your installed Chrome

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

Not needed: the browser step reads the SGX page itself. As an optional shortcut you can copy the
`authorizationtoken` the page sends (SGX announcements page → DevTools → Network → the request to
`api.sgx.com/announcements` → Request Headers) into `SGX_AUTH_TOKEN`; screening then queries the
API directly first and falls back to the browser when the token expires.

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
CHROME_PATH=/path/to/chrome node tests/browse_test.mjs   # browser scraper vs a stand-in SGX site
NODE_PATH=$(npm root -g) node tests/make_sample_pdf.js   # regenerate the sample PDF (needs playwright)
```

## Limits

- Scanned (image-only) PDFs have no text layer, so the keyword scan cannot locate pages; the scan
  reports them and you can pass `--pages` / "Override pages" manually.
- Banks, insurers and REITs use different statement formats; figures map to the closest lines and
  the AI notes the format in the **notes** field. Check the **Checks** sheet before relying on numbers.
