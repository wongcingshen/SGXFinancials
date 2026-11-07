// SGX Financials front end. Talks to /api/health, /api/browse (or /api/screen) and /api/process.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name, style = "") => `<svg class="i" ${style ? `style="${style}"` : ""}><use href="#i-${name}"/></svg>`;
const store = {
  get(k, d = "") { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const ACCENTS = ["lagoon", "indigo", "lacquer", "olive", "plum", "ochre", "rose", "slate"];
const STATEMENTS = [
  { key: "income_statement", cls: "s-is", name: "Income statement", icon: "trend" },
  { key: "balance_sheet", cls: "s-bs", name: "Balance sheet", icon: "scale" },
  { key: "cash_flow", cls: "s-cf", name: "Cash flow", icon: "cash" },
];
const MAX_UPLOAD = 4_000_000;

const state = {
  health: null,
  ai: "AI",
  effort: store.get("sgxf-effort", "high"),
  items: [], // announcements on screen (+ filings processed directly), each with its job state
  bulk: null, // { stop: bool } while Extract all runs
  dir: null, // folder chosen for downloads (FileSystemDirectoryHandle)
  folderOk: false, // write permission granted in this visit
};

// SGX lists issuers in capitals; "DBS GROUP HOLDINGS LTD" -> "DBS Group Holdings Ltd".
const LOWER_SHORT = new Set(["LTD", "CO", "INC", "AND", "THE", "OF", "PTE", "BHD", "TBK", "CORP"]);
function nameCase(s) {
  if (!s || s !== s.toUpperCase()) return s || "";
  let first = true;
  return s.split(/(\s+|-|\/)/).map((w) => {
    const core = w.replace(/[^A-Z&]/g, "");
    const isFirst = first && core.length > 0;
    if (core) first = false;
    const acronym = !/[AEIOUY]/.test(core) || (isFirst && core.length === 3) || core === "REIT";
    if (!core || /\d/.test(w) || (acronym && !LOWER_SHORT.has(core))) return w;
    return w.charAt(0) + w.slice(1).toLowerCase();
  }).join("");
}
// "Financial Statements and Related Announcement::Third Quarter Results" -> "Third Quarter Results"
const shortTitle = (t) => String(t || "").replace(/^.*?::\s*/, "") || t || "";
function when(broadcast, withYear = false) {
  const m = /^(\d{4})-?(\d\d)-?(\d\d)(?:[ T](\d\d:\d\d))?/.exec(broadcast || "");
  if (!m) return broadcast || "";
  const d = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00`).toLocaleDateString("en-SG",
    withYear ? { day: "numeric", month: "short", year: "numeric" } : { day: "numeric", month: "short" });
  return m[4] ? `${d} · ${m[4]}` : d;
}

function accentFor(key) {
  let h = 0;
  for (const ch of String(key || "x")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return ACCENTS[h % ACCENTS.length];
}

function toast(msg) {
  document.querySelector(".toast")?.remove();
  const t = document.createElement("div");
  t.className = "toast";
  t.setAttribute("role", "status");
  t.textContent = msg;
  document.body.append(t);
  setTimeout(() => t.remove(), 3200);
}

// Tells the guided tour (tour.js) that something it waits for has happened.
const tourSignal = (name) => document.dispatchEvent(new CustomEvent("sgxf:tour", { detail: name }));

async function api(path, opts = {}) {
  const headers = { "X-App-Password": store.get("sgxf-pw"), ...(opts.headers || {}) };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs || 330_000);
  let r;
  try { r = await fetch(path, { ...opts, headers, signal: ctrl.signal }); }
  catch (e) {
    throw new Error(e.name === "AbortError"
      ? "No answer after 5 minutes (the server’s time limit). Try again: the PDF is kept, so the retry is faster. Or open the details and choose fewer pages."
      : "Can’t reach the server. Check your connection and try again.");
  } finally { clearTimeout(timer); }
  const body = await r.json().catch(() => ({ error: r.status === 504 ? "The server took too long (Vercel time limit). Try again; downloads are cached." : `HTTP ${r.status}` }));
  if (r.status === 401) throw Object.assign(new Error("Wrong or missing app password. Add it in Settings."), { auth: true });
  if (!r.ok) throw Object.assign(new Error(body.error || `HTTP ${r.status}`), { body, status: r.status });
  return body;
}

// ------------------------------------------------------------------ dates & screen URL
// Default screen, in Singapore time: on Saturday, Sunday and Monday, the weekend's Friday to Sunday;
// on Tuesday to Friday, the day before.
function defaultRange(now = Date.now()) {
  const sgt = new Date(now + 8 * 3600e3); // read with getUTC* = Singapore calendar
  const day = (offset) => new Date(sgt.getTime() + offset * 864e5).toISOString().slice(0, 10);
  switch (sgt.getUTCDay()) {
    case 6: return [day(-1), day(1)]; // Saturday: Fri – Sun
    case 0: return [day(-2), day(0)]; // Sunday:   Fri – Sun
    case 1: return [day(-3), day(-1)]; // Monday:   Fri – Sun
    default: return [day(-1), day(-1)]; // Tue – Fri: the previous day
  }
}
const compact = (d) => d.replaceAll("-", "");
const nice = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-SG", { day: "numeric", month: "short", year: "numeric" });

const pageUrl = () =>
  `https://www.sgx.com/stock-exchange/company-announcements?from=${compact($("#from").value)}&to=${compact($("#to").value)}&ANNC=ANNC17`;

// ------------------------------------------------------------------ server status
async function loadHealth() {
  const pill = $("#statusPill");
  try {
    const r = await fetch("/api/health?op=health");
    const h = await r.json();
    if (!r.ok) throw new Error(h.error || `HTTP ${r.status}`);
    state.health = h;
    state.ai = h.provider_label || "AI";
    document.querySelectorAll("[data-ai]").forEach((el) => { el.textContent = state.ai; });
    if ($("#results .steps")) renderIntro();
    pill.className = "status-pill " + (h.ai_key ? "ok" : "warn");
    $("#statusText").textContent = h.ai_key ? `${state.ai} ready` : "AI key missing";
    $("#serverInfo").innerHTML = `${icon("info")}<div><b>Server</b><br>
      Model: <span class="mono">${esc(h.model || "—")}</span><br>
      AI: ${h.ai_key ? `${esc(state.ai)} (key configured)` : `<b>no key</b> — add GEMINI_API_KEY to this Vercel project (${esc(h.deployment || "")}), then redeploy`}<br>
      Photo filings: ${(h.vision_models || []).length ? (h.vision_models || []).map(esc).join(", then ") : "no AI for pictures"}${h.photo_model ? "" : " (set GLM_FLASH to add GLM 5.3 Flash as a fallback)"}<br>
      AI variables visible: ${esc((h.ai_vars || []).join(", ") || "none")}<br>
      App password: ${h.password_required ? "required" : "not set (anyone with the link can run extractions)"}<br>
      SGX token: ${h.sgx_token ? "configured" : "not set (only needed if SGX refuses requests)"}</div>`;
  } catch (e) {
    pill.className = "status-pill warn";
    $("#statusText").textContent = "Server offline";
    $("#serverInfo").textContent = `The API isn’t reachable: ${e.message || e}`;
  }
}

// ------------------------------------------------------------------ announcements
function renderIntro() {
  $("#results").innerHTML = `
    <div class="intro">
      <div>
        <h3 style="margin-top:0">Pick the dates and screen SGX</h3>
        <p>The dates start on the last results days: Friday to Sunday over a weekend, otherwise yesterday. Leave the company blank to see every financial statements announcement.</p>
        <p style="margin-bottom:0">New here? <button type="button" class="link-btn" data-tour-start>Take the tour</button> <span class="muted">— about three minutes, on the real page.</span></p>
      </div>
      <ol class="steps">
        <li><span class="badge">1</span><span><strong>Screen</strong> <span class="muted">— opens every title and lists its PDFs.</span></span></li>
        <li><span class="badge">2</span><span><strong>Extract</strong> <span class="muted">— a keyword scan finds the three statements; ${esc(state.ai)} reads only those pages.</span></span></li>
        <li><span class="badge">3</span><span><strong>Excel</strong> <span class="muted">— a standard workbook with tie-out checks.</span></span></li>
      </ol>
    </div>`;
  renderBulk();
}

function renderSkeleton() {
  const bone = (w, h, extra = "") => `<div class="bone" style="width:${w};height:${h}px;${extra}"></div>`;
  $("#results").innerHTML = `<p class="muted" style="margin:0 0 14px;font-size:14px">Opening sgx.com, reading the results and opening every title for its PDFs. This takes about 20–90 seconds.</p>
    <div class="grid" aria-busy="true" aria-label="Loading announcements">${
    Array.from({ length: 6 }, () => `<div class="skeleton">${bone("100%", 104, "border-radius:0")}
      <div style="padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px">${bone("70%", 20)}${bone("95%", 12)}${bone("60%", 12)}
      ${bone("100%", 42, "border-radius:999px;margin-top:10px")}</div></div>`).join("")
  }</div>`;
}

// Press releases, presentations, highlights and the like never hold the formal statements: they are
// left out of every attachment list. Keep in step with EXCLUDED_ATTACHMENT in scraper/sgx.mjs.
const EXCLUDED_ATTACHMENT =
  /(press|media|news)[\s_-]*release|presentation|slides?\b|highlights?|webcast|transcript|infographic|fact[\s_-]*sheet|speech/i;
const fileOf = (u) => { try { return decodeURIComponent(new URL(u).pathname.split("/").pop() || ""); } catch { return ""; } };
const keepAttachments = (files) => Array.isArray(files)
  ? files.filter((f) => !EXCLUDED_ATTACHMENT.test(`${f.name || ""} ${fileOf(f.url || "")}`))
  : files;

// The PDFs in the announcement's attachment list, each linked. Once extracted, the one that held
// the financial statements is shown first and marked; the rest (releases, slides) are listed after.
function filesLine(a) {
  if (!Array.isArray(a.attachments)) return "";
  if (!a.attachments.length) return `<span class="files none">${icon("file", "width:14px;height:14px")}No financial statements PDF (press releases, presentations and highlights are hidden)</span>`;
  const usedUrl = (a.job?.result || a.job?.scan)?.scan?.used_url;
  const link = (f) => `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.name || "PDF")}</a>`;
  const used = usedUrl && a.attachments.find((f) => f.url === usedUrl);
  if (used) {
    const others = a.attachments.filter((f) => f !== used);
    const verb = a.job?.status === "skipped" ? "Checked" : "Extracted from";
    return `<span class="files used">${icon(a.job?.status === "skipped" ? "file" : "check", "width:14px;height:14px")}<span>${verb} ${link(used)}${
      others.length ? `<span class="others"> · not used: ${others.map(link).join(", ")}</span>` : ""}</span></span>`;
  }
  return `<span class="files">${icon("file", "width:14px;height:14px")}<span>${a.attachments.length} PDF${a.attachments.length === 1 ? "" : "s"}: ${a.attachments.map(link).join(", ")}</span></span>`;
}

const BUSY = { queued: "Queued", reading: "Reading PDF…", asking: "Photos: your choice…", ai: () => `${state.ai} reading…` };

// Whether a finished extraction can be relied on: the server's verdict (ok only when a tie-out check
// passes, none fails, and every statement and gross profit is there). "0 checks pass" is a failure,
// not a success: nothing confirms such figures (a presentation's highlights, lines read as 0).
function verdictOf(res) {
  if (res?.verdict) return res.verdict;
  const checks = res?.checks || []; // a server from before verdicts: judge by the checks alone
  const pass = checks.filter((c) => c.status === "PASS").length;
  const fail = checks.filter((c) => c.status === "FAIL").length;
  const reasons = [fail && `${fail} tie-out check${fail === 1 ? " fails" : "s fail"}`,
    !pass && "no tie-out check passes, so nothing confirms the figures"].filter(Boolean);
  return { ok: !reasons.length, pass, fail, reasons, summary: [fail && `${fail} check${fail === 1 ? " fails" : "s fail"}`, !pass && "0 checks pass"].filter(Boolean).join(" · ") };
}
const isSlides = (j) => Boolean((j.scan || j.result)?.scan?.presentation);

function cardHtml(a, i) {
  const j = a.job;
  const busy = j.status in BUSY;
  const label = j.status === "ai" ? `${j.ai || state.ai} reading…` : BUSY[j.status];
  const v = j.status === "done" ? verdictOf(j.result) : null;
  const unverified = Boolean(v && !v.ok);
  const pill = j.status === "done" ? (unverified ? `${icon("alert", "width:14px;height:14px")}Check failed` : `${icon("check", "width:14px;height:14px")}Excel ready`)
    : j.status === "failed" ? `${icon("alert", "width:14px;height:14px")}Failed`
    : j.status === "skipped" ? `${icon("x", "width:14px;height:14px")}${isSlides(j) ? "Slides only" : "Not results"}`
    : j.status === "claude" ? `${icon("sparkles", "width:14px;height:14px")}Reading in Claude`
    : busy ? `<span class="spin" aria-hidden="true"></span>${esc(label)}${j.since ? ` <span class="elapsed" data-since="${j.since}">${clock(j.since)}</span>` : ""}`
    : `<span class="dot"></span>${esc(when(a.broadcast) || "SGX")}`;
  const claudeBtn = (why, text, cls = "btn-ember") => !a.url ? "" : `<button type="button" class="btn ${cls}" data-claude="${i}" data-why="${why}" ${why === "recheck" ? 'data-tour="recheck"' : ""} title="Opens a Claude chat that reads the filing itself through the SGX Financials connector">${icon("sparkles")}${text}</button>`;
  const action = j.status === "done"
    ? unverified
      ? a.url
        ? `${claudeBtn("recheck", "Check with Claude")}<a class="btn btn-outline btn-icon" data-tour="excel" href="${j.blobUrl}" download="${esc(j.filename)}" title="Download the Excel anyway (figures not verified)" aria-label="Download the Excel anyway (figures not verified)">${icon("download")}</a>`
        : `<a class="btn btn-ember" data-tour="excel" href="${j.blobUrl}" download="${esc(j.filename)}" title="The figures are not verified">${icon("download")}Excel</a>`
      : `<a class="btn btn-ember" data-tour="excel" href="${j.blobUrl}" download="${esc(j.filename)}">${icon("download")}Excel</a>`
    : busy
      ? `<button type="button" class="btn btn-ember" disabled><span class="spin" aria-hidden="true"></span>${esc(label)}</button>`
      : j.status === "skipped" ? (isSlides(j) && a.url
        ? `${claudeBtn("skipped", "Ask Claude", "btn-outline")}<button type="button" class="btn btn-outline btn-icon" data-details="${i}" title="Why skipped" aria-label="Why skipped">${icon("eye")}</button>`
        : `<button type="button" class="btn btn-outline" data-details="${i}">${icon("eye")}Why skipped</button>`)
      : j.status === "claude" ? `<button type="button" class="btn btn-ember" data-claude="${i}">${icon("sparkles")}Open in Claude</button>
        <button type="button" class="btn btn-outline" data-gemini="${i}" title="Read it here with ${esc(state.ai)} instead">Use ${esc(state.ai)}</button>`
      : `<button type="button" class="btn ${a.kind === "other" ? "btn-outline" : "btn-ember"}" data-extract="${i}" ${a.kind === "results" ? 'data-tour="extract"' : ""}>${icon("sparkles")}${j.status === "failed" ? "Try again" : "Extract"}</button>`;
  const saved = j.saved ? ` · ${j.merged ? "merged into" : "saved to"} folder` : j.saveError ? " · not saved to folder" : "";
  const status = j.status === "done"
    ? unverified
      ? `<span class="card-note bad multiline" data-tour="verdict" title="${esc(v.reasons.join("; "))}">${j.sentToClaude ? "Sent to Claude · Excel link in the chat" : `Not verified: ${esc(v.summary || v.reasons[0] || "")}`}${saved}</span>`
      : `<span class="card-note" data-tour="verdict">${v.pass} check${v.pass === 1 ? "" : "s"} pass, none fails${saved}</span>`
    : j.status === "failed" ? `<span class="card-note bad" title="${esc(j.error)}">${esc(j.error)}</span>`
    : j.status === "skipped" ? `<span class="card-note muted" title="${esc(j.error)}">${esc(isSlides(j) ? "Presentation only: no formal financial statements" : j.error)}</span>`
    : j.status === "claude" ? `<span class="card-note muted" title="The Excel link arrives in the Claude chat">Sent to Claude ${esc(CLAUDE_MODEL)} · Excel link in the chat</span>`
    : a.reason ? `<span class="card-note muted" title="${esc(a.reason)}">${esc(a.reason)}</span>` : "";
  const hasDetails = j.scan || j.result || j.error;
  const name = esc(nameCase(a.issuer) || a.fileName || "Unknown issuer");
  // Cards fade in once, when a screen's results first appear; later redraws don't replay it.
  const motion = state.animate ? `animation-delay:${Math.min(i, 12) * 30}ms` : "animation:none";
  return `<article class="card ${j.status} ${unverified ? "unverified" : ""} ${a.kind === "other" ? "other" : ""}" data-card="${i}" ${a.kind === "results" ? 'data-tour="card"' : ""} data-accent="${accentFor(a.stock_code || a.issuer)}" style="${motion}">
    <div class="band">
      <svg class="route" viewBox="0 0 260 104" preserveAspectRatio="none" aria-hidden="true"><path d="M20 84 C70 70 90 40 140 46 S210 20 250 14" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="2" stroke-linecap="round" stroke-dasharray="1 7"/></svg>
      <span class="pill">${pill}</span>
      <span class="code">${esc(a.stock_code || "SGX")}</span>
    </div>
    <div class="card-body">
      ${hasDetails ? `<button type="button" class="name name-btn" data-details="${i}" ${j.status === "done" ? 'data-tour="details"' : ""} title="Show pages and checks">${name}</button>` : `<span class="name">${name}</span>`}
      ${a.url ? `<a class="title" href="${esc(a.url)}" target="_blank" rel="noopener" title="${esc(a.title)}">${esc(shortTitle(a.title))}</a>` : `<span class="title">${esc(shortTitle(a.title))}</span>`}
      ${filesLine(a)}
      ${status}
      <div class="card-actions">${action}</div>
    </div>
  </article>`;
}

// Some issuers file the same results twice: the consolidated (group) statements and the separate
// (parent-company-only) ones. File names usually say which; otherwise the keyword scan's reading of
// the statement headings decides during extraction. Only the consolidated filing is extracted.
const CONSOL_NAME = /consolidat|consol[^a-z]|(^|[^a-z])group([^a-z]|$)|hop[\s_-]?nhat|hợp nhất/i;
const SEPARATE_NAME = /separate|(^|[^a-z])sep([^a-z]|$)|standalone|stand[\s_-]alone|company[\s_-]only|parent[\s_-]company|rieng|riêng/i;
function basisFromNames(a) {
  const text = [shortTitle(a.title), ...(a.attachments || []).map((f) => f.name || "")].join(" ");
  const c = CONSOL_NAME.test(text), s = SEPARATE_NAME.test(text);
  return c && !s ? "consolidated" : s && !c ? "separate" : "unknown";
}
const dupKey = (a) => [nameCase(a.issuer).toLowerCase(), shortTitle(a.title).toLowerCase(), String(a.broadcast || "").slice(0, 10)].join("|");
const SEPARATE_NOTE = "Separate (company-only) statements: the consolidated filing is extracted instead.";

function markDuplicates(items) {
  const groups = new Map();
  for (const a of items.filter((x) => x.kind === "results")) {
    const k = dupKey(a);
    groups.set(k, [...(groups.get(k) || []), a]);
  }
  for (const [k, group] of groups) {
    if (group.length < 2) continue;
    const consolidated = group.filter((a) => basisFromNames(a) === "consolidated");
    if (consolidated.length) {
      for (const a of group) if (!consolidated.includes(a)) Object.assign(a, { kind: "other", reason: SEPARATE_NOTE });
    } else {
      for (const a of group) a.dupGroup = k; // decided by the statement headings when extracted
    }
  }
}

// Notices of the results release date: read the date, period and timing from each (no AI).
async function loadReleaseDates() {
  const queue = state.items.filter((a) => a.kind === "release_date" && !a.release);
  const worker = async () => {
    while (queue.length) {
      const a = queue.shift();
      try {
        a.release = await api(`/api/release_date?op=release_date&url=${encodeURIComponent(a.url)}`, { timeoutMs: 60_000 });
      } catch (e) {
        a.release = { failed: true };
        if (e.body?.need_browser) {  // links.sgx.com refused a direct read: let the browser read the page
          try {
            const { text } = await api(`/api/browse?op=text&url=${encodeURIComponent(a.url)}`, { timeoutMs: 90_000 });
            const fd = new FormData();
            fd.append("text", text || "");
            a.release = await api("/api/release_text?op=release_text", { method: "POST", body: fd, timeoutMs: 30_000 });
          } catch { /* keep "date not found" */ }
        }
      }
      renderReleases();
    }
  };
  await Promise.all([worker(), worker(), worker()]);
}

function renderReleases() {
  const el = $("#releases");
  if (!el) return;
  const list = state.items.filter((a) => a.kind === "release_date")
    .sort((x, y) => (x.release?.release_date || "9999").localeCompare(y.release?.release_date || "9999"));
  const day = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString("en-SG", { weekday: "short", day: "numeric", month: "short", year: "numeric" }) : "";
  el.innerHTML = list.map((a) => {
    const r = a.release;
    const when = !r ? `<span class="spin" aria-hidden="true"></span>reading…` : r.release_date ? `<b>${esc(day(r.release_date))}</b>` : `<span class="muted">date not found</span>`;
    return `<div class="rel-row"><span class="rel-when">${when}${r?.timing ? `<small>${esc(r.timing)}</small>` : ""}</span>
      <span class="rel-co"><b>${esc(nameCase(a.issuer))}</b><small>${esc(r?.period || shortTitle(a.title))}</small></span>
      <a class="rel-link" href="${esc(r?.pdf_url || a.url)}" target="_blank" rel="noopener">${icon("file", "width:15px;height:15px")}Notice</a></div>`;
  }).join("");
  saveScreen();
}

function renderCards() {
  const list = state.items;
  const nResults = list.filter((a) => a.kind === "results").length;
  const nRelease = list.filter((a) => a.kind === "release_date").length;
  const nOther = list.length - nResults - nRelease;
  const hidden = state.debug?.not_listed || [];  // issuers with only bonds or notes on SGX
  if (state.range !== undefined) {
    $("#annNote").textContent = [`${nResults} results filing${nResults === 1 ? "" : "s"}`,
      nRelease && `${nRelease} release date${nRelease === 1 ? "" : "s"}`, nOther && `${nOther} other`,
      hidden.length && `${hidden.length} from bond issuers left out`, state.range].filter(Boolean).join(" · ");
    $("#annNote").title = hidden.length ? `Not listed companies on SGX (bonds, notes only): ${[...new Set(hidden.map((h) => h.issuer))].join(", ")}` : "";
  }
  if (!list.length) {
    const d = state.debug || {};
    const saw = d.source ? `<p style="font-size:12.5px">What the browser saw: “${esc(d.title || "untitled page")}”, ${d.counts ? `${d.counts.table} rows in the table, ${d.counts.page_data} in the page data` : `${d.dom_links ?? 0} links`}.${
      d.body_excerpt ? `<br><span class="mono">${esc(d.body_excerpt.slice(0, 240))}</span>` : ""}</p>` : "";
    $("#results").innerHTML = `<div class="intro"><div><h3 style="margin-top:0">No financial results for these dates</h3>
      <p>SGX lists nothing under Financial Statements for these dates${$("#company").value.trim() ? " and this company" : ""}. Try a wider range.</p>${hidden.length
      ? `<p style="font-size:12.5px">${hidden.length} announcement${hidden.length === 1 ? " was" : "s were"} left out: issuers with only bonds or notes on SGX (${esc([...new Set(hidden.map((h) => h.issuer))].join(", "))}).</p>` : ""}${saw}</div></div>`;
  } else {
    const results = list.map((a, i) => [a, i]).filter(([a]) => a.kind === "results");
    const releases = list.filter((a) => a.kind === "release_date");
    const others = list.map((a, i) => [a, i]).filter(([a]) => a.kind === "other");
    $("#results").innerHTML = (results.length
      ? `<div class="grid">${results.map(([a, i]) => cardHtml(a, i)).join("")}</div>`
      : `<p class="muted" style="margin:0">No results filings for these dates.</p>`)
      + (releases.length ? `<section class="releases" data-tour="releases"><h3>${icon("calendar", "width:18px;height:18px")}Results release dates</h3>
          <p class="muted">Companies announcing when their results will come out.</p><div id="releases"></div></section>` : "")
      + (others.length ? `<details class="others" ${state.othersOpen ? "open" : ""}><summary data-tour="others">${icon("file", "width:15px;height:15px")}
          ${others.length} other announcement${others.length === 1 ? "" : "s"} in this category: releases, presentations, updates and duplicate filings.
          Not extracted unless you choose to.</summary>
          <div class="grid">${others.map(([a, i]) => cardHtml(a, i)).join("")}</div></details>` : "");
    $("#results .others")?.addEventListener("toggle", (e) => { state.othersOpen = e.target.open; });
    renderReleases();
  }
  renderBulk();
  saveScreen();
}

function renderCard(item) {
  const i = state.items.indexOf(item);
  const el = $(`#results [data-card="${i}"]`);
  if (!el) return renderCards();
  el.outerHTML = cardHtml(item, i);
  renderBulk();
  saveScreen();
  if ($("#runSheet").open && $("#runSheet").dataset.key === item.key) showDetails(item);
}

const clock = (since) => {
  const s = Math.max(0, Math.round((Date.now() - since) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
setInterval(() => document.querySelectorAll(".elapsed").forEach((el) => { el.textContent = clock(+el.dataset.since); }), 1000);

// The main action: Extract all → Stop while running → Download all once workbooks exist.
function renderBulk() {
  const el = $("#bulk");
  const items = state.items;
  const done = items.filter((a) => a.job.status === "done");
  const todo = items.filter((a) => a.kind === "results" && (a.job.status === "idle" || a.job.status === "failed"));
  const working = items.filter((a) => a.job.status in BUSY).length;
  let html = "";
  if (state.bulk) {
    html = `<span class="bulk-note">${done.length} of ${items.filter((x) => x.kind === "results" || x.job.status === "done").length} done</span>
      <button type="button" class="btn btn-outline" id="bulkStop" ${state.bulk.stop ? "disabled" : ""}>${state.bulk.stop ? "Stopping…" : "Stop"}</button>`;
  } else {
    if (done.length > 1 || (done.length && !todo.length && !working))
      html += `<button type="button" class="btn ${todo.length ? "btn-outline" : "btn-ember"}" id="bulkZip" data-tour="download-all">${icon("download")}Download all (${done.length})</button>`;
    if (todo.length > 1 || (todo.length && !done.length))
      html += `<button type="button" class="btn btn-ember" id="bulkRun" data-tour="extract-all">${icon("sparkles")}Extract all (${todo.length})</button>`;
  }
  el.innerHTML = html;
  el.hidden = !html;
}

async function screen(ev) {
  ev?.preventDefault();
  if (state.bulk) return toast("Stop Extract all before screening again.");
  const btn = $("#screenBtn");
  if ($("#from").value > $("#to").value) {
    toast("The From date is after the To date.");
    return;
  }
  btn.disabled = true;
  state.items = [];
  clearSavedScreen(); // the last screen's cards are replaced by this one's
  renderBulk();
  renderSkeleton();
  try {
    const q = new URLSearchParams({ url: pageUrl(), company: $("#company").value.trim() });
    // A browser reads the SGX page like a person would; with SGX_AUTH_TOKEN set, try the direct API first.
    const routes = state.health?.sgx_token ? ["/api/screen?op=screen&attachments=1", "/api/browse?op=screen&attachments=1"] : ["/api/browse?op=screen&attachments=1"];
    let res, err;
    for (const route of routes) {
      try { res = await api(`${route}&${q}`); break; } catch (e) { err = e; if (e.auth) break; }
    }
    if (!res) throw err;
    state.items = res.announcements.map((a) => ({ ...a, attachments: keepAttachments(a.attachments), kind: a.kind || "results", key: a.url, job: { status: "idle" } }));
    markDuplicates(state.items);
    state.animate = true;
    state.debug = res.debug || null;
    const m = res.page_url.match(/from=(\d{8}).*?to=(\d{8})/);
    const fmt = (s) => nice(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`);
    state.range = m ? (m[1] === m[2] ? fmt(m[1]) : `${fmt(m[1])} – ${fmt(m[2])}`) : "";
    renderCards();
    state.animate = false;
    loadReleaseDates();
  } catch (e) {
    $("#results").innerHTML = `<div class="notice error">${icon("alert")}<div><b>Couldn’t screen SGX.</b> ${esc(e.message)}</div></div>`;
    if (e.auth) openSheet("#settingsSheet");
  } finally {
    btn.disabled = false;
    tourSignal("screened");
  }
}

// ------------------------------------------------------------------ download folder
// Chrome and Edge can write straight into a folder you pick (File System Access API). The folder
// keeps one workbook per company, and each new extraction is merged into the file already there.
const canPickFolder = typeof window.showDirectoryPicker === "function";
const workbookName = (a) =>
  `${String(a.stock_code || a.issuer || "").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "financials"}.xlsx`;

function idb(mode, fn) {
  return new Promise((resolve) => {
    try {
      const open = indexedDB.open("sgxf", 1);
      open.onupgradeneeded = () => open.result.createObjectStore("kv");
      open.onsuccess = () => {
        try {
          const tx = open.result.transaction("kv", mode);
          const req = fn(tx.objectStore("kv"));
          tx.oncomplete = () => resolve(req?.result);
          tx.onerror = tx.onabort = () => resolve(undefined);
        } catch { resolve(undefined); } // e.g. a handle this browser can't store
      };
      open.onerror = () => resolve(undefined);
    } catch { resolve(undefined); }
  });
}

// ------------------------------------------------------------------ last screen, kept across reloads
// The cards of the last screen (with their extractions and workbooks) are kept in this browser, so a
// refresh shows them again. A new screen replaces them. Work still running when the page went away
// can't resume: those cards come back as stopped, ready to try again.
const SCREEN_KEY = "screen";
let saveTimer = null;
function saveScreen() {
  if (!state.restored) return; // not before the saved screen has been read back
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const items = state.items.map((a) => {
      const { blobUrl, ...job } = a.job;
      if (job.result?.workbook_b64) job.result = { ...job.result, workbook_b64: undefined }; // the blob holds it
      if (job.status in BUSY) Object.assign(job, { status: "failed", since: null, error: "Stopped when the page was reloaded. Try again." });
      return { ...a, job };
    });
    const saved = { items, range: state.range, debug: state.debug, from: $("#from").value, to: $("#to").value,
      company: $("#company").value, at: Date.now() };
    idb("readwrite", (st) => st.put(saved, SCREEN_KEY));
  }, 300);
}

function clearSavedScreen() {
  clearTimeout(saveTimer);
  idb("readwrite", (st) => st.delete(SCREEN_KEY));
}

async function restoreScreen() {
  const saved = await idb("readonly", (st) => st.get(SCREEN_KEY));
  state.restored = true;
  if (!saved?.items?.length || state.items.length || $("#screenBtn").disabled) return; // a new screen has started
  state.items = saved.items.map((a) => ({ ...a, attachments: keepAttachments(a.attachments), job: { ...a.job, blobUrl: a.job.blob ? URL.createObjectURL(a.job.blob) : undefined } }));
  Object.assign(state, { range: saved.range ?? "", debug: saved.debug || null });
  if (saved.from) $("#from").value = saved.from;
  if (saved.to) $("#to").value = saved.to;
  $("#company").value = saved.company || "";
  renderCards();
  loadReleaseDates(); // any notice whose date hadn't been read yet
}

function renderFolder() {
  const b = $("#folderBtn");
  b.hidden = !canPickFolder;
  $("#folderLabel").textContent = state.dir ? `Saving to “${state.dir.name}”` : "Choose download folder";
  b.title = state.dir ? "Workbooks are saved here. Click to choose another folder." : "Save every workbook straight into a folder";
  b.classList.toggle("on", Boolean(state.dir));
}

async function chooseFolder() {
  if (!canPickFolder) return false;
  try {
    state.dir = await window.showDirectoryPicker({ id: "sgx-financials", mode: "readwrite" });
    state.folderOk = true;
    renderFolder();
    renderBulk();
    toast(`Workbooks will be saved in “${state.dir.name}”.`);
    await idb("readwrite", (st) => st.put(state.dir, "dir")); // remembered for the next visit
    return true;
  } catch (e) {
    if (e.name !== "AbortError") toast(`Couldn’t use that folder: ${e.message}`);
    return false;
  }
}

// Write access has to be re-granted after a reload, and only from a click: call this first in click handlers.
async function folderReady() {
  if (!state.dir) return (state.folderOk = false);
  try {
    if ((await state.dir.queryPermission({ mode: "readwrite" })) !== "granted")
      await state.dir.requestPermission({ mode: "readwrite" });
    state.folderOk = (await state.dir.queryPermission({ mode: "readwrite" })) === "granted";
  } catch { state.folderOk = false; }
  return state.folderOk;
}

async function readFromFolder(name) {
  try { return await (await state.dir.getFileHandle(name)).getFile(); } catch { return null; }
}

async function saveToFolder(name, blob) {
  const w = await (await state.dir.getFileHandle(name, { create: true })).createWritable();
  await w.write(blob);
  await w.close();
}

// ------------------------------------------------------------------ extraction
function formData(a, dryRun, pages, workbook, files) {
  const fd = new FormData();
  if (a.url && !a.file) fd.append("announcement_url", a.url);
  for (const [k, v] of [["issuer", a.issuer], ["stock_code", a.stock_code], ["title", a.title]]) if (v) fd.append(k, v);
  for (const f of files || a.attachments || []) { fd.append("pdf_url", f.url); fd.append("pdf_name", f.name || ""); }
  const cogs = cogsPrefs();
  if (cogs.industries.length || cogs.companies.length) fd.append("cogs_rules", JSON.stringify(cogs));
  const std = standardFor(a); // the user's own layout for this company
  if (std) fd.append("standard", JSON.stringify(std.format));
  if (a.file) fd.append("pdf", a.file);
  fd.append("dry_run", dryRun ? "true" : "false");
  fd.append("effort", state.effort);
  if (pages) fd.append("pages", pages);
  if (!dryRun && workbook) fd.append("workbook", workbook);
  return fd;
}

/** Download → keyword scan → AI → workbook for one card. Progress is shown on the card. */
async function extract(a, { pages } = {}) {
  const j = a.job;
  if (j.status === "skipped" && !pages) return; // a duplicate settled while it waited in the queue
  Object.assign(j, { status: "reading", error: "", scan: null, result: null, since: Date.now(), started: Date.now() });
  renderCard(a);
  try {
    let scanRes;
    try {
      scanRes = await api("/api/process?op=process", { method: "POST", body: formData(a, true, pages || a.pages) });
    } catch (e) {
      // The announcement page wouldn't give up its attachments: open the Title link in a browser.
      if (!e.body?.need_browser || !a.url || a.attachments?.length) throw e;
      const files = keepAttachments((await api(`/api/browse?op=attachments&url=${encodeURIComponent(a.url)}`)).files);
      if (!files.length) throw new Error("No financial statements PDF in the announcement (press releases, presentations and highlights are left out)");
      a.attachments = files;
      scanRes = await api("/api/process?op=process", { method: "POST", body: formData(a, true, pages || a.pages) });
    }
    j.scan = scanRes;
    if (a.dupGroup && !pages) {
      const twins = state.items.filter((x) => x !== a && x.dupGroup === a.dupGroup);
      if (scanRes.scan.basis === "separate" && twins.length) {
        Object.assign(j, { status: "skipped", error: SEPARATE_NOTE });
        Object.assign(a, { kind: "other", reason: SEPARATE_NOTE });
        renderCards();
        return;
      }
      if (scanRes.scan.basis === "consolidated") {
        for (const t of twins) if (["idle", "queued"].includes(t.job.status)) {
          Object.assign(t.job, { status: "skipped", error: SEPARATE_NOTE });
          Object.assign(t, { kind: "other", reason: SEPARATE_NOTE });
        }
        renderCards();
      }
    }
    if (!scanRes.pages.length) throw new Error("The keyword scan found no statement pages. Open the details to choose pages.");
    // Statements that are photos: offer Claude (it reads them itself through the connector).
    if (scanRes.scan.vision && a.url && !a.file && a.readWith !== "gemini") {
      j.status = "asking";
      j.since = Date.now();
      renderCard(a);
      if ((await askReader(a)) === "claude") {
        Object.assign(j, { status: "claude", since: null });
        renderCard(a);
        return;
      }
    }
    j.ai = state.ai;
    j.status = "ai";
    j.since = Date.now();
    renderCard(a);
    // With a download folder, merge into this company's workbook if it is already there.
    const name = a.stock_code || a.issuer ? workbookName(a) : null;
    const existing = state.folderOk && name ? await readFromFolder(name) : null;
    // Only the PDF the scan chose: the server needn't download the other attachments again.
    const chosen = scanRes.scan.used_url && a.attachments?.filter((f) => f.url === scanRes.scan.used_url);
    // Pages go along only when chosen by hand: otherwise the server picks them (and, for pictures,
    // narrows them down to the statement pages itself).
    const res = await api("/api/process?op=process", { method: "POST", body: formData(a, false, pages || a.pages || "", existing, chosen?.length ? chosen : null) });
    const blob = new Blob([Uint8Array.from(atob(res.workbook_b64), (c) => c.charCodeAt(0))],
      { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    if (j.blobUrl) URL.revokeObjectURL(j.blobUrl);
    const filename = name || res.filename;
    Object.assign(j, { status: "done", result: res, blob, blobUrl: URL.createObjectURL(blob), filename, merged: Boolean(existing), saved: false,
      seconds: Math.round((Date.now() - j.started) / 1000) });
    if (state.folderOk) {
      try { await saveToFolder(filename, blob); j.saved = true; }
      catch (e) { j.saveError = e.message || String(e); }
    }
  } catch (e) {
    if (e.body?.not_results) {
      Object.assign(j, { status: "skipped", error: e.message, scan: e.body.scan ? { scan: e.body.scan, pages: [] } : null });
      // Anything set aside (a notice, or results that came only as slides) moves to the other
      // announcements, where its card still says why and offers Ask Claude.
      a.kind = "other";
      renderCards();
      return;
    }
    Object.assign(j, { status: "failed", error: e.message || String(e) });
    if (e.auth) openSheet("#settingsSheet");
  }
  renderCard(a);
}

async function extractAll() {
  if (state.bulk) return;
  const queue = state.items.filter((a) => a.kind === "results" && (a.job.status === "idle" || a.job.status === "failed"));
  if (!queue.length) return;
  state.bulk = { stop: false };
  for (const a of queue) a.job.status = "queued";
  renderCards();
  // Two at a time: quick enough, and gentle on SGX and on the AI's rate limits.
  const worker = async () => {
    while (queue.length && !state.bulk.stop) await extract(queue.shift());
  };
  await Promise.all([worker(), worker()]);
  for (const a of queue) a.job.status = "idle"; // left over after Stop
  const done = state.items.filter((a) => a.job.status === "done");
  const unverified = done.filter((a) => !verdictOf(a.job.result).ok).length;
  state.bulk = null;
  renderCards();
  // Auto-download: workbooks already went into the chosen folder as each one finished; any that
  // couldn't (no folder, or this browser can't write to one) come down now as one zip.
  const unsaved = done.filter((a) => !a.job.saved);
  const where = state.folderOk && state.dir ? ` and saved to “${state.dir.name}”` : "";
  toast(`Extract all finished: ${done.length} workbook${done.length === 1 ? "" : "s"} ready${done.length > unsaved.length ? where : ""}${unverified ? `, ${unverified} not verified (Check with Claude)` : ""}.`);
  if (unsaved.length) await downloadAll(unsaved);
}

let zipLib;
async function downloadAll(only) {
  const done = only || state.items.filter((a) => a.job.status === "done");
  if (!done.length) return;
  // With a download folder, Download all writes the workbooks there (one per company), not a zip.
  if (state.folderOk && state.dir) {
    const failed = [];
    for (const a of done) {
      try { await saveToFolder(a.job.filename || workbookName(a), a.job.blob); a.job.saved = true; a.job.saveError = ""; }
      catch (e) { a.job.saveError = e.message || String(e); failed.push(a); }
      renderCard(a);
    }
    const ok = done.length - failed.length;
    if (ok) toast(`${ok} workbook${ok === 1 ? "" : "s"} saved to “${state.dir.name}”${failed.length ? `; ${failed.length} couldn’t be saved and come down as a zip` : ""}.`);
    if (!failed.length) return;
    return zipDownload(failed);
  }
  return zipDownload(done);
}

async function zipDownload(done) {
  zipLib ||= await new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js";
    s.onload = () => resolve(window.JSZip);
    s.onerror = () => reject(new Error("Couldn’t load the zip helper"));
    document.head.append(s);
  }).catch((e) => { toast(e.message); return null; });
  if (!zipLib) return;
  const zip = new zipLib();
  const used = new Set();
  for (const a of done) {
    let name = a.job.filename || "workbook.xlsx";
    for (let n = 2; used.has(name); n++) name = name.replace(/(\.xlsx)?$/, `-${n}.xlsx`).replace(/(-\d+)+-(\d+)\.xlsx$/, "-$2.xlsx");
    used.add(name);
    zip.file(name, a.job.blob);
  }
  const blob = await zip.generateAsync({ type: "blob" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `SGX-results-${compact($("#from").value)}-${compact($("#to").value)}.zip`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
}

// ------------------------------------------------------------------ details sheet
const stepNames = () => ["Download", "Keyword scan", `${state.ai} reads`, "Workbook"];

function stepsHtml(j) {
  const active = { reading: 0, queued: 0, ai: 2, done: 4 }[j.status] ?? (j.result ? 4 : j.scan ? 2 : 0);
  const failed = j.status === "failed";
  const stopped = j.status === "skipped"; // set aside after the scan: no AI step is under way
  return `<ol class="progress">${stepNames().map((s, i) => {
    const st = i < active ? "done" : i === active && !stopped ? (failed ? "failed" : "active") : "";
    const inner = st === "done" ? icon("check", "width:16px;height:16px") : st === "failed" ? icon("x", "width:16px;height:16px") : i + 1;
    return `<li class="${st}"><span class="badge">${inner}</span>${s}</li>`;
  }).join("")}</ol>`;
}

function scanHtml(scan, pages, skipped = false) {
  const owner = {};
  for (const s of STATEMENTS) for (const p of scan.selected[s.key] || []) owner[p] = s.cls;
  const img = new Set(scan.image_only_pages);
  const ocr = scan.scanned_pages || [];
  const rows = STATEMENTS.map((s) => {
    const sel = scan.selected[s.key] || [];
    const top = scan.candidates[s.key]?.[0];
    return `<div class="stmt ${s.cls}"><span class="ico">${icon(s.icon)}</span>
      <span class="nm">${s.name}<small>${sel.length ? `keyword score ${top ? Math.round(top.score) : "–"}` : "no page scored high enough"}</small></span>
      <span class="chips">${sel.length ? sel.map((p) => `<span class="chip">p${p}</span>`).join("")
        : scan.vision ? `<span class="chip">in the pictures</span>`
        : scan.ai_locate && !skipped ? `<span class="chip">${esc(state.ai)} finds it</span>` : `<span class="chip missing">not found</span>`}</span></div>`;
  }).join("");
  const strip = scan.page_count <= 400
    ? `<div class="pm" aria-label="Page map">${Array.from({ length: scan.page_count }, (_, i) => {
        const p = i + 1;
        return `<span class="${owner[p] || (img.has(p) ? "img" : "")}" title="Page ${p}">p${p}</span>`;
      }).join("")}</div>
      <div class="legend"><span><i style="background:var(--trip-lagoon)"></i>Income</span><span><i style="background:var(--trip-indigo)"></i>Balance sheet</span>
        <span><i style="background:var(--trip-ochre)"></i>Cash flow</span>${img.size ? `<span><i class="img" style="background:repeating-linear-gradient(45deg,#F0ECE4 0 3px,#D8CFC1 3px 5px)"></i>No text layer</span>` : ""}</div>`
    : "";
  return `<div>
      <p class="block-title">Pages chosen without AI</p>
      <div class="statements">${rows}</div>
      ${strip}
      <p class="muted" style="margin:10px 0 0;font-size:13px">${esc(scan.pdf)} · ${scan.page_count} pages${scan.attachments.length > 1 ? ` · the financial statements PDF of ${scan.attachments.length} attachments (${scan.downloaded.length} opened)` : ""}${
        pages?.length ? ` · ${pages.length} page${pages.length === 1 ? "" : "s"} go to ${esc(state.ai)}` : ""}</p>
    </div>
    ${scan.ai_locate && !skipped ? `<div class="notice info">${icon("sparkles")}<div>The keyword scan didn’t find the deciding words for ${
        scan.missing.map((k) => STATEMENTS.find((x) => x.key === k).name.toLowerCase()).join(", ")}, so ${pages?.length || "the"} page${pages?.length === 1 ? "" : "s"} (${
        pages?.length === scan.page_count ? "the whole document" : "what was found plus the most number-heavy pages"}) go to ${esc(state.ai)}, which finds the three statements itself.</div></div>` : ""}
    ${scan.vision
      ? `<div class="notice info">${icon("eye")}<div>This filing’s statements are pictures (scanned or photographed), which a keyword scan can’t read. Small previews of its ${img.size} picture page${img.size === 1 ? "" : "s"} find the statement pages (consolidated first); those are turned upright, straightened and cleaned up, copied exactly as printed, checked against their own totals, and any rows that don’t add up are read again enlarged, before the template is filled.</div></div>`
      : ocr.length ? `<div class="notice info">${icon("eye")}<div>${ocr.length} page${ocr.length === 1 ? " is a scan" : "s are scans"} with a hidden text layer: the keywords came from that layer, but the figures are read from the pictures, which are checked against their own totals.</div></div>`
      : img.size ? `<div class="notice">${icon("alert")}<div>${img.size} page${img.size === 1 ? " is a picture" : "s are pictures"} with no readable text. If a statement is missing, enter its pages below.</div></div>` : ""}`;
}

function verdictHtml(r, canClaude) {
  const v = verdictOf(r);
  if (v.ok) return `<div class="notice ok">${icon("check")}<div><b>Verified.</b> ${v.pass} tie-out check${v.pass === 1 ? "" : "s"} pass and none fails; every statement and gross profit found.</div></div>`;
  return `<div class="notice error" role="alert">${icon("alert")}<div><b>Not verified: don’t rely on these figures yet.</b>
    <ul class="reasons">${v.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    ${canClaude ? "<b>Re-check with Claude</b> (below) has Claude read the filing itself through the connector and check it again."
      : "Check the figures against the PDF, or choose the statement pages below and extract again."}</div></div>`;
}

// How cost of goods sold was made up: the industry, the rule, every expense line in or out. Lines can be
// ticked differently and recalculated (no AI), or saved as the company's rule for its next filings.
const COGS_METHOD = { printed: "Printed in the filing", by_nature: "Judged from expenses by nature", judged: "Judged from the expense lines",
  none: "Nothing on the statement is a cost of earning the revenue", program: "Worked out by the program", chosen: "Your choice of lines",
  missing: "Not found" };
const money = (v) => (v == null ? "–" : v < 0 ? `(${Math.abs(v).toLocaleString()})` : Number(v).toLocaleString());
const periodName = (p) => {
  const d = new Date(`${p.period_end}T00:00:00`);
  const my = Number.isNaN(d.getTime()) ? p.period_end : d.toLocaleDateString("en-SG", { month: "short", year: "numeric" });
  return p.months ? `${p.months === 12 ? "FY" : `${p.months}M`} ${my}` : my;
};
function cogsHtml(r, a) {
  const b = r.meta?.cogs_basis;
  if (!b) return "";
  const shown = (b.periods || []).slice(0, 2);
  const lines = b.lines || [];
  const editable = b.method !== "printed" && lines.length && r.normalised;
  const rows = lines.map((ln, k) => `<tr><td><label class="cogs-pick">${editable
    ? `<input type="checkbox" data-line="${k}" ${ln.in_cogs ? "checked" : ""} aria-label="${esc(ln.label)} is cost of goods sold">`
    : `<span class="yn ${ln.in_cogs ? "in" : ""}">${ln.in_cogs ? "in" : "out"}</span>`}<span>${esc(ln.label)}${ln.why ? `<small>${esc(ln.why)}</small>` : ""}</span></label></td>${
    shown.map((_, i) => `<td class="num">${money((ln.values || [])[i])}</td>`).join("")}</tr>`).join("");
  const who = a.stock_code || nameCase(a.issuer) || "this company";
  return `<div>
      <p class="block-title">Industry and cost of goods sold</p>
      <div class="kv"><span>Industry: <b>${esc(b.industry_name)}</b>${b.evidence ? ` · ${esc(b.evidence)}` : ""}</span>
        <span>decided by ${esc(b.decided_by === "AI" ? state.ai : b.decided_by || "")}</span></div>
      ${b.also_suggests ? `<div class="notice" style="margin-top:8px">${icon("alert")}<div>The filing’s own lines suggest <b>${esc(b.also_suggests)}</b>. If that’s right, give ${esc(who)} a company rule in Settings.</div></div>` : ""}
      <p class="cogs-how"><b>${esc(COGS_METHOD[b.method] || b.method || "")}</b>${b.rule && ["judged", "by_nature", "none"].includes(b.method) ? `, by ${esc(b.rule)}` : ""}. ${esc(b.explanation || "")}</p>
      ${lines.length ? `<table class="cogs-table"><thead><tr><th>${editable ? "Tick the lines that are cost of goods sold" : "Expense line"}</th>${
        shown.map((p) => `<th class="num">${esc(periodName(p))}</th>`).join("")}</tr></thead><tbody>${rows}</tbody>
        <tfoot><tr><td>Cost of goods sold</td>${shown.map((_, i) => `<td class="num">${money((b.cogs || [])[i])}</td>`).join("")}</tr>${
          b.da_in_cogs ? `<tr class="sub"><td>of which depreciation &amp; amortisation</td>${shown.map((_, i) => `<td class="num">${money(b.da_in_cogs[i])}</td>`).join("")}</tr>
          <tr class="sub"><td>excluding D&amp;A <small>(FactSet shows both)</small></td>${shown.map((_, i) => `<td class="num">${money((b.cogs_excl_da || [])[i])}</td>`).join("")}</tr>` : ""}</tfoot></table>`
        : `<p class="muted" style="margin:6px 0 0;font-size:13px">Cost of goods sold: ${esc(b.cogs_label || "blank")}</p>`}
      ${editable ? `<div class="notice-actions"><button type="button" class="btn btn-ink" data-cogs="recalc">${icon("refresh")}Recalculate</button>
        <button type="button" class="btn btn-outline" data-cogs="save" title="Its next filings are read with these lines as COGS">Save as my rule for ${esc(who)}</button></div>
        <p class="muted" style="margin:6px 0 0;font-size:12.5px">Recalculate adds up the ticked lines again without AI and updates the workbook; every period is in its COGS Basis sheet.</p>` : ""}
    </div>`;
}

// Which layout the workbook is in: the user's standard (its name and unit), the bank format, or the general one.
function formatNote(id) {
  const s = savedStandards().find((x) => x.id === id);
  if (s) return ` · your standard “${esc(s.name)}” · ${esc(UNIT_TEXT[s.conventions?.unit] || "’000")}`;
  return `${id === "bank" ? " · bank format" : ""} · '000`;
}
function resultHtml(r, canClaude, a = {}) {
  const pass = r.checks.filter((c) => c.status === "PASS").length;
  const fail = r.checks.filter((c) => c.status === "FAIL");
  const na = r.checks.length - pass - fail.length;
  const ordered = [...fail, ...r.checks.filter((c) => c.status !== "FAIL")];
  return `${verdictHtml(r, canClaude)}<div>
      <p class="block-title">Extracted</p>
      <div class="kv"><span><b>${esc(nameCase(r.meta.company_name))}</b></span><span>${esc(r.meta.currency)} · ${esc(r.meta.basis || "Group")}${formatNote(r.meta.format)}</span>
        <span>pages ${esc(r.meta.pages)}</span><span>${r.usage?.input_tokens ? `${r.usage.input_tokens.toLocaleString()} in / ${(r.usage.output_tokens || 0).toLocaleString()} out tokens` : "from cache, no tokens used"}</span>
        <span>read by ${esc(r.meta.model || "")}</span>${r.timings ? `<span>download &amp; scan ${r.timings.download_and_scan} s · AI ${r.timings.ai} s</span>` : ""}</div>
    </div>
    <div>
      <p class="block-title">Tie-out checks</p>
      <div class="check-sum"><span class="tag pass">${icon("check", "width:14px;height:14px")}${pass} pass</span>
        <span class="tag ${fail.length ? "fail" : "na"}">${icon(fail.length ? "alert" : "check", "width:14px;height:14px")}${fail.length} fail</span>
        ${na ? `<span class="tag na">${na} not checkable or derived</span>` : ""}</div>
      <details ${fail.length ? "open" : ""} style="margin-top:10px"><summary>${fail.length ? "Review the failed checks" : "Show every check"}</summary>
        <div class="check-list">${ordered.map((c) => `<div><span>${esc(c.check)}<br><span class="per">${esc(c.period)}${c.note ? ` · ${esc(c.note)}` : c.difference != null && c.status === "FAIL" ? ` · off by ${Number(c.difference).toLocaleString()}` : ""}</span></span>
          <span class="tag ${c.status === "PASS" ? "pass" : c.status === "FAIL" ? "fail" : "na"}">${c.status === "n/a" ? "n/a" : c.status.toLowerCase()}</span></div>`).join("")}</div>
      </details>
    </div>
    ${cogsHtml(r, a)}
    ${r.meta.pictures ? picturesHtml(r.meta.pictures) : ""}
    ${(r.meta.vision || r.meta.located_by_ai) && r.meta.found_pages ? `<div class="kv">${STATEMENTS.map((s) => `<span>${s.name}: <b>${
      (r.meta.found_pages[s.key] || []).map((p) => `p${p}`).join(", ") || "not found"}</b></span>`).join("")}</div>` : ""}
    ${r.meta.notes ? `<div class="notice info">${icon("sparkles")}<div><b>${esc(state.ai)}’s notes</b><br>${esc(r.meta.notes)}</div></div>` : ""}`;
}

function picturesHtml(p) {
  const turned = Object.entries(p.turned || {}).map(([pg, deg]) => `p${pg} ${deg}°`);
  const fixes = (p.corrections || []).map((c) => `p${c.page} ${c.label}, column ${c.col}: ${c.was ?? "blank"} → ${c.now}`);
  return `<div>
      <p class="block-title">Read from pictures</p>
      <div class="kv"><span>pages <b>${(p.pages || []).map((x) => `p${x}`).join(", ")}</b></span><span>${p.tables} table${p.tables === 1 ? "" : "s"} copied by ${esc(p.model || "")}</span>
        ${turned.length ? `<span>turned upright: ${esc(turned.join(", "))}</span>` : ""}
        <span>${fixes.length ? `${fixes.length} figure${fixes.length === 1 ? "" : "s"} corrected on a second, enlarged reading` : "every row added up on the first reading"}</span></div>
      ${fixes.length ? `<details style="margin-top:8px"><summary>Corrected figures</summary><div class="check-list">${fixes.map((f) => `<div><span>${esc(f)}</span></div>`).join("")}</div></details>` : ""}
      ${(p.doubtful || []).length ? `<div class="notice" style="margin-top:10px">${icon("alert")}<div><b>Check against the PDF</b> (yellow on the workbook’s Transcription sheet)<br>${(p.doubtful || []).map(esc).join("<br>")}</div></div>` : ""}
    </div>`;
}

function showDetails(a) {
  const j = a.job;
  const sheet = $("#runSheet");
  sheet.dataset.accent = accentFor(a.stock_code || a.issuer);
  sheet.dataset.key = a.key;
  $("#runEyebrow").textContent = [a.stock_code, when(a.broadcast, true)].filter(Boolean).join(" · ") || "Filing";
  $("#runTitle").textContent = nameCase(a.issuer) || a.fileName || "SGX filing";
  $("#runSub").textContent = shortTitle(a.title);
  const scan = j.scan || j.result; // the scan step saw every attachment; the AI step only the chosen one
  const pages = (j.result || j.scan)?.pages?.join(",") || "";
  $("#runBody").innerHTML = `${stepsHtml(j)}<div class="stage">
    ${filesLine(a) ? `<div>${filesLine(a)}</div>` : ""}
    ${scan ? scanHtml(scan.scan, scan.pages, j.status === "skipped") : ""}
    ${j.result ? resultHtml(j.result, Boolean(a.url), a) : ""}
    ${j.status === "failed" ? `<div class="notice error">${icon("alert")}<div>${esc(j.error)}${a.url ? `
      <div class="notice-actions"><button type="button" class="btn btn-outline" data-why="failed">${icon("sparkles")}Read it with Claude</button></div>` : ""}</div></div>` : ""}
    ${j.status === "skipped" ? `<div class="notice info">${icon("info")}<div>${esc(j.error)} No AI was used. If this is wrong, enter the statement pages below to extract anyway${
      a.url ? ", or ask Claude to look at it." : "."}${a.url ? `
      <div class="notice-actions"><button type="button" class="btn btn-outline" data-why="skipped">${icon("sparkles")}Ask Claude</button></div>` : ""}</div></div>` : ""}
    ${j.status in BUSY ? "" : `<details ${j.status === "failed" ? "open" : ""}><summary>Wrong pages? Choose them yourself</summary>
      <form class="rerun" id="rerunForm"><label class="sr-only" for="rerunPages">Pages</label>
        <input class="input" id="rerunPages" placeholder="e.g. 7,8,9,10" value="${esc(pages)}" inputmode="numeric">
        <button class="btn btn-ink" type="submit">${icon("refresh")}Extract</button></form></details>`}
  </div>`;
  const unverified = j.status === "done" && !verdictOf(j.result).ok;
  $("#runFoot").hidden = j.status !== "done";
  $("#runFoot").innerHTML = j.status === "done"
    ? `${unverified && a.url ? `<button type="button" class="btn btn-ember btn-lg btn-block" data-why="recheck">${icon("sparkles")}Re-check with Claude</button>` : ""}
      <a class="btn ${unverified ? "btn-outline" : "btn-ember"} btn-lg btn-block" href="${j.blobUrl}" download="${esc(j.filename)}">${icon("download")}Download ${esc(j.filename)}${unverified ? " (not verified)" : ""}</a>` : "";
  $("#rerunForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const p = $("#rerunPages").value.replace(/\s/g, "");
    if (!/^\d+(,\d+)*$/.test(p)) return toast("Enter page numbers like 7,8,9");
    extract(a, { pages: p });
  });
  openSheet("#runSheet");
}

// ------------------------------------------------------------------ dialogs
function openSheet(sel) {
  const d = $(sel);
  if (!d.open) d.showModal();
}
document.addEventListener("click", (e) => {
  const close = e.target.closest("[data-close]");
  if (close) close.closest("dialog").close();
  if (e.target.matches("dialog.sheet")) e.target.close(); // backdrop click
});

// ------------------------------------------------------------------ my standards
// A finished workbook (one company's, or a whole listing) is read on the server: each standard is a
// layout with its unit, column order, signs and sums, and the companies laid out that way. Standards are
// kept in this browser; a filing of one of those companies is sent with its standard and comes out in it.
let foundStandards = []; // read from the files just uploaded, not saved yet
function savedStandards() {
  try { const s = JSON.parse(store.get("sgxf-standards", "[]")); return Array.isArray(s) ? s : []; } catch { return []; }
}
function saveStandards(list) {
  try { localStorage.setItem("sgxf-standards", JSON.stringify(list)); }
  catch { toast("This browser has no room to keep that many standards. Remove some first."); return false; }
  renderStandards();
  refreshTokens();
  return true;
}
// Tokens for the Claude connector (it can't see this browser): each standard's own, which goes into the
// message a Claude button opens, and one with every standard and its companies for "Copy for Claude".
// Worked out on the server whenever the standards change, so a click can use them straight away.
let tokenRun = null;
async function refreshTokens() {
  const saved = savedStandards();
  if (!saved.length) { store.set("sgxf-standards-pack", ""); return renderStandards(); }
  const run = tokenRun = Symbol("tokens");
  try {
    const res = await api("/api/standards_token?op=standards_token", { method: "POST", timeoutMs: 60_000,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ standards: saved.map((x) => ({ format: x.format, companies: x.companies })) }) });
    if (run !== tokenRun) return; // the standards changed again meanwhile
    saved.forEach((x, k) => { x.token = res.tokens[k]; });
    try { localStorage.setItem("sgxf-standards", JSON.stringify(saved)); } catch { /* kept without tokens */ }
    store.set("sgxf-standards-pack", res.pack);
  } catch { /* offline: tried again next time */ }
  renderStandards();
}
// "DBS Group Holdings Ltd" and "DBS GROUP HOLDINGS LIMITED" are the same company.
const CO_SUFFIX = /\b(LIMITED|LTD|PLC|INC|INCORPORATED|CORPORATION|CORP|BHD|BERHAD|TBK|PTE|CO|COMPANY|HOLDINGS?|GROUP|THE|REIT|TRUST)\b/g;
const coKey = (s) => String(s || "").toUpperCase().replace(/[^A-Z0-9 ]+/g, " ").replace(CO_SUFFIX, " ").replace(/\s+/g, " ").trim();
function sameCompany(c, code, name) {
  code = String(code || "").toUpperCase();
  if (c.code && code) return c.code.toUpperCase() === code;
  const x = coKey(c.name), y = coKey(name);
  return x.length >= 2 && y.length >= 2 && (x === y || y.startsWith(`${x} `) || x.startsWith(`${y} `));
}
const standardFor = (a) => savedStandards().find((s) => (s.companies || []).some((c) => sameCompany(c, a.stock_code, a.issuer)));
const coText = (c) => c.code || c.name;
const IS_CODE = /^[A-Z0-9]{2,5}$/;
function parseCompanies(text, known = []) {
  return text.split(/[,;\n]/).map((t) => t.trim()).filter(Boolean).map((t) => known.find((c) => coText(c).toUpperCase() === t.toUpperCase())
    || (IS_CODE.test(t.toUpperCase()) && /\d/.test(t) ? { code: t.toUpperCase(), name: "" } : { code: "", name: t }));
}
const UNIT_TEXT = { units: "Units", thousands: "’000", millions: "Millions", billions: "Billions" };
function convChips(c) {
  return `<span class="tag na">${esc(UNIT_TEXT[c.unit] || "’000")}${c.unit_found === false ? " (assumed)" : ""}</span>
    <span class="tag na">${c.latest_first ? "Latest period first" : "Oldest period first"}</span>
    <span class="tag na">${c.expenses === "positive" ? "Costs shown positive" : c.expenses === "mixed" ? "Some costs shown positive" : "Costs shown negative"}</span>`;
}
function stdCard(s, k, saved) {
  const lines = Object.values(s.rows || {}).reduce((x, y) => x + y, 0);
  const list = (s.companies || []).map(coText).join(", ");
  return `<div class="std-card">
    <div class="std-head"><b>${saved ? `Standard ${k + 1} · ` : ""}${esc(s.name)}</b><span class="tag pass">${esc(s.company_type || "general")}</span>
      ${saved ? `<button type="button" class="link-btn" data-std-del="${k}" style="margin-left:auto;color:var(--trip-lacquer)">Remove</button>` : ""}</div>
    <small class="muted">${esc(s.applies_to || "")}</small>
    <div class="std-conv">${convChips(s.conventions || {})}
      ${lines ? `<span class="tag na">${lines} lines${s.mapped != null ? ` · ${s.mapped} matched to the app’s` : ""}</span>` : ""}
      ${s.formulas?.length ? `<span class="tag na" title="${esc(s.formulas.join("\n"))}">${s.formulas.length} sum${s.formulas.length === 1 ? "" : "s"} worked out</span>` : ""}</div>
    ${s.missing?.length ? `<p class="help">Not in the file: ${esc(s.missing.join(", "))}. The built-in lines are used for ${s.missing.length === 1 ? "it" : "them"}.</p>` : ""}
    ${s.notes ? `<p class="help">${esc(s.notes)}</p>` : ""}
    <label class="lbl" for="std-co-${saved ? "s" : "f"}${k}">For ${s.companies?.length > 1 ? `${s.companies.length} companies` : "company"} (stock codes or names, comma-separated)</label>
    <input class="input${list ? "" : " need"}" id="std-co-${saved ? "s" : "f"}${k}" data-std-co="${k}" data-saved="${saved ? 1 : 0}" value="${esc(list)}" placeholder="Which company? e.g. D05 or DBS Group">
    <details><summary>Lines</summary><pre class="mono std-lines">${esc(s.marked || "")}</pre></details>
  </div>`;
}
function renderStandards() {
  const saved = savedStandards();
  $("#stdList").innerHTML = saved.map((s, k) => stdCard(s, k, true)).join("")
    || `<p class="muted" style="margin:0;font-size:13px">None yet. Upload a workbook above.</p>`;
  $("#stdFound").innerHTML = foundStandards.length ? `<div class="form-row"><span class="lbl">Found in your file${foundStandards.length > 1 ? "s" : ""}: ${foundStandards.length} standard${foundStandards.length === 1 ? "" : "s"}</span>
    ${foundStandards.map((s, k) => stdCard(s, k, false)).join("")}</div>` : "";
  $("#stdFoot").hidden = !foundStandards.length;
  const pack = store.get("sgxf-standards-pack");
  $("#stdClaude").hidden = !saved.length;
  $("#stdClaude").disabled = !pack;
  $("#stdClaudeHelp").hidden = !saved.length;
}
async function copyForClaude() {
  const pack = store.get("sgxf-standards-pack");
  if (!pack) return toast("Still preparing your standards for Claude; try again in a moment.");
  const text = "My SGX Financials standards. For every filing, pass this token unchanged as `standard` to the SGX Financials "
    + "connector's open_filing, extract_filing and submit_statements, with the stock_code and company_name:\n" + pack;
  try { await navigator.clipboard.writeText(text); toast("Copied. Paste it into a Claude project’s instructions, or into a chat."); }
  catch { toast("Your browser didn’t allow copying."); }
}
async function learnFiles(files) {
  files = [...files].filter(Boolean);
  if (!files.length) return;
  const notes = [];
  for (const [n, f] of files.entries()) {
    if (!/\.(xlsx|xlsm|csv)$/i.test(f.name)) { notes.push(`${f.name}: not an Excel or CSV file`); continue; }
    if (f.size > MAX_UPLOAD) { notes.push(`${f.name}: over 4 MB, split it into smaller files`); continue; }
    $("#stdName").textContent = `Reading ${files.length > 1 ? `${n + 1} of ${files.length}: ` : ""}${f.name}…`;
    $("#stdDrop").classList.add("busy");
    const fd = new FormData();
    fd.append("file", f);
    try {
      const res = await api("/api/learn_standard?op=learn_standard", { method: "POST", body: fd, timeoutMs: 300_000 });
      for (const s of res.standards) {
        const same = foundStandards.find((x) => x.id === s.id);
        if (same) same.companies = [...same.companies, ...s.companies.filter((c) => !same.companies.some((d) => coText(d) && coText(d) === coText(c)))];
        else foundStandards.push(s);
      }
      if (res.ai_error) notes.push(`${f.name}: ${state.ai} couldn’t review it (${res.ai_error}); the program’s own reading is shown`);
      else if (!res.ai) notes.push(`${f.name}: read without ${state.ai} (no AI key on the server)`);
    } catch (e) {
      notes.push(`${f.name}: ${e.message}`);
    }
  }
  $("#stdDrop").classList.remove("busy");
  $("#stdName").textContent = "Choose or drop .xlsx or .csv files";
  renderStandards();
  if (notes.length) toast(notes.join(" · "));
  else toast(`${foundStandards.length} standard${foundStandards.length === 1 ? "" : "s"} found. Check the companies, then save.`);
}
function keepStandards() {
  let saved = savedStandards();
  const kept = foundStandards.filter((s) => s.companies.length);
  if (!kept.length) return toast("Say which company each standard is for first.");
  for (const s of kept) {
    // A company has one standard: the newest one it was uploaded with.
    saved = saved.map((x) => ({ ...x, companies: (x.companies || []).filter((c) => !s.companies.some((d) => sameCompany(d, c.code, c.name) || sameCompany(c, d.code, d.name))) }));
    const same = saved.find((x) => x.id === s.id);
    const { source, ...rest } = s;
    const companies = s.companies.map(({ name, code }) => ({ name, code }));
    if (same) Object.assign(same, rest, { companies: [...same.companies, ...companies] });
    else saved.push({ ...rest, companies, saved_at: new Date().toISOString() });
  }
  saved = saved.filter((x) => x.companies.length);
  const skipped = foundStandards.length - kept.length;
  foundStandards = [];
  if (saveStandards(saved)) toast(`Saved.${skipped ? ` ${skipped} without a company left out.` : ""} These companies’ next filings come out in their standard.`);
}
$("#stdFile").addEventListener("change", (e) => { learnFiles(e.target.files); e.target.value = ""; });
["dragenter", "dragover"].forEach((t) => $("#stdDrop").addEventListener(t, (e) => { e.preventDefault(); $("#stdDrop").classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => $("#stdDrop").addEventListener(t, () => $("#stdDrop").classList.remove("drag")));
$("#stdDrop").addEventListener("drop", (e) => { e.preventDefault(); learnFiles(e.dataTransfer.files); });
$("#stdSave").addEventListener("click", keepStandards);
$("#stdClaude").addEventListener("click", copyForClaude);
$("#stdSheet").addEventListener("change", (e) => {
  const input = e.target.closest("[data-std-co]");
  if (!input) return;
  const k = +input.dataset.stdCo;
  if (input.dataset.saved === "1") {
    const saved = savedStandards();
    saved[k].companies = parseCompanies(input.value, saved[k].companies);
    saveStandards(saved.filter((x) => x.companies.length));
  } else {
    foundStandards[k].companies = parseCompanies(input.value, foundStandards[k].companies);
    input.classList.toggle("need", !foundStandards[k].companies.length);
  }
});
$("#stdSheet").addEventListener("click", (e) => {
  const b = e.target.closest("[data-std-del]");
  if (!b) return;
  saveStandards(savedStandards().filter((_, k) => k !== +b.dataset.stdDel));
  toast("Removed. Its companies go back to the built-in layout.");
});

// ------------------------------------------------------------------ settings
function syncEffort() {
  document.querySelectorAll("#effort button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.effort)));
}
$("#effort").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.effort = b.dataset.v;
  store.set("sgxf-effort", state.effort);
  syncEffort();
});
$("#pw").value = store.get("sgxf-pw");
$("#pw").addEventListener("input", () => store.set("sgxf-pw", $("#pw").value));
// ------------------------------------------------------------------ cost of goods sold rules
// Built-in industries and their COGS rules come from the server (industries/cogs.json). A user's changes
// (an industry's rule, new industries, company rules) are kept in this browser and sent with every
// extraction; a company rule (by stock code or part of the name) comes before its industry's rule.
state.industries = [];
let editingCogs = null; // { kind: "industry" | "company", key }
function cogsPrefs() {
  try {
    const p = JSON.parse(store.get("sgxf-cogs", "{}")) || {};
    return { industries: Array.isArray(p.industries) ? p.industries : [], companies: Array.isArray(p.companies) ? p.companies : [] };
  } catch { return { industries: [], companies: [] }; }
}
function saveCogsPrefs(p) {
  store.set("sgxf-cogs", JSON.stringify(p));
  renderCogs();
}
function allIndustries() {
  const mine = cogsPrefs().industries;
  const builtin = new Set(state.industries.map((i) => i.id));
  return [...state.industries.map((b) => {
    const m = mine.find((x) => x.id === b.id);
    return m ? { ...b, ...m, tag: "your version" } : { ...b, tag: "built-in" };
  }), ...mine.filter((m) => !builtin.has(m.id)).map((m) => ({ ...m, tag: "yours" }))];
}
const industryName = (id) => allIndustries().find((i) => i.id === id)?.name || id;
async function loadIndustries() {
  try { state.industries = (await (await fetch("/api/industries?op=industries")).json()).industries || []; } catch { /* offline */ }
  renderCogs();
}
function renderCogs() {
  $("#indList").innerHTML = allIndustries().map((i) => `<div class="fmt-item"><span class="t"><b>${esc(i.name)}</b>
      <span class="tag ${i.tag === "built-in" ? "na" : "pass"}">${i.tag}</span><small title="${esc(i.cogs)}">${esc(i.cogs)}</small></span>
      <button type="button" class="btn btn-outline" data-ind="${esc(i.id)}" style="min-height:36px;padding:0 12px">Edit</button></div>`).join("")
    || `<p class="muted" style="margin:0;font-size:13px">Industries load when the server is reachable.</p>`;
  const cos = cogsPrefs().companies;
  $("#coList").innerHTML = cos.map((c, k) => `<div class="fmt-item"><span class="t"><b>${esc(c.match)}</b>
      ${c.industry ? `<span class="tag na">${esc(industryName(c.industry))}</span>` : ""}<small title="${esc(c.cogs)}">${esc(c.cogs || "its industry’s rule")}</small></span>
      <button type="button" class="btn btn-outline" data-co="${k}" style="min-height:36px;padding:0 12px">Edit</button></div>`).join("")
    || `<p class="muted" style="margin:0;font-size:13px">None yet. Add one here, or tick a filing’s cost of goods sold lines in its details and save them as its rule.</p>`;
}
const industryId = (name) => {
  const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
  return /^[a-z]/.test(id) ? id : `i_${id || "industry"}`;
};
function openCogs(kind, key = null) {
  editingCogs = { kind, key };
  const isInd = kind === "industry";
  $("#indFields").hidden = !isInd;
  $("#coFields").hidden = isInd;
  if (isInd) {
    const i = key ? allIndustries().find((x) => x.id === key) : null;
    $("#cogsTitle").textContent = i ? i.name : "New industry";
    $("#cogsSub").textContent = i?.tag === "built-in" ? "A built-in rule: your version replaces it in this browser." : "How cost of goods sold is made up when a filing prints none.";
    $("#indName").value = i?.name || "";
    $("#indApplies").value = i?.applies_to || "";
    $("#indKeywords").value = (i?.keywords || []).join(", ");
    $("#cogsText").value = i?.cogs || "";
    $("#cogsDelete").hidden = !i || i.tag === "built-in";
    $("#cogsDelete").textContent = i?.tag === "your version" ? "Back to the built-in rule" : "Remove this industry";
  } else {
    const c = key != null ? cogsPrefs().companies[key] : null;
    $("#cogsTitle").textContent = c ? `Rule for ${c.match}` : "Company rule";
    $("#cogsSub").textContent = "Comes before its industry’s rule, for every filing of this company.";
    $("#coMatch").value = c?.match || "";
    $("#coIndustry").innerHTML = `<option value="">Let ${esc(state.ai)} decide</option>`
      + allIndustries().map((i) => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join("");
    $("#coIndustry").value = c?.industry || "";
    $("#cogsText").value = c?.cogs || "";
    $("#cogsDelete").hidden = c == null;
    $("#cogsDelete").textContent = "Remove this company rule";
  }
  openSheet("#cogsSheet");
}
$("#cogsSave").addEventListener("click", () => {
  const p = cogsPrefs();
  const text = $("#cogsText").value.trim();
  if (editingCogs.kind === "industry") {
    const name = $("#indName").value.trim();
    if (!name) return toast("Give the industry a name");
    if (!text) return toast("Say what its cost of goods sold is made of");
    const id = editingCogs.key || industryId(name);
    const entry = { id, name, applies_to: $("#indApplies").value.trim(), cogs: text,
      keywords: $("#indKeywords").value.split(",").map((w) => w.trim()).filter(Boolean) };
    p.industries = [...p.industries.filter((i) => i.id !== id), entry];
  } else {
    const match = $("#coMatch").value.trim();
    if (!match) return toast("Enter the stock code or part of the company name");
    if (!text && !$("#coIndustry").value) return toast("Choose its industry or say what its cost of goods sold is made of");
    const entry = { match, industry: $("#coIndustry").value, cogs: text };
    p.companies = [...p.companies];
    if (editingCogs.key != null) p.companies[editingCogs.key] = entry;
    else p.companies = [...p.companies.filter((c) => c.match.toUpperCase() !== match.toUpperCase()), entry];
  }
  saveCogsPrefs(p);
  $("#cogsSheet").close();
  toast("Saved. The next extractions follow it.");
});
$("#cogsDelete").addEventListener("click", () => {
  const p = cogsPrefs();
  if (editingCogs.kind === "industry") p.industries = p.industries.filter((i) => i.id !== editingCogs.key);
  else p.companies = p.companies.filter((_, k) => k !== editingCogs.key);
  saveCogsPrefs(p);
  $("#cogsSheet").close();
  toast("Removed");
});
$("#indList").addEventListener("click", (e) => { const b = e.target.closest("[data-ind]"); if (b) openCogs("industry", b.dataset.ind); });
$("#coList").addEventListener("click", (e) => { const b = e.target.closest("[data-co]"); if (b) openCogs("company", +b.dataset.co); });
$("#indAdd").addEventListener("click", () => openCogs("industry"));
$("#coAdd").addEventListener("click", () => openCogs("company"));
$("#cogsExport").addEventListener("click", () => {
  const p = cogsPrefs();
  const out = { industries: allIndustries().map(({ tag, ...i }) => i), companies: p.companies };
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: "application/json" }));
  link.download = "cogs.json";
  link.click();
});

// The user's rule for a filing, as the Claude connector takes it (cogs_rule): the company's own, else
// their version of its industry's rule.
function cogsRuleFor(a) {
  const p = cogsPrefs();
  const code = (a.stock_code || "").toUpperCase(), name = (a.issuer || "").toUpperCase();
  const c = p.companies.find((x) => (code && x.match.toUpperCase() === code)
    || (name && x.match.length >= 3 && name.includes(x.match.toUpperCase())));
  if (c) return { match: c.match, industry: c.industry, cogs: c.cogs };
  const iid = a.job?.result?.meta?.cogs_basis?.industry;
  const mine = iid && p.industries.find((i) => i.id === iid);
  return mine ? { match: a.stock_code || a.issuer || "this company", industry: iid, cogs: mine.cogs } : null;
}

const pickedLines = () => [...document.querySelectorAll("#runBody [data-line]")]
  .sort((x, y) => x.dataset.line - y.dataset.line).map((x) => x.checked);

// The ticked lines added up again on the server (no AI): new COGS, gross profit, verdict and workbook.
async function recalcCogs(a) {
  const j = a.job, r = j.result;
  const fd = new FormData();
  fd.append("normalised", JSON.stringify(r.normalised));
  fd.append("meta", JSON.stringify(r.meta));
  fd.append("in_cogs", JSON.stringify(pickedLines()));
  const std = standardFor(a);
  if (std) fd.append("standard", JSON.stringify(std.format));
  const existing = state.folderOk && j.saved ? await readFromFolder(j.filename) : j.blob;
  if (existing) fd.append("workbook", existing, j.filename);
  try {
    const res = await api("/api/recompute?op=recompute", { method: "POST", body: fd, timeoutMs: 60_000 });
    Object.assign(r, { normalised: res.normalised, meta: res.meta, verdict: res.verdict, checks: res.checks });
    const blob = new Blob([Uint8Array.from(atob(res.workbook_b64), (c) => c.charCodeAt(0))],
      { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    if (j.blobUrl) URL.revokeObjectURL(j.blobUrl);
    Object.assign(j, { blob, blobUrl: URL.createObjectURL(blob) });
    if (state.folderOk) {
      try { await saveToFolder(j.filename, blob); j.saved = true; } catch (e) { j.saveError = e.message || String(e); }
    }
    renderCard(a);
    toast(`Cost of goods sold recalculated${res.chosen.length ? ` from ${res.chosen.length} line${res.chosen.length === 1 ? "" : "s"}` : ": none, so gross profit equals revenue"}. The workbook is updated.`);
  } catch (e) {
    toast(e.message || String(e));
  }
}
function saveCogsRule(a) {
  const b = a.job.result.meta.cogs_basis;
  const picks = pickedLines();
  const lines = (b.lines || []).map((ln, k) => ({ label: ln.label, in: picks[k] ?? ln.in_cogs }));
  const inn = lines.filter((x) => x.in).map((x) => x.label), out = lines.filter((x) => !x.in).map((x) => x.label);
  const rule = inn.length ? `Cost of goods sold = ${inn.join(" + ")}.${out.length ? ` Leave out ${out.join(", ")}.` : ""}`
    : "Nothing on the statement is a cost of earning the revenue: gross profit equals revenue.";
  const match = a.stock_code || nameCase(a.issuer);
  const p = cogsPrefs();
  p.companies = [...p.companies.filter((c) => c.match.toUpperCase() !== match.toUpperCase()), { match, industry: b.industry, cogs: rule }];
  saveCogsPrefs(p);
  toast(`Saved as the rule for ${match}: its next filings follow it (Settings › Cost of goods sold).`);
}

// ------------------------------------------------------------------ wiring
[$("#from").value, $("#to").value] = defaultRange();
$("#screenForm").addEventListener("submit", screen);
$("#openStandards").addEventListener("click", () => { renderStandards(); openSheet("#stdSheet"); });
$("#openSettings").addEventListener("click", () => openSheet("#settingsSheet"));

// ------------------------------------------------------------------ photo filings: Gemini or Claude?
// When the scan finds the statements are photos, a small window offers to read them with Claude
// instead. One window at a time (Extract all can find several); no answer in 30 s keeps Gemini.
const CLAUDE_MODEL = "Opus 5.5";
const ASK_SECONDS = 30;
let askChain = Promise.resolve();

// What Claude is asked, by why: "photos" (statements that are pictures), "recheck" (the server's AI read
// it but the figures aren't verified), "skipped" (set aside without AI, e.g. slides only), "failed".
function claudePrompt(a, why = "photos") {
  const j = a.job, res = j.result;
  const used = (j.scan || res)?.scan?.used_url;
  const v = res ? verdictOf(res) : null;
  const found = Object.values(res?.meta?.found_pages || {}).flat();
  const pages = [...new Set(found.length ? found : res?.pages || [])].sort((x, y) => x - y);
  const pagesArg = pages.length && pages.length <= 6 ? ` and pages=[${pages.join(", ")}]` : "";
  // This filing's own failing checks (the workbook's may include periods merged in from earlier filings).
  const fails = (v?.failing || (res?.checks || []).filter((c) => c.status === "FAIL")).slice(0, 4).map((c) => `- ${c.period}: ${c.check}${
    c.note ? ` (${c.note})` : c.difference != null ? ` (off by ${Number(c.difference).toLocaleString()} '000)` : ""}`);
  const ask = {
    photos: "Use the SGX Financials connector to read this SGX results filing. Its financial statements are photos/scans, "
      + "so read the page images yourself (open_filing, then submit_statements), re-read any lines whose checks fail, "
      + "and give me the Excel download link.",
    recheck: `Use the SGX Financials connector to re-check this SGX results filing. ${res?.meta?.model || state.ai} read it, `
      + `but the figures are NOT verified: ${(v?.reasons || []).join("; ")}.${fails.length ? `\nFailing checks:\n${fails.join("\n")}` : ""}\n`
      + `Read the statements yourself: open_filing with as_images=true${pagesArg}, then submit_statements. Re-read any lines `
      + "whose checks fail, don't leave gross profit blank (judge the gross expense when none is printed), and give me the "
      + "Excel download link. Don't use extract_filing: that is the reading that failed. If the filing has no formal "
      + "financial statements (only a presentation), tell me instead.",
    skipped: `Use the SGX Financials connector to check this SGX filing. The app set it aside without AI: ${j.error || "not a results filing."} `
      + "Look at it yourself (open_filing with as_images=true). If it has the Group's income statement, balance sheet and "
      + "cash flow statement, read them, submit_statements and give me the Excel download link; if not, tell me what it contains.",
    failed: `Use the SGX Financials connector to read this SGX results filing; the app couldn't (${j.error || "it failed"}). `
      + "Open it with open_filing (as_images=true to read the pages yourself), then submit_statements, and give me the Excel download link.",
  }[why] || "";
  const rule = cogsRuleFor(a);
  const std = standardFor(a);
  return [
    ask,
    rule ? `My cost of goods sold rule for this company (pass it to open_filing and submit_statements as cogs_rule): ${JSON.stringify(rule)}` : "",
    std?.token ? `Write it in my own standard “${std.name}”: pass this token unchanged as standard to open_filing, extract_filing and submit_statements: ${std.token}` : "",
    `Company: ${nameCase(a.issuer) || "?"}${a.stock_code ? ` (${a.stock_code})` : ""}`,
    a.title ? `Filing: ${shortTitle(a.title)}` : "",
    `Announcement: ${a.url}`,
    used && used !== a.url ? `Results PDF: ${used}` : "",
  ].filter(Boolean).join("\n");
}
function openClaudeChat(a, why = "photos") {
  const url = `https://claude.ai/new?q=${encodeURIComponent(claudePrompt(a, why))}`;
  const win = window.open(url, "_blank");
  if (win) win.opener = null;
  else toast("Your browser blocked the Claude tab: allow pop-ups for this site, then click the Claude button again");
}

function askReader(a) {
  const run = () => new Promise((resolve) => {
    const pop = document.createElement("div");
    pop.className = "ask-pop";
    pop.setAttribute("role", "alertdialog");
    pop.setAttribute("aria-labelledby", "askTitle");
    pop.innerHTML = `
      <div class="ask-head">${icon("eye")}<b id="askTitle">${esc(nameCase(a.issuer) || "This filing")}: statements are photos</b></div>
      <p>Read them with <b>Claude ${esc(CLAUDE_MODEL)}</b> instead of ${esc(state.ai)}? A Claude chat opens with the filing; it reads the pages itself and replies with the Excel link.</p>
      <div class="ask-actions">
        <button type="button" class="btn btn-ember" data-pick="claude">${icon("sparkles")}Use ${esc(CLAUDE_MODEL)}</button>
        <button type="button" class="btn btn-outline" data-pick="gemini">Keep ${esc(state.ai)} <span class="ask-count">${ASK_SECONDS}</span></button>
      </div>
      <p class="ask-foot">No answer in ${ASK_SECONDS} s keeps ${esc(state.ai)}. Claude needs the SGX Financials connector: <button type="button" class="link-btn" data-pick="setup">Add to Claude</button></p>
      <div class="ask-bar"><span style="animation-duration:${ASK_SECONDS}s"></span></div>`;
    document.body.append(pop);
    const started = Date.now();
    const tick = setInterval(() => {
      const left = Math.max(0, ASK_SECONDS - Math.floor((Date.now() - started) / 1000));
      pop.querySelector(".ask-count").textContent = left;
    }, 250);
    const timer = setTimeout(() => done("gemini"), ASK_SECONDS * 1000);
    const onKey = (e) => { if (e.key === "Escape") done("gemini"); };
    document.addEventListener("keydown", onKey);
    function done(choice) {
      clearTimeout(timer);
      clearInterval(tick);
      document.removeEventListener("keydown", onKey);
      pop.remove();
      a.readWith = choice;
      resolve(choice);
    }
    pop.addEventListener("click", (e) => {
      const pick = e.target.closest("[data-pick]")?.dataset.pick;
      if (pick === "setup") return $("#openClaude").click();
      if (pick === "claude") openClaudeChat(a); // inside the click, so the new tab isn't blocked
      if (pick) done(pick);
    });
    pop.querySelector('[data-pick="claude"]').focus({ preventScroll: true });
  });
  const turn = askChain.then(run);
  askChain = turn.catch(() => {});
  return turn;
}

// ------------------------------------------------------------------ Claude connector (MCP)
// One click opens Claude's "Add custom connector" dialog with the name and URL filled in (Claude's
// documented install link: modal=add-custom-connector&connectorName=&connectorUrl=); the user only
// clicks Continue. Custom connectors can't send our password header, so it travels in the
// connector URL (?key=), which is why the link is only made for someone who has the password.
const CONNECTOR_NAME = "SGX Financials";
function connectorUrl() {
  const pw = store.get("sgxf-pw");
  const base = `${location.origin}/api/mcp?op=mcp`;
  return pw ? `${base}&key=${encodeURIComponent(pw)}` : base;
}
const installLink = () => "https://claude.ai/customize/connectors?modal=add-custom-connector"
  + `&connectorName=${encodeURIComponent(CONNECTOR_NAME)}&connectorUrl=${encodeURIComponent(connectorUrl())}`;
function openInstall() {
  const win = window.open(installLink(), "_blank");
  if (win) win.opener = null;
  else location.href = installLink(); // popup blocked: go in this tab
}
async function copyConnector() {
  try { await navigator.clipboard.writeText(connectorUrl()); return true; }
  catch { $("#mcpUrl").select(); return document.execCommand?.("copy") ?? false; }
}
$("#openClaude").addEventListener("click", () => {
  const needPw = state.health?.password_required && !store.get("sgxf-pw");
  if (!needPw) return openInstall(); // straight to Claude's prefilled dialog
  // Without the password the connector couldn't work: ask for it first.
  $("#mcpUrl").value = connectorUrl();
  $("#mcpHelp").innerHTML = `<b style="color:var(--color-ember-deep)">This app has a password.</b> Enter it in Settings first so the connector can use it, then click Add to Claude again.`;
  openSheet("#claudeSheet");
});
$("#mcpCopy").addEventListener("click", async () => {
  toast((await copyConnector()) ? "Connector link copied" : "Select the link and copy it");
});
$("#mcpGo").addEventListener("click", () => {
  if (state.health?.password_required && !store.get("sgxf-pw")) {
    $("#claudeSheet").close();
    return openSheet("#settingsSheet");
  }
  $("#claudeSheet").close();
  openInstall();
});
$("#folderBtn").addEventListener("click", chooseFolder);
$("#bulk").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (b?.id === "bulkRun") {
    // Extract all saves into the download folder: ask for one now (still inside the click) if
    // none is set, so every workbook lands there as it finishes. Cancel → one zip at the end.
    if (!(await folderReady()) && !state.dir) await chooseFolder();
    extractAll();
  }
  if (b?.id === "bulkZip") {
    if (!(await folderReady()) && !state.dir) await chooseFolder(); // cancel → zip to Downloads
    downloadAll();
  }
  if (b?.id === "bulkStop" && state.bulk) { state.bulk.stop = true; renderBulk(); }
});
$("#results").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const i = b.dataset.extract ?? b.dataset.details ?? b.dataset.claude ?? b.dataset.gemini;
  if (i == null) return;
  const a = state.items[+i];
  if (b.dataset.details != null) return showDetails(a);
  if (b.dataset.claude != null) return askClaude(a, b.dataset.why || "photos");
  if (state.bulk && b.dataset.gemini == null) return;
  a.readWith = b.dataset.gemini != null ? "gemini" : undefined; // Extract / Try again asks again for photos
  await folderReady();
  await extract(a);
  tourSignal("extracted");
});

// A Claude chat that reads the filing itself; a re-check is noted on the card (the link arrives in the chat).
function askClaude(a, why) {
  openClaudeChat(a, why); // inside the click, so the new tab isn't blocked
  if (why === "recheck") {
    a.job.sentToClaude = true;
    renderCard(a);
  }
}
$("#runSheet").addEventListener("click", (e) => {
  const b = e.target.closest("[data-why], [data-cogs]");
  const a = b && state.items.find((x) => x.key === $("#runSheet").dataset.key);
  if (!a) return;
  if (b.dataset.cogs === "recalc") return recalcCogs(a);
  if (b.dataset.cogs === "save") return saveCogsRule(a);
  askClaude(a, b.dataset.why);
});

// Deep link: /?url=<SGX page URL> (or ?from=YYYYMMDD&to=YYYYMMDD) pre-fills the dates and screens.
const params = new URLSearchParams(location.search);
const deep = params.get("url") ? new URL(params.get("url")).searchParams : params;
const iso = (d) => (/^\d{8}$/.test(d || "") ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : "");
if (iso(deep.get("from"))) {
  $("#from").value = iso(deep.get("from"));
  $("#to").value = iso(deep.get("to")) || iso(deep.get("from"));
}
syncEffort();
renderIntro();
loadHealth();
loadIndustries();
if (savedStandards().some((x) => !x.token) || (savedStandards().length && !store.get("sgxf-standards-pack"))) refreshTokens();
if (canPickFolder) idb("readonly", (st) => st.get("dir")).then((d) => { state.dir = d || null; renderFolder(); });
renderFolder();
if (iso(deep.get("from"))) screen();
else restoreScreen();
state.restored ||= Boolean(iso(deep.get("from"))); // a deep link screens afresh: save its cards
