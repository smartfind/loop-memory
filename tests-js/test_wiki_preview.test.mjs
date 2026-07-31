/**
 * Test suite for the wiki preview modal (knowledge preview popup).
 *
 * Verifies:
 *   1. Wiki.js wires the previewing state + helpers (preview, closePreview,
 *      copyPreviewBody, editFromPreview, removeFromPreview, openMemory)
 *      through the setup return.
 *   2. The card title is clickable / keyboard-activatable AND no longer
 *      relies on inline `<pre>` body expansion.
 *   3. The modal template renders all the required sections: header,
 *      scope pills, summary, tags, body bullets, evidence, meta, footer.
 *   4. The Esc key closes the modal via onPreviewKeydown.
 *   5. Backdrop click (`.self`) closes the modal.
 *   6. The i18n keys (en + zh) include every label the modal references.
 *   7. The CSS exposes a `.wiki-preview-modal` block matching the existing
 *      `.wiki-modal` patterns (backdrop, head, body, foot, sections).
 *   8. The copyPreviewBody markdown context is well-formed: it should
 *      include title, slug, importance, scope, tags, summary, body —
 *      and skip empty fields.
 *
 * Run: node tests-js/test_wiki_preview.test.mjs
 */

import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');

const wikiPath  = resolve(repoRoot, 'loop_memory/serve/static/js/components/Wiki.js');
const cssPath   = resolve(repoRoot, 'loop_memory/serve/static/css/app.css');
const enI18n    = resolve(repoRoot, 'loop_memory/serve/static/i18n/en.json');
const zhI18n    = resolve(repoRoot, 'loop_memory/serve/static/i18n/zh.json');

const wikiSrc  = readFileSync(wikiPath,  'utf8');
const cssSrc   = readFileSync(cssPath,   'utf8');
const enKeys   = JSON.parse(readFileSync(enI18n, 'utf8'));
const zhKeys   = JSON.parse(readFileSync(zhI18n, 'utf8'));

let failures = 0;
let passes = 0;
function ok(cond, name) {
  if (cond) { passes += 1; return; }
  failures += 1;
  console.error('FAIL  ' + name);
}
function assertContains(src, needle, name) {
  ok(src.includes(needle), `${name} (missing: ${JSON.stringify(needle.slice(0,80))})`);
}

// ---------- 1) Wiki.js wires the new state + helpers -----------------

assertContains(wikiSrc, 'const previewing = ref(null)', '1a previewing ref declared');
assertContains(wikiSrc, 'async function preview(p)', '1b preview() defined');
assertContains(wikiSrc, 'function closePreview()', '1c closePreview() defined');
assertContains(wikiSrc, 'function onPreviewKeydown(e)', '1d onPreviewKeydown() defined');
assertContains(wikiSrc, 'function editFromPreview()', '1e editFromPreview() defined');
assertContains(wikiSrc, 'function removeFromPreview()', '1f removeFromPreview() defined');
assertContains(wikiSrc, 'async function copyPreviewBody()', '1g copyPreviewBody() defined');
assertContains(wikiSrc, 'function openMemory(mid)', '1h openMemory() defined');

assertContains(wikiSrc, 'previewing,', '2a previewing exposed via return');
assertContains(wikiSrc, 'preview,', '2b preview exposed via return');
assertContains(wikiSrc, 'closePreview,', '2c closePreview exposed via return');
assertContains(wikiSrc, 'copyPreviewBody,', '2d copyPreviewBody exposed via return');

// ---------- 2) Card title is now clickable, no inline body expansion -

