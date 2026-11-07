// Browser automation for SGX: open the announcements page exactly as a person would,
// read the result list, open each title link and collect the PDFs under "Attachments".
//
// The SGX listing is a JavaScript app backed by api.sgx.com, which refuses requests without
// the token the page itself generates. Driving a real browser avoids that: we read the JSON
// the page loads (and page through it with the page's own headers), falling back to the
// links rendered in the result table.

import puppeteer from "puppeteer-core";
import { filterListed, loadListed } from "./listed.mjs";

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36";
const API_HIT = /api\.sgx\.com\/announcements/i;

// The Financial Statements category also carries notices that aren't results ("Notification of
// Results Release", press releases, presentations, replies to queries). Judged by title alone, so
// only real results get their pages opened. Keep in step with classify_title() in sgx_client.py.
const RELEASE_DATE = /notification of (the )?results|results? release|(date|release) of (the )?(financial )?results|intends? to release|results announcement date/i;
const NOT_RESULTS = /media release|press release|news release|presentation|slides|webcast|briefing|analyst|clarification|response to|reply to|queries|business update|operational update|investor day|notice of|record date|dividend|change of|cessation|appointment/i;
const IS_RESULTS = /results|financial statements?|financial information|financial report|unaudited|audited/i;

export function classifyTitle(title) {
  const t = String(title || "");
  const type = t.includes("::") ? t.split("::").slice(1).join("::") : t;
  if (!type.trim()) return "results"; // just the category name: give it the benefit of the doubt
  if (RELEASE_DATE.test(type)) return "release_date"; // a notice of when results will be released
  if (NOT_RESULTS.test(type)) return "other";
  return IS_RESULTS.test(type) ? "results" : "other";
}

// SGX links sometimes come back protocol-relative, as http:// or as a path.
export function normUrl(u, base = "https://links.sgx.com/") {
  if (!u) return "";
  try {
    const url = new URL(String(u).trim(), base);
    if (url.hostname.endsWith("sgx.com")) url.protocol = "https:";
    return url.toString();
  } catch {
    return "";
  }
}

export function isSgxUrl(u) {
  try {
    const { protocol, hostname } = new URL(u);
    return protocol === "https:" && (hostname === "sgx.com" || hostname.endsWith(".sgx.com"));
  } catch {
    return false;
  }
}

export async function launchBrowser() {
  if (process.env.CHROME_PATH) {
    return puppeteer.launch({
      executablePath: process.env.CHROME_PATH,
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });
  }
  // Serverless Chromium (Vercel / Linux x64).
  const chromium = (await import("@sparticuz/chromium")).default;
  chromium.setGraphicsMode = false;
  return puppeteer.launch({
    args: await puppeteer.defaultArgs({ args: chromium.args, headless: "shell" }),
    executablePath: await chromium.executablePath(),
    headless: "shell",
  });
}

async function newPage(browser) {
  const page = await browser.newPage();
  await page.setUserAgent(UA);
  await page.setExtraHTTPHeaders({ "Accept-Language": "en-SG,en;q=0.9" });
  await page.setViewport({ width: 1366, height: 900 });
  return page;
}

// ------------------------------------------------------------------ normalising records
const first = (o, ...keys) => {
  for (const k of keys) if (o?.[k] !== undefined && o[k] !== null && o[k] !== "") return String(o[k]);
  return "";
};

function sgt(ms) {
  const d = new Date(Number(ms) + 8 * 3600e3);
  return d.toISOString().slice(0, 16).replace("T", " ");
}

export function fromApiItem(it) {
  // Every stock code the announcement is filed under (shares and bonds can share one filing).
  const codes = [first(it, "stock_code", "security_code", "securityCode", "issuer_code")];
  for (const k of ["issuers", "securities", "issuer"]) {
    for (const v of Array.isArray(it[k]) ? it[k] : [it[k]]) codes.push(first(v, "stock_code", "security_code", "code"));
  }
  const stock_codes = [...new Set(codes.filter(Boolean))];
  const code = stock_codes[0] || "";
  let broadcast = first(it, "broadcast_date_time", "submission_date_time", "submission_date");
  if (/^\d{12,}$/.test(broadcast)) broadcast = sgt(broadcast);
  return {
    title: first(it, "title", "announcement_title"),
    issuer: first(it, "issuer_name", "security_name", "company_name"),
    stock_code: code,
    stock_codes,
    url: normUrl(first(it, "url", "link", "announcement_url")),
    broadcast,
    category: first(it, "category_name", "sub_category_name", "cat"),
  };
}

