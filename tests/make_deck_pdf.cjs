// Builds tests/deck_results.pdf: a 16:9 results presentation (slides with highlights, no formal
// financial statements), the kind of PDF some results announcements carry instead of the statements.
const { chromium } = require('playwright');
const path = require('path');
const slide = (body) => `<section style="width:13.333in;height:7.5in;box-sizing:border-box;padding:.6in .8in;page-break-after:always;font:22px Arial">${body}</section>`;
const tbl = (rows) => '<table style="width:100%;border-collapse:collapse;font-size:20px">' +
  rows.map(r => '<tr>' + r.map((c, i) => `<td style="padding:6px;text-align:${i ? 'right' : 'left'}">${c}</td>`).join('') + '</tr>').join('') + '</table>';
const html = [
  slide('<h1 style="font-size:44px">ACME HOLDINGS LIMITED</h1><h2>1H FY2026 Results Presentation</h2><p>12 August 2026</p>'),
  slide('<h2>Agenda</h2><p>Key highlights</p><p>Financial review</p><p>Outlook</p><p>Q&amp;A</p>'),
  slide('<h2>Key highlights</h2><p>Revenue up 12% to S$120.5 million</p><p>Gross profit S$40.1 million</p><p>PATMI S$15.2 million</p><p>Net cash S$45.3 million</p>'),
  slide('<h2>Income statement highlights</h2>' + tbl([
    ['S$\'000', '1H FY2026', '1H FY2025', 'Change'], ['Revenue', '120,512', '107,600', '12.0%'],
    ['Gross profit', '40,100', '35,500', '13.0%'], ['Profit before tax', '19,400', '16,100', '20.5%'],
    ['PATMI', '15,200', '12,700', '19.7%']])),
  slide('<h2>Balance sheet highlights</h2>' + tbl([
    ['S$\'000', '30 Jun 2026', '31 Dec 2025'], ['Total assets', '199,000', '189,000'],
    ['Total equity', '129,000', '119,500'], ['Net cash', '45,300', '41,950']])),
  slide('<h2>Outlook</h2><p>The Group expects demand to remain firm in the second half.</p><p style="font-size:14px">Disclaimer: this presentation may contain forward-looking statements.</p>'),
  slide('<h1 style="font-size:48px;text-align:center;margin-top:2in">Thank you</h1>'),
].join('');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  await p.setContent(`<html><body style="margin:0">${html}</body></html>`);
  await p.pdf({ path: path.join(__dirname, 'deck_results.pdf'), width: '13.333in', height: '7.5in', printBackground: true });
  await b.close();
})();