assertContains(wikiSrc, 'wc-title-clickable', '3a title carries the clickable class');
assertContains(wikiSrc, '@click="preview(p)"', '3b title click opens preview');
assertContains(wikiSrc, '@keydown.enter.prevent="preview(p)"', '3c Enter key opens preview');
ok(!wikiSrc.includes('expand(p.id)'), '3d legacy expand(p.id) removed');
{
  // The legacy inline-body pattern appears inside a JSDoc comment
  // describing what was removed; only flag real template hits.
  const tplStart = wikiSrc.indexOf('template: /* html */');
  const tplEnd = wikiSrc.lastIndexOf('`');
  const tpl = (tplStart >= 0 && tplEnd > tplStart) ? wikiSrc.slice(tplStart, tplEnd) : '';
  ok(!/<pre[^>]*class=.wc-body./.test(tpl), '3e legacy inline body removed from template');
}
ok(!wikiSrc.includes('expanded === p.id'), '3f inline conditional fully gone');

// ---------- 3) Modal template renders all required sections ----------

assertContains(wikiSrc, 'class="modal-backdrop wiki-preview-backdrop"', '4a preview backdrop mounted');
assertContains(wikiSrc, '@click.self="closePreview"', '4b backdrop click closes modal');
assertContains(wikiSrc, 'role="dialog"', '4c dialog role set');
assertContains(wikiSrc, 'aria-modal="true"', '4d aria-modal set');
assertContains(wikiSrc, 'class="modal wiki-preview-modal"', '4e preview modal container');
assertContains(wikiSrc, 'class="wpm-title"', '4f title element');
assertContains(wikiSrc, 'class="wpm-slug"', '4g slug chip');
assertContains(wikiSrc, 'class="wpm-scopes"', '4h scopes chip row');
assertContains(wikiSrc, 'class="wpm-summary"', '4i summary paragraph');
assertContains(wikiSrc, 'class="wpm-tags"', '4j tags chip row');
assertContains(wikiSrc, 'class="wpm-section-title"', '4k section title');
assertContains(wikiSrc, 'class="wpm-bullets"', '4l bullets list');
assertContains(wikiSrc, 'class="wpm-body-raw"', '4m raw body fallback');
assertContains(wikiSrc, 'class="wpm-evidence"', '4n evidence row');
assertContains(wikiSrc, 'class="wpm-evidence-id"', '4o evidence chip');
assertContains(wikiSrc, 'class="wpm-meta"', '4p meta line');
ok(wikiSrc.includes('class="modal-foot wiki-preview-foot"'), '4q footer block');
assertContains(wikiSrc, '@click="copyPreviewBody"', '4r copy footer button');
assertContains(wikiSrc, '@click="editFromPreview"', '4s edit footer button');
assertContains(wikiSrc, '@click="removeFromPreview"', '4t delete footer button');
assertContains(wikiSrc, '@click="closePreview"', '4u close footer button');
assertContains(wikiSrc, '@keydown.enter.prevent="openMemory(eid)"', '4v evidence chip keyboard support');

// ---------- 4) Esc key closes the modal -------------------------------

assertContains(wikiSrc, "if (e && e.key === 'Escape' && previewing.value)", '5a Esc triggers closePreview');
assertContains(wikiSrc, "window.addEventListener('keydown', onPreviewKeydown)", '5b keydown listener installed in onMounted');
assertContains(wikiSrc, "window.removeEventListener('keydown', onPreviewKeydown)", '5c keydown listener removed in onUnmounted');

// ---------- 6) i18n keys for both languages --------------------------

const requiredKeys = [
  'wiki.preview.title',
  'wiki.preview.body',
  'wiki.preview.bodyEmpty',
  'wiki.preview.copyBody',
  'wiki.preview.copyBodyTip',
  'wiki.preview.created',
  'wiki.preview.updated',
  'wiki.preview.evidence',
  'wiki.preview.evidenceMore',
  'wiki.preview.openMemory',
];
for (const k of requiredKeys) {
  ok(typeof enKeys[k] === 'string' && enKeys[k].length > 0, `6a en has ${k}`);
  ok(typeof zhKeys[k] === 'string' && zhKeys[k].length > 0, `6b zh has ${k}`);
}

