// SGX Financials front end. Talks to /api/health, /api/screen and /api/process.
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
  announcements: [],
  done: new Map(), // announcement url -> { url (blob), filename, result }
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
  let r;
  try { r = await fetch(path, { ...opts, headers }); }
  catch { throw new Error("Can’t reach the server. Check your connection and try again."); }
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

function pageUrl() {
  const pasted = $("#sgxUrl").value.trim();
  if ($("#pasteRow").classList.contains("open") && pasted) return pasted;
  return `https://www.sgx.com/stock-exchange/company-announcements?from=${compact($("#from").value)}&to=${compact($("#to").value)}&ANNC=ANNC17`;
}
function refreshLink() {
  const u = pageUrl();
  $("#pageLink").href = u;
  $("#pageLink").textContent = u.replace(/^https:\/\/www\./, "");
}

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
        <svg width="100%" viewBox="0 0 260 120" aria-hidden="true" style="max-width:300px">
          <rect width="260" height="120" rx="16" fill="#FDEBDD"/><circle cx="232" cy="20" r="34" fill="#FFD66B"/>
          <path d="M0 86 C50 76 74 102 122 88 S200 44 260 58" fill="none" stroke="#F9D6C2" stroke-width="10"/>
          <path d="M40 90 C70 82 84 60 120 62 S176 36 206 32" fill="none" stroke="#141413" stroke-opacity=".55" stroke-width="2.5" stroke-linecap="round" stroke-dasharray="1 7"/>
          <circle cx="40" cy="90" r="10" fill="#1D6766"/><circle cx="40" cy="90" r="3.5" fill="#fff"/>
          <circle cx="120" cy="62" r="10" fill="#3B4A8C"/><circle cx="120" cy="62" r="3.5" fill="#fff"/>
          <circle cx="206" cy="32" r="12" fill="#F2784B"/><path d="M200.5 32l3.8 3.8 7-7.6" fill="none" stroke="#141413" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <h3>Pick a date and screen SGX</h3>
        <p>Results season is busiest from late January to March, May, August and November. The dates start on the last trading days: Friday to Sunday over a weekend, otherwise yesterday. Leave the company blank to see every financial statements announcement.</p>
      </div>
      <ol class="steps">
        <li><span class="badge">1</span><span><strong>Screen</strong> <span class="muted">— the same date and ANNC17 filter as the SGX announcements page.</span></span></li>
        <li><span class="badge">2</span><span><strong>Scan</strong> <span class="muted">— a keyword scan finds the income statement, balance sheet and cash flow pages. No AI, no cost.</span></span></li>
        <li><span class="badge">3</span><span><strong>Extract</strong> <span class="muted">— ${esc(state.ai)} reads only those pages and fills a ShareInvestor-style workbook, with tie-out checks.</span></span></li>
      </ol>
    </div>`;
}

function renderSkeleton() {
  const bone = (w, h, extra = "") => `<div class="bone" style="width:${w};height:${h}px;${extra}"></div>`;
  $("#results").innerHTML = `<p class="muted" style="margin:0 0 14px;font-size:14px">Opening sgx.com in a browser, reading the results and opening every title for its attachments. This takes about 20–90 seconds.</p>
    <div class="grid" aria-busy="true" aria-label="Loading announcements">${
    Array.from({ length: 6 }, () => `<div class="skeleton">${bone("100%", 104, "border-radius:0")}
      <div style="padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px">${bone("70%", 20)}${bone("95%", 12)}${bone("60%", 12)}
      <div style="display:flex;gap:8px;margin-top:10px">${bone("50%", 42, "border-radius:999px")}${bone("50%", 42, "border-radius:999px")}</div></div></div>`).join("")
  }</div>`;
}

function cardHtml(a, i) {
  const done = state.done.get(a.url);

  return `<article class="card ${done ? "done" : ""}" data-accent="${accentFor(a.stock_code || a.issuer)}" style="animation-delay:${Math.min(i, 12) * 30}ms">
    <div class="band">
      <svg class="route" viewBox="0 0 260 104" preserveAspectRatio="none" aria-hidden="true"><path d="M20 84 C70 70 90 40 140 46 S210 20 250 14" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="2" stroke-linecap="round" stroke-dasharray="1 7"/></svg>
      <span class="pill">${done ? `${icon("check", "width:14px;height:14px")}Workbook ready` : `<span class="dot"></span>${esc(when(a.broadcast) || "SGX")}`}</span>
      <span class="code">${esc(a.stock_code || "SGX")}</span>
    </div>
    <div class="card-body">
      <span class="name">${esc(nameCase(a.issuer) || "Unknown issuer")}</span>
      <a class="title" href="${esc(a.url)}" target="_blank" rel="noopener" title="${esc(a.title)}">${esc(shortTitle(a.title))}</a>
      ${filesLine(a)}
      <div class="card-actions">${done
        ? `<button type="button" class="btn btn-outline" data-view="${i}">${icon("eye")}View</button>
           <a class="btn btn-ember" href="${done.url}" download="${esc(done.filename)}">${icon("download")}Excel</a>`
        : `<button type="button" class="btn btn-outline" data-scan="${i}">${icon("scan")}Scan</button>
           <button type="button" class="btn btn-ember" data-extract="${i}">${icon("sparkles")}Extract</button>`}
      </div>
    </div>
  </article>`;
}

// The PDFs the browser found in the announcement's attachment list.
function filesLine(a) {
  if (!Array.isArray(a.attachments)) return "";
  if (!a.attachments.length) return `<span class="files none">${icon("file", "width:14px;height:14px")}No attachments found</span>`;
  const names = a.attachments.map((f) => f.name || "file").join(", ");
  return `<span class="files" title="${esc(names)}">${icon("file", "width:14px;height:14px")}${a.attachments.length} PDF${a.attachments.length === 1 ? "" : "s"}: ${esc(names)}</span>`;
}

function renderCards() {
  const list = state.announcements;
  if (!list.length) {
    const d = state.debug || {};
    const saw = d.source ? `<p style="font-size:12.5px">What the browser saw: “${esc(d.title || "untitled page")}”, ${d.api_seen ? "the page's result data loaded" : `no result data${d.api_status ? ` (SGX answered ${d.api_status})` : ""}`}, ${d.dom_links ?? 0} announcement links.${
      d.body_excerpt ? `<br><span class="mono">${esc(d.body_excerpt.slice(0, 240))}</span>` : ""}</p>` : "";
    $("#results").innerHTML = `<div class="intro"><div><h3 style="margin-top:0">No financial results on this screen</h3>
      <p>SGX lists nothing under ANNC17 for these dates${$("#company").value.trim() ? " and this company" : ""}. Try a wider range, or open the screening link to check on sgx.com.</p>${saw}</div>
      <div><button type="button" class="btn btn-ember btn-lg" data-direct>${icon("file")}Process a filing directly</button></div></div>`;
    return;
  }
  $("#results").innerHTML = `<div class="grid">${list.map(cardHtml).join("")}
    <button type="button" class="add-card" data-direct>
      <span class="sun-circle">${icon("plus")}</span>Process another filing
      <small>An SGX link or a PDF you already have.</small>
    </button></div>`;
}

async function screen(ev) {
  ev?.preventDefault();
  const btn = $("#screenBtn");
  if (!$("#pasteRow").classList.contains("open") && $("#from").value > $("#to").value) {
    toast("The From date is after the To date.");
    return;
  }
  btn.disabled = true;
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
    state.announcements = res.announcements;
    state.debug = res.debug || null;
    const m = res.page_url.match(/from=(\d{8}).*?to=(\d{8})/);
    const fmt = (s) => nice(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`);
    $("#annNote").textContent = `${res.count} result${res.count === 1 ? "" : "s"} · ANNC17 · ${m ? (m[1] === m[2] ? fmt(m[1]) : `${fmt(m[1])} – ${fmt(m[2])}`) : ""}`;
    renderCards();
  } catch (e) {
    $("#results").innerHTML = `<div class="notice error">${icon("alert")}<div><b>Couldn’t screen SGX.</b> ${esc(e.message)}
      ${e.auth ? "" : `<br><span style="font-size:13px">Open the screening link above to check sgx.com loads, then try again. You can also process a filing directly from its SGX link.</span>`}</div></div>`;
    if (e.auth) openSheet("#settingsSheet");
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ run sheet
const stepNames = () => ["Download", "Keyword scan", `${state.ai} reads`, "Workbook"];

function setSteps(active, { failed = false, scanOnly = false } = {}) {
  const steps = scanOnly ? stepNames().slice(0, 2) : stepNames();
  const el = $("#runSteps");
  el.style.gridTemplateColumns = `repeat(${steps.length}, 1fr)`;
  el.innerHTML = steps.map((s, i) => {
    const st = i < active ? "done" : i === active ? (failed ? "failed" : "active") : "";
    const inner = st === "done" ? icon("check", "width:16px;height:16px") : st === "failed" ? icon("x", "width:16px;height:16px") : i + 1;
    return `<li class="${st}"><span class="badge">${inner}</span>${s}</li>`;
  }).join("");
}

function scanHtml(scan, pages) {
  const owner = {};
  for (const s of STATEMENTS) for (const p of scan.selected[s.key] || []) owner[p] = s.cls;
  const img = new Set(scan.image_only_pages);
  const rows = STATEMENTS.map((s) => {
    const sel = scan.selected[s.key] || [];
    const top = scan.candidates[s.key]?.[0];
    return `<div class="stmt ${s.cls}"><span class="ico">${icon(s.icon)}</span>
      <span class="nm">${s.name}<small>${sel.length ? `keyword score ${top ? Math.round(top.score) : "–"}` : "no page scored high enough"}</small></span>
      <span class="chips">${sel.length ? sel.map((p) => `<span class="chip">p${p}</span>`).join("") : `<span class="chip missing">not found</span>`}</span></div>`;
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
      <p class="muted" style="margin:10px 0 0;font-size:13px">${esc(scan.pdf)} · ${scan.page_count} pages${scan.attachments.length > 1 ? ` · picked from ${scan.attachments.length} attachments` : ""}${
        pages?.length ? ` · ${pages.length} page${pages.length === 1 ? "" : "s"} go to ${esc(state.ai)}` : ""}</p>
    </div>
    ${img.size ? `<div class="notice">${icon("alert")}<div>${img.size} page${img.size === 1 ? " has" : "s have"} no text layer (scanned images), so the keyword scan can’t read ${img.size === 1 ? "it" : "them"}. If a statement is missing, enter its pages below.</div></div>` : ""}`;
}

function resultHtml(r) {
  const pass = r.checks.filter((c) => c.status === "PASS").length;
  const fail = r.checks.filter((c) => c.status === "FAIL");
  const na = r.checks.length - pass - fail.length;
  const ordered = [...fail, ...r.checks.filter((c) => c.status !== "FAIL")];
  return `<div>
      <p class="block-title">Extracted</p>
      <div class="kv"><span><b>${esc(nameCase(r.meta.company_name))}</b></span><span>${esc(r.meta.currency)} · ${esc(r.meta.basis || "Group")}</span>
        <span>pages ${esc(r.meta.pages)}</span><span>${r.usage?.input_tokens ? `${r.usage.input_tokens.toLocaleString()} in / ${r.usage.output_tokens.toLocaleString()} out tokens` : "from cache, no tokens used"}</span></div>
    </div>
    <div>
      <p class="block-title">Tie-out checks</p>
      <div class="check-sum"><span class="tag pass">${icon("check", "width:14px;height:14px")}${pass} pass</span>
        <span class="tag ${fail.length ? "fail" : "na"}">${icon(fail.length ? "alert" : "check", "width:14px;height:14px")}${fail.length} fail</span>
        ${na ? `<span class="tag na">${na} not checkable</span>` : ""}</div>
      <details ${fail.length ? "open" : ""} style="margin-top:10px"><summary>${fail.length ? "Review the failed checks" : "Show every check"}</summary>
        <div class="check-list">${ordered.map((c) => `<div><span>${esc(c.check)}<br><span class="per">${esc(c.period)}${c.difference != null && c.status === "FAIL" ? ` · off by ${Number(c.difference).toLocaleString()}` : ""}</span></span>
          <span class="tag ${c.status === "PASS" ? "pass" : c.status === "FAIL" ? "fail" : "na"}">${c.status === "n/a" ? "n/a" : c.status.toLowerCase()}</span></div>`).join("")}</div>
      </details>
    </div>
    ${r.meta.notes ? `<div class="notice info">${icon("sparkles")}<div><b>${esc(state.ai)}’s notes</b><br>${esc(r.meta.notes)}</div></div>` : ""}`;
}

function rerunHtml(job) {
  return `<details><summary>Wrong pages? Choose them yourself</summary>
    <form class="rerun" id="rerunForm"><label class="sr-only" for="rerunPages">Pages</label>
      <input class="input" id="rerunPages" placeholder="e.g. 7,8,9,10" value="${esc(job.pages || "")}" inputmode="numeric">
      <button class="btn btn-ink" type="submit">${icon("refresh")}Extract</button></form></details>`;
}

function formData(job, dryRun, pages) {
  const fd = new FormData();
  for (const k of ["announcement_url", "pdf_url", "issuer", "stock_code", "title"]) if (job[k]) fd.append(k, job[k]);
  for (const u of job.pdf_urls || []) fd.append("pdf_url", u);
  if (job.file) fd.append("pdf", job.file);
  fd.append("dry_run", dryRun ? "true" : "false");
  fd.append("effort", state.effort);
  if (pages) fd.append("pages", pages);
  if (!dryRun && state.mergeFile) fd.append("workbook", state.mergeFile);
  return fd;
}

async function run(job, mode) {
  const scanOnly = mode === "scan";
  const sheet = $("#runSheet");
  sheet.dataset.accent = job.accent || accentFor(job.stock_code || job.issuer);
  sheetHead(job);
  $("#runBody").innerHTML = `<ol class="progress" id="runSteps"></ol><div class="stage" id="runStage"></div>`;
  $("#runFoot").hidden = true;
  setSteps(0, { scanOnly });
  openSheet("#runSheet");

  const stage = $("#runStage");
  stage.innerHTML = `<p class="muted" style="margin:0">Downloading the filing from SGX and scanning every page for the three statements…</p>`;
  let scanRes;
  try {
    scanRes = await api("/api/process?op=process", { method: "POST", body: formData(job, true, job.pages) });
  } catch (e) {
    if (e.body?.need_browser && job.announcement_url && !job.pdf_urls) {
      // Open the Title link in a browser and take the PDFs under "Attachments".
      stage.innerHTML = `<p class="muted" style="margin:0">Opening the announcement in a browser to find its attachments…</p>`;
      try {
        const { files } = await api(`/api/browse?op=attachments&url=${encodeURIComponent(job.announcement_url)}`);
        if (files.length) return run({ ...job, pdf_urls: files.map((f) => f.url) }, mode);
      } catch (e2) { e = e2; }
    }
    setSteps(1, { failed: true, scanOnly });
    stage.innerHTML = `<div class="notice error">${icon("alert")}<div>${esc(e.message)}</div></div>`;
    return;
  }
  const pages = scanRes.pages.join(",");
  stage.innerHTML = scanHtml(scanRes.scan, scanRes.pages);

  if (scanOnly || !scanRes.pages.length) {
    setSteps(2, { scanOnly: true });
    if (!scanRes.pages.length) stage.insertAdjacentHTML("beforeend", `<div class="notice error">${icon("alert")}<div>No statement pages found. Enter the pages to extract.</div></div>`);
    stage.insertAdjacentHTML("beforeend", rerunHtml({ ...job, pages }));
    $("#runFoot").hidden = false;
    $("#runFoot").innerHTML = scanRes.pages.length
      ? `<button type="button" class="btn btn-ember btn-lg btn-block" id="goExtract">${icon("sparkles")}Extract these ${scanRes.pages.length} pages</button>`
      : "";
    $("#goExtract")?.addEventListener("click", () => run({ ...job, pages }, "extract"));
    bindRerun(job);
    return;
  }

  setSteps(2);
  stage.insertAdjacentHTML("beforeend", `<div class="notice info" id="aiWait">${icon("sparkles")}<div>${esc(state.ai)} is reading ${scanRes.pages.length} pages. This usually takes one to three minutes.</div></div>`);
  let res;
  try {
    res = await api("/api/process?op=process", { method: "POST", body: formData(job, false, pages) });
  } catch (e) {
    setSteps(2, { failed: true });
    $("#aiWait").outerHTML = `<div class="notice error">${icon("alert")}<div>${esc(e.message)}</div></div>`;
    stage.insertAdjacentHTML("beforeend", rerunHtml({ ...job, pages }));
    bindRerun(job);
    return;
  }
  setSteps(4);
  const merged = state.mergeFile?.name;
  if (merged) clearMerge();
  const blob = new Blob([Uint8Array.from(atob(res.workbook_b64), (c) => c.charCodeAt(0))],
    { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  const url = URL.createObjectURL(blob);
  const key = job.announcement_url || job.pdf_url || job.fileName;
  state.done.set(key, { url, filename: res.filename, result: res, job });
  stage.innerHTML = scanHtml(res.scan, res.pages) + resultHtml(res) + rerunHtml({ ...job, pages });
  bindRerun(job);
  $("#runFoot").hidden = false;
  $("#runFoot").innerHTML = `<a class="btn btn-ember btn-lg btn-block" href="${url}" download="${esc(res.filename)}">${icon("download")}Download ${esc(res.filename)}</a>
    ${merged ? `<p class="muted" style="margin:0;font-size:13px;text-align:center">Merged into ${esc(merged)}: earlier periods are kept.</p>` : ""}`;
  if (state.announcements.some((a) => a.url === key)) renderCards();
}

function sheetHead(job) {
  $("#runEyebrow").textContent = [job.stock_code, when(job.broadcast, true)].filter(Boolean).join(" · ") || "Filing";
  $("#runTitle").textContent = nameCase(job.issuer) || job.fileName || "SGX filing";
  $("#runSub").textContent = shortTitle(job.title);
}

function bindRerun(job) {
  $("#rerunForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const p = $("#rerunPages").value.replace(/\s/g, "");
    if (!/^\d+(,\d+)*$/.test(p)) { toast("Enter page numbers like 7,8,9"); return; }
    run({ ...job, pages: p }, "extract");
  });
}

function viewDone(key) {
  const d = state.done.get(key);
  if (!d) return;
  const { job, result: res, url } = d;
  $("#runSheet").dataset.accent = accentFor(job.stock_code || job.issuer);
  sheetHead(job);
  $("#runBody").innerHTML = `<ol class="progress" id="runSteps"></ol><div class="stage" id="runStage">${scanHtml(res.scan, res.pages)}${resultHtml(res)}${rerunHtml({ ...job, pages: res.pages.join(",") })}</div>`;
  setSteps(4);
  bindRerun(job);
  $("#runFoot").hidden = false;
  $("#runFoot").innerHTML = `<a class="btn btn-ember btn-lg btn-block" href="${url}" download="${esc(d.filename)}">${icon("download")}Download ${esc(d.filename)}</a>`;
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
$("#dExtract").addEventListener("click", () => { const j = directJob(); if (j) { $("#directSheet").close(); run(j, "extract"); } });
$("#dScan").addEventListener("click", () => { const j = directJob(); if (j) { $("#directSheet").close(); run(j, "scan"); } });

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

// ------------------------------------------------------------------ wiring
[$("#from").value, $("#to").value] = defaultRange();
["from", "to", "sgxUrl"].forEach((id) => $(`#${id}`).addEventListener("input", refreshLink));
$("#togglePaste").addEventListener("click", () => {
  const open = $("#pasteRow").classList.toggle("open");
  $("#togglePaste").textContent = open ? "Use the dates instead" : "Paste an SGX link instead";
  if (open) $("#sgxUrl").focus();
  refreshLink();
});
$("#screenForm").addEventListener("submit", screen);
$("#openDirect").addEventListener("click", () => openSheet("#directSheet"));
$("#openSettings").addEventListener("click", () => openSheet("#settingsSheet"));
$("#results").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.hasAttribute("data-direct")) return openSheet("#directSheet");
  const i = b.dataset.scan ?? b.dataset.extract ?? b.dataset.view;
  if (i == null) return;
  const a = state.announcements[+i];
  if (b.dataset.view != null) return viewDone(a.url);
  const pdf_urls = a.attachments?.length ? a.attachments.map((f) => f.url) : undefined;
  run({ announcement_url: a.url, pdf_urls, issuer: a.issuer, stock_code: a.stock_code, title: a.title, broadcast: a.broadcast },
    b.dataset.scan != null ? "scan" : "extract");
});

// Deep link: /?url=<SGX page URL> pre-fills and screens.
const deep = new URLSearchParams(location.search).get("url");
if (deep) {
  $("#pasteRow").classList.add("open");
  $("#togglePaste").textContent = "Use the dates instead";
  $("#sgxUrl").value = deep;
}
syncEffort();
refreshLink();
renderIntro();
loadHealth();
if (deep) screen();
