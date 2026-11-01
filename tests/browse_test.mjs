// Tests the browser scraper against a stand-in SGX site served through request interception.
//   CHROME_PATH=/path/to/chrome node tests/browse_test.mjs
import assert from "node:assert/strict";
import { attachments, launchBrowser, screen } from "../scraper/sgx.mjs";

const TOKEN = "page-generated-token";
const ROWS = [
  ["30 Oct 2026", "11:11 PM", "MIYOSHI LIMITED", "MIYOSHI LIMITED", "Full Yearly Results", "M1"],
  ["30 Oct 2026", "09:18 PM", "NO VA LAND INVESTMENT GROUP CORPORATION", "", "Third Quarter Results", "N1"],
  ["30 Oct 2026", "07:39 PM", "BANK OF BARODA", "MULTIPLE", "Second Quarter and/ or Half Yearly Results", "B1"],
];
// A notice in the same category: listed, but its page must not be opened.
const NOTICE_ROW = ["30 Oct 2026", "05:48 PM", "KSH HOLDINGS LIMITED", "KSH HOLDINGS LIMITED", "Notification of Results Release", "K1"];
const annUrl = (id) => `https://links.sgx.com/1.0.0/corporate-announcements/${id}/abc`;
const fullTitle = (t) => `Financial Statements and Related Announcement::${t}`;

// Listing laid out like sgx.com: Date & Time | Issuer Name | Security Name | Title | Category.
function tablePage({ clickable }) {
  const rows = [...ROWS, NOTICE_ROW].map(([d, t, issuer, sec, title, id]) => {
    const cell = clickable
      ? `<span class="link" onclick="window.open('${annUrl(id)}', '_blank')">${fullTitle(title)}</span>`
      : `<a href="${annUrl(id)}" target="_blank">${fullTitle(title)}</a>`;
    return `<tr><td>${d}<br>${t}</td><td>${issuer}</td><td>${sec}</td><td>${cell}</td><td><a href="#">Financial Statements</a></td></tr>`;
  }).join("");
  return `<!doctype html><title>Company Announcements</title><body><nav><a href="https://www.sgx.com/">Home</a></nav>
    <sgx-table id="h"></sgx-table><script>
    const root = document.getElementById("h").attachShadow({ mode: "open" });
    setTimeout(() => { root.innerHTML = '<table><thead><tr><th>Date &amp; Time</th><th>Issuer Name</th><th>Security Name</th><th>Title</th><th>Category</th></tr></thead><tbody>${rows.replace(/'/g, "\\'")}</tbody></table>'; }, 600);
    </script></body>`;
}

// Listing that loads its records like sgx.com: a token-bearing call to api.sgx.com, 2 per page.
const items = ROWS.map(([, , issuer, , title, id], i) => ({
  title: fullTitle(title), issuer_name: issuer, url: annUrl(id),
  broadcast_date_time: Date.UTC(2026, 9, 30, 10, 2 + i), category_name: "Financial Statements",
}));
const apiPage = `<!doctype html><title>Company Announcements</title><body><div id="t"></div><script>
fetch("https://api.sgx.com/announcements/v1.1/config", { headers: { authorizationtoken: "${TOKEN}" } });
fetch("https://api.sgx.com/announcements/v1.1/?periodstart=20261029_160000&periodend=20261030_155959&cat=ANNC&sub=ANNC17&pagestart=0&pagesize=2",
  { headers: { authorizationtoken: "${TOKEN}" } }).then(r => r.json()).then(j => {
  document.getElementById("t").innerHTML = j.data.map(d => '<a href="' + d.url + '">' + d.title + '</a>').join("<br>");
});</script></body>`;

// Announcement page: the PDFs live in "announcement-attachment-list"; a decoy PDF sits outside it.
const annPage = (id) => `<!doctype html><html><body><h1>Announcement ${id}</h1>
<div class="announcement-attachment-list">
  <a href="https://links.sgx.com/FileOpen/${id}_Results.ashx?App=Announcement&FileID=1">${id} Results.pdf</a>
  <a href="https://links.sgx.com/FileOpen/${id}_Press.ashx?App=Announcement&FileID=2">${id} Press Release.pdf</a>
</div>
<aside><a href="https://links.sgx.com/FileOpen/Related.ashx?App=Announcement&FileID=9">Related.pdf</a></aside>
<a href="https://evil.example/x.pdf">elsewhere.pdf</a></body></html>`;

