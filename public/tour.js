// The guided tour's overlay: a sun-ringed spotlight on the real control, a scrim with a hole in it,
// and a tip card beside it. Clicking the control through the hole does the real thing.
// Steps and state are in tour-core.js; app.js sends signals ("screened", "extracted") as a
// `sgxf:tour` event on document.
import {
  CHAPTERS, STEPS, back, chapterOf, fill, next, park, readSaved, shouldAutoStart, signal, skipMissing, startAt, writeSaved,
} from "./tour-core.js";

const PAD = 6;
const GAP = 14;
const MAX_W = 400;
const SKIP_AFTER_MS = 900;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const svg = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const aiName = () => document.querySelector("[data-ai]")?.textContent?.trim() || "the AI";
// localStorage can throw (blocked site data); the tour then just doesn't remember being seen.
const storage = (() => { try { return window.localStorage; } catch { return null; } })();
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

let tour = null; // { i, waiting } while the tour runs
let shown = -1; // the step whose card is rendered
let missingSince = 0;
let scrolls = { id: "", tries: 0, last: -Infinity };
let frame = 0;

// ------------------------------------------------------------------ elements
const root = document.createElement("div");
root.className = "tour";
root.hidden = true;
root.innerHTML = `<div class="tour-block" aria-hidden="true"></div><div class="tour-ring" aria-hidden="true"></div>
  <section class="tour-card" role="dialog" aria-modal="false" aria-labelledby="tourTitle" aria-describedby="tourBody"></section>`;
const block = root.querySelector(".tour-block");
const ring = root.querySelector(".tour-ring");
const card = root.querySelector(".tour-card");

const wait = document.createElement("div");
wait.className = "tour-wait";
wait.setAttribute("role", "status");
wait.hidden = true;
document.body.append(root, wait);

// ------------------------------------------------------------------ finding the control
function visibleAll(target) {
  return [...document.querySelectorAll(`[data-tour="${target}"]`)].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
}
const present = (step) => visibleAll(step.target).length > 0;

function boxOf(step) {
  const els = visibleAll(step.target);
  if (!els.length) return null;
  const rects = (step.all ? els : els.slice(0, 1)).map((el) => el.getBoundingClientRect());
  const x = Math.min(...rects.map((r) => r.left)), y = Math.min(...rects.map((r) => r.top));
  const w = Math.max(...rects.map((r) => r.right)) - x, h = Math.max(...rects.map((r) => r.bottom)) - y;
  return { x, y, w, h, el: els[0] };
}

/** Bring the control into the upper third, leaving room for the card below it. */
function bringIntoView(step, box) {
  if (scrolls.id !== step.id) scrolls = { id: step.id, tries: 0, last: -Infinity };
  const now = performance.now();
  if (scrolls.tries >= 3 || now - scrolls.last < 700) return;
  const vh = window.innerHeight;
  const tall = box.h > vh * 0.4; // the card goes over its lower end: keep its top near the top
  if (tall ? box.y >= 56 && box.y <= 120 : box.y >= 72 && box.y + box.h <= vh - 220) return;
  scrolls.tries += 1;
  scrolls.last = now;
  window.scrollBy({ top: box.y - (tall ? 80 : vh * 0.3), behavior: reduceMotion() ? "auto" : "smooth" });
}

/** On a phone the keyboard shifts the page; the card steps aside while someone types. */
function typing() {
  if (!matchMedia("(pointer: coarse)").matches) return false;
  const el = document.activeElement;
  if (!el || el.closest(".tour-card, .tour-wait")) return false;
  if (el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) return true;
  if (el instanceof HTMLInputElement) return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes(el.type);
  return el instanceof HTMLElement && el.isContentEditable;
}

/** A sheet or the photo prompt is up: it has the floor, and the tour waits behind it. */
const paused = () => Boolean(document.querySelector("dialog[open], .ask-pop"));

