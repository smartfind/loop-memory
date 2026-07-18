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
  modelInfo:      { provider: 'rules', model: 'rules', api_key_set: false, key_len: 0 },
  stripDismissed: false,
  lastRunId:      null,
});

// Persist prefs whenever they change.
watch(() => [store.lang, store.theme, store.showZh], () => {
  savePrefs({ lang: store.lang, theme: store.theme, showZh: store.showZh });
});

// ---- i18n ----
let _i18n = { en: {}, zh: {} };
let _i18nLoaded = false;

export async function loadI18n() {
  if (_i18nLoaded) return _i18n;
  // Load in parallel; ignore individual failures so a single missing file
  // doesn't break the whole UI.
  const [en, zh] = await Promise.all([
    fetch('static/i18n/en.json').then(r => r.ok ? r.json() : {}).catch(() => ({})),
    fetch('static/i18n/zh.json').then(r => r.ok ? r.json() : {}).catch(() => ({})),
  ]);
  _i18n = { en, zh };
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
