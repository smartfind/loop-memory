/** Regression checks for explicit Timeline search interactions. */
import { readFileSync } from 'node:fs';

const timeline = readFileSync('loop_memory/serve/static/js/components/Timeline.js', 'utf8');
const css = readFileSync('loop_memory/serve/static/css/layout.css', 'utf8');
const zh = JSON.parse(readFileSync('loop_memory/serve/static/i18n/zh.json', 'utf8'));
const en = JSON.parse(readFileSync('loop_memory/serve/static/i18n/en.json', 'utf8'));
let failures = 0;

function ok(condition, name) {
  if (condition) return;
  failures += 1;
  console.error('FAIL ' + name);
}

ok(timeline.includes('@submit.prevent="onSearchSubmit"'), 'A) Enter submits without navigating');
ok(timeline.includes('type="search"'), 'B) query uses a semantic search input');
ok(timeline.includes('type="submit" class="tl-btn primary"'), 'C) toolbar has a visible search button');
ok(timeline.includes(':disabled="loading"'), 'D) search button blocks duplicate loading requests');
ok(timeline.includes("if (!loading.value) refresh();"), 'E) submit triggers refresh once');
ok(/\.tl-btn\.primary\s*\{[^}]*background:\s*var\(--accent\)/m.test(css), 'F) search button is visually primary');
ok(zh['timeline.search'] === '搜索', 'G) Chinese search label exists');
ok(en['timeline.search'] === 'Search', 'H) English search label exists');

if (failures === 0) {
  console.log('OK timeline search: 8 checks passed');
  process.exit(0);
}
console.error(`FAIL timeline search: ${failures} of 8 checks failed`);
process.exit(1);