// ------------------------------------------------------------------ drawing
function renderCard() {
  const step = STEPS[tour.i];
  const ch = chapterOf(tour.i);
  const last = tour.i >= STEPS.length - 1;
  const ai = aiName();
  card.innerHTML = `
    <div class="tour-top">
      <div class="tour-heading">
        <p class="tour-eyebrow">${esc(ch.name)} · ${ch.n} of ${ch.of}</p>
        <h2 id="tourTitle">${esc(fill(step.title, ai))}</h2>
      </div>
      <button type="button" class="icon-btn tour-x" data-tour-act="close" aria-label="Close the tour">${svg("x")}</button>
    </div>
    <p class="tour-body" id="tourBody">${esc(fill(step.body, ai))}</p>
    <div class="tour-dots" aria-hidden="true">${CHAPTERS.map((c) => `<span class="${
      c.id === step.chapterId ? "on" : STEPS.findIndex((s) => s.chapterId === c.id) < tour.i ? "past" : ""}"></span>`).join("")}</div>
    <div class="tour-actions">
      <span class="tour-count">${tour.i + 1} / ${STEPS.length}</span>
      ${tour.i > 0 ? `<button type="button" class="btn btn-ghost" data-tour-act="back">Back</button>` : ""}
      <button type="button" class="btn btn-ember" data-tour-act="next">${esc(last ? "Done" : step.cta || "Next")}</button>
    </div>
    <span class="tour-arrow" aria-hidden="true"></span>`;
  shown = tour.i;
}

function renderWait() {
  const step = tour && STEPS[tour.i];
  wait.hidden = !(tour?.waiting);
  if (wait.hidden) return;
  wait.innerHTML = `<span class="spin" aria-hidden="true"></span><span class="tour-wait-text">${esc(step.waitText || "Waiting…")}</span>
    <button type="button" class="link-btn" data-tour-act="skip">Skip</button>
    <button type="button" class="icon-btn" data-tour-act="close" aria-label="Close the tour">${svg("x")}</button>`;
}

function hideLayer() {
  root.hidden = true;
}

function draw() {
  frame = requestAnimationFrame(draw);
  if (!tour) return hideLayer();
  if (tour.waiting || paused() || typing()) {
    missingSince = 0;
    return hideLayer();
  }
  const step = STEPS[tour.i];
  const fresh = shown !== tour.i;
  if (fresh) renderCard();

  if (!step.target) {
    root.hidden = false;
    root.classList.add("centred");
    block.style.clipPath = "";
    card.removeAttribute("style");
    delete card.dataset.side;
    if (fresh) focusPrimary();
    return;
  }
  root.classList.remove("centred");
  const box = boxOf(step);
  if (!box) {
    // Not on the page (no results yet, no folder support…): skip to the next control that is.
    hideLayer();
    missingSince ||= performance.now();
    if (performance.now() - missingSince > SKIP_AFTER_MS) go(skipMissing(tour, present));
    return;
  }
  missingSince = 0;
  bringIntoView(step, box);

  const vw = document.documentElement.clientWidth, vh = window.innerHeight;
  const s = { x: box.x - PAD, y: box.y - PAD, w: box.w + PAD * 2, h: box.h + PAD * 2 };
  Object.assign(ring.style, { left: `${s.x}px`, top: `${s.y}px`, width: `${s.w}px`, height: `${s.h}px`, borderRadius: `${Math.min(20, s.h / 2)}px` });
  block.style.clipPath = `path(evenodd, "M0 0H${vw}V${vh}H0Z M${s.x} ${s.y}h${s.w}v${s.h}h-${s.w}Z")`;

  root.hidden = false;
  const w = Math.min(MAX_W, vw - 32);
  const ch = card.offsetHeight;
  const cx = box.x + box.w / 2;
  const left = Math.max(16, Math.min(vw - w - 16, cx - w / 2));
  const roomBelow = vh - (s.y + s.h) - GAP, roomAbove = s.y - GAP;
  // A control too tall to have the card beside it (a whole filing card on a phone): the card
  // sits at the bottom of the screen, over the control's lower end, and leaves its top in view.
  const fitsBelow = roomBelow >= ch + 12, fitsAbove = roomAbove >= ch + 12;
  const below = fitsBelow || !fitsAbove;
  const wanted = fitsBelow || fitsAbove ? (below ? s.y + s.h + GAP : s.y - GAP - ch) : below ? vh - ch - 12 : 12;
  const top = Math.max(12, Math.min(vh - ch - 12, wanted));
  Object.assign(card.style, { left: `${left}px`, top: `${top}px`, width: `${w}px` });
  card.dataset.side = (fitsBelow || fitsAbove) && top === wanted ? (below ? "below" : "above") : "none";
  card.style.setProperty("--arrow-x", `${Math.max(18, Math.min(w - 34, cx - left - 8))}px`);
  if (fresh) focusPrimary();
}

function focusPrimary() {
  if (document.activeElement?.matches?.("input, textarea, select")) return;
  card.querySelector('[data-tour-act="next"]')?.focus({ preventScroll: true });
}

// ------------------------------------------------------------------ moving through
function go(nextTour) {
  if (!nextTour) return end("done");
  tour = nextTour;
  missingSince = 0;
  shown = -1;
  renderWait();
}

export function start(chapterId) {
  go(startAt(chapterId));
  if (!frame) frame = requestAnimationFrame(draw);
}

function end(how) {
  tour = null;
  shown = -1;
  hideLayer();
  renderWait();
  cancelAnimationFrame(frame);
  frame = 0;
  writeSaved({ ...readSaved(storage), seen: true }, storage);
  if (typeof window.toast === "function")
    window.toast(how === "done" ? "That’s the tour. Tap ? in the header to see it again." : "Tour closed. Tap ? in the header to pick it up again.");
}

function act(name) {
  if (!tour) return;
  if (name === "close") return end("closed");
  if (name === "back") return go(back(tour));
  if (name === "next" || name === "skip") return go(next(tour));
}

root.addEventListener("click", (e) => {
  const b = e.target.closest("[data-tour-act]");
  if (b) act(b.dataset.tourAct);
});
wait.addEventListener("click", (e) => {
  const b = e.target.closest("[data-tour-act]");
  if (b) act(b.dataset.tourAct);
});

// Using the highlighted control itself: a waiting step parks, others may move on.
document.addEventListener("click", (e) => {
  if (!tour || tour.waiting || root.hidden) return;
  const step = STEPS[tour.i];
  if (!step.target || !e.target.closest?.(`[data-tour="${step.target}"]`)) return;
  if (step.waitFor) {
    tour = park(tour);
    renderWait();
  } else if (step.tapAdvances) {
    go(next(tour));
  }
}, true);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && tour && !paused()) end("closed");
});

document.addEventListener("sgxf:tour", (e) => {
  if (!tour) return;
  const moved = signal(tour, e.detail);
  if (moved !== tour) go(moved);
});

// ------------------------------------------------------------------ the ? button: replay or jump to a chapter
const sheet = document.getElementById("tourSheet");
const list = document.getElementById("tourChapters");
if (list) {
  list.innerHTML = CHAPTERS.map((c, k) => `<button type="button" class="tour-chapter" data-tour-chapter="${c.id}">
    <span class="badge">${k + 1}</span><span><b>${esc(c.name)}</b><small>${esc(c.blurb)}</small></span>
    <span class="tour-chapter-n">${c.steps.length} step${c.steps.length === 1 ? "" : "s"}</span></button>`).join("");
}
document.addEventListener("click", (e) => {
  const open = e.target.closest?.("#openTour");
  if (open && sheet) return sheet.showModal();
  const ch = e.target.closest?.("[data-tour-chapter], [data-tour-start]");
  if (!ch) return;
  sheet?.open && sheet.close();
  start(ch.dataset.tourChapter || undefined);
});

if (shouldAutoStart(readSaved(storage))) setTimeout(() => start(), 500);
