// Browser automation for SGX: open the announcements page exactly as a person would,
// read the result list, open each title link and collect the PDFs under "Attachments".
//
// The SGX listing is a JavaScript app backed by api.sgx.com, which refuses requests without
// the token the page itself generates. Driving a real browser avoids that: we read the JSON
// the page loads (and page through it with the page's own headers), falling back to the
// links rendered in the result table.

import puppeteer from "puppeteer-core";

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36";
const ANN_LINK = /links\.sgx\.com\/.*corporate-announcements/i;
const API_HIT = /api\.sgx\.com\/announcements/i;

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
  let code = first(it, "stock_code", "security_code", "securityCode", "issuer_code");
  if (!code) {
    for (const k of ["issuers", "securities", "issuer"]) {
      const v = Array.isArray(it[k]) ? it[k][0] : it[k];
      code = first(v, "stock_code", "security_code", "code");
      if (code) break;
    }
  }
  let broadcast = first(it, "broadcast_date_time", "submission_date_time", "submission_date");
  if (/^\d{12,}$/.test(broadcast)) broadcast = sgt(broadcast);
  return {
    title: first(it, "title", "announcement_title"),
    issuer: first(it, "issuer_name", "security_name", "company_name"),
    stock_code: code,
    url: first(it, "url", "link"),
    broadcast,
    category: first(it, "category_name", "sub_category_name", "cat"),
  };
}

const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };

export function fromRow({ title, url, cells }) {
  // cells: the row's text pieces, e.g. ["30 Oct 2026 06:02 PM", "DBS GROUP HOLDINGS LTD", "<title>"]
  let broadcast = "";
  const rest = [];
  for (const c of cells) {
    const m = /(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})\s*(AM|PM)?)?/i.exec(c);
    if (!broadcast && m && MONTHS[m[2].toLowerCase()]) {
      let h = m[4] ? Number(m[4]) : null;
      if (h !== null && m[6]) h = (h % 12) + (m[6].toUpperCase() === "PM" ? 12 : 0);
      const pad = (n) => String(n).padStart(2, "0");
      broadcast = `${m[3]}-${pad(MONTHS[m[2].toLowerCase()])}-${pad(m[1])}${h !== null ? ` ${pad(h)}:${m[5]}` : ""}`;
      continue;
    }
    if (c !== title && /[A-Za-z]/.test(c)) rest.push(c);
  }
  const issuer = rest.find((c) => c === c.toUpperCase() && c.length > 2) || rest[0] || "";
  return { title, issuer, stock_code: "", url, broadcast, category: "" };
}

// ------------------------------------------------------------------ screening
/**
 * Open an SGX company-announcements page URL and return its announcements.
 * opts.company filters by issuer substring or stock code; opts.preparePage(page) is a test hook.
 */