const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };

export function fromRow({ title, url, cells }) {
  // cells: the row's text pieces, e.g. ["30 Oct 2026", "11:11 PM", "MIYOSHI LIMITED", "MIYOSHI LIMITED",
  //   "Financial Statements and Related Announcement::Full Yearly Results", "Financial Statements"]
  const pad = (n) => String(n).padStart(2, "0");
  let date = "", time = "";
  const rest = [];
  for (const c of cells) {
    const d = /(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})/.exec(c);
    const t = /\b(\d{1,2}):(\d{2})\s*(AM|PM)?\b/i.exec(c);
    if (!date && d && MONTHS[d[2].toLowerCase()]) date = `${d[3]}-${pad(MONTHS[d[2].toLowerCase()])}-${pad(d[1])}`;
    if (!time && t && (d || date)) {
      let h = Number(t[1]);
      if (t[3]) h = (h % 12) + (t[3].toUpperCase() === "PM" ? 12 : 0);
      time = `${pad(h)}:${t[2]}`;
    }
    if ((d && MONTHS[d[2].toLowerCase()]) || (t && c.replace(t[0], "").trim() === "")) continue;
    if (c !== title && /[A-Za-z]/.test(c)) rest.push(c);
  }
  const issuer = rest.find((c) => c === c.toUpperCase() && c.length > 2) || rest[0] || "";
  const category = rest.length > 1 && rest[rest.length - 1] !== issuer ? rest[rest.length - 1] : "";
  return { title, issuer, stock_code: "", url, broadcast: [date, time].filter(Boolean).join(" "), category };
}

// ------------------------------------------------------------------ screening
/**
 * Open an SGX company-announcements page URL and return its announcements.
 * Only listed companies' announcements are returned (see listed.mjs); the rest (bond and note
 * issuers) are counted in debug.not_listed. opts.allIssuers keeps them; opts.listed is a test hook.
 * opts.company filters by issuer substring or stock code; opts.preparePage(page) is a test hook.
 */
