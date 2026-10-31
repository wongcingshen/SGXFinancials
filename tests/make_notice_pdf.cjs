// Builds tests/notice.pdf: a one-page "date of release of financial results" notice (not results).
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  await p.setContent(`<div style="font:13px Arial;padding:40px"><h3>KSH HOLDINGS LIMITED</h3>
    <p>(Company Registration No. 200603337G)</p><h3>DATE OF RELEASE OF FINANCIAL RESULTS FOR FIRST HALF OF FY2027</h3>
    <p>The Board of Directors (the “Board”) of KSH Holdings Limited (the “Company” and together with its subsidiaries, the
    “Group”) wishes to announce that the Company intends to release its financial results for the first half of FY2027 ended
    30 September 2026 on 13 November 2026 (Friday) after the close of trading on the Singapore Exchange.</p>
    <p>BY ORDER OF THE BOARD<br>Company Secretary<br>30 October 2026</p></div>`);
  await p.pdf({ path: path.join(__dirname, 'notice.pdf'), format: 'A4' });
  await b.close();
})();
