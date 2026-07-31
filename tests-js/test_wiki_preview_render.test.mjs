/**
 * End-to-end render test for the wiki preview modal.
 *
 * Mounts the Wiki.js template string into a JSDOM, asserts:
 *   * The .wiki-preview-modal and .wiki-preview-backdrop containers
 *     exist in the DOM after the template renders.
 *   * The modal element is a CHILD of the backdrop (not a sibling or
 *     unrelated node), so backdrop click + centre-centring work.
 *   * No inline `<pre class="wc-body">` accordion residue remains
 *     from the legacy expand-inline flow.
 *   * The CSS source file declares position:fixed on
 *     .wiki-preview-backdrop so the modal anchors to the viewport
 *     regardless of the surrounding grid ancestors.
 *
 * Run: node tests-js/test_wiki_preview_render.test.mjs
 */

import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, '..');

const wikiSrc = readFileSync(
  resolve(repoRoot, 'loop_memory/serve/static/js/components/Wiki.js'),
  'utf8',
);
const cssSrc = readFileSync(
  resolve(repoRoot, 'loop_memory/serve/static/css/app.css'),
  'utf8',
);

let failures = 0;
function ok(cond, name) {
  if (cond) return;
  failures += 1;
  console.error('FAIL ' + name);
}

// 1. Make sure the rendered DOM is mountable. Strip top-level Vue
//    directives that aren't understood by vanilla DOM templates —
//    we only care about structure, not runtime semantics.
const tplStart = wikiSrc.indexOf("template: /* html */");
const tplEnd = wikiSrc.lastIndexOf('`');
const tplSlice = wikiSrc.slice(tplStart, tplEnd);
// Drop ``\n`-prefixed JS expressions, just keep the inner HTML.
const tpl = tplSlice
  .replace(/^template:\s*\/\*\s*html\s*\*\/\s*`/, '')
  .replace(/`\s*$/, '')
    .replace(/\{\{\s*t\('wiki\.preview'\)\s*\}\}/g, '预览')
  .replace(/\{\{\s*t\('wiki\.preview\.body'\)\s*\}\}/g, '正文')
  .replace(/\{\{\s*t\('wiki\.preview\.copyBody'\)\s*\}\}/g, '复制正文')
  .replace(/\{\{\s*t\('action\.close'\)\s*\}\}/g, '关闭')
  .replace(/\{\{\s*t\('action\.edit'\)\s*\}\}/g, '编辑')
  .replace(/\{\{\s*t\('action\.delete'\)\s*\}\}/g, '删除')
  .replace(/\{\{\s*t\('wiki\.field\.tags'\)\s*\}\}/g, '标签')
  .replace(/\{\{\s*[^}]*\s*\}\}/g, 'X')
  .replace(/v-for="[^"]+"/g, '')
  .replace(/v-if="[^"]+"/g, '')
  .replace(/v-else-if="[^"]+"/g, '')
  .replace(/v-else/g, '')
  .replace(/:[a-z][a-z0-9.-]*="[^"]*"/g, '')
  .replace(/@(?:[a-z]+\.)?[a-z]+(?:\.[a-z]+)*="[^"]*"/g, '');

// 2. Mount the template into a JSDOM and gather selectors.
const dom = new JSDOM(
  `<!DOCTYPE html><html><body><div id="app"><div class="tab-pane active" id="pane-wiki"><div class="wiki-wrap">${tpl}</div></div></div></body></html>`,
);
const doc = dom.window.document;

const backdrop = doc.querySelector('.wiki-preview-backdrop');
ok(backdrop !== null, 'A) .wiki-preview-backdrop mounts to the DOM');

const modal = doc.querySelector('.wiki-preview-modal');
ok(modal !== null, 'B) .wiki-preview-modal mounts to the DOM');

// 3. Modal must be a child of the backdrop (centred overlay), not a sibling.
ok(backdrop && modal && backdrop.contains(modal),
   'C) .wiki-preview-modal is a CHILD of .wiki-preview-backdrop');

// 4. The dismiss-region (`.modal-backdrop` with the .self click) lives
//    on the backdrop element.
ok(backdrop && backdrop.classList.contains('modal-backdrop'),
   'D) backdrop element carries .modal-backdrop (for click.self close)');

// 5. No inline body expand remains — the legacy accordion should be gone.
const inlineBody = doc.querySelector('.wc-body');
ok(inlineBody === null, 'E) no inline .wc-body accordion residue');

// 6. The CSS rules explicitly anchor the backdrop with position:fixed
//    and full inset, regardless of how the base .modal-backdrop class
//    evolves.
const fixedRule = /\.wiki-preview-backdrop\s*\{[^}]*position:\s*fixed[^}]*inset:\s*0/m
  .test(cssSrc);
ok(fixedRule, 'F) .wiki-preview-backdrop CSS declares position:fixed + inset:0');

// 7. Backdrop uses flex / grid centering so the modal sits centred.
const centersRule = /\.wiki-preview-backdrop\s*\{[^}]*(display:\s*flex|place-items:\s*center|align-items:\s*center)/m
  .test(cssSrc);
ok(centersRule, 'G) .wiki-preview-backdrop CSS uses flex/place-items centering');

// 8. Footer renders copy / delete / edit / close in that order.
const footButtons = modal ? modal.querySelectorAll('.wiki-preview-foot button') : [];
const footLabels = Array.from(footButtons).map((b) => b.textContent.trim());
ok(footLabels.includes('X') || footLabels.some((t) => /复制/i.test(t)),
   'H) footer has the copy-body button (label "复制正文")');
ok(footLabels.some((t) => /删除/.test(t)),
   'I) footer has the delete button (label "删除")');
ok(footLabels.some((t) => /编辑/.test(t)),
   'J) footer has the edit button (label "编辑")');
ok(footLabels.some((t) => /关闭/.test(t)),
   'K) footer has the close button (label "关闭")');

// 9. Clickable card title element exists.
const title = doc.querySelector('.wc-title-clickable');
ok(title !== null, 'L) card title carries .wc-title-clickable');

// 10. Every template binding must be exposed by setup(). Missing
//     `visible` previously made the entire Wiki pane fail at
//     `visible.length` during its first render.
const setupReturn = wikiSrc.match(
  /return\s*\{([\s\S]*?)\};\s*\n\s*\},\s*\n\s*template:/,
);
ok(setupReturn && /\bvisible\b/.test(setupReturn[1]),
   'M) setup exposes visible to the template');

// 11. The modal frame must clip overflow and let the body shrink as
//     a flex child; otherwise long pages extend below the footer and
//     the lower tags/evidence cannot be reached.
const modalClipsRule = /\.wiki-preview-modal\s*\{[^}]*overflow:\s*hidden/m
  .test(cssSrc);
ok(modalClipsRule, 'N) preview modal clips child overflow');
const bodyScrollRule = /\.wiki-preview-body\s*\{[^}]*min-height:\s*0[^}]*overflow-y:\s*auto/m
  .test(cssSrc);
ok(bodyScrollRule, 'O) preview body is an independently scrollable flex child');
const bodyChildrenRule = /\.wiki-preview-body\s*>\s*\*\s*\{[^}]*flex:\s*0\s+0\s+auto/m
  .test(cssSrc);
ok(bodyChildrenRule, 'P) preview body sections cannot shrink away overflow');

if (failures === 0) {
  console.log('OK wiki preview render: 16 checks passed');
  process.exit(0);
}
console.error(`FAIL wiki preview render: ${failures} of 16 checks failed`);
process.exit(1);
