/** Regression checks for the import-popover column alignment. */
import { readFileSync } from 'node:fs';

const css = readFileSync('loop_memory/serve/static/css/layout.css', 'utf8');
let failures = 0;

function ok(condition, name) {
  if (condition) return;
  failures += 1;
  console.error('FAIL ' + name);
}

const rowRules = [...css.matchAll(
  /\.topbar \.tb-ingest-menu \.ingest-row\s*\{([^}]*)\}/g,
)];
const finalRowRule = rowRules.at(-1)?.[1] || '';
ok(/display:\s*flex/.test(finalRowRule), 'A) final source row uses flex layout');
ok(/align-items:\s*stretch/.test(finalRowRule), 'B) source rows stretch to one width');

const mainRule = css.match(
  /\.topbar \.tb-ingest-menu \.ingest-row-main\s*\{([^}]*)\}/,
)?.[1] || '';
ok(/width:\s*100%/.test(mainRule), 'C) source headers fill the row width');
ok(/min-width:\s*0/.test(mainRule), 'D) source headers may shrink safely');
ok(/grid-template-columns:\s*18px\s+20px\s+76px\s+minmax\(0,\s*1fr\)\s+auto/.test(mainRule),
   'E) checkbox, icon, name, path, and status use stable columns');

const activeRule = css.match(
  /\.topbar \.tb-ingest-menu \.ingest-active\s*\{([^}]*)\}/,
)?.[1] || '';
ok(/box-sizing:\s*border-box/.test(activeRule), 'F) active-session cards size consistently');

if (failures === 0) {
  console.log('OK ingest popover layout: 6 checks passed');
  process.exit(0);
}
console.error(`FAIL ingest popover layout: ${failures} of 6 checks failed`);
process.exit(1);
