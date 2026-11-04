// Which announcements come from companies listed on SGX (shares, REITs, business trusts, SDRs),
// and which from issuers of bonds, notes and other debt only.
//
// The Financial Statements category also carries results filed by foreign banks and companies
// whose only SGX listing is a bond ("BANK OF BARODA" under 2V6B, "GRAB HOLDINGS" under 52WB),
// and by issuers with no stock code at all. Only listed companies are kept:
//   1. listings/companies.csv: the SGX companies list (from the Google Sheet), and a snapshot
//      of SGX's own list of equity securities for listed companies the sheet doesn't have.
//   2. SGX's live list of securities (api.sgx.com/securities, no token needed), so a company
//      that lists after the file was made (a new IPO) is kept from its first day of trading.
// An announcement is kept when one of its stock codes is listed, or, when it carries no stock
// code (the result-table fallback), when its issuer name matches a listed company's name.
// Keep in step with sgx_fin/listed.py.

import { readFileSync } from "node:fs";

const FILE = new URL("../listings/companies.csv", import.meta.url);
const LIVE_URL = "https://api.sgx.com/securities/v1.1?params=nc,n,type";
const EQUITY_TYPES = new Set(["stocks", "reits", "businesstrusts", "adrs"]);
// Company-form words, and the trading-counter tags in SGX's short names ("AEM USD", "Nomura Yen1k").
const SUFFIX = new Set(["LIMITED", "LTD", "PLC", "INC", "CORP", "CORPORATION", "CO", "BHD", "BERHAD",
  "SGD", "USD", "HKD", "RMB", "YEN1K", "OV"]);

export const words = (name) =>
  String(name || "").toUpperCase().replace(/&/g, " AND ").split(/[^A-Z0-9]+/).filter((w) => w && !SUFFIX.has(w));

// "Hotung Inv" ~ "HOTUNG INVESTMENT HOLDINGS LIMITED", "YHI Intl" ~ "YHI INTERNATIONAL LIMITED",
// "TheHourGlass" ~ "THE HOUR GLASS LIMITED", "KSH" ~ "KSH HOLDINGS LIMITED" (3 letters: whole words only).
export function nameMatches(short, issuer) {
  const a = words(short), b = words(issuer);
  const ca = a.join(""), cb = b.join("");
  if (ca.length < 3 || !cb) return false;
  if (ca.length === 3) return a.length <= b.length && a.every((w, i) => w === b[i]);
  if (ca === cb || (ca.length >= 5 && cb.startsWith(ca))) return true;
  const abbreviates = (w, v) => {
    if (v.startsWith(w)) return true;
    if (w[0] !== v[0]) return false;
    let i = 0;
    for (const ch of v) if (ch === w[i]) i++;
    return i === w.length;
  };
  return a.length <= b.length && a.every((w, i) => abbreviates(w, b[i]));
}

function parseCsv(text) {
  const [head, ...lines] = text.trim().split(/\r?\n/);
  const cols = head.split(",");
  return lines.map((line) => {
    const cells = [];
    for (const m of line.matchAll(/("(?:[^"]|"")*"|[^,]*)(,|$)/g)) {
      cells.push(m[1].startsWith('"') ? m[1].slice(1, -1).replace(/""/g, '"') : m[1]);
      if (!m[2]) break;
    }
    return Object.fromEntries(cols.map((c, i) => [c, (cells[i] || "").trim()]));
  });
}

export function bundled() {
  return parseCsv(readFileSync(FILE, "utf8")).filter((r) => r.code).map((r) => ({ code: r.code.toUpperCase(), name: r.name }));
}

async function live() {
  const r = await fetch(LIVE_URL, {
    headers: { Accept: "application/json", Origin: "https://www.sgx.com", Referer: "https://www.sgx.com/" },
    signal: AbortSignal.timeout(20_000),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const prices = (await r.json())?.data?.prices || [];
  const out = prices.filter((p) => p?.nc && EQUITY_TYPES.has(p.type)).map((p) => ({ code: String(p.nc).toUpperCase(), name: p.n || "" }));
  if (out.length < 100) throw new Error(`only ${out.length} equities listed`);
  return out;
}

let cached;
/** The listed companies: { codes: Set, names: [...], source: "bundled + SGX live" | "bundled" }. */
export async function loadListed() {
  if (cached) return cached;
  const all = bundled();
  let source = "bundled + SGX live", error;
  try { all.push(...(await live())); } catch (e) { source = "bundled"; error = String(e.message || e); }
  cached = { codes: new Set(all.map((c) => c.code)), names: [...new Set(all.map((c) => c.name).filter(Boolean))], source, error };
  return cached;
}

/** Split announcements into those from listed companies and the rest (debt and other issuers). */
export function filterListed(anns, listed) {
  const kept = [], dropped = [];
  for (const a of anns) {
    const codes = (a.stock_codes?.length ? a.stock_codes : [a.stock_code]).map((c) => String(c || "").toUpperCase()).filter(Boolean);
    const code = codes.find((c) => listed.codes.has(c));
    if (code) {
      a.stock_code = code; // an announcement filed for both shares and bonds: show the share code
      kept.push(a);
    } else if (!codes.length && listed.names.some((n) => nameMatches(n, a.issuer))) {
      kept.push(a);
    } else {
      dropped.push(a);
    }
  }
  return { kept, dropped };
}
