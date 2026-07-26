// Bypass test suite for the v-html sanitizer (audit M1).
// Run: node tests-js/test_sanitize.test.mjs
import { JSDOM } from 'jsdom';
import { sanitizeHtml } from '../loop_memory/serve/static/js/lib/sanitize.js';

const dom = new JSDOM('<!doctype html><html><body></body></html>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.DOMParser = dom.window.DOMParser;

let failures = 0;
function notContains(got, needle, name) {
  if (!got.includes(needle)) return;
  failures += 1;
  console.error('FAIL  ' + name);
  console.error('  got should NOT contain ' + JSON.stringify(needle));
  console.error('  got: ' + JSON.stringify(got));
}
function checkContains(got, needle, name) {
  if (got.includes(needle)) return;
  failures += 1;
  console.error('FAIL  ' + name);
  console.error('  got should contain ' + JSON.stringify(needle));
  console.error('  got: ' + JSON.stringify(got));
}

// Negative cases: bypass payloads must be neutralised.
const cases = [
  ['script-tag',      'before <script>alert(1)</script> after', '<script'],
  ['script-content',  'before <script>alert(1)</script> after', 'alert(1)'],
  ['inline-handler',  '<p onclick="alert(1)">click me</p>', 'onclick'],
  ['js-href',         '<a href="javascript:alert(1)">click</a>', 'javascript:'],
  ['js-href-entity',  '<a href="java&#x73;cript:alert(1)">x</a>', 'javascript:alert(1)'],
  ['js-href-tab',     '<a href="java\tscript:alert(1)">x</a>', 'javascript:alert(1)'],
  ['js-href-newline', '<a href="java\nscript:alert(1)">x</a>', 'javascript:alert(1)'],
  ['css-style',       '<p style="background:url(javascript:alert(1))">x</p>', 'style='],
  ['iframe',          'ok <iframe src="https://evil"></iframe> done', '<iframe'],
  ['data-html',       '<img src="data:text/html,<script>alert(1)</script>">', 'data:text/html'],
  ['unknown-marquee', '<marquee onstart="alert(1)">x</marquee>', '<marquee'],
  ['extra-attr',      '<a href="https://example.com" src="javascript:alert(1)">x</a>', 'javascript'],
  ['vbscript',        '<a href="vbscript:msgbox(1)">x</a>', 'vbscript:'],
  ['file-url',        '<a href="file:///etc/passwd">x</a>', 'file:'],
  ['comment',         '<!-- <script>alert(1)</script> -->', 'alert(1)'],
];
for (const [name, dirty, needle] of cases) {
  const out = sanitizeHtml(dirty);
  notContains(out.toLowerCase(), needle.toLowerCase(), name);
}

// Positive cases: legitimate content must survive.
checkContains(sanitizeHtml('<img src="data:image/png;base64,iVBORw0KGgo=" alt="x">'), 'data:image/png', 'data-png-img-allowed');
checkContains(sanitizeHtml('<a href="/local/path">x</a>'), 'href="/local/path"', 'relative-url-allowed');
checkContains(sanitizeHtml('<a href="#section">x</a>'), 'href="#section"', 'anchor-url-allowed');
checkContains(sanitizeHtml('<a href="mailto:foo@bar.com">x</a>'), 'mailto:', 'mailto-allowed');
checkContains(sanitizeHtml('<a href="https://example.com/">x</a>'), 'rel="noopener noreferrer"', 'outbound-rel-auto');
checkContains(sanitizeHtml('<h1>Title</h1>'), '<h1>', 'h1-preserved');
checkContains(sanitizeHtml('<strong>bold</strong>'), '<strong>', 'strong-preserved');

// Empty / nullish inputs.
for (const inp of [null, undefined, '']) {
  if (sanitizeHtml(inp) !== '') {
    failures += 1;
    console.error('FAIL  empty-input ' + JSON.stringify(inp));
  }
}

if (failures === 0) {
  console.log('OK   sanitizer bypass suite passed (' + cases.length + ' bypass + 8 positive cases)');
} else {
  console.error('FAIL  ' + failures + ' sanitizer test(s) failed');
  process.exit(1);
}
