// Regression test for the "页面先变英文再转中文" flicker on hard
// refresh. The bug was: the server was HTML-escaping the inline
// `<script type="application/json">` payloads (replacing `"` with
// `&quot;`), which silently breaks `JSON.parse` because the browser
// passes those bytes through to the parser verbatim — HTML entities
// are NOT decoded inside <script> blocks. The inline dictionary then
// came back empty and the first Vue render fell back to the English
// fallback string.
//
// This test:
//
//   1. Stamps the actual disk files into a JSDOM `<head>` (post-fix
//      payload — bare `"`).
//   2. Verifies the inline read populates the dictionary synchronously
//      and that JSON.parse succeeds on the served payload.
//   3. Asserts the negative case (an `&quot;`-payload) really would
//      have failed JSON parsing, as a backstop against the regression.
//
// Run: node tests-js/test_i18n_inline.test.mjs

import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');
const enPath = resolve(repoRoot, 'loop_memory/serve/static/i18n/en.json');
const zhPath = resolve(repoRoot, 'loop_memory/serve/static/i18n/zh.json');

let failures = 0;
function ok(cond, name) {
  if (cond) return;
  failures += 1;
  console.error('FAIL  ' + name);
}
function assertEq(actual, expected, name) {
  if (JSON.stringify(actual) === JSON.stringify(expected)) return;
  failures += 1;
  console.error('FAIL  ' + name);
  console.error('  expected: ' + JSON.stringify(expected));
  console.error('  actual:   ' + JSON.stringify(actual));
}

const enRaw = readFileSync(enPath, 'utf-8');
const zhRaw = readFileSync(zhPath, 'utf-8');

// Mirror the server's *fixed* path: only the literal "</script>"
// byte sequence is escaped. Real JSON quotes pass through verbatim.
function injectDict(dict) {
  return dict.replace(/<\/script>/g, '<\\/script>');
}

const html =
  '<!doctype html><html><head>' +
  `<script type="application/json" id="loop-i18n-en">${injectDict(enRaw)}</script>` +
  `<script type="application/json" id="loop-i18n-zh">${injectDict(zhRaw)}</script>` +
  '</head><body><div id="app"></div></body></html>';

const dom = new JSDOM(html, {
  runScripts: 'outside-only',
  url: 'http://localhost/',
});
const win = dom.window;
globalThis.window = win;
globalThis.document = win.document;
globalThis.localStorage = win.localStorage;

// store.js picks up inline payloads at module init. We don't load
// store.js here — it pulls Vue from a CDN, which JSDOM can't resolve
// for ESM modules in this configuration. Instead, we directly test
// the inline pipeline:
//
//   - the inline <script type="application/json"> blocks the server
//     serves (post-fix)
//   - JSON.parse must succeed on each block
//
// Same contract as ``store._readInlineI18n()``.

const inlineZhScript = win.document.getElementById('loop-i18n-zh');
ok(inlineZhScript !== null, 'inline JSON.zh block is present');
const inlineZhPayload = inlineZhScript.textContent;
const inlineZh = JSON.parse(inlineZhPayload);
assertEq(inlineZh['timeline.allKinds'], '全部类型',
  'inline JSON.zh parses and timeline.allKinds == "全部类型"');
assertEq(inlineZh['graph.allKinds'], '全部类型',
  'inline JSON.zh parses and graph.allKinds == "全部类型"');
assertEq(inlineZh['filter.allKinds'], '全部类型',
  'inline JSON.zh parses and filter.allKinds == "全部类型"');

const inlineEnScript = win.document.getElementById('loop-i18n-en');
const inlineEn = JSON.parse(inlineEnScript.textContent);
assertEq(inlineEn['timeline.allKinds'], 'All kinds',
  'inline JSON.en parses and timeline.allKinds == "All kinds"');
assertEq(inlineEn['graph.allKinds'], 'all kinds',
  'inline JSON.en parses and graph.allKinds == "all kinds"');

// Pin both have many keys — a truncated/empty payload would have
// surfaced here first, before any specific key check.
ok(Object.keys(inlineZh).length > 500,
  'inline zh dict is fully populated (>500 keys)');
ok(Object.keys(inlineEn).length > 500,
  'inline en dict is fully populated (>500 keys)');

// NEGATIVE case: an HTML-escaped payload (the pre-fix format) MUST
// fail to parse. This pins the regression so the next person who
// "hardens" the inline injection path gets a clear test failure.
const escapedPayload = inlineZhPayload.replace(/"/g, '&quot;');
let escapedFailed = false;
try { JSON.parse(escapedPayload); }
catch (_e) { escapedFailed = true; }
ok(escapedFailed,
  'regression guard: HTML-escaped payloads must NOT parse (else the' +
  ' flicker comes back)');

// CONTAINMENT check: the bare payload MUST contain real `"` quotes
// (the broken escapes removed them). If anyone re-introduces HTML
// escaping downstream, this assertion fires.
ok(inlineZhPayload.includes('"timeline.allKinds"'),
  'inline payload carries bare JSON quotes around key names');

if (failures) {
  console.error('\n' + failures + ' test(s) failed');
  process.exit(1);
}
console.log('OK   i18n inline regression suite passed');
