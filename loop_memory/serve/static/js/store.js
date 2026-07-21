/**
 * Reactive global state for the loop-memory dashboard.
 *
 * Uses Vue 3 reactivity (no Vuex / Pinia) — this app is small enough that
 * a plain reactive() object is easier to maintain than a state library.
 * Components import { store, persistPrefs } and read / mutate.
 */
import { reactive, watch, computed } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';

const STORE_KEY = 'loop_memory_prefs_v1';

function loadPrefs() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) { /* ignore */ }
  return {};
}

function savePrefs(p) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(p)); } catch (e) { /* ignore */ }
}

const initial = loadPrefs();

function _detectInitialLang(stored) {
  if (stored) return stored;            // explicit user choice — respect it
  if (typeof navigator !== 'undefined') {
    const nav = (navigator.language || (navigator.languages && navigator.languages[0]) || '').toLowerCase();
    if (nav.startsWith('zh')) return 'zh';
    // Anything else (en, ja, fr, etc.) defaults to zh only as a last resort.
    // The user can always switch via the kebab menu — their selection will stick.
    if (nav) return nav.startsWith('en') ? 'en' : 'zh';
  }
  return 'zh';
}

// Reactive global state. The shape is documented in applyDefaults().
export const store = reactive({
  // ---- user preferences (persisted) ----
  // Default picks Chinese for zh-* browser locales, English for en-* locales,
  // and Chinese as the final fallback. Stored value (explicit user choice) wins.
  lang:           _detectInitialLang(initial.lang),
  theme:          initial.theme || 'auto',      // 'auto' | 'light' | 'dark'
  showZh:         initial.showZh ?? true,       // mixed-lang UI helper

  // ---- runtime state (NOT persisted) ----
  activeTab:      'timeline',                   // 'timeline' | 'dashboard' | 'wiki' | 'graph'
  ready:          false,                        // true after first i18n + theme apply
  toast:          null,                         // { msg, ts }
  toastTimer:     null,
  runStatus:      { is_running: false, progress: { current: 0, total: 0, message: '' } },
  stats:          { memories: 0, sessions: 0, wiki_pages: 0, avg_score: 0, graph: '0/0', dbPath: '' },
  modelInfo:      { provider: 'rules', model: 'rules', api_key_set: false, key_len: 0,
                    // Reachability: 'unset' | 'ok' | 'stale' | 'fail'
                    //   unset  — no key configured
                    //   ok     — key set AND last test (or live run) succeeded recently
                    //   stale  — key set, no recent test (treat as 'configured but unverified')
                    //   fail   — key set, last test/error was a provider failure
                    reachability: 'unset', last_test_ok: null, last_test_at: null, last_test_message: '' },
  stripDismissed: false,
  lastRunId:      null,
});

// ---- shared actions ----
// Lightweight event-bus: App.js registers the implementation that opens
// drawers (settings / diagnostic). Other components call these helpers
// without having to bubble events through the Vue tree.
const _actions = {
  openSettings: () => { /* set by App.js */ },
  openDiag:     () => { /* set by App.js */ },
  llmRun:       () => { /* set by App.js — kicks off an LLM consolidation run */ },
};
export function registerActions(map) { Object.assign(_actions, map); }
export function callAction(name, ...args) {
  const fn = _actions[name];
  if (typeof fn === 'function') {
    try { return fn(...args); } catch (_e) { return undefined; }
  }
  return undefined;
}

// Persist prefs whenever they change.
watch(() => [store.lang, store.theme, store.showZh], () => {
  savePrefs({ lang: store.lang, theme: store.theme, showZh: store.showZh });
});

// ---- i18n ----
// _i18n is a reactive proxy so any component template / computed that calls
// `t()` automatically re-renders when the dictionaries finish loading. Without
// this, the first render flashes raw keys like `tab.wiki` until something else
// forces a re-render (see commit a34de38 → a plain object's keys aren't tracked).
const _i18n = reactive({ en: {}, zh: {} });
let _i18nLoaded = false;

/**
 * Read the inline i18n JSON that the server injects into ``<script
 * type="application/json" id="loop-i18n-en">`` / ``id="loop-i18n-zh"``
 * (see ``serve/app.py`` index route). Reading these SYNCHRONOUSLY at
 * module init means Vue's first render already has the strings — no
 * flash of raw keys like ``tab.wiki`` while the JSON fetch is in
 * flight, which the user reported as "页面先变英文再转中文".
 *
 * Returns true if at least one dict was populated inline.
 */
