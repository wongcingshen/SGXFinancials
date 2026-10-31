// Command-line wrapper used by the Python CLI and the GitHub Action.
//   node scraper/cli.mjs screen "<SGX page URL>" [company] [--attachments]
//   node scraper/cli.mjs attachments "<announcement URL>"
// Prints JSON. Set CHROME_PATH to use an installed Chrome instead of @sparticuz/chromium.
import { attachments, launchBrowser, screen } from "./sgx.mjs";

const args = process.argv.slice(2);
const withFiles = args.includes("--attachments");
const [op, url, company] = args.filter((a) => a !== "--attachments");
if (!op || !url) {
  console.error('usage: node scraper/cli.mjs screen|attachments "<url>" [company]');
  process.exit(2);
}
let browser;
try {
  browser = await launchBrowser();
  const out = op === "attachments" ? { files: await attachments(browser, url) } : await screen(browser, url, { company, attachments: withFiles });
  process.stdout.write(JSON.stringify(out));
} catch (e) {
  console.error(e?.message || String(e));
  process.exitCode = 1;
} finally {
  await browser?.close();
}
