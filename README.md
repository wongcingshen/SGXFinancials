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
   **Only listed companies.** The category also carries results filed by issuers whose only SGX
   listing is a bond or note (State Bank of India `3J7B`, Grab `52WB`, Bank of Baroda `2V6B`, or no
   stock code at all). An announcement is kept only when one of its stock codes is a listed
   company's (`scraper/listed.mjs`, `sgx_fin/listed.py`): those in `listings/companies.csv` (the SGX
   companies list from the Google Sheet, plus a snapshot of SGX's own list of shares, REITs, business
   trusts and SDRs for companies the sheet lacks), or in SGX's live list of securities
   (`api.sgx.com/securities`, read at each screen, no token needed), so a **new IPO** is kept from its
   first day of trading. A filing made for both shares and bonds is shown under the share code.
   Without a stock code (the result-table fallback), the issuer's name is matched against the
   companies' names instead. What was left out is reported (`debug.not_listed`, "N from bond issuers
   left out" in the app); `--all-issuers` (CLI) or `all_issuers=1` (`/api/browse`) keeps everything.
   To refresh the file from the sheet, export it as CSV and write it as `code,name,sector,source`
   (check codes the spreadsheet turned into numbers, such as `1E3` shown as `1.00E+03`).
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
   **Presentations only**: some results announcements carry nothing but slides. A PDF made in
   PowerPoint (Keynote, Impress…), with 16:9 pages, or with landscape pages of little text and slide
   words (agenda, disclaimer, Q&A, thank you) is a presentation; unless its pages are headed by all
   three statement titles it is marked **Slides only** ("a presentation without the formal financial
   statements") before any AI call, and stays on screen with an **Ask Claude** button.
3. **Pick the financial statements PDF** (no AI): attachments are tried by name, results /
   financial statements first and presentations, slides and media releases last, and the first
   one whose pages hold all three statements is used; the others are never downloaded. A slide
   deck is never that PDF, even when named "Results": another attachment with the statements is
   preferred to it. The card shows which PDF was extracted.
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
6. **Check and correct** (`sgx_fin/checks.py`, `sgx_fin/textlayer.py`): parts must add up to their printed
   totals (PBT + tax (+ discontinued operations) = PAT, interest income + interest expense = net interest
   income, assets = liabilities + equity, total assets − total liabilities = net assets, cash at the
   beginning + net change + exchange differences = cash at the end, …). The checks are **lenient about
   presentation and strict about figures**:
   - **Either layout passes.** A line a filing may print inside or outside a total counts either way:
     exchange differences above the net change in cash (Japanese and US layouts, e.g. Panasonic) or below
     it, perpetual securities inside or after the owners' equity, assets and liabilities held for sale
     inside or after the current subtotals, attributable-to-perpetual-holders lines inside or beside the
     owners' share. Cash moved to assets held for sale (or a disposal group, overdrafts) between the net
     change and cash at the end has its own line (**Other Changes in Cash**).
   - **Rounding never fails a check.** The slack is the rounding of the figures involved (half of the
     filing's unit each, or of its decimals when every figure has some, never less than two units), plus
     0.005% of the largest figure on big totals, **capped at 10 of the filing's units**. It is never a real
     share of the total, so a misread digit that matters can't hide inside 0.2% of a bank's total assets.
   - **A pass has to prove something**: a check whose figures are all 0 **fails** ("every figure is 0":
     lines read as 0 that may not be printed at all).
   - **Mistakes the figures themselves prove are put right, without AI**, and labelled in the Source Map:
     a sign read backwards is flipped where that, and only that, makes a failing check add up; a line
     that **two failing checks agree** is wrong (both give it the same figure from other printed lines,
     e.g. total assets misread, or a total taken from the row next door) takes that figure; total equity
     (or liabilities) equal to total assets is the "Total equity and liabilities" line read for it, and is
     worked out from the other two totals.
   - **Text filings are re-read from the PDF's own text first** (free, exact digits): for each failing
     check, the figures printed on its lines (the line the AI named, or a line whose wording means that
     line: "Non-controlling interests" for the minority's share) are tried, and one is kept only where it
     makes the check add up without breaking another. Labels wrapped over two lines and figures wrapped
     onto the next line are followed.
   - Any check still failing, or gross profit left blank, sends the pages back to the AI once with the
     failing lines pointed out, and **what usually causes that failure** ("total_equity is never the
     'Total equity and liabilities' line", "take the lines under 'attributable to'"…); the second reading's
     figures are taken only where they make the totals add up (or leave out lines the first reading
     invented as 0), and its gross expense where the first had none. The AI is also asked to add up every
     column itself before answering.
   **Verdict**: an extraction is *verified* only when at least one check passes, none fails, every
   statement has figures and gross profit isn't blank. **0 checks pass is a failure**, not a success:
   nothing confirms such figures (a presentation's highlights, a page read as zeros).
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
`formats/*.json` (General, Bank); edit or add a file there to change them for everyone.

**Your own standards** (web app, **My standards** in the header). Upload a finished workbook of older
results: one company's file, or a whole listing (one sheet per company, sheets such as "D05 Income
Statement", or one table with a company / stock code column). The program (`sgx_fin/standards.py`) finds
each company (stock code and name from the cells, sheet names or file name) and reads its income
statement, balance sheet and cash flow lines in order with their figures. Companies laid out alike (80%
of lines in common) become one **standard**: "Standard 1 for X and Y, Standard 2 for A, B and C". For
each standard it records:

| | How it is found |
|---|---|
| Lines, order, headings and totals | The file's own rows (bold = total, filled = heading); each line is matched to the app's line of the same meaning, so checks and derived lines still work |
| Unit ('000, millions, billions) | The sheet's own words ("S$ million", "S$'000"), else the AI's judgement, else '000 |
| Column order | The period headers (FY2023, FY2024 = oldest first) |
| Sign convention | Costs and outflows (marked by the AI, or by their wording) that the file shows as positive are flipped when written |
| Sums | Totals the AI says are sums of other lines ("Gross profit = Revenue - Cost of sales") become formulas, but only where the file's figures prove them |

The AI (one request per file, for up to 8 standards) names each standard, says what kind of company it
suits, matches lines the program can't, marks costs and outflows and proposes the sums; the program checks
it against the figures. Without an AI key the program's own reading is used. Check the companies, then
**Save**: standards are kept in this browser, a company has one standard (the latest it was uploaded
with), and a filing of a listed company is sent with its standard, so it is read into exactly those lines
and written in that unit, order and sign convention. Figures are still kept in '000 with costs negative
inside the workbook (and the Checks sheet stays in '000), so tie-outs and merging work as before.

**In Claude too.** The connector can't see your browser, so a standard travels as a **token** (`sgxstd1.…`:
the standard compacted and compressed, with the companies it is for; `/api/standards_token` makes them).
The Claude buttons on a filing put its company's token in the message, and **My standards → Copy for
Claude** copies one token with all your standards: paste it into a Claude project's instructions (or a
chat) to use them in chats you start yourself. Claude passes it as `standard` to `open_filing`,
`extract_filing` and `submit_statements` (with the stock code; the server picks the company's standard), so
the statements are read into your lines, and the Excel link carries the standard and builds the workbook in
your unit, column order and signs. A company the token doesn't list keeps the built-in layout, and Claude is
told so; a token copied wrongly is refused rather than half-used.

**Banks** get their own layout: Gross Income, Gross Expense, Gross Profit, then Interest Income,
Interest Expense, Net Interest Income, Fee & Commission Income, Other Non-Interest Income, Operating
Expenses, Operating Profit before Allowances, Allowances, then PBT to EPS; and a balance sheet of Cash & Central Bank Balances,
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

**Gross profit is never left blank, and the COGS is explained.** When a filing prints a cost of sales
it is taken as printed. When not, the cost of goods sold (COGS, the gross expense) is judged by
**industry** and gross profit = Revenue + COGS:

1. **Industry**: the AI names the company's industry from a fixed list (manufacturing, trading &
   retail, food & beverage, property, REITs, construction, shipping & transport, hotels & gaming,
   energy, mining & plantations, technology & telecoms, healthcare, services, banks, finance,
   insurance, investment holding, other), with the words in the filing that show it. The program
   checks that answer against the filing's own lines ("raw materials", "net property income",
   "interest earned", "vessel"…): it fills in when the AI gives none, and flags a contradiction.
2. **Every expense line** above operating profit comes back with its figures, marked in or out of
   COGS by the industry's rule and **why**. Materials always go in (raw materials, consumables,
   purchases, changes in inventories). E.g. manufacturing: materials + changes in inventories +
   subcontract and production costs; REITs: property operating expenses (gross profit = net property
   income); banks and finance companies: financial cost associated with financial income + provision
   for credit losses + insurance service expenses + operating costs.
3. **The program adds up** the marked lines (no AI arithmetic), and keeps the list: the workbook's
   **COGS Basis** sheet shows, per filing, the industry and its evidence, the rule used, an
   explanation, every line with its figures, in or out and why, and the total. The Source Map names
   the lines ("judged (Manufacturing & industrials): Raw materials… + Subcontract costs").

**FactSet's convention for depreciation.** The FactSet Fundamentals feed used in
[EDGARtoCSV](https://github.com/wongcingshen/EDGARtoCSV) (`FF_GEN_ANN_US`, 26,512 companies) puts
depreciation & amortisation inside cost of revenue ("COGS including D&A", in 99.9% of rows) and also
shows D&A on its own; EBIT = revenue − COGS incl. D&A − SG&A − other operating expenses, and EBITDA =
EBIT + D&A. The industrial rules here follow it: D&A on the face of the statement goes into the judged
COGS, and the COGS Basis shows "of which D&A" and COGS excluding D&A, so both of FactSet's figures are
there and gross profit compares with FactSet's gross income. A printed cost of sales stays as printed
(the D&A inside it isn't shown on the face). FactSet gives banks and most other companies without a
cost of sales no cost of revenue at all; here they still get one (banks, finance companies and insurers
their gross expense, REITs their property expenses), so gross profit is never blank.

When nothing on the statement is a cost of earning the revenue (an investment holding company's
dividends), COGS is 0 and gross profit equals revenue, and the sheet says so. Readings without line
marks fall back to the program's sums (expenses by nature: raw materials + employee benefits +
depreciation; banks: interest expense + operating expenses + allowances), also explained. Gross
profit still blank after the first reading gets a second, targeted look; if it stays blank the
extraction is not verified.

**Your COGS preferences** (web app, **Settings → Cost of goods sold**): edit any industry's rule
(your version replaces the built-in in this browser), add industries (name, how to recognise it,
words its statements use, what COGS is), and give a company its own rule by stock code or part of
its name, with its industry pinned. They are sent with every extraction and come before the
built-in rules. To correct one filing, open its details: the COGS lines have tick boxes;
**Recalculate** adds up the ticked lines again without AI and updates the workbook (and the copy in
your download folder), and **Save as my rule for …** keeps that choice for the company's next
filings. **Download my rules (JSON)** exports them; put the industries into `industries/cogs.json`
to make them everyone's default.

Formats are loaded by **`sgx_fin/template.py`**; importing and the editable text form are in
**`sgx_fin/formats.py`**, and standards learnt from your workbooks in **`sgx_fin/standards.py`**. Industries and their COGS rules are in **`industries/cogs.json`**, loaded
by **`sgx_fin/industries.py`**.

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
- **Industry and COGS** (details): the industry and why, how cost of goods sold was made up, and every
  expense line with tick boxes to correct it (see *Your COGS preferences* above).
- **Check failed**: a workbook whose figures aren't verified (a check fails, **0 checks pass**, a
  statement or gross profit is missing) is not shown as "Excel ready". The card turns red with the
  reasons ("Not verified: 0 checks pass · no balance sheet or cash flow · gross profit blank") and a
  **Check with Claude** button: a new Claude chat opens with the filing, the failing checks and the
  statement pages, asking Claude to read the pages itself through the connector (not
  `extract_filing`, which repeats the reading that failed) and reply with an Excel link. The workbook
  can still be downloaded (marked not verified). **Slides only** cards offer **Ask Claude**, and
  extraction errors **Read it with Claude** in the details.
- **Extract all** works through every card (two at a time) and becomes **Download all** (zip).
- **Choose download folder** (Chrome / Edge): every workbook is saved straight into that folder,
  one file per company, and a new filing is merged into the company's file already there.
- **My standards** (header): upload results you already have in Excel or CSV, and each company's next
  filings come out in the same layout (see *Your own standards* below).
- **Settings**: app password, how hard the AI reads, and COGS rules.
- **Guided tour** (`tour.js`, `tour-core.js`): on a first visit, a spotlight walks through the real
  page in six chapters (Welcome, Find results, The list, Extract, Many at once, Make it yours). It
  waits while you screen SGX or extract a filing for real, then carries on, and skips controls that
  aren't on the page (no results yet, a browser without folder support). **?** in the header
  replays it or jumps to one chapter; "seen" is remembered in this browser (`sgxf-tour`). Steps are
  data in `tour-core.js`, pointing at `data-tour` attributes on the controls.

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
in the chat) reads the figures and submits them; the server then rescales, repairs signs, puts right
a line two printed totals agree on (and says so), derives missing totals, runs the tie-out checks and
replies with any failing lines to re-read (with what usually causes each failure), and a link to
the Excel workbook. **In the web app**, when the scan finds a filing's statements are photos, a small window asks
whether to read them with Claude Opus 5.5 instead. **Use Opus 5.5** opens a new Claude chat with the
filing already typed in (the connector does the rest and replies with the Excel link); **Keep
Gemini**, Escape, or 30 seconds without an answer carries on with the server's AI. The card then
offers **Open in Claude** and **Use Gemini** to change your mind. Long scanned documents come as small previews first so Claude picks the
statement pages. Text filings still go to the server's AI (`extract_filing`), unless Claude asks
for them as images; a text page read as images comes with its text layer, so the digits are exact.

Results also give the industry and the cost of goods sold line by line (IN / out, and why). The web
app's Claude messages carry your COGS rule for the company, which Claude passes to the tools as
`cogs_rule`, so your preference applies in the chat too.

`submit_statements` and `extract_filing` end with a **verdict** (OK, or NOT VERIFIED with the
reasons) and tell Claude what to do: re-read failing lines, read the missing totals or statement,
judge the gross expense when gross profit is blank, or tell you the filing has no formal statements
(a presentation). `open_filing` says when a filing was set aside as not results (e.g. slides only),
and still shows its pages with `as_images=true`.

| Tool | What it does |
|---|---|
| `screen_sgx_results` | A date range's ANNC17 announcements (on Vercel via `/api/browse`) |
| `open_filing` | Downloads the filing, keyword-scans it (no AI); picture statements → page images + rules |
| `submit_statements` | Claude's JSON → checks, figures table, Excel link (re-read prompts for failing checks) |
| `extract_filing` | The web app's own pipeline with the server's AI |

Each of `open_filing`, `extract_filing` and `submit_statements` also takes `standard`, your standard token
(see *Your own standards*).

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
   | `GLM_FLASH` | no | Key for the fallback models when Gemini fails (503 unavailable, quota used up, ...): **GLM 5.3 Flash** for filings whose statements are pictures, and **DeepSeek Flash, then GLM Flash** for text filings, which get the pages' own text instead of the PDF (`NVIDIA_TEXT_MODELS`, comma-separated, pins those). An `nvapi-` key uses NVIDIA's free API catalog (the model id is looked up from the key's model list; pin it with `GLM_MODEL`); other keys use Z.ai. `GLM_MAX_IMAGES` (default 8) caps pictures per request |
   | `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` | no | Use Claude (`claude-opus-5-5`) instead; with both keys set, `AI_PROVIDER=gemini\|anthropic` picks |

3. Deploy. Open the site, enter the password, pick a date and click **Screen**, then
   **Scan** (free, keyword scan only) or **Extract** (AI) on any announcement.

Vercel notes: two functions are deployed. `api/browse.mjs` (Node) runs Chromium from
`@sparticuz/chromium` for screening and attachments (`maxDuration: 120` s; a screen takes 10–40 s).
`api/index.py` (Python) downloads, scans and extracts (`maxDuration: 300` s; extraction usually
takes 1–3 min). Request bodies are capped at 4.5 MB by Vercel, so large PDFs should be given by SGX link
rather than uploaded. Vercel has no storage, so each run returns a workbook to download; choose a
download folder to merge new periods into a company's workbook already there.

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
| Checks | Each filing's verdict first (OK, or NOT VERIFIED and why), then the tie-outs per period: PASS / FAIL (with a note when every figure is 0) / derived / n/a. A filing that isn't verified also gets a red "Not verified" line above every statement |
| Source Map | For every figure: the company's own line label, filing, PDF, pages and model |
| COGS Basis | Per filing: the industry and its evidence, the rule used, how the cost of goods sold was made up, every expense line with its figures, in or out and why, and the total |
| Transcription | Picture filings only: the tables as read from the pictures, corrections in green, doubtful rows in yellow |

## Tests

```bash
python tests/test_offline.py               # scanner, pictures, checks, schema, Excel merge, standards
node --test tests/tour_test.mjs             # the guided tour's steps, targets and state
python tests/accuracy.py 2V6B.xlsx          # score a workbook against tests/answers/ (Bank of Baroda by default)
CHROME_PATH=/path/to/chrome node tests/browse_test.mjs   # browser scraper vs a stand-in SGX site
NODE_PATH=$(npm root -g) node tests/make_sample_pdf.cjs   # regenerate the sample PDF (needs playwright)
NODE_PATH=$(npm root -g) node tests/make_deck_pdf.cjs     # the slides-only results deck
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
- A verified extraction is one whose printed totals confirm it; a misread that keeps every total adding
  up, or a judged gross expense that adds the wrong lines, can't be caught that way.