function _readInlineI18n() {
  if (typeof document === 'undefined') return false;
  let ok = false;
  for (const lang of ['en', 'zh']) {
    const tag = document.getElementById('loop-i18n-' + lang);
    if (!tag) continue;
    try {
      const dict = JSON.parse(tag.textContent || '{}');
      Object.assign(_i18n[lang], dict);
      ok = true;
    } catch (_e) { /* ignore parse errors — fetch fallback below */ }
  }
  return ok;
}

// Synchronously hydrate from inline JSON before Vue mounts. This is the
// critical line that prevents the "flash of English keys" the user
// complained about on hard refresh.
if (_readInlineI18n()) {
  _i18nLoaded = true;
}

export async function loadI18n() {
  if (_i18nLoaded) return _i18n;
  // Fetch the JSON files for any keys that weren't inlined (e.g. when
  // the page is opened from a different host / dev mode). ``cache:
  // 'no-store'`` bypasses the browser HTTP cache so that a user who
  // just edited an i18n string and hit reload actually sees the new
  // copy — Safari in particular is aggressive about caching small
  // JSON files served with ``Cache-Control: max-age=3600``.
  const [en, zh] = await Promise.all([
    fetch('static/i18n/en.json', { cache: 'no-store' }).then(r => r.ok ? r.json() : {}).catch(() => ({})),
    fetch('static/i18n/zh.json', { cache: 'no-store' }).then(r => r.ok ? r.json() : {}).catch(() => ({})),
  ]);
  // Merge on top of any inline dicts (the inline version usually wins
  // since it's what the server served for this exact render, but if
  // the user is in a stale browser cache and we got fresher JSON from
  // the network, that's the better source).
  Object.assign(_i18n.en, en);
  Object.assign(_i18n.zh, zh);
  _i18nLoaded = true;
  return _i18n;
}

export function t(key, vars) {
  const dict = _i18n[store.lang] || _i18n.zh || {};
  let s = dict[key] ?? _i18n.en[key] ?? key;
  if (vars) {
    for (const k of Object.keys(vars)) s = s.replace('{' + k + '}', vars[k]);
  }
  return s;
}


export function tOrKey(key) {
  return t(key, undefined);
}

export const lang = computed(() => store.lang);
export const theme = computed(() => store.theme);

// ---- theme ----
function _systemPrefersDark() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export function effectiveTheme() {
  if (store.theme === 'auto') return _systemPrefersDark() ? 'dark' : 'light';
  return store.theme;
}

export function applyTheme() {
  document.documentElement.setAttribute('data-theme', effectiveTheme());
}

export function applyLang() {
  document.documentElement.setAttribute('data-lang', store.lang);
  // Also update the standard ``lang`` attribute so screen readers,
  // browser translation prompts, and search-engine hints reflect the
  // actual content language. ``data-lang`` is kept for CSS selectors
  // that key off it.
  const htmlLang = store.lang === 'zh' ? 'zh-CN' : 'en';
  document.documentElement.setAttribute('lang', htmlLang);
}

// Apply theme/lang to <html> reactively.
watch(() => store.theme, () => applyTheme());
watch(() => store.lang, () => applyLang());
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (store.theme === 'auto') applyTheme();
  });
}

// ---- toast ----
export function toast(msg, ms = 2200) {
  store.toast = { msg, ts: Date.now() };
  if (store.toastTimer) clearTimeout(store.toastTimer);
  store.toastTimer = setTimeout(() => { store.toast = null; }, ms);
}

// ---- prefs patch ----
export function patchPrefs(patch) {
  Object.assign(store, patch);
  savePrefs({
    lang: store.lang, theme: store.theme, showZh: store.showZh,
  });
}

// ---- formatting helpers ----
export function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function timeAgo(ts) {
  if (!ts) return '—';
  const d = Date.now() / 1000 - Number(ts);
  if (d < 60) return Math.max(0, Math.floor(d)) + 's';
  if (d < 3600) return Math.floor(d / 60) + 'm';
  if (d < 86400) return Math.floor(d / 3600) + 'h';
  if (d < 86400 * 30) return Math.floor(d / 86400) + 'd';
  return Math.floor(d / 86400 / 30) + 'mo';
}

export function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  return d.toLocaleString();
}
