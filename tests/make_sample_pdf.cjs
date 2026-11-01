// Builds tests/sample_results.pdf: a fake SGX half-year results announcement
// (cover, contents, commentary, the three statements, notes) for the scanner tests.
const { chromium } = require('playwright');
const path = require('path');
const pg = (body) => `<section style="page-break-after:always;font:11px Arial;padding:30px">${body}</section>`;
const tbl = (rows) => '<table style="width:100%;border-collapse:collapse">' +
  rows.map(r => '<tr>' + r.map((c, i) => `<td style="text-align:${i ? 'right' : 'left'}">${c}</td>`).join('') + '</tr>').join('') + '</table>';
const html = [
  pg('<h1>ACME HOLDINGS LIMITED</h1><p>Condensed Interim Financial Statements for the six months ended 30 June 2026</p>'),
  pg('<h2>Table of Contents</h2><p>A. Condensed interim consolidated statement of profit or loss ... 3</p><p>B. Condensed interim statements of financial position ... 4</p><p>C. Condensed interim consolidated statement of cash flows ... 6</p><p>E. Notes to the condensed interim financial statements ... 7</p>'),
  pg('<h2>Review of Group Performance</h2><p>Revenue increased by 12% to S$120.5 million due to higher demand. Gross profit rose to S$40.1 million. The Group remains in a net cash position. Profit for the period attributable to owners was S$15.2 million.</p>'.repeat(3)),
  pg('<h2>A. Condensed interim consolidated statement of profit or loss and other comprehensive income</h2><p>For the six months ended 30 June 2026</p>' + tbl([
    ['', 'Group 6M 30 Jun 2026 S$\'000', 'Group 6M 30 Jun 2025 S$\'000', '%'],
    ['Revenue', '120,512', '107,600', '12.0'], ['Cost of sales', '(80,412)', '(72,100)', '11.5'],
    ['Gross profit', '40,100', '35,500', '13.0'], ['Other income', '1,250', '980', ''],
    ['Distribution expenses', '(5,300)', '(4,900)', ''], ['Administrative expenses', '(14,200)', '(13,100)', ''],
    ['Other operating expenses', '(1,850)', '(1,480)', ''], ['Finance costs', '(1,200)', '(1,350)', ''],
    ['Share of results of associates', '600', '450', ''], ['Profit before tax', '19,400', '16,100', ''],
    ['Income tax expense', '(3,700)', '(3,050)', ''], ['Profit for the period', '15,700', '13,050', ''],
    ['Attributable to: Owners of the Company', '15,200', '12,700', ''], ['Non-controlling interests', '500', '350', ''],
    ['Earnings per share (cents) Basic', '3.04', '2.54', ''], ['Diluted', '3.02', '2.53', '']])),
  pg('<h2>B. Condensed interim statements of financial position</h2>' + tbl([
    ['', 'Group 30 Jun 2026', 'Group 31 Dec 2025', 'Company 30 Jun 2026', 'Company 31 Dec 2025'],
    ['ASSETS', '', '', '', ''], ['Non-current assets', '', '', '', ''],
    ['Property, plant and equipment', '85,000', '82,300', '1,200', '1,300'], ['Right-of-use assets', '6,000', '6,500', '', ''],
    ['Investment in associates', '9,500', '8,900', '', ''], ['Investment in subsidiaries', '', '', '60,000', '60,000'],
    ['Deferred tax assets', '800', '750', '', ''], ['Total non-current assets', '101,300', '98,450', '61,200', '61,300'],
    ['Current assets', '', '', '', ''], ['Inventories', '22,000', '20,500', '', ''],
    ['Trade and other receivables', '30,400', '28,100', '5,000', '4,800'], ['Cash and cash equivalents', '45,300', '41,950', '3,000', '2,500'],
    ['Total current assets', '97,700', '90,550', '8,000', '7,300'], ['Total assets', '199,000', '189,000', '69,200', '68,600']])),
  pg('<p>Statements of financial position (continued)</p>' + tbl([
    ['', 'Group 30 Jun 2026', 'Group 31 Dec 2025', 'Company 30 Jun 2026', 'Company 31 Dec 2025'],
    ['EQUITY AND LIABILITIES', '', '', '', ''], ['Share capital', '50,000', '50,000', '50,000', '50,000'],
    ['Treasury shares', '(1,000)', '(1,000)', '(1,000)', '(1,000)'], ['Retained earnings', '78,000', '68,800', '15,000', '14,500'],
    ['Other reserves', '(2,000)', '(1,800)', '', ''], ['Equity attributable to owners of the Company', '125,000', '116,000', '64,000', '63,500'],
    ['Non-controlling interests', '4,000', '3,500', '', ''], ['Total equity', '129,000', '119,500', '64,000', '63,500'],
    ['Non-current liabilities', '', '', '', ''], ['Loans and borrowings', '20,000', '22,000', '', ''],
    ['Lease liabilities', '4,500', '5,000', '', ''], ['Deferred tax liabilities', '1,500', '1,400', '', ''],
    ['Total non-current liabilities', '26,000', '28,400', '', ''], ['Current liabilities', '', '', '', ''],
    ['Trade and other payables', '33,000', '30,100', '5,200', '5,100'], ['Loans and borrowings', '7,000', '7,500', '', ''],
    ['Lease liabilities', '1,500', '1,500', '', ''], ['Income tax payable', '2,500', '2,000', '', ''],
    ['Total current liabilities', '44,000', '41,100', '5,200', '5,100'], ['Total liabilities', '70,000', '69,500', '5,200', '5,100'],
    ['Total equity and liabilities', '199,000', '189,000', '69,200', '68,600']])),
  pg('<h2>C. Condensed interim consolidated statement of cash flows</h2>' + tbl([
    ['', '6M 2026 S$\'000', '6M 2025 S$\'000'], ['Cash flows from operating activities', '', ''],
    ['Profit before tax', '19,400', '16,100'], ['Adjustments for: Depreciation of property, plant and equipment', '5,200', '4,900'],
    ['Depreciation of right-of-use assets', '800', '750'], ['Interest expense', '1,200', '1,350'], ['Share of results of associates', '(600)', '(450)'],
    ['Operating cash flow before working capital changes', '26,000', '22,650'], ['Inventories', '(1,500)', '(900)'],
    ['Trade and other receivables', '(2,300)', '(1,800)'], ['Trade and other payables', '2,900', '1,500'],
    ['Cash generated from operations', '25,100', '21,450'], ['Income tax paid', '(3,100)', '(2,700)'],
    ['Net cash from operating activities', '22,000', '18,750'], ['Cash flows from investing activities', '', ''],
    ['Purchase of property, plant and equipment', '(7,900)', '(6,500)'], ['Proceeds from disposal of property, plant and equipment', '200', '150'],
    ['Net cash used in investing activities', '(7,700)', '(6,350)'], ['Cash flows from financing activities', '', ''],
    ['Repayment of loans and borrowings', '(2,500)', '(2,000)'], ['Payment of lease liabilities', '(800)', '(750)'],
    ['Interest paid', '(1,200)', '(1,350)'], ['Dividends paid', '(6,000)', '(5,000)'],
    ['Net cash used in financing activities', '(10,500)', '(9,100)'], ['Net increase in cash and cash equivalents', '3,800', '3,300'],
    ['Cash and cash equivalents at beginning of the period', '41,950', '36,000'], ['Effect of exchange rate changes', '(450)', '(200)'],
    ['Cash and cash equivalents at end of the period', '45,300', '39,100']])),
  pg('<h2>E. Notes to the condensed interim financial statements</h2><p>1. Corporate information. 2. Basis of preparation. The condensed interim financial statements have been prepared in accordance with SFRS(I) 1-34. Revenue 120,512 107,600. Profit before tax 19,400. Cash and cash equivalents 45,300.</p>'),
].join('');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  await p.setContent(html); await p.pdf({ path: path.join(__dirname, 'sample_results.pdf'), format: 'A4' });
  // The same statements filed as the parent company's alone ("separate"), for the duplicate-filing tests.
  await p.setContent(html.replace(/consolidated/gi, 'separate').replace('B. Condensed interim statements of financial position', 'B. Condensed interim separate statement of financial position'));
  await p.pdf({ path: path.join(__dirname, 'separate_results.pdf'), format: 'A4' });
  await b.close();
})();