export async function screen(browser, pageUrl, opts = {}) {
  if (!isSgxUrl(pageUrl)) throw new Error("Only https://*.sgx.com page URLs can be screened");
  const page = await newPage(browser);
  if (opts.preparePage) await opts.preparePage(page);
  const apiPages = [];
  let apiRequest = null;
  page.on("request", (r) => {
    if (API_HIT.test(r.url()) && r.method() === "GET" && !apiRequest) apiRequest = { url: r.url(), headers: r.headers() };
  });
  page.on("response", async (r) => {
    if (!API_HIT.test(r.url()) || r.request().method() !== "GET" || !r.ok()) return;
    try { apiPages.push(await r.json()); } catch { /* not JSON */ }
  });

  const debug = { page_url: pageUrl, api_seen: false, api_status: null, dom_links: 0 };
  page.on("response", (r) => { if (API_HIT.test(r.url())) debug.api_status = r.status(); });
  await page.goto(pageUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });

  // Wait until the page has loaded its list (or clearly shows nothing).
  const deadline = Date.now() + (opts.waitMs ?? 35_000);
  let rows = [];
  while (Date.now() < deadline) {
    if (apiPages.length) break;
    rows = await page.evaluate(collectRows).catch(() => []);
    if (rows.length) {
      await new Promise((r) => setTimeout(r, 1500)); // let the table finish rendering
      rows = await page.evaluate(collectRows).catch(() => rows);
      break;
    }
    const empty = await page.evaluate(() => /no (results|records|announcements) (found|available)/i.test(document.body?.innerText || "")).catch(() => false);
    if (empty) break;
    await new Promise((r) => setTimeout(r, 700));
  }
  debug.api_seen = apiPages.length > 0;
  debug.dom_links = rows.length;
  debug.title = await page.title().catch(() => "");

  let anns = [];
  if (apiPages.length) {
    debug.source = "page data";
    const items = apiPages.flatMap((p) => p.data || []);
    const total = Number(apiPages[0]?.meta?.totalItems || items.length);
    // Page through the rest with the page's own request (same headers and token).
    if (apiRequest && items.length < total) {
      const u = new URL(apiRequest.url);
      const start = Number(u.searchParams.get("pagestart")) || 0;
      const headers = Object.fromEntries(Object.entries(apiRequest.headers).filter(([k]) => /^(authorizationtoken|accept|authorization)$/i.test(k)));
      for (let p = start + 1; items.length < total && p < start + 40; p++) {
        u.searchParams.set("pagestart", String(p));
        const body = await page.evaluate(async (url, h) => {
          const r = await fetch(url, { headers: h });
          return r.ok ? r.json() : null;
        }, u.toString(), headers).catch(() => null);
        if (!body?.data?.length) break;
        items.push(...body.data);
      }
    }
    anns = items.map(fromApiItem);
  } else {
    debug.source = "result table";
    anns = rows.map(fromRow);
    if (!anns.length) debug.body_excerpt = (await page.evaluate(() => document.body?.innerText || "").catch(() => "")).slice(0, 600);
  }
  await page.close();

  const seen = new Set();
  anns = anns.filter((a) => a.url && !seen.has(a.url) && seen.add(a.url));
  if (opts.company) {
    const c = opts.company.toLowerCase();
    anns = anns.filter((a) => a.issuer.toLowerCase().includes(c) || a.stock_code.toLowerCase() === c);
  }
  return { announcements: anns, debug };
}

// Runs inside the page: every announcement link (piercing shadow DOM) with its row's text.
function collectRows() {
  const out = [];
  const seen = new Set();
  const isAnn = (h) => /links\.sgx\.com\/.*corporate-announcements/i.test(h);
  const walk = (root) => {
    for (const a of root.querySelectorAll("a[href]")) {
      if (!isAnn(a.href) || seen.has(a.href)) continue;
      seen.add(a.href);
      // Climb to the row: the largest ancestor that still holds only this one link.
      let row = a;
      for (let i = 0; i < 8 && row.parentElement; i++) {
        const up = row.parentElement;
        const n = [...up.querySelectorAll("a[href]")].filter((x) => isAnn(x.href)).length;
        if (n > 1) break;
        row = up;
      }
      const cells = (row.innerText || "").split(/\n|\t/).map((s) => s.trim()).filter(Boolean);
      out.push({ title: (a.innerText || a.textContent || "").trim(), url: a.href, cells });
    }
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  return out;
}

// ------------------------------------------------------------------ attachments
/** Open one announcement (the Title link) and return the files listed under Attachments. */
export async function attachments(browser, annUrl, opts = {}) {
  if (!isSgxUrl(annUrl)) throw new Error("Only https://*.sgx.com announcement links can be opened");
  const page = await newPage(browser);
  if (opts.preparePage) await opts.preparePage(page);
  await page.goto(annUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
  const files = await page.evaluate(() => {
    const out = [];
    for (const a of document.querySelectorAll("a[href]")) {
      const h = a.href;
      if (/fileopen/i.test(h) || /\.pdf($|\?)/i.test(h)) out.push({ name: (a.innerText || a.textContent || "").trim(), url: h });
    }
    return out;
  });
  await page.close();
  const seen = new Set();
  return files.filter((f) => isSgxUrl(f.url) && !seen.has(f.url) && seen.add(f.url));
}