export async function screen(browser, pageUrl, opts = {}) {
  if (!isSgxUrl(pageUrl)) throw new Error("Only https://*.sgx.com page URLs can be screened");
  const page = await newPage(browser);
  if (opts.preparePage) await opts.preparePage(page);
  const debug = { page_url: pageUrl, api_responses: [], dom_links: 0 };
  const lists = []; // [{ url, headers, body }] responses that hold announcement records
  page.on("response", async (r) => {
    if (!API_HIT.test(r.url()) || r.request().method() !== "GET") return;
    const seen = { url: r.url().replace(/^https:\/\/api\.sgx\.com/, "").slice(0, 200), status: r.status() };
    debug.api_responses.push(seen);
    if (!r.ok()) return;
    try {
      const body = await r.json();
      const data = Array.isArray(body?.data) ? body.data : null;
      seen.items = data ? data.length : null;
      seen.keys = data?.[0] && typeof data[0] === "object" ? Object.keys(data[0]).slice(0, 25) : Object.keys(body || {}).slice(0, 10);
      if (data?.some((d) => d && typeof d === "object" && ("title" in d || "announcement_title" in d))) {
        lists.push({ url: r.url(), headers: r.request().headers(), body });
        if (!debug.sample_item) debug.sample_item = JSON.stringify(data[0]).slice(0, 600);
      }
    } catch { /* not JSON */ }
  });
  await page.goto(pageUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });

  // Wait until the page shows its list (or clearly shows nothing).
  const deadline = Date.now() + (opts.waitMs ?? 35_000);
  let rows = [];
  while (Date.now() < deadline) {
    rows = await page.evaluate(collectRows).catch(() => []);
    if (rows.length || lists.length) {
      await new Promise((r) => setTimeout(r, 1500)); // let the table finish rendering
      rows = await page.evaluate(collectRows).catch(() => rows);
      break;
    }
    const empty = await page.evaluate(() => /no (results|records|announcements|data) (found|available)/i.test(document.body?.innerText || "")).catch(() => false);
    if (empty) break;
    await new Promise((r) => setTimeout(r, 700));
  }
  // Titles that are click targets rather than plain links: click each one.
  for (const r of rows) if (r.click !== undefined && !r.url) r.url = await clickForUrl(page, r.click);
  debug.dom_links = rows.length;
  debug.clicked = rows.filter((r) => r.click !== undefined).length;
  debug.title = await page.title().catch(() => "");

  // 1) The records the page loaded for itself, paged through with the page's own request.
  let fromData = [];
  if (lists.length) {
    const items = lists.flatMap((l) => l.body.data);
    const total = Number(lists[0].body?.meta?.totalItems || items.length);
    const u = new URL(lists[0].url);
    if (items.length < total && u.searchParams.has("pagestart")) {
      const start = Number(u.searchParams.get("pagestart")) || 0;
      const headers = Object.fromEntries(Object.entries(lists[0].headers).filter(([k]) => /^(authorizationtoken|accept|authorization)$/i.test(k)));
      for (let p = start + 1; items.length < total && p < start + 40; p++) {
        u.searchParams.set("pagestart", String(p));
        const body = await page.evaluate(async (url, h) => {
          const r = await fetch(url, { headers: h });
          return r.ok ? r.json() : null;
        }, u.toString(), headers).catch(() => null);
        if (!Array.isArray(body?.data) || !body.data.length) break;
        items.push(...body.data);
      }
    }
    fromData = items.map(fromApiItem);
  }
  // 2) What the result table shows.
  const fromTable = rows.map(fromRow);

  const valid = (list) => {
    const seen = new Set();
    return list.filter((a) => a.title && isSgxUrl(a.url) && !seen.has(a.url) && seen.add(a.url));
  };
  const data = valid(fromData), table = valid(fromTable);
  let anns = data.length >= table.length && data.length ? data : table;
  debug.source = anns === data && data.length ? "page data" : "result table";
  debug.counts = { page_data: data.length, table: table.length };
  if (!anns.length) debug.body_excerpt = (await page.evaluate(() => document.body?.innerText || "").catch(() => "")).slice(0, 600);
  await page.close();

  if (!opts.allIssuers) {
    const listed = opts.listed || (await loadListed());
    const { kept, dropped } = filterListed(anns, listed);
    anns = kept;
    debug.listed_source = listed.source;
    if (listed.error) debug.listed_error = listed.error;
    debug.not_listed = dropped.map((a) => ({ issuer: a.issuer, stock_code: a.stock_code, title: a.title }));
  }
  if (opts.company) {
    const c = opts.company.toLowerCase();
    anns = anns.filter((a) => a.issuer.toLowerCase().includes(c) || a.stock_code.toLowerCase() === c);
  }
  for (const a of anns) a.kind = classifyTitle(a.title);
  // Only results are opened for their PDFs; the rest are listed from their titles.
  if (opts.attachments) await openAll(browser, anns.filter((a) => a.kind === "results"), opts);
  debug.opened = anns.filter((a) => a.attachments).length;
  return { announcements: anns, debug };
}

// Open every title link (a few tabs at a time) and record the files under "Attachments".
async function openAll(browser, anns, opts) {
  let next = 0;
  const worker = async () => {
    while (next < anns.length) {
      const a = anns[next++];
      a.attachments = await attachments(browser, a.url, { preparePage: opts.preparePage, waitMs: opts.attachmentWaitMs })
        .catch((e) => { a.attachments_error = String(e.message || e); return []; });
    }
  };
  await Promise.all(Array.from({ length: Math.min(opts.concurrency ?? 4, anns.length) }, worker));
}