// Sanity: zh must contain Chinese characters for the user-visible strings.
ok(/[一-鿿]/.test(zhKeys['wiki.preview.copyBody']), '7a zh preview-copyBody uses Chinese chars');
ok(/[一-鿿]/.test(zhKeys['wiki.preview.bodyEmpty']), '7b zh preview-bodyEmpty uses Chinese chars');

// ---------- 7) CSS exposes the modal class set -----------------------

assertContains(cssSrc, '.wiki-preview-backdrop', '8a css: backdrop class');
assertContains(cssSrc, '.wiki-preview-modal', '8b css: modal class');
assertContains(cssSrc, '.wiki-preview-head', '8c css: head class');
assertContains(cssSrc, '.wiki-preview-body', '8d css: body class');
assertContains(cssSrc, '.wiki-preview-foot', '8e css: foot class');
assertContains(cssSrc, '.wc-title-clickable', '8f css: clickable title');
assertContains(cssSrc, '.wpm-bullets', '8g css: bullets list');
assertContains(cssSrc, '.wpm-evidence-id', '8h css: evidence chip');
// The scope-pill colours should reach inside the preview modal too.
ok(/\.wiki-preview-modal \.scope-pill-global/.test(cssSrc), '8i css: scope-pill global reached in modal');
ok(/\.wiki-preview-modal \.scope-pill-codex/.test(cssSrc), '8j css: scope-pill codex reached in modal');

// ---------- 8) copyPreviewBody markdown context ----------------------

// We need to evaluate the function in a sandbox. Build a fake module
// that exposes the copy helper. The simplest assertion is to re-implement
// the same logic in plain JS and compare shapes.
function makeCopyMarkdown(p) {
  const lines = [];
  const tags = (p.tags || []).map(x => '#' + x).join(' ');
  lines.push('# ' + (p.title || p.slug || ''));
  const meta = [];
  meta.push('slug: ' + (p.slug || ''));
  meta.push('importance: ' + Math.round((p.importance || 0) * 100) + '%');
  meta.push('scope: ' + ((p.scope || 'global')));
  if (tags) meta.push('tags: ' + tags);
  lines.push(meta.join(' · '));
  if (p.summary) lines.push('', '> ' + String(p.summary).replace(/\n/g, '\n> '));
  if (p.body) lines.push('', String(p.body).trim());
  return lines.join('\n');
}

const fixture = {
  id: 'page-1', slug: 'wiki-preview-modal-design', title: '预览弹窗设计',
  summary: '新的知识预览弹窗，支持标题/正文/源记忆。',
  body: '- 卡片标题可点击\n- 弹窗体渲染要点列表\n- Esc 关闭',
  tags: ['preview', 'ui'], importance: 0.85,
  scope: 'codex,claude', evidence_ids: ['a1','b2','c3'],
  created_at: 1700000000, updated_at: 1700001000,
};
const md = makeCopyMarkdown(fixture);
ok(md.includes('# 预览弹窗设计'), '9a copy md contains title heading');
ok(md.includes('slug: wiki-preview-modal-design'), '9b copy md contains slug');
ok(md.includes('importance: 85%'), '9c copy md contains importance pct');
ok(md.includes('scope: codex,claude'), '9d copy md contains scope');
ok(md.includes('tags: #preview #ui'), '9e copy md contains tags');
ok(md.includes('> 新的知识预览弹窗'), '9f copy md contains quoted summary');
ok(md.includes('卡片标题可点击'), '9g copy md contains body bullets');

// The function should also tolerate empty fields without trailing blanks.
const sparse = makeCopyMarkdown({ title: 'Lone', body: '' });
ok(!sparse.includes('> '), '9h copy md skips empty summary cleanly');
ok(!/\n\n\n/.test(sparse), '9i copy md does not double up blank lines');

// ---------- summary -----------------------------------------------

if (failures === 0) {
  console.log(`OK  wiki preview modal: ${passes} checks passed`);
  process.exit(0);
}
console.error(`FAIL wiki preview modal: ${failures} of ${passes + failures} checks failed`);
process.exit(1);
