/**
 * TopBar — the header bar at the top of every page.
 *
 * Shows: brand, stats pills, run-status indicator, model chip, action
 * buttons, language / theme / settings menu.
 *
 * The legacy vanilla-JS code mixed state mutation into 20+ event
 * listeners scattered through the file. Here the bar is a single Vue
 * component with one event-emitter for the actions (clicking "AI Run"
 * bubbles up to App which knows how to call the API).
 */
import { defineComponent, computed, ref, onMounted, onUnmounted } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, patchPrefs, toast, t, timeAgo, fmtTime } from '../store.js';
import { api, ApiError } from '../api.js';

export const TopBar = defineComponent({
  name: 'TopBar',
  emits: ['ingest', 'rescore', 'llm-run', 'run-now', 'open-settings', 'open-stats', 'open-diag'],
  setup(props, { emit }) {
    const statsOpen = ref(false);
    const kebabOpen = ref(false);

    const runLabel = computed(() => {
      if (store.runStatus?.is_running) {
        const p = store.runStatus.progress || {};
        if (p.total > 0) return `${p.current}/${p.total}`;
        return '…';
      }
      return t('topbar.run.idle');
    });

    const runState = computed(() => store.runStatus?.is_running ? 'running' : 'idle');

    function toggleStats() { statsOpen.value = !statsOpen.value; kebabOpen.value = false; }
    function toggleKebab() { kebabOpen.value = !kebabOpen.value; statsOpen.value = false; }
    function closeAll() { statsOpen.value = false; kebabOpen.value = false; }

    function setLang(l) { patchPrefs({ lang: l }); closeAll(); }
    function setTheme(th) { patchPrefs({ theme: th }); closeAll(); }

    // Close popovers on outside click
    function onDocClick(e) {
      if (!e.target.closest('#stats-chip') && !e.target.closest('#stats-pop')) statsOpen.value = false;
      if (!e.target.closest('.tb-kebab') && !e.target.closest('.tb-kebab-menu')) kebabOpen.value = false;
    }
    onMounted(() => document.addEventListener('click', onDocClick));
    onUnmounted(() => document.removeEventListener('click', onDocClick));

    return {
      store, t, runLabel, runState,
      statsOpen, kebabOpen,
      toggleStats, toggleKebab, closeAll,
      setLang, setTheme,
      onIngest:      () => emit('ingest'),
      onRescore:     () => emit('rescore'),
      onLlmRun:      () => emit('llm-run'),
      onRunNow:      () => emit('run-now'),
      onOpenSettings:() => emit('open-settings'),
      onOpenStats:   () => emit('open-stats'),
      onOpenDiag:    () => emit('open-diag'),
    };
  },
  template: /* html */ `
<header class="topbar">
  <div class="topbar-brand">
    <div class="logo">LM</div>
    <div class="brand">
      <span class="brand-name">{{ t('app.title') }}</span>
      <span class="brand-tag">{{ t('app.tagline') }}</span>
    </div>
  </div>

  <div style="position:relative;">
    <span class="stats-pills" id="stats-chip" role="button" tabindex="0"
          aria-haspopup="true" :aria-label="t('topbar.stats')"
          :title="t('topbar.statsDetails')" @click="toggleStats">
      <span class="stats-pill" :title="t('stat.memories')">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="10" height="10" rx="2"/><path d="M5.5 7h5M5.5 9.5h5"/></svg>
        <strong>{{ store.stats.memories || '…' }}</strong>
      </span>
      <span class="stats-pill" :title="t('stat.sessions')">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="6" r="2.5"/><path d="M3 13c.7-2.3 2.7-3.5 5-3.5s4.3 1.2 5 3.5"/></svg>
        <strong>{{ store.stats.sessions || '…' }}</strong>
      </span>
      <span class="stats-pill" :title="t('stat.scoreLabel')">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1.5l2 4.2 4.6.7-3.3 3.2.8 4.6L8 12l-4.1 2.2.8-4.6L1.4 6.4 6 5.7z"/></svg>
        <strong>{{ store.stats.avg_score ? (store.stats.avg_score * 100).toFixed(0) + '%' : '…' }}</strong>
      </span>
    </span>
    <div class="stats-pop" id="stats-pop" v-show="statsOpen" @click.stop>
      <div class="row"><span class="label">{{ t('stat.memories') }}</span><span class="val">{{ store.stats.memories || '…' }}</span></div>
      <div class="row"><span class="label">{{ t('stat.sessions') }}</span><span class="val">{{ store.stats.sessions || '…' }}</span></div>
      <div class="row"><span class="label">{{ t('stat.graph') }}</span><span class="val">{{ store.stats.graph || '0/0' }}</span></div>
      <div class="row"><span class="label">{{ t('stat.scoreLabel') }}</span><span class="val">{{ store.stats.avg_score ? (store.stats.avg_score * 100).toFixed(1) + '%' : '…' }}</span></div>
      <hr/>
      <div class="row"><span class="label">{{ t('stat.dbPath') }}</span><span class="val" style="font-family:var(--mono); font-size:11px; cursor:pointer;" :title="store.stats.dbPath">{{ store.stats.dbPath ? (store.stats.dbPath.length > 36 ? '…' + store.stats.dbPath.slice(-34) : store.stats.dbPath) : '…' }}</span></div>
    </div>
  </div>

  <div class="run-status" v-show="store.runStatus && store.runStatus.is_running">
    <span class="run-status-dot"></span>
    <span class="run-status-label">{{ runLabel }}</span>
  </div>

  <div class="spacer"></div>

  <div class="group-right">
    <span class="model-chip" id="model-chip" :data-state="store.modelInfo.api_key_set ? 'on' : 'off'" role="button" tabindex="0"
          :title="t('model.configureTip')" @click="onOpenSettings">
      <span class="dot"></span>
      <span>{{ t('model.label') }}</span>
      <span class="model-name">{{ store.modelInfo.model || 'rules' }}</span>
      <svg class="lock" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 015 0v2"/></svg>
    </span>

    <div class="tb-divider"></div>

    <button class="tb-action" :title="t('action.ingestTip')" @click="onIngest">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1l3.5 4H9v6H7V5H4.5L8 1zM2 13h12v1.5H2z"/></svg>
      <span>{{ t('action.ingest') }}</span>
    </button>
    <button class="tb-action" :title="t('action.rescoreTip')" @click="onRescore">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 3a5 5 0 105 5h-2a3 3 0 11-3-3V3z"/></svg>
      <span>{{ t('action.rescore') }}</span>
    </button>
    <button class="tb-action primary" :title="t('action.llmRunTip')" @click="onLlmRun">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1l2.2 4.6L15 6.3l-3.5 3.4.8 4.8L8 12.4 3.7 14.5l.8-4.8L1 6.3l4.8-.7L8 1z"/></svg>
      <span>{{ t('action.llmRun') }}</span>
    </button>
    <button class="tb-action accent" :title="t('action.runNowTip')" @click="onRunNow">
      <svg viewBox="0 0 16 16" fill="currentColor"><path d="M9 1L3 9h4l-1 6 6-8H8l1-6z"/></svg>
      <span>{{ t('action.runNow') }}</span>
    </button>

    <div class="tb-divider"></div>

    <button class="tb-action ghost" :title="t('diag.title')" @click="onOpenDiag">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></svg>
    </button>

    <div class="tb-kebab" style="position:relative;">
      <button class="tb-action ghost" :title="t('topbar.settings')" @click.stop="toggleKebab">
        <svg viewBox="0 0 16 16" fill="currentColor"><circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="13" cy="8" r="1.5"/></svg>
      </button>
      <div class="tb-kebab-menu" v-show="kebabOpen" @click.stop>
        <div class="menu-section">{{ t('topbar.language') }}</div>
        <button class="menu-item" :class="{ active: store.lang === 'zh' }" @click="setLang('zh')">中文</button>
        <button class="menu-item" :class="{ active: store.lang === 'en' }" @click="setLang('en')">English</button>
        <hr/>
        <div class="menu-section">{{ t('topbar.theme') }}</div>
        <button class="menu-item" :class="{ active: store.theme === 'auto' }" @click="setTheme('auto')">Auto</button>
        <button class="menu-item" :class="{ active: store.theme === 'light' }" @click="setTheme('light')">Light</button>
        <button class="menu-item" :class="{ active: store.theme === 'dark' }" @click="setTheme('dark')">Dark</button>
        <hr/>
        <button class="menu-item" @click="onOpenSettings">{{ t('action.settings') }}</button>
      </div>
    </div>
  </div>
</header>
  `,
});