// Runs inside the page. Returns one entry per result row: the link under the "Title" column
// (piercing shadow DOM), plus any other announcement links. A title without a plain link is
// marked data-sgxf-click="n" so the caller can click it and see where it leads.
function collectRows() {
  const out = [];
  const seen = new Set();
  const text = (el) => (el?.innerText || el?.textContent || "").replace(/\s+/g, " ").trim();
  const cellsOf = (row) => (row.innerText || row.textContent || "").split(/\n|\t/).map((x) => x.trim()).filter(Boolean);
  const roots = [];
  const gather = (root) => { roots.push(root); for (const el of root.querySelectorAll("*")) if (el.shadowRoot) gather(el.shadowRoot); };
  gather(document);

  // 1) The "Title" column of the results table.
  let clicks = 0;
  for (const root of roots) {
    for (const h of root.querySelectorAll("th, [role=columnheader], div, span, td")) {
      if (text(h) !== "Title" || h.children.length > 2) continue;
      let cell = h;
      while (cell.parentElement && cell.parentElement.children.length < 3) cell = cell.parentElement;
      const headerRow = cell.parentElement;
      if (!headerRow || !/issuer|date/i.test(text(headerRow))) continue;
      const idx = [...headerRow.children].indexOf(cell);
      const table = headerRow.closest("table");
      const rows = table
        ? [...table.querySelectorAll("tr")].filter((r) => r !== headerRow)
        : [...(headerRow.parentElement?.querySelectorAll(":scope > *, :scope > * > *") || [])]
            .filter((r) => r !== headerRow && r.children.length === headerRow.children.length);
      for (const row of rows) {
        const tc = row.children[idx];
        const title = text(tc);
        if (!tc || title.length < 6) continue;
        const a = tc.querySelector("a[href]") || tc.closest("a[href]");
        const href = a && /^https?:/.test(a.href) ? a.href : "";
        const key = href || title + "|" + text(row);
        if (seen.has(key)) continue;
        seen.add(key);
        const entry = { title, url: href, cells: cellsOf(row) };
        if (!href) {
          const target = tc.querySelector("a, button, [role=link], [onclick], span, div") || tc;
          target.setAttribute("data-sgxf-click", String(clicks));
          entry.click = clicks++;
        }
        out.push(entry);
      }
    }
  }

  if (out.length) return out; // the Title column is the source of truth when it exists

  // 2) No recognisable header: announcement links anywhere, ignoring links back to this page
  //    (category filters, "#" anchors) and the site's own navigation.
  const here = location.href.split("#")[0];
  const isAnn = (a) => {
    const h = a.href || "";
    const t = text(a);
    if (!/^https?:/.test(h) || h.split("#")[0] === here || h.split("?")[0] === here.split("?")[0]) return false;
    return (/links\.sgx\.com|corporate-announcements/i.test(h) && t.length > 8) || (t.includes("::") && t.length > 10);
  };
  for (const root of roots) {
    for (const a of root.querySelectorAll("a[href]")) {
      if (!isAnn(a) || seen.has(a.href)) continue;
      seen.add(a.href);
      let row = a;
      for (let i = 0; i < 8 && row.parentElement; i++) {
        const up = row.parentElement;
        if ([...up.querySelectorAll("a[href]")].filter(isAnn).length > 1) break;
        row = up;
      }
      out.push({ title: text(a), url: a.href, cells: cellsOf(row) });
    }
  }
  return out;
}

// Click a marked title and return the address it opens (new tab or same tab).
async function clickForUrl(page, n) {
  const el = await page.$(`pierce/[data-sgxf-click="${n}"]`);
  if (!el) return "";
  const popup = page.browser().waitForTarget((t) => t.opener() === page.target(), { timeout: 10_000 }).catch(() => null);
  const nav = page.waitForNavigation({ timeout: 10_000 }).then(() => "nav").catch(() => null);
  await el.click().catch(() => {});
  const got = await Promise.race([popup, nav]);
  if (got === "nav") {
    const url = page.url();
    await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => {});
    await new Promise((r) => setTimeout(r, 2500));
    await page.evaluate(collectRows).catch(() => []); // re-mark the titles
    return url;
  }
  if (got) {
    const url = got.url();
    (await got.page().catch(() => null))?.close().catch(() => {});
    return url;
  }
  return "";
}

