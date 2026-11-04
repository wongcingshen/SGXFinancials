// The guided tour: its steps and state. Pure functions, so node:test covers them without a DOM.
// Targets are `data-tour` attributes on real controls. The overlay is in tour.js.

/**
 * @typedef {{
 *   id: string,
 *   target: string | null,   // data-tour value; null = a centred card with no spotlight
 *   title: string,
 *   body: string,
 *   cta?: string,            // label of the main button (default Next, Done on the last step)
 *   all?: boolean,           // spotlight every visible match together (e.g. both date fields)
 *   tapAdvances?: boolean,   // using the highlighted control also moves the tour on
 *   waitFor?: string,        // using the control parks the tour until app.js sends this signal
 *   waitText?: string,       // what the parked tour says meanwhile
 * }} Step
 */

/** @type {{ id: string, name: string, blurb: string, steps: Step[] }[]} */
export const CHAPTERS = [
  {
    id: "welcome", name: "Welcome", blurb: "What the app does, and whether the server is ready.",
    steps: [
      { id: "welcome", target: null, cta: "Show me around", title: "Welcome to SGX Financials",
        body: "This app collects the financial results that SGX-listed companies publish and turns them into Excel for you. Pick the dates, see every company that reported, and get each one’s income statement, balance sheet and cash flow in the same layout, ready to compare or build on. No more copying figures out of PDFs. The tour takes about three minutes, and you can close it any time." },
      { id: "status", target: "status", title: "Is the AI key set up?",
        body: "This shows whether the server has an API key for {ai}, the AI that reads the statements. Green: a key is in place. Amber: no key, or the server can’t be reached, so nothing can be extracted yet. Settings shows which. The key itself is only tried when you extract: a wrong or expired key shows up as an error on the card." },
    ],
  },
  {
    id: "screen", name: "Find results", blurb: "Pick dates, narrow to a company, and screen SGX.",
    steps: [
      { id: "dates", target: "dates", all: true, title: "Pick the dates",
        body: "Singapore dates. They start on the latest results days: Friday to Sunday over a weekend, otherwise yesterday. Widen the range to catch a whole week." },
      { id: "company", target: "company", title: "One company, or all",
        body: "Type a stock code such as D05, or part of a name. Leave it blank to see every financial statements announcement for those dates." },
      { id: "screen", target: "screen", waitFor: "screened", cta: "Not now", title: "Screen SGX",
        body: "A browser opens sgx.com as a person would and opens every title for its PDFs. It takes about 20–90 seconds. Press Screen SGX to try it, or Not now to read on.",
        waitText: "Screening SGX… The tour carries on when the list arrives." },
    ],
  },
  {
    id: "list", name: "The list", blurb: "Results cards, release dates and what is set aside.",
    steps: [
      { id: "card", target: "card", title: "One card per results filing",
        body: "The band shows the stock code and when it was filed. The title opens the announcement on SGX, and the PDFs underneath are its attachments." },
      { id: "releases", target: "releases", title: "Who reports next",
        body: "Notices of a results date are read for the date, the period and the timing (such as “after the close of trading”), without AI, and listed in date order." },
      { id: "others", target: "others", title: "Everything else, set aside",
        body: "Press releases, presentations, notices and the separate (company-only) copy of a duplicate filing land here. They aren’t extracted unless you open one and choose to. Issuers with only bonds or notes on SGX are left out altogether." },
    ],
  },
  {
    id: "extract", name: "Extract", blurb: "Turn one filing into a checked workbook.",
    steps: [
      { id: "extract", target: "extract", waitFor: "extracted", cta: "Not now", title: "Extract one filing",
        body: "A keyword scan picks the PDF and the three statement pages without AI. Then {ai} reads only those pages, Group figures only. Press Extract to try it (about a minute), or Not now to read on.",
        waitText: "Extracting… The tour carries on when the workbook is ready." },
      { id: "verdict", target: "verdict", title: "Checked, not just read",
        body: "Parts must add up to the printed totals: PBT + tax = PAT, assets = liabilities + equity, opening cash + net change = closing cash, and more. Figures count as verified only when a check passes and none fails." },
      { id: "details", target: "details", tapAdvances: true, title: "Open the details",
        body: "Tap the company name to see which PDF and pages were read, every check with its difference, and the tokens used. If the scan picked the wrong pages, enter the right ones there and extract again." },
      { id: "excel", target: "excel", title: "Download the workbook",
        body: "Sheets for the income statement, balance sheet and cash flow, plus Checks and a Source Map back to the company’s own labels. The workbook is built in memory: nothing is stored on the server." },
      { id: "recheck", target: "recheck", title: "Not verified? Ask Claude",
        body: "Check with Claude opens a Claude chat that reads the filing itself through the SGX Financials connector and replies with a new Excel link. Add the connector first (Make it yours, below)." },
    ],
  },
  {
    id: "bulk", name: "Many at once", blurb: "A download folder, Extract all and Download all.",
    steps: [
      { id: "folder", target: "folder", title: "Choose a download folder",
        body: "In Chrome and Edge, workbooks are written straight into a folder you pick: one file per company, and each new set of results is merged into the file already there." },
      { id: "extract-all", target: "extract-all", title: "Extract all",
        body: "Runs every results filing on the list, two at a time. Stop it whenever you like: finished workbooks are kept." },
      { id: "download-all", target: "download-all", title: "Download all",
        body: "Saves every finished workbook into your folder, or as one zip when there’s no folder." },
    ],
  },
  {
    id: "yours", name: "Make it yours", blurb: "Your own layout, the Claude connector and settings.",
    steps: [
      { id: "standards", target: "standards", title: "Your own layout",
        body: "Upload results you already keep in Excel. The lines, order, unit, period order and signs are learnt from it, and that company’s next filings come out the same way." },
      { id: "claude", target: "claude", title: "Add to Claude for the hard filings",
        body: "{ai} handles most filings well. Some are harder: statements that are scanned or photographed pages, or figures that fail the checks. Those need a stronger AI, so this adds SGX Financials to Claude as a connector, and Claude reads those pages itself. When an extraction finds the statements are photos, a prompt pops up offering Claude ({claude}) instead of {ai}. With no answer in 30 seconds, {ai} carries on." },
      { id: "settings", target: "settings", title: "Settings",
        body: "Speed against care (Faster, Balanced, Thorough), the app password if the server has one, and rules for how cost of goods sold is made up in your industries." },
      { id: "help", target: "help", title: "See this again",
        body: "Tap ? any time to replay the tour or jump to one chapter." },
    ],
  },
];

