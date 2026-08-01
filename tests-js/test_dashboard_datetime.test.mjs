/** Regression checks for the Dashboard refresh date-time chip. */
import { readFileSync } from 'node:fs';

const dashboard = readFileSync('loop_memory/serve/static/js/components/Dashboard.js', 'utf8');
const css = readFileSync('loop_memory/serve/static/css/app.css', 'utf8');
let failures = 0;

function ok(condition, name) {
  if (condition) return;
  failures += 1;
  console.error('FAIL ' + name);
}

ok(dashboard.includes('const lastRefreshParts = computed(() =>'), 'A) refresh stamp uses structured parts');
ok(dashboard.includes("date: `${yyyy}-${mon}-${day}`"), 'B) date uses YYYY-MM-DD');
ok(dashboard.includes("time: `${hh}:${mm}:${ss}`"), 'C) time keeps HH:mm:ss');
ok(dashboard.includes('class="ins-meta-date"'), 'D) date has a dedicated style hook');
ok(dashboard.includes('class="ins-meta-time"'), 'E) time has a dedicated style hook');
ok(dashboard.includes('class="ins-meta-sep"'), 'F) date and time use a visual separator');
ok(/\.ins-head \.ins-meta-datetime\s*\{[^}]*white-space:\s*nowrap/m.test(css),
   'G) date-time stays on one line');
ok(/\.ins-head \.ins-meta-datetime\s*\{[^}]*font-variant-numeric:\s*tabular-nums/m.test(css),
   'H) changing seconds do not shift the chip width');
ok(/\.ins-head \.ins-meta-datetime\s*\{[^}]*gap:\s*3px/m.test(css),
   'I) date and time use compact spacing');
ok(/\.ins-head \.ins-meta-sep\s*\{[^}]*width:\s*0/m.test(css),
   'J) separator does not add glyph width');

if (failures === 0) {
  console.log('OK dashboard datetime: 10 checks passed');
  process.exit(0);
}
console.error(`FAIL dashboard datetime: ${failures} of 10 checks failed`);
process.exit(1);