const cors = { "Access-Control-Allow-Origin": "https://www.sgx.com", "Access-Control-Allow-Headers": "authorizationtoken", "Access-Control-Allow-Methods": "GET" };
let apiCalls = 0;
const opened = new Set(); // announcement pages loaded
function site(listing) {
  return async (page) => {
    await page.setRequestInterception(true);
    page.on("request", (r) => {
      const u = new URL(r.url());
      if (u.hostname === "www.sgx.com") return r.respond({ status: 200, contentType: "text/html", body: listing });
      if (u.hostname === "links.sgx.com") {
        if (r.resourceType() === "document") opened.add(u.pathname.split("/")[3]);
        return r.respond({ status: 200, contentType: "text/html", body: annPage(u.pathname.split("/")[3]) });
      }
      if (u.hostname === "api.sgx.com") {
        if (r.method() === "OPTIONS") return r.respond({ status: 204, headers: cors });
        if (u.pathname.endsWith("/config")) return r.respond({ status: 200, headers: cors, contentType: "application/json", body: '{"data":{"categories":[]}}' });
        apiCalls++;
        if (r.headers().authorizationtoken !== TOKEN) return r.respond({ status: 401, headers: cors, contentType: "application/json", body: "{}" });
        const p = Number(u.searchParams.get("pagestart"));
        return r.respond({ status: 200, headers: cors, contentType: "application/json",
          body: JSON.stringify({ meta: { code: "200", totalItems: items.length }, data: items.slice(p * 2, p * 2 + 2) }) });
      }
      return r.abort();
    });
  };
}
// New tabs opened by clicks need the same stand-in site.
function serveNewTabs(browser) {
  browser.on("targetcreated", async (t) => {
    if (!t.opener()) return; // only tabs opened by clicking a title
    const p = await t.page().catch(() => null);
    if (p) await site("")(p).catch(() => {});
  });
}

const URL_ = "https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17";
const browser = await launchBrowser();
serveNewTabs(browser);
try {
  // Title links in the results table, every title opened for its attachments.
  const t = await screen(browser, URL_, { preparePage: site(tablePage({ clickable: false })), attachments: true });
  assert.equal(t.debug.source, "result table");
  const notice = t.announcements.find((a) => a.issuer.startsWith("KSH"));
  assert.equal(notice.kind, "release_date");
  assert.equal(notice.attachments, undefined, "a notice is listed from its title only");
  assert.ok(!opened.has("K1") && ["M1", "N1", "B1"].every((id) => opened.has(id)), [...opened]);
  console.log("ok notice listed, not opened; results opened:", [...opened].join(","));
  t.announcements = t.announcements.filter((a) => a !== notice);
  assert.deepEqual(t.announcements.map((a) => [a.broadcast, a.issuer, a.title]), [
    ["2026-10-30 23:11", "MIYOSHI LIMITED", fullTitle("Full Yearly Results")],
    ["2026-10-30 21:18", "NO VA LAND INVESTMENT GROUP CORPORATION", fullTitle("Third Quarter Results")],
    ["2026-10-30 19:39", "BANK OF BARODA", fullTitle("Second Quarter and/ or Half Yearly Results")],
  ]);
  assert.deepEqual(t.announcements[0].attachments.map((f) => [f.name, f.from]),
    [["M1 Results.pdf", "announcement-attachment-list"], ["M1 Press Release.pdf", "announcement-attachment-list"]]);
  assert.ok(t.announcements.every((a) => a.attachments.length === 2), "only the files inside announcement-attachment-list");
  console.log("ok title links + attachment lists:", t.announcements.map((a) => `${a.issuer} (${a.attachments.length} PDFs)`).join(" | "));

  // Titles that are click targets (open a new tab) rather than plain links.
  const c = await screen(browser, URL_, { preparePage: site(tablePage({ clickable: true })) });
  assert.equal(c.debug.clicked, 4);
  assert.deepEqual(c.announcements.map((a) => a.url), [...ROWS, NOTICE_ROW].map((r) => annUrl(r[5])));
  console.log("ok clicked every title:", c.announcements.length, "links");

  // Records the page loads for itself (non-list responses ignored), paged with the page's token.
  apiCalls = 0;
  const a = await screen(browser, URL_, { preparePage: site(apiPage) });
  assert.equal(a.debug.source, "page data");
  assert.equal(a.announcements.length, 3);
  assert.equal(apiCalls, 2);
  assert.equal(a.announcements[0].broadcast, "2026-10-30 18:02");
  console.log("ok page data + pagination, config response ignored");

  const f = await screen(browser, URL_, { preparePage: site(apiPage), company: "baroda" });
  assert.deepEqual(f.announcements.map((x) => x.issuer), ["BANK OF BARODA"]);
  console.log("ok company filter");

  const files = await attachments(browser, annUrl("B1"), { preparePage: site("") });
  assert.deepEqual(files.map((x) => x.name), ["B1 Results.pdf", "B1 Press Release.pdf"]);
  console.log("ok single announcement:", files.length, "PDFs");
} finally {
  await browser.close();
}