// ------------------------------------------------------------------ attachments
// Attachments that never hold the formal financial statements are left out of every list, so they
// are neither shown nor opened. Matched on the link text and the file name (underscores count as
// spaces: "OCBC_3Q26_Results_Press_Release.pdf"). Keep in step with EXCLUDED_ATTACHMENT in
// sgx_fin/sgx_client.py.
export const EXCLUDED_ATTACHMENT =
  /(press|media|news)[\s_-]*release|presentation|slides?\b|highlights?|webcast|transcript|infographic|fact[\s_-]*sheet|speech/i;
const fileName = (url) => { try { return decodeURIComponent(new URL(url).pathname.split("/").pop() || ""); } catch { return ""; } };
export const isExcludedAttachment = (f) => EXCLUDED_ATTACHMENT.test(`${f.name || ""} ${fileName(f.url || "")}`);

/**
 * Open one announcement (a Title link) and return the files in its "announcement-attachment-list"
 * element. Pages without that element fall back to any PDF / FileOpen link on the page.
 */
export async function attachments(browser, annUrl, opts = {}) {
  if (!isSgxUrl(annUrl)) throw new Error("Only https://*.sgx.com announcement links can be opened");
  const page = await newPage(browser);
  try {
    if (opts.preparePage) await opts.preparePage(page);
    await page.goto(annUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
    const read = () => page.evaluate(() => {
      const LIST = ".announcement-attachment-list, #announcement-attachment-list, announcement-attachment-list, [class*='announcement-attachment-list']";
      const roots = [];
      const gather = (root) => { roots.push(root); for (const el of root.querySelectorAll("*")) if (el.shadowRoot) gather(el.shadowRoot); };
      gather(document);
      const linksIn = (el) => {
        const found = [...el.querySelectorAll("a[href]")];
        for (const x of el.querySelectorAll("*")) if (x.shadowRoot) found.push(...x.shadowRoot.querySelectorAll("a[href]"));
        return found;
      };
      const lists = roots.flatMap((r) => [...r.querySelectorAll(LIST)]);
      const anchors = lists.length
        ? lists.flatMap(linksIn)
        : roots.flatMap((r) => [...r.querySelectorAll("a[href]")]).filter((a) => /fileopen/i.test(a.href) || /\.pdf($|\?)/i.test(a.href));
      return {
        inList: lists.length > 0,
        files: anchors.map((a) => ({ name: (a.innerText || a.textContent || "").replace(/\s+/g, " ").trim(), url: a.href })),
      };
    }).catch(() => ({ inList: false, files: [] }));
    let got = await read();
    for (let waited = 0; !got.files.length && waited < (opts.waitMs ?? 15_000); waited += 700) {
      await new Promise((r) => setTimeout(r, 700));
      got = await read();
    }
    const seen = new Set();
    return got.files
      .filter((f) => isSgxUrl(f.url) && !isExcludedAttachment(f) && !seen.has(f.url) && seen.add(f.url))
      .map((f) => ({ ...f, from: got.inList ? "announcement-attachment-list" : "page" }));
  } finally {
    await page.close().catch(() => {});
  }
}

/** The readable text of an announcement page (some notices state their date there). */
export async function pageText(browser, url, opts = {}) {
  if (!isSgxUrl(url)) throw new Error("Only https://*.sgx.com pages can be opened");
  const page = await newPage(browser);
  try {
    if (opts.preparePage) await opts.preparePage(page);
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
    let text = "";
    for (let waited = 0; waited < (opts.waitMs ?? 8_000); waited += 500) {
      text = await page.evaluate(() => document.body?.innerText || "").catch(() => "");
      if (text.trim().length > 200) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    return text;
  } finally {
    await page.close().catch(() => {});
  }
}
