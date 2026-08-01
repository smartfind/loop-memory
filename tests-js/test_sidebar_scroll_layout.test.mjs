/** Regression checks for Sidebar header/content scroll isolation. */
import { readFileSync } from 'node:fs';

const layout = readFileSync('loop_memory/serve/static/css/layout.css', 'utf8');
const app = readFileSync('loop_memory/serve/static/css/app.css', 'utf8');
let failures = 0;

function ok(condition, name) {
  if (condition) return;
  failures += 1;
  console.error('FAIL ' + name);
}

ok(/\.app-body > \.sidebar\s*\{[^}]*display:\s*flex/m.test(layout), 'A) sidebar is a flex column');
ok(/\.app-body > \.sidebar\s*\{[^}]*flex-direction:\s*column/m.test(layout), 'B) sidebar stacks fixed and scrolling regions');
ok(/\.app-body > \.sidebar\s*\{[^}]*overflow:\s*hidden/m.test(layout), 'C) session content cannot bleed outside sidebar');
ok(/\.sidebar-head\s*\{[^}]*flex-shrink:\s*0/m.test(app), 'D) sidebar heading stays fixed');
ok(/\.source-pills\s*\{[^}]*flex-shrink:\s*0/m.test(app), 'E) source filters stay fixed');
ok(/\.sessions\s*\{[^}]*flex:\s*1/m.test(app), 'F) session list consumes remaining height');
ok(/\.sessions\s*\{[^}]*overflow-y:\s*auto/m.test(app), 'G) only session list scrolls vertically');
ok(/\.sessions\s*\{[^}]*overscroll-behavior:\s*contain/m.test(app), 'H) sidebar scrolling stays contained');

if (failures === 0) {
  console.log('OK sidebar scroll layout: 8 checks passed');
  process.exit(0);
}
console.error(`FAIL sidebar scroll layout: ${failures} of 8 checks failed`);
process.exit(1);
