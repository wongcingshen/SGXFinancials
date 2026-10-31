// Vercel Node function: drives a headless browser through the SGX website.
//   GET /api/browse?op=screen&url=<SGX announcements page URL>&company=&attachments=1
//       attachments=1 also opens every Title link and lists its announcement-attachment-list PDFs
//   GET /api/browse?op=attachments&url=<links.sgx.com announcement URL>
import { timingSafeEqual } from "node:crypto";
import { attachments, launchBrowser, screen } from "../scraper/sgx.mjs";

function authorised(req) {
  const pw = process.env.APP_PASSWORD;
  if (!pw) return true;
  const got = Buffer.from(String(req.headers["x-app-password"] || ""));
  const want = Buffer.from(pw);
  return got.length === want.length && timingSafeEqual(got, want);
}

export default async function handler(req, res) {
  const q = new URL(req.url, "http://localhost").searchParams;
  const op = q.get("op") || "screen";
  if (!authorised(req)) return res.status(401).json({ error: "Wrong or missing password" });
  const url = q.get("url") || "";
  if (!url) return res.status(400).json({ error: "url is required" });

  let browser;
  try {
    browser = await launchBrowser();
    if (op === "attachments") {
      return res.status(200).json({ url, files: await attachments(browser, url) });
    }
    const { announcements, debug } = await screen(browser, url, {
      company: q.get("company") || "",
      attachments: q.get("attachments") === "1",
    });
    return res.status(200).json({ page_url: url, count: announcements.length, announcements, debug });
  } catch (e) {
    console.error(e);
    return res.status(502).json({ error: `Browser: ${e.message || e}` });
  } finally {
    await browser?.close().catch(() => {});
  }
}
