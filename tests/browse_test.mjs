// Tests the browser scraper against a stand-in SGX site served through request interception.
//   CHROME_PATH=/path/to/chrome node tests/browse_test.mjs
import assert from "node:assert/strict";
import { attachments, launchBrowser, screen } from "../scraper/sgx.mjs";

const TOKEN = "page-generated-token";
const items = [1, 2, 3].map((i) => ({
  title: `Financial Statements and Related Announcement::Half Yearly Results ${i}`,
  issuer_name: ["DBS GROUP HOLDINGS LTD", "KEPPEL LTD.", "HIAP HOE LIMITED"][i - 1],
  security_name: "", url: `https://links.sgx.com/1.0.0/corporate-announcements/ANN${i}/abc${i}`,
  broadcast_date_time: Date.UTC(2026, 9, 30, 10, 2 + i), // 30 Oct 2026 18:0x SGT
  category_name: "Financial Statements",
}));

// Listing page that loads its data like sgx.com: a token-bearing call to api.sgx.com, 2 per page.
const apiPage = `<!doctype html><title>Company Announcements</title><body><div id="t"></div><script>
fetch("https://api.sgx.com/announcements/v1.1/?periodstart=20261029_160000&periodend=20261030_155959&cat=ANNC&sub=ANNC17&pagestart=0&pagesize=2",
  { headers: { authorizationtoken: "${TOKEN}" } }).then(r => r.json()).then(j => {
  document.getElementById("t").innerHTML = j.data.map(d => '<a href="' + d.url + '">' + d.title + '</a>').join("<br>");
});</script></body>`;

// Listing page with no API call to observe: rows rendered inside a shadow root.
const domPage = `<!doctype html><title>Company Announcements</title><body><sgx-table id="h"></sgx-table><script>
const root = document.getElementById("h").attachShadow({ mode: "open" });
setTimeout(() => { root.innerHTML = '<table>' + [
  ["30 Oct 2026 06:02 PM", "DBS GROUP HOLDINGS LTD", "Third Quarter Results", "ANNA"],
  ["30 Oct 2026 07:05 AM", "UNITED OVERSEAS BANK LIMITED", "Third Quarter Results", "ANNB"],
].map(([d, n, t, id]) => '<tr><td>' + d + '</td><td>' + n + '</td><td><a href="https://links.sgx.com/1.0.0/corporate-announcements/' + id + '/x">' + t + '</a></td></tr>').join("") + '</table>'; }, 600);
</script></body>`;

const annPage = `<!doctype html><html><body><h1>Half Yearly Results</h1>
<h2>Attachments</h2>
<a href="https://links.sgx.com/FileOpen/Results.ashx?App=Announcement&FileID=111">Results.pdf</a>
<a href="https://links.sgx.com/FileOpen/Press.ashx?App=Announcement&FileID=112">Press Release.pdf</a>
<a href="https://evil.example/x.pdf">elsewhere.pdf</a>
<a href="https://www.sgx.com/">SGX</a></body></html>`;

const cors = { "Access-Control-Allow-Origin": "https://www.sgx.com", "Access-Control-Allow-Headers": "authorizationtoken", "Access-Control-Allow-Methods": "GET" };
let apiCalls = 0;
function site(listing) {
  return async (page) => {
    await page.setRequestInterception(true);
    page.on("request", (r) => {
      const u = new URL(r.url());
      if (u.hostname === "www.sgx.com") return r.respond({ status: 200, contentType: "text/html", body: listing });
      if (u.hostname === "links.sgx.com") return r.respond({ status: 200, contentType: "text/html", body: annPage });
      if (u.hostname === "api.sgx.com") {
        if (r.method() === "OPTIONS") return r.respond({ status: 204, headers: cors });
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

const browser = await launchBrowser();
try {
  const url = "https://www.sgx.com/stock-exchange/company-announcements?from=20261030&to=20261030&ANNC=ANNC17";

  const a = await screen(browser, url, { preparePage: site(apiPage) });
  assert.equal(a.debug.source, "page data");
  assert.equal(a.announcements.length, 3, "pages through all results with the page's token");
  assert.equal(apiCalls, 2);
  assert.deepEqual(a.announcements[0], { title: items[0].title, issuer: "DBS GROUP HOLDINGS LTD", stock_code: "",
    url: items[0].url, broadcast: "2026-10-30 18:03", category: "Financial Statements" });
  console.log("ok page data + pagination:", a.announcements.map((x) => x.issuer).join(" | "));

  const f = await screen(browser, url, { preparePage: site(apiPage), company: "keppel" });
  assert.equal(f.announcements.length, 1);
  console.log("ok company filter");

  const d = await screen(browser, url, { preparePage: site(domPage) });
  assert.equal(d.debug.source, "result table");
  assert.deepEqual(d.announcements.map((x) => [x.issuer, x.broadcast, x.title]), [
    ["DBS GROUP HOLDINGS LTD", "2026-10-30 18:02", "Third Quarter Results"],
    ["UNITED OVERSEAS BANK LIMITED", "2026-10-30 07:05", "Third Quarter Results"],
  ]);
  console.log("ok result table (shadow DOM):", d.announcements.length, "rows");

  const files = await attachments(browser, items[0].url, { preparePage: site(apiPage) });
  assert.deepEqual(files.map((x) => x.name), ["Results.pdf", "Press Release.pdf"]);
  console.log("ok attachments:", files.map((x) => x.url).join(" , "));
} finally {
  await browser.close();
}
