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
  mergeFile: null,
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
      </div>
      <ol class="steps">
        <li><span class="badge">1</span><span><strong>Screen</strong> <span class="muted">— opens every title and lists its PDFs.</span></span></li>
        <li><span class="badge">2</span><span><strong>Extract</strong> <span class="muted">— a keyword scan finds the three statements; ${esc(state.ai)} reads only those pages.</span></span></li>
        <li><span class="badge">3</span><span><strong>Excel</strong> <span class="muted">— a ShareInvestor-style workbook with tie-out checks.</span></span></li>
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

// The PDFs in the announcement's attachment list, each linked. Once extracted, the one that held
// the financial statements is shown first and marked; the rest (releases, slides) are listed after.
function filesLine(a) {
  if (!Array.isArray(a.attachments)) return "";
  if (!a.attachments.length) return `<span class="files none">${icon("file", "width:14px;height:14px")}No attachments found</span>`;
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

function cardHtml(a, i) {
  const j = a.job;
  const busy = j.status in BUSY;
  const label = j.status === "ai" ? `${j.ai || state.ai} reading…` : BUSY[j.status];
  const pass = j.result ? j.result.checks.filter((c) => c.status === "PASS").length : 0;
  const fail = j.result ? j.result.checks.filter((c) => c.status === "FAIL").length : 0;
  const pill = j.status === "done" ? `${icon("check", "width:14px;height:14px")}Excel ready`
    : j.status === "failed" ? `${icon("alert", "width:14px;height:14px")}Failed`
    : j.status === "skipped" ? `${icon("x", "width:14px;height:14px")}Not results`
    : j.status === "claude" ? `${icon("sparkles", "width:14px;height:14px")}Reading in Claude`
    : busy ? `<span class="spin" aria-hidden="true"></span>${esc(label)}${j.since ? ` <span class="elapsed" data-since="${j.since}">${clock(j.since)}</span>` : ""}`
    : `<span class="dot"></span>${esc(when(a.broadcast) || "SGX")}`;
  const action = j.status === "done"
    ? `<a class="btn btn-ember" href="${j.blobUrl}" download="${esc(j.filename)}">${icon("download")}Excel</a>`
    : busy
      ? `<button type="button" class="btn btn-ember" disabled><span class="spin" aria-hidden="true"></span>${esc(label)}</button>`
      : j.status === "skipped" ? `<button type="button" class="btn btn-outline" data-details="${i}">${icon("eye")}Why skipped</button>`
      : j.status === "claude" ? `<button type="button" class="btn btn-ember" data-claude="${i}">${icon("sparkles")}Open in Claude</button>
        <button type="button" class="btn btn-outline" data-gemini="${i}" title="Read it here with ${esc(state.ai)} instead">Use ${esc(state.ai)}</button>`
      : `<button type="button" class="btn ${a.kind === "other" ? "btn-outline" : "btn-ember"}" data-extract="${i}">${icon("sparkles")}${j.status === "failed" ? "Try again" : "Extract"}</button>`;
  const saved = j.saved ? ` · ${j.merged ? "merged into" : "saved to"} folder` : j.saveError ? " · not saved to folder" : "";
  const status = j.status === "done"
    ? `<span class="card-note ${fail ? "bad" : ""}">${pass} checks pass${fail ? `, ${fail} fail` : ""}${saved}</span>`
    : j.status === "failed" ? `<span class="card-note bad" title="${esc(j.error)}">${esc(j.error)}</span>`
    : j.status === "skipped" ? `<span class="card-note muted" title="${esc(j.error)}">${esc(j.error)}</span>`
    : j.status === "claude" ? `<span class="card-note muted" title="The Excel link arrives in the Claude chat">Sent to Claude ${esc(CLAUDE_MODEL)} · Excel link in the chat</span>`
    : a.reason ? `<span class="card-note muted" title="${esc(a.reason)}">${esc(a.reason)}</span>` : "";
  const hasDetails = j.scan || j.result || j.error;
  const name = esc(nameCase(a.issuer) || a.fileName || "Unknown issuer");
  // Cards fade in once, when a screen's results first appear; later redraws don't replay it.
  const motion = state.animate ? `animation-delay:${Math.min(i, 12) * 30}ms` : "animation:none";
  return `<article class="card ${j.status} ${a.kind === "other" ? "other" : ""}" data-card="${i}" data-accent="${accentFor(a.stock_code || a.issuer)}" style="${motion}">
    <div class="band">
      <svg class="route" viewBox="0 0 260 104" preserveAspectRatio="none" aria-hidden="true"><path d="M20 84 C70 70 90 40 140 46 S210 20 250 14" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="2" stroke-linecap="round" stroke-dasharray="1 7"/></svg>
      <span class="pill">${pill}</span>
      <span class="code">${esc(a.stock_code || "SGX")}</span>
    </div>
    <div class="card-body">
      ${hasDetails ? `<button type="button" class="name name-btn" data-details="${i}" title="Show pages and checks">${name}</button>` : `<span class="name">${name}</span>`}
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
}

function renderCards() {
  const list = state.items;
  const nResults = list.filter((a) => a.kind === "results").length;
  const nRelease = list.filter((a) => a.kind === "release_date").length;
  const nOther = list.length - nResults - nRelease;
  if (state.range !== undefined)
    $("#annNote").textContent = [`${nResults} results filing${nResults === 1 ? "" : "s"}`,
      nRelease && `${nRelease} release date${nRelease === 1 ? "" : "s"}`, nOther && `${nOther} other`, state.range].filter(Boolean).join(" · ");
  if (!list.length) {
    const d = state.debug || {};
    const saw = d.source ? `<p style="font-size:12.5px">What the browser saw: “${esc(d.title || "untitled page")}”, ${d.counts ? `${d.counts.table} rows in the table, ${d.counts.page_data} in the page data` : `${d.dom_links ?? 0} links`}.${
      d.body_excerpt ? `<br><span class="mono">${esc(d.body_excerpt.slice(0, 240))}</span>` : ""}</p>` : "";
    $("#results").innerHTML = `<div class="intro"><div><h3 style="margin-top:0">No financial results for these dates</h3>
      <p>SGX lists nothing under Financial Statements for these dates${$("#company").value.trim() ? " and this company" : ""}. Try a wider range.</p>${saw}</div></div>`;
  } else {
    const results = list.map((a, i) => [a, i]).filter(([a]) => a.kind === "results");
    const releases = list.filter((a) => a.kind === "release_date");
    const others = list.map((a, i) => [a, i]).filter(([a]) => a.kind === "other");
    $("#results").innerHTML = (results.length
      ? `<div class="grid">${results.map(([a, i]) => cardHtml(a, i)).join("")}</div>`
      : `<p class="muted" style="margin:0">No results filings for these dates.</p>`)
      + (releases.length ? `<section class="releases"><h3>${icon("calendar", "width:18px;height:18px")}Results release dates</h3>
          <p class="muted">Companies announcing when their results will come out.</p><div id="releases"></div></section>` : "")
      + (others.length ? `<details class="others" ${state.othersOpen ? "open" : ""}><summary>${icon("file", "width:15px;height:15px")}
          ${others.length} other announcement${others.length === 1 ? "" : "s"} in this category: releases, presentations, updates and duplicate filings.
          Not extracted unless you choose to.</summary>
          <div class="grid">${others.map(([a, i]) => cardHtml(a, i)).join("")}</div></details>` : "");
    $("#results .others")?.addEventListener("toggle", (e) => { state.othersOpen = e.target.open; });
    renderReleases();
  }
  renderBulk();
}

function renderCard(item) {
  const i = state.items.indexOf(item);
  const el = $(`#results [data-card="${i}"]`);
  if (!el) return renderCards();
  el.outerHTML = cardHtml(item, i);
  renderBulk();
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
      html += `<button type="button" class="btn ${todo.length ? "btn-outline" : "btn-ember"}" id="bulkZip">${icon("download")}Download all (${done.length})</button>`;
    if (todo.length > 1 || (todo.length && !done.length))
      html += `<button type="button" class="btn btn-ember" id="bulkRun">${icon("sparkles")}Extract all (${todo.length})</button>`;
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
    state.items = res.announcements.map((a) => ({ ...a, kind: a.kind || "results", key: a.url, job: { status: "idle" } }));
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

function renderFolder() {
  const b = $("#folderBtn");
  b.hidden = !canPickFolder;
  $("#folderLabel").textContent = state.dir ? `Saving to “${state.dir.name}”` : "Choose download folder";
  b.title = state.dir ? "Workbooks are saved here. Click to choose another folder." : "Save every workbook straight into a folder";
  b.classList.toggle("on", Boolean(state.dir));
}

async function chooseFolder() {
  try {
    state.dir = await window.showDirectoryPicker({ id: "sgx-financials", mode: "readwrite" });
    state.folderOk = true;
    renderFolder();
    renderBulk();
    toast(`Workbooks will be saved in “${state.dir.name}”.`);
    await idb("readwrite", (st) => st.put(state.dir, "dir")); // remembered for the next visit
  } catch (e) {
    if (e.name !== "AbortError") toast(`Couldn’t use that folder: ${e.message}`);
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
  const formats = customFormats();
  if (formats.length) fd.append("formats", JSON.stringify(formats.map(({ marked, ...f }) => f)));
  if (a.file) fd.append("pdf", a.file);
  fd.append("dry_run", dryRun ? "true" : "false");
  fd.append("effort", state.effort);
  if (pages) fd.append("pages", pages);
  if (!dryRun && (workbook || state.mergeFile)) fd.append("workbook", workbook || state.mergeFile);
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
      const { files } = await api(`/api/browse?op=attachments&url=${encodeURIComponent(a.url)}`);
      if (!files.length) throw new Error("No PDFs in the announcement's attachment list");
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
    const existing = state.folderOk && name && !state.mergeFile ? await readFromFolder(name) : null;
    // Only the PDF the scan chose: the server needn't download the other attachments again.
    const chosen = scanRes.scan.used_url && a.attachments?.filter((f) => f.url === scanRes.scan.used_url);
    // Pages go along only when chosen by hand: otherwise the server picks them (and, for pictures,
    // narrows them down to the statement pages itself).
    const res = await api("/api/process?op=process", { method: "POST", body: formData(a, false, pages || a.pages || "", existing, chosen?.length ? chosen : null) });
    if (state.mergeFile) clearMerge();
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
  const done = state.items.filter((a) => a.job.status === "done").length;
  state.bulk = null;
  renderCards();
  toast(`Extract all finished: ${done} workbook${done === 1 ? "" : "s"} ready.`);
}

let zipLib;
async function downloadAll() {
  const done = state.items.filter((a) => a.job.status === "done");
  if (!done.length) return;
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
  return `<ol class="progress">${stepNames().map((s, i) => {
    const st = i < active ? "done" : i === active ? (failed ? "failed" : "active") : "";
    const inner = st === "done" ? icon("check", "width:16px;height:16px") : st === "failed" ? icon("x", "width:16px;height:16px") : i + 1;
    return `<li class="${st}"><span class="badge">${inner}</span>${s}</li>`;
  }).join("")}</ol>`;
}

function scanHtml(scan, pages) {
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
        : scan.ai_locate ? `<span class="chip">${esc(state.ai)} finds it</span>` : `<span class="chip missing">not found</span>`}</span></div>`;
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
    ${scan.ai_locate ? `<div class="notice info">${icon("sparkles")}<div>The keyword scan didn’t find the deciding words for ${
        scan.missing.map((k) => STATEMENTS.find((x) => x.key === k).name.toLowerCase()).join(", ")}, so ${pages?.length || "the"} page${pages?.length === 1 ? "" : "s"} (${
        pages?.length === scan.page_count ? "the whole document" : "what was found plus the most number-heavy pages"}) go to ${esc(state.ai)}, which finds the three statements itself.</div></div>` : ""}
    ${scan.vision
      ? `<div class="notice info">${icon("eye")}<div>This filing’s statements are pictures (scanned or photographed), which a keyword scan can’t read. Small previews of its ${img.size} picture page${img.size === 1 ? "" : "s"} find the statement pages (consolidated first); those are turned upright, straightened and cleaned up, copied exactly as printed, checked against their own totals, and any rows that don’t add up are read again enlarged, before the template is filled.</div></div>`
      : ocr.length ? `<div class="notice info">${icon("eye")}<div>${ocr.length} page${ocr.length === 1 ? " is a scan" : "s are scans"} with a hidden text layer: the keywords came from that layer, but the figures are read from the pictures, which are checked against their own totals.</div></div>`
      : img.size ? `<div class="notice">${icon("alert")}<div>${img.size} page${img.size === 1 ? " is a picture" : "s are pictures"} with no readable text. If a statement is missing, enter its pages below.</div></div>` : ""}`;
}

function resultHtml(r) {
  const pass = r.checks.filter((c) => c.status === "PASS").length;
  const fail = r.checks.filter((c) => c.status === "FAIL");
  const na = r.checks.length - pass - fail.length;
  const ordered = [...fail, ...r.checks.filter((c) => c.status !== "FAIL")];
  return `<div>
      <p class="block-title">Extracted</p>
      <div class="kv"><span><b>${esc(nameCase(r.meta.company_name))}</b></span><span>${esc(r.meta.currency)} · ${esc(r.meta.basis || "Group")}${r.meta.format === "bank" ? " · bank format" : ""} · '000</span>
        <span>pages ${esc(r.meta.pages)}</span><span>${r.usage?.input_tokens ? `${r.usage.input_tokens.toLocaleString()} in / ${(r.usage.output_tokens || 0).toLocaleString()} out tokens` : "from cache, no tokens used"}</span>
        <span>read by ${esc(r.meta.model || "")}</span>${r.timings ? `<span>download &amp; scan ${r.timings.download_and_scan} s · AI ${r.timings.ai} s</span>` : ""}</div>
    </div>
    <div>
      <p class="block-title">Tie-out checks</p>
      <div class="check-sum"><span class="tag pass">${icon("check", "width:14px;height:14px")}${pass} pass</span>
        <span class="tag ${fail.length ? "fail" : "na"}">${icon(fail.length ? "alert" : "check", "width:14px;height:14px")}${fail.length} fail</span>
        ${na ? `<span class="tag na">${na} not checkable or derived</span>` : ""}</div>
      <details ${fail.length ? "open" : ""} style="margin-top:10px"><summary>${fail.length ? "Review the failed checks" : "Show every check"}</summary>
        <div class="check-list">${ordered.map((c) => `<div><span>${esc(c.check)}<br><span class="per">${esc(c.period)}${c.difference != null && c.status === "FAIL" ? ` · off by ${Number(c.difference).toLocaleString()}` : ""}</span></span>
          <span class="tag ${c.status === "PASS" ? "pass" : c.status === "FAIL" ? "fail" : "na"}">${c.status === "n/a" ? "n/a" : c.status.toLowerCase()}</span></div>`).join("")}</div>
      </details>
    </div>
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
    ${scan ? scanHtml(scan.scan, scan.pages) : ""}
    ${j.result ? resultHtml(j.result) : ""}
    ${j.status === "failed" ? `<div class="notice error">${icon("alert")}<div>${esc(j.error)}</div></div>` : ""}
    ${j.status === "skipped" ? `<div class="notice info">${icon("info")}<div>${esc(j.error)} No AI was used. If this is wrong, enter the statement pages below to extract anyway.</div></div>` : ""}
    ${j.status in BUSY ? "" : `<details ${j.status === "failed" ? "open" : ""}><summary>Wrong pages? Choose them yourself</summary>
      <form class="rerun" id="rerunForm"><label class="sr-only" for="rerunPages">Pages</label>
        <input class="input" id="rerunPages" placeholder="e.g. 7,8,9,10" value="${esc(pages)}" inputmode="numeric">
        <button class="btn btn-ink" type="submit">${icon("refresh")}Extract</button></form></details>`}
  </div>`;
  $("#runFoot").hidden = j.status !== "done";
  $("#runFoot").innerHTML = j.status === "done"
    ? `<a class="btn btn-ember btn-lg btn-block" href="${j.blobUrl}" download="${esc(j.filename)}">${icon("download")}Download ${esc(j.filename)}</a>` : "";
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

// ------------------------------------------------------------------ direct filing
function setDrop(dropSel, nameSel, file, fallback) {
  $(nameSel).textContent = file ? `${file.name} · ${(file.size / 1e6).toFixed(1)} MB` : fallback;
}
function wireDrop(dropSel, inputSel, onFile) {
  const drop = $(dropSel), input = $(inputSel);
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("drag")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]); });
  input.addEventListener("change", () => onFile(input.files[0]));
}