/** Every step in order, each knowing its chapter. */
export const STEPS = CHAPTERS.flatMap((c) => c.steps.map((s) => ({ ...s, chapterId: c.id, chapter: c.name })));

// ------------------------------------------------------------------ saved on this browser
const KEY = "sgxf-tour";

/** @returns {{ seen: boolean, off: boolean }} */
export function readSaved(storage) {
  try {
    const raw = JSON.parse(storage.getItem(KEY) ?? "null");
    return { seen: raw?.seen === true, off: raw?.off === true };
  } catch {
    return { seen: false, off: false };
  }
}

export function writeSaved(saved, storage) {
  try { storage.setItem(KEY, JSON.stringify({ seen: !!saved.seen, off: !!saved.off })); } catch { /* private mode */ }
}

/** The tour starts on its own once, on a first visit. The ? button always shows it. */
export const shouldAutoStart = (saved) => !saved.seen && !saved.off;

// ------------------------------------------------------------------ the running tour: { i, waiting }
/** Start at a chapter (or the beginning). */
export function startAt(chapterId) {
  const i = chapterId ? STEPS.findIndex((s) => s.chapterId === chapterId) : 0;
  return { i: Math.max(0, i), waiting: false };
}

/** Next, or null when the tour is over. */
export const next = (tour) => (tour.i >= STEPS.length - 1 ? null : { i: tour.i + 1, waiting: false });

export const back = (tour) => ({ i: Math.max(0, tour.i - 1), waiting: false });

/** Someone used the highlighted control of a step that waits: hide the card until the signal. */
export const park = (tour) => (STEPS[tour.i]?.waitFor ? { ...tour, waiting: true } : tour);

/** A signal from the app, e.g. "screened". Moves on only from the step that waits for it. */
export function signal(tour, name) {
  if (STEPS[tour.i]?.waitFor !== name) return tour;
  return next(tour);
}

/**
 * The current step's control isn't on the page (no results yet, a browser without folders…).
 * Jump to the next step whose control is there, or that needs none. Null: nothing left to show.
 * @param {(step: Step) => boolean} present
 */
export function skipMissing(tour, present) {
  for (let k = tour.i + 1; k < STEPS.length; k++) {
    if (!STEPS[k].target || present(STEPS[k])) return { i: k, waiting: false };
  }
  return null;
}

/** "The list · 2 of 3" */
export function chapterOf(i) {
  const step = STEPS[i];
  if (!step) return { name: "", n: 0, of: 0 };
  const inChapter = STEPS.filter((s) => s.chapterId === step.chapterId);
  return { name: step.chapter, n: inChapter.findIndex((s) => s.id === step.id) + 1, of: inChapter.length };
}

/** Step copy with the app's AI name filled in. */
export const CLAUDE_MODEL = "Opus 5.5"; // keep in step with CLAUDE_MODEL in app.js
export const fill = (text, ai) => String(text).replaceAll("{ai}", ai || "the AI").replaceAll("{claude}", CLAUDE_MODEL);
