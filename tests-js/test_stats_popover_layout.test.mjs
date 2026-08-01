/** Regression checks for the compact database row in the stats popover. */
import { readFileSync } from 'node:fs';

const topBar = readFileSync('loop_memory/serve/static/js/components/TopBar.js', 'utf8');
const css = readFileSync('loop_memory/serve/static/css/app.css', 'utf8');
let failures = 0;

function ok(condition, name) {
  if (condition) return;
  failures += 1;
  console.error('FAIL ' + name);
}

ok(topBar.includes('const dbInfo = computed(() =>'), 'A) database display uses computed metadata');
ok(topBar.includes("replace(/^\\/Users\\/[^/]+/, '~')"), 'B) home directory is compacted to tilde');
ok(topBar.includes('class="stats-db"'), 'C) database row has a dedicated layout hook');
ok(topBar.includes('{{ dbInfo.name }}'), 'D) filename renders separately');
ok(topBar.includes('{{ dbInfo.dir }}'), 'E) compact directory renders separately');
ok(/\.stats-pop \.stats-db\s*\{[^}]*grid-template-columns:\s*64px\s+minmax\(0,\s*1fr\)/m.test(css),
   'F) database label and value use stable grid columns');
ok(/\.stats-pop \.stats-db-value\s*\{[^}]*align-items:\s*flex-end/m.test(css),
   'G) database values align to the right');
ok(/\.stats-pop \.stats-db-value strong,[\s\S]*text-overflow:\s*ellipsis/m.test(css),
   'H) long database values truncate without wrapping');

if (failures === 0) {
  console.log('OK stats popover layout: 8 checks passed');
  process.exit(0);
}
console.error(`FAIL stats popover layout: ${failures} of 8 checks failed`);
process.exit(1);