let directFile = null;
wireDrop("#pdfDrop", "#pdfFile", (f) => {
  if (f && f.size > MAX_UPLOAD) { toast("That PDF is over 4 MB. Use its SGX link instead."); return; }
  directFile = f || null;
  setDrop("#pdfDrop", "#pdfName", directFile, "Choose or drop a PDF");
});

function directJob() {
  const url = $("#directUrl").value.trim();
  const job = { issuer: $("#dName").value.trim(), stock_code: $("#dCode").value.trim().toUpperCase(), pages: $("#dPages").value.replace(/\s/g, "") };
  if (directFile) { job.file = directFile; job.fileName = directFile.name; job.title = directFile.name; }
  else if (url) {
    if (!/^https:\/\/([a-z0-9-]+\.)*sgx\.com\//i.test(url)) { toast("Use an https://…sgx.com link, or upload the PDF."); return null; }
    job[/\.pdf($|\?)/i.test(url) || /fileopen/i.test(url) ? "pdf_url" : "announcement_url"] = url;
  } else { toast("Add an SGX link or a PDF first."); return null; }
  if (job.pages && !/^\d+(,\d+)*$/.test(job.pages)) { toast("Enter page numbers like 7,8,9"); return null; }
  return job;
}
$("#dExtract").addEventListener("click", () => {
  const j = directJob();
  if (!j) return;
  $("#directSheet").close();
  const item = { ...j, url: j.announcement_url || j.pdf_url || "", key: j.announcement_url || j.pdf_url || `file:${j.fileName}:${Date.now()}`,
    attachments: j.pdf_url ? [{ name: "PDF", url: j.pdf_url }] : undefined, job: { status: "idle" } };
  state.items.unshift(item);
  renderCards();
  extract(item);
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
wireDrop("#wbDrop", "#wbFile", (f) => {
  state.mergeFile = f || null;
  setDrop("#wbDrop", "#wbName", f, "Choose an earlier .xlsx from this app");
  $("#wbClear").hidden = !f;
});
function clearMerge() {
  state.mergeFile = null;
  $("#wbFile").value = "";
  setDrop("#wbDrop", "#wbName", null, "Choose an earlier .xlsx from this app");
  $("#wbClear").hidden = true;
}
$("#wbClear").addEventListener("click", clearMerge);

// ------------------------------------------------------------------ statement formats
// Built-in formats come from the server (formats/*.json). Formats imported from ShareInvestor or
// edited here are kept in this browser and sent with every extraction; one with a built-in's id
// replaces it.
const FORMAT_USES = {
  general: "All companies except banks: trading, manufacturing, property, services, holding companies.",
  bank: "Banks: statements show interest earned/expended (interest income/expense), deposits and loans/advances.",
  reit: "REITs and business trusts: gross revenue, property expenses, net property income, distributions.",
  insurance: "Insurers: gross written premiums, claims, underwriting result, investment income.",
};
const FORMAT_NAMES = { general: "General", bank: "Bank", reit: "REIT", insurance: "Insurance" };
state.builtinFormats = [];
let editingFormat = null;

function customFormats() {
  try { return JSON.parse(store.get("sgxf-formats", "[]")) || []; } catch { return []; }
}
function saveCustomFormats(list) {
  store.set("sgxf-formats", JSON.stringify(list));
  renderFormats();
}
async function loadFormats() {
  try { state.builtinFormats = (await (await fetch("/api/formats?op=formats")).json()).formats || []; } catch { /* offline */ }
  renderFormats();
}
function renderFormats() {
  const custom = customFormats();
  const builtinIds = new Set(state.builtinFormats.map((f) => f.id));
  const items = [
    ...state.builtinFormats.map((b) => {
      const mine = custom.find((c) => c.id === b.id);
      return mine ? { ...mine, tag: "your version" } : { ...b, tag: "built-in" };
    }),
    ...custom.filter((c) => !builtinIds.has(c.id)).map((c) => ({ ...c, tag: "yours" })),
  ];
  $("#fmtList").innerHTML = items.map((f) => `<div class="fmt-item"><span class="t"><b>${esc(f.name)}</b>
      <span class="tag ${f.tag === "built-in" ? "na" : "pass"}">${f.tag}</span><small>${esc(f.applies_to)}</small></span>
      <button type="button" class="btn btn-outline" data-fmt="${esc(f.id)}" style="min-height:36px;padding:0 12px">Edit</button></div>`).join("")
    || `<p class="muted" style="margin:0;font-size:13px">Formats load when the server is reachable.</p>`;
}
function fmtUseChanged() {
  const use = $("#fmtUse").value;
  $("#fmtDescRow").hidden = use !== "other";
  if (use !== "other" && !$("#fmtName").value.trim()) $("#fmtName").value = FORMAT_NAMES[use];
}
function openFormat(id) {
  const mine = customFormats().find((f) => f.id === id);
  const f = mine || state.builtinFormats.find((x) => x.id === id);
  editingFormat = f?.id || null;
  const use = f ? (FORMAT_USES[f.id] ? f.id : "other") : "reit";
  $("#fmtUse").value = use;
  $("#fmtName").value = f?.name || FORMAT_NAMES[use] || "";
  $("#fmtDesc").value = use === "other" ? f?.applies_to || "" : "";
  $("#fmtDescRow").hidden = use !== "other";
  $("#fmtText").value = f?.marked || "[Income Statement]\n\n[Balance Sheet]\n\n[Cash Flow]\n";
  $("#fmtFileName").textContent = "Choose or drop an Excel / CSV export";
  $("#fmtProblems").hidden = true;
  $("#fmtDelete").hidden = !mine;
  $("#fmtTitle").textContent = f ? `${f.name} format` : "New format";
  openSheet("#formatSheet");
}
function formatFields() {
  const use = $("#fmtUse").value;
  const fd = new FormData();
  fd.append("id", use === "other" ? $("#fmtName").value.trim() || "custom" : use);
  fd.append("name", $("#fmtName").value.trim() || FORMAT_NAMES[use] || "Custom");
  fd.append("applies_to", use === "other" ? $("#fmtDesc").value.trim() : FORMAT_USES[use]);
  fd.append("base", use === "other" ? "general" : use);
  return fd;
}
function showProblems(problems) {
  $("#fmtProblems").hidden = !problems?.length;
  $("#fmtProblems").innerHTML = problems?.length ? `${icon("alert")}<div>${problems.map(esc).join("<br>")}</div>` : "";
}
async function readFormat(source) {
  const fd = formatFields();
  if (source instanceof File) fd.append("file", source);
  else fd.append("text", source);
  const res = await api("/api/parse_format?op=parse_format", { method: "POST", body: fd, timeoutMs: 60_000 });
  showProblems(res.problems);
  return res;
}
async function guarded(fn) {
  try { await fn(); } catch (e) { showProblems([e.message]); }
}

// ------------------------------------------------------------------ wiring
[$("#from").value, $("#to").value] = defaultRange();
$("#screenForm").addEventListener("submit", screen);
$("#openDirect").addEventListener("click", () => openSheet("#directSheet"));
$("#openSettings").addEventListener("click", () => openSheet("#settingsSheet"));

// ------------------------------------------------------------------ photo filings: Gemini or Claude?
// When the scan finds the statements are photos, a small window offers to read them with Claude
// instead. One window at a time (Extract all can find several); no answer in 30 s keeps Gemini.
const CLAUDE_MODEL = "Opus 5.5";
const ASK_SECONDS = 30;
let askChain = Promise.resolve();

function claudePrompt(a) {
  const used = a.job.scan?.scan?.used_url;
  return [
    "Use the SGX Financials connector to read this SGX results filing. Its financial statements are photos/scans, "
      + "so read the page images yourself (open_filing, then submit_statements), re-read any lines whose checks fail, "
      + "and give me the Excel download link.",
    `Company: ${nameCase(a.issuer) || "?"}${a.stock_code ? ` (${a.stock_code})` : ""}`,
    a.title ? `Filing: ${shortTitle(a.title)}` : "",
    `Announcement: ${a.url}`,
    used && used !== a.url ? `Results PDF: ${used}` : "",
  ].filter(Boolean).join("\n");
}
function openClaudeChat(a) {
  const url = `https://claude.ai/new?q=${encodeURIComponent(claudePrompt(a))}`;
  const win = window.open(url, "_blank");
  if (win) win.opener = null;
  else toast("Your browser blocked the Claude tab: allow pop-ups for this site, then click Open in Claude");
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
$("#fmtList").addEventListener("click", (e) => { const b = e.target.closest("[data-fmt]"); if (b) openFormat(b.dataset.fmt); });
$("#fmtAdd").addEventListener("click", () => openFormat(null));
$("#fmtUse").addEventListener("change", fmtUseChanged);
wireDrop("#fmtDrop", "#fmtFile", (file) => file && guarded(async () => {
  $("#fmtFileName").textContent = file.name;
  $("#fmtText").value = (await readFormat(file)).marked;
}));
$("#fmtRead").addEventListener("click", () => guarded(async () => {
  $("#fmtText").value = (await readFormat($("#fmtText").value)).marked;
}));
$("#fmtSave").addEventListener("click", () => guarded(async () => {
  const res = await readFormat($("#fmtText").value);
  const list = customFormats().filter((f) => f.id !== res.format.id && f.id !== editingFormat);
  saveCustomFormats([...list, { ...res.format, marked: res.marked }]);
  editingFormat = res.format.id;
  $("#fmtText").value = res.marked;
  $("#fmtDelete").hidden = false;
  toast(res.problems.length ? "Saved — check the formula notes" : `“${res.format.name}” format saved`);
  if (!res.problems.length) $("#formatSheet").close();
}));
$("#fmtExport").addEventListener("click", () => guarded(async () => {
  const res = await readFormat($("#fmtText").value);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([JSON.stringify(res.format, null, 1)], { type: "application/json" }));
  link.download = `${res.format.id}.json`;
  link.click();
}));
$("#fmtDelete").addEventListener("click", () => {
  saveCustomFormats(customFormats().filter((f) => f.id !== editingFormat));
  toast("Your changes were removed");
  $("#formatSheet").close();
});
$("#bulk").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (b?.id === "bulkRun") await folderReady();
  if (b?.id === "bulkRun") extractAll();
  if (b?.id === "bulkZip") downloadAll();
  if (b?.id === "bulkStop" && state.bulk) { state.bulk.stop = true; renderBulk(); }
});
$("#results").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const i = b.dataset.extract ?? b.dataset.details ?? b.dataset.claude ?? b.dataset.gemini;
  if (i == null) return;
  const a = state.items[+i];
  if (b.dataset.details != null) return showDetails(a);
  if (b.dataset.claude != null) return openClaudeChat(a);
  if (state.bulk && b.dataset.gemini == null) return;
  a.readWith = b.dataset.gemini != null ? "gemini" : undefined; // Extract / Try again asks again for photos
  await folderReady();
  extract(a);
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
loadFormats();
if (canPickFolder) idb("readonly", (st) => st.get("dir")).then((d) => { state.dir = d || null; renderFolder(); });
renderFolder();
if (iso(deep.get("from"))) screen();
