# SGX Financial Results → Excel

Automatically retrieve financial results of SGX-listed companies and turn the three primary
statements (income statement, balance sheet, cash flow) into an Excel workbook in a standard,
editable layout ("statement format") per type of company.

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
   Only **results filings** are opened: the Financial Statements category also carries notices
   of the results date ("Notification of Results Release"), press and media releases,
   presentations, business updates and replies to queries. These are recognised by title and
   listed separately ("not extracted unless you choose to"), so screening doesn't spend time on
   them and **Extract all** skips them.
   **Results release dates** get their own list: each notice is read with patterns (no AI) for
   the release date, the period and the timing, first from the announcement page's own text
   (some notices state the date there and attach nothing; the browser reads the page if
   links.sgx.com refuses a direct request), then from the notice PDF ("Fri 13 Nov 2026, after the
   close of trading — first half of FY2027 ended 30 September 2026").
   **Duplicate filings** (the same company, results type and day, e.g. consolidated and separate
   statements filed as two announcements): attachment names decide ("CONSOLIDATED" / "Group" /
   "hợp nhất" over "SEPARATE" / "Standalone" / "riêng"); when they don't, the statement headings
   do during extraction, before any AI call. Only the consolidated filing is extracted. A filing whose PDF turns out to hold no statements (no
   statement titles, no number tables, or notice wording such as "intends to release its
   financial results") is marked "Not a results filing" before any AI call.
3. **Pick the financial statements PDF** (no AI): attachments are tried by name, results /
   financial statements first and presentations, slides and media releases last, and the first
   one whose pages hold all three statements is used; the others are never downloaded. The card
   shows which PDF was extracted.
4. **Non-AI page scan** (`sgx_fin/page_scanner.py`): scores every page against keyword sets
   (statement titles such as *"statement of financial position"*, line items such as *"total
   assets"*, *"net cash from operating activities"*, plus numeric density) and penalises notes,
   commentary and contents pages. Picks the best attachment and the 1–3 pages for each statement,
   including continuation pages.
   **Fallback:** when the deciding words for a statement aren't found, the AI finds the
   statements itself in a shortlist (a document of up to 24 pages whole; otherwise the pages
   found plus the most number-heavy pages). Bank and Indian-format titles ("Statement of
   Standalone Financial Results", "Statement of Assets and Liabilities") are recognised.
5. **AI extraction** (`sgx_fin/extractor.py`): only the selected pages are cut into a small PDF
   and sent to the AI with a strict JSON schema built from the template, so
   every number lands on a known line. Group (consolidated) figures only, with the company's own label kept for audit.
   Statements that are **pictures** take their own route first (see below).
6. **Check and correct** (`sgx_fin/checks.py`): parts must add up to their printed totals (PBT +
   tax = PAT, interest income + interest expense = net interest income, assets = liabilities +
   equity, cash at the beginning + net change = cash at the end, …). A figure whose sign was read
   backwards is flipped where that, and only that, makes a failing check add up. Any check still
   failing sends the pages back to the AI once with the failing lines pointed out; the second
   reading's figures are taken only where they make the totals add up.
7. **Excel** (`sgx_fin/excel_writer.py`): rescales to `'000`, writes the sheets in the chosen format,
   reports the tie-out checks and **merges** new periods into an existing workbook so
   quarters/half-years/full years accumulate.

**Statements as pictures** (`sgx_fin/pictures.py`). Scanned and photographed filings go wrong
in different ways, so each step deals with one of them:

| Problem | Step |
|---|---|
| No text layer at all, or a scan whose hidden OCR layer gets digits wrong | **Triage** (no AI): pages that are pictures, and scans with an invisible OCR layer, are told apart from text pages with a background picture. OCR layers still help the keyword scan, but the figures are read from the picture |
| Many pages, few of them statements; standalone and consolidated sets | **Locate**: small previews of every picture page (one cheap Flash-Lite request) give each statement page, its heading and basis. Consolidated pages are used, with their unlabelled continuation pages |
| Pages turned sideways or upside down, tilted scans, dim or shadowed photos, wide margins | **Prepare** (no AI): each page is rendered at the scan's own resolution, turned upright (the previews' answer, checked against the direction the ink lines run), straightened (projection profile), cropped to the content, contrast-stretched and lightly sharpened |
| Figures slipping to the row or column next door in dense, many-column tables | **Copy as printed**: the statement pages alone, at the finest image resolution, are copied as text tables (every row and column, with each column's period) by Gemini Flash (`SGX_VISION_MODEL` to change; Flash-Lite if Flash's quota is used up; GLM 5.3 Flash if Gemini fails). Reading is kept apart from interpreting |
| Misread digits (a 6 for an 8) | **Verify** (no AI): what the tables say about themselves: totals named in labels ("Total Income (1 + 2)", "(7-8-9)", "(a)+(b)+(c)+(d)", "(A)+(B)+(C)"), Total / Sub total / net cash lines against the lines above them, quarter + quarter = half year (6M + 3M = 9M…) on every line, and the cash roll-forward |
| | **Re-read**: rows that fail are read again from enlarged top and bottom halves of their page; a new reading is taken only where it makes the failing relation hold |
| Mapping printed lines to the template | **Fill the template** from the checked tables as text, the same way as a text filing, then the checks above |

The workbook gets a **Transcription** sheet with the tables as read: corrected figures in green
(first reading in the cell comment), rows that still don't add up in yellow to check against the PDF.

**Statement formats.** The rows, their order, subtotals and formulas come from a *format*, chosen
per filing by the AI from each format's "used for" description. Built-in formats live in
`formats/*.json` (General, Bank). In the web app (**Settings → Statement formats**) you can:

- import a format from an Excel / CSV export or rows pasted from a financial data site's
  statements page: labels, order, headings and totals are read and shown as editable text;
- add, rename, reorder or delete entries, and change a built-in (your version replaces it);
- add **formula entries**, e.g. `= EBITDA := Profit Before Tax + Finance Costs + Depreciation & Amortisation`
  or `Gross Profit := Revenue + Cost of Goods Sold`: an entry with `:=` is calculated from other
  entries of the same statement (+ and −, plain numbers allowed) wherever the filing doesn't
  print it; a printed figure is kept. Misspelt references are reported when you save;
- add a format for another type of company (REITs, insurers, …).

Editable text syntax: `[Income Statement]` / `[Balance Sheet]` / `[Cash Flow]` start a statement,
one entry per line, `# ` marks a heading, `= ` a bold total, `:= …` a formula. Formats are kept in
the browser and sent with each extraction; **Download as JSON** and add the file to `formats/`
to make it the default for everyone. Imported entries are matched by meaning ("Cost of Sales",
"Taxation", "PATMI"…) to the built-in ones, so derived lines and tie-out checks keep working.

**Banks** get their own layout: Interest Income, Interest Expense, Net Interest Income, Fee &
Commission Income, Other Non-Interest Income, Operating Expenses, Operating Profit before
Allowances, Allowances, then PBT to EPS; and a balance sheet of Cash & Central Bank Balances,
Due from Banks, Investments, Loans & Advances, Deposits, Due to Banks, Borrowings and equity.
The AI classifies each filing as `bank` or `general`; consolidated statements are preferred over
standalone ones. Totals a bank doesn't print (net interest income, total operating income,
operating expenses, equity, total liabilities) are derived and marked "computed". Indian bank
layouts are handled: their "Total Income" (interest earned + other income, before interest
expended) is never taken as total operating income; Profit After Tax is the profit after tax before
minority interest, with a share of associates added after tax in its own line; the bottom line
"Net Profit for the period (12-13+14+15)" is the profit attributable to shareholders; exceptional
items deducted in "(7-8-9)" are charges.

**All figures are in '000** of the reporting currency (EPS in cents / paise). The AI reports the
unit printed (units, thousands, lakhs, millions, crores, billions) and the program rescales; a
filing that states no unit is taken as thousands. Each figure comes back tagged with the column
it sits under, so a blank cell can't shift numbers into the wrong period, and period columns are
grouped by length (full year, half year, quarter) with the latest first.

**No cost of sales line?** Companies that present expenses by nature report no cost of sales or
gross profit. The AI then extracts *Raw materials, consumables used and changes in inventories*,
*Employee benefits expenses* and *Depreciation and amortisation*, and the program derives
**Cost of Goods Sold** = the sum of those three and **Gross Profit** = Revenue − Cost of Goods Sold.
Both are marked "computed" in the Source Map, and the gross-profit check shows "derived".

Formats are loaded by **`sgx_fin/template.py`**; importing and the editable text form are in
**`sgx_fin/formats.py`**.

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

## Use it from Claude (MCP connector)

The app is also a remote MCP server, so you can work from a Claude chat (claude.ai, the Claude
desktop and mobile apps, Claude Code): "screen last Friday's SGX results and put DBS into Excel".

**Add it:** click **Add to Claude** in the header. Claude opens its **Add custom connector**
dialog with the name and URL filled in (Claude's install link,
`https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=…&connectorUrl=…`);
click **Continue**. The connector URL is `https://<your-app>/api/mcp?op=mcp&key=<APP_PASSWORD>`
(custom connectors can't send a password header, so the key travels in the URL; keep it private).
Enter the app password in Settings first, or the button asks for it. On Team/Enterprise plans an
owner adds org-wide connectors (same parameters on `claude.ai/admin-settings/connectors`).

**Photo filings are read by Claude.** When the statements are pictures (scans, photos), the
connector doesn't call Gemini or GLM: it hands Claude the pages, turned upright, straightened,
cropped and contrast-stretched (`sgx_fin/pictures.py`), each as two overlapping halves for small
digits, plus the same transcription rules and template the server's AI gets. Claude (e.g. Opus 5.5
in the chat) reads the figures and submits them; the server then rescales, repairs signs, derives
missing totals, runs the tie-out checks and replies with any failing lines to re-read, and a link to
the Excel workbook. **In the web app**, when the scan finds a filing's statements are photos, a small window asks
whether to read them with Claude Opus 5.5 instead. **Use Opus 5.5** opens a new Claude chat with the
filing already typed in (the connector does the rest and replies with the Excel link); **Keep
Gemini**, Escape, or 30 seconds without an answer carries on with the server's AI. The card then
offers **Open in Claude** and **Use Gemini** to change your mind. Long scanned documents come as small previews first so Claude picks the
statement pages. Text filings still go to the server's AI (`extract_filing`), unless Claude asks
for them as images.

| Tool | What it does |
|---|---|
| `screen_sgx_results` | A date range's ANNC17 announcements (on Vercel via `/api/browse`) |
| `open_filing` | Downloads the filing, keyword-scans it (no AI); picture statements → page images + rules |
| `submit_statements` | Claude's JSON → checks, figures table, Excel link (re-read prompts for failing checks) |
| `extract_filing` | The web app's own pipeline with the server's AI |

Nothing is stored: the Excel link (`/api/download`) carries the figures compressed and signed with
`APP_PASSWORD`, and the workbook is built when it is opened. The endpoint is stateless MCP
Streamable HTTP with JSON responses (`sgx_fin/mcp_server.py`). If Vercel **Deployment Protection**
is on for production, Claude can't reach the URL: turn it off for production or use a bypass.

## Deploy to Vercel

1. Import this GitHub repo in Vercel (**Add New → Project**). Framework preset: **Other**. No build settings needed.
2. In **Settings → Environment Variables** add:

   | Variable | Required | Purpose |
   |---|---|---|
   | `GEMINI_API_KEY` | yes (or `ANTHROPIC_API_KEY`) | Gemini API key for extraction. Variables belong to one Vercel project: add it to this one (Production ticked) even if another project has it, then **Redeploy** |
   | `APP_PASSWORD` | strongly recommended | Anyone with the URL could otherwise spend your API credits |
   | `SGX_AUTH_TOKEN` | no | Optional shortcut, see *SGX token* below |
   | `SGX_GEMINI_MODEL` | no | Pin a model. Default is Flash-Lite (largest free daily allowance): latest `gemini-3.5-flash-lite`, then `gemini-3.1-flash-lite`, then `gemini-flash-lite-latest` if one is unavailable or out of quota. Each filing uses one request. Try `gemini-3.1-pro-preview` for hard filings |
   | `SGX_VISION_MODEL` | no | The Gemini model that copies tables from pictures. Default `gemini-3.5-flash`, falling back to Flash-Lite when its quota is used up. Picture filings are few and need 2–3 requests each; set `gemini-3.5-flash-lite` to keep everything on Flash-Lite |
   | `GLM_FLASH` | no | Key for **GLM 5.3 Flash**, the fallback for filings whose statements are pictures when Gemini fails. An `nvapi-` key uses NVIDIA's free API catalog (the model id is looked up from the key's model list; pin it with `GLM_MODEL`); other keys use Z.ai. `GLM_MAX_IMAGES` (default 8) caps pictures per request |
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
| Income Statement / Balance Sheet / Cash Flow | The format's rows, one column per period (grouped by length, latest first), `'000` of reporting currency, EPS in cents |
| Checks | Arithmetic tie-outs per period: PASS / FAIL / n/a |
| Source Map | For every figure: the company's own line label, filing, PDF, pages and model |
| Transcription | Picture filings only: the tables as read from the pictures, corrections in green, doubtful rows in yellow |

## Tests

```bash
python tests/test_offline.py               # scanner, pictures, checks, schema, Excel merge
python tests/accuracy.py 2V6B.xlsx          # score a workbook against tests/answers/ (Bank of Baroda by default)
CHROME_PATH=/path/to/chrome node tests/browse_test.mjs   # browser scraper vs a stand-in SGX site
NODE_PATH=$(npm root -g) node tests/make_sample_pdf.cjs   # regenerate the sample PDF (needs playwright)
```

## Limits

- **Statements as pictures**: a figure that is unreadable stays blank ("?"), and a misread that
  happens to keep every total adding up can't be caught by the checks; rows that still don't add
  up after the re-read are marked yellow on the Transcription sheet. Handwriting is not
  supported. A picture filing takes 3–5 AI requests (locate, copy, re-read when needed, fill,
  re-check when needed) and about 1–3 minutes. "Wrong pages?" lets you name the statement pages
  of a long scanned document yourself.
- Banks, insurers and REITs use different statement formats; figures map to the closest lines and
  the AI notes the format in the **notes** field. Check the **Checks** sheet before relying on numbers.
