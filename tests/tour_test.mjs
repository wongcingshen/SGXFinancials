// node --test tests/tour_test.mjs — the guided tour's steps and state (public/tour-core.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  CHAPTERS, CLAUDE_MODEL, STEPS, back, chapterOf, fill, next, park, readSaved, shouldAutoStart, signal, skipMissing, startAt, writeSaved,
} from "../public/tour-core.js";

const memory = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) };
};
const at = (id) => STEPS.findIndex((s) => s.id === id);

test("step ids are unique and every step has copy", () => {
  assert.equal(new Set(STEPS.map((s) => s.id)).size, STEPS.length);
  for (const s of STEPS) assert.ok(s.title && s.body, s.id);
});

test("every target is a data-tour attribute in the page or the cards app.js draws", () => {
  const html = readFileSync(new URL("../public/index.html", import.meta.url), "utf8")
    + readFileSync(new URL("../public/app.js", import.meta.url), "utf8");
  for (const s of STEPS.filter((x) => x.target)) assert.match(html, new RegExp(`data-tour="${s.target}"`), s.id);
});

test("signals the tour waits for are sent by app.js", () => {
  const app = readFileSync(new URL("../public/app.js", import.meta.url), "utf8");
  for (const s of STEPS.filter((x) => x.waitFor)) {
    assert.ok(app.includes(`tourSignal("${s.waitFor}")`), s.id);
    assert.ok(s.waitText, `${s.id} says what it waits for`);
  }
});

test("starts once on a first visit, and remembers being seen", () => {
  const st = memory();
  assert.ok(shouldAutoStart(readSaved(st)));
  writeSaved({ seen: true, off: false }, st);
  assert.equal(shouldAutoStart(readSaved(st)), false);
});

test("bad or missing storage reads as a first visit", () => {
  const st = memory();
  st.setItem("sgxf-tour", "{not json");
  assert.deepEqual(readSaved(st), { seen: false, off: false });
  assert.deepEqual(readSaved(null), { seen: false, off: false });
  assert.doesNotThrow(() => writeSaved({ seen: true }, null));
});

test("startAt jumps to a chapter's first step", () => {
  assert.deepEqual(startAt(), { i: 0, waiting: false });
  assert.equal(STEPS[startAt("extract").i].id, "extract");
  assert.equal(startAt("nope").i, 0);
});

test("next, back and the end", () => {
  assert.deepEqual(next({ i: 0, waiting: false }), { i: 1, waiting: false });
  assert.equal(next({ i: STEPS.length - 1, waiting: false }), null);
  assert.deepEqual(back({ i: 0, waiting: false }), { i: 0, waiting: false });
  assert.deepEqual(back({ i: 3, waiting: true }), { i: 2, waiting: false });
});

test("a waiting step parks, and only its own signal moves it on", () => {
  const t = { i: at("screen"), waiting: false };
  const parked = park(t);
  assert.equal(parked.waiting, true);
  assert.equal(signal(parked, "extracted"), parked);
  assert.deepEqual(signal(parked, "screened"), { i: at("screen") + 1, waiting: false });
  // A step that waits for nothing doesn't park.
  const plain = { i: at("dates"), waiting: false };
  assert.equal(park(plain), plain);
  assert.equal(signal(plain, "screened"), plain);
});

test("missing controls are skipped to the next one on the page", () => {
  const onPage = new Set(["folder", "standards"]);
  const t = skipMissing({ i: at("card"), waiting: false }, (s) => onPage.has(s.target));
  assert.equal(STEPS[t.i].id, "folder");
  assert.equal(skipMissing({ i: STEPS.length - 2, waiting: false }, () => false), null);
});

test("chapter label counts within the chapter", () => {
  const c = chapterOf(at("company"));
  assert.equal(c.name, "Find results");
  assert.equal(c.n, 2);
  assert.equal(c.of, CHAPTERS.find((x) => x.id === "screen").steps.length);
});

test("the AI's name is filled in", () => {
  assert.equal(fill("{ai} reads it", "Gemini"), "Gemini reads it");
  assert.equal(fill("{ai} reads it", ""), "the AI reads it");
});

test("the Claude model named in the tour matches the app's", () => {
  const app = readFileSync(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(app, new RegExp(`const CLAUDE_MODEL = "${CLAUDE_MODEL}"`));
  assert.equal(fill("Claude ({claude})", "Gemini"), `Claude (${CLAUDE_MODEL})`);
});
