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
import { IngestPopover } from './IngestPopover.js';
import { api, ApiError } from '../api.js';

export const TopBar = defineComponent({
  name: 'TopBar',
  components: { IngestPopover },
  emits: ['ingest', 'rescore', 'llm-run', 'open-settings', 'open-llm-config', 'open-stats', 'open-diag', 'rebuild-graph', 'consolidate'],
  setup(props, { emit }) {
    const statsOpen = ref(false);
    const toolsOpen = ref(false);
    const ingestOpen = ref(false);

    const runLabel = computed(() => {
      if (store.runStatus && store.runStatus.is_running) {
        const p = store.runStatus.progress || {};
        if (p.total > 0) return `${p.current}/${p.total}`;
        return '…';
      }
      return t('topbar.run.idle');
    });

    // Model-chip tooltip — picks one of four i18n keys based on
    // reachability. The chip itself is a small pill (icon + model
    // name + tiny dot); the tooltip carries the full status text so
    // the topbar stays narrow.
    const modelChipTip = computed(() => {
      const r = store.modelInfo.reachability || 'unset';
      const provider = store.modelInfo.provider || 'rules';
      const model = store.modelInfo.model || 'rules';
      const ctx = { provider, model, msg: store.modelInfo.last_test_message || '' };
      return t('model.tip.' + r, ctx);
    });

    const runState = computed(() => store.runStatus && store.runStatus.is_running ? 'running' : 'idle');

    function toggleStats() { statsOpen.value = !statsOpen.value; toolsOpen.value = false; ingestOpen.value = false; }
    function toggleTools() { toolsOpen.value = !toolsOpen.value; statsOpen.value = false; ingestOpen.value = false; }
    function toggleIngest() { ingestOpen.value = !ingestOpen.value; statsOpen.value = false; toolsOpen.value = false; }
    function closeAll() { statsOpen.value = false; toolsOpen.value = false; ingestOpen.value = false; }

    function setLang(l) { patchPrefs({ lang: l }); closeAll(); }
    function setTheme(th) { patchPrefs({ theme: th }); closeAll(); }

    function onDocClick(e) {
      if (!e.target.closest('#stats-chip') && !e.target.closest('#stats-pop')) statsOpen.value = false;
      if (!e.target.closest('.tb-tools') && !e.target.closest('.tb-tools-menu')) toolsOpen.value = false;
      if (!e.target.closest('.tb-ingest') && !e.target.closest('.tb-ingest-menu')) ingestOpen.value = false;
    }
    onMounted(() => document.addEventListener('click', onDocClick));
    onUnmounted(() => document.removeEventListener('click', onDocClick));

    return {
      store, t, runLabel, runState, modelChipTip,
      statsOpen, toolsOpen, ingestOpen,
      toggleStats, toggleTools, toggleIngest, closeAll,
      setLang, setTheme,
      onIngest:      () => { closeAll(); toggleIngest(); },
      onRescore:     () => { closeAll(); emit('rescore'); },
      onLlmRun:      () => { closeAll(); emit('llm-run'); },
      onOpenSettings:() => { closeAll(); emit('open-settings'); },
      onOpenLlmConfig:() => { closeAll(); emit('open-llm-config'); },
      onOpenStats:   () => emit('open-stats'),
      onOpenDiag:    () => { closeAll(); emit('open-diag'); },
      onRebuildGraph:() => { closeAll(); emit('rebuild-graph'); },
      onConsolidate:  () => { closeAll(); emit('consolidate'); },
    };
  },
  template: /* html */ `
<header class="topbar">
  <div class="topbar-brand">
    <img src="static/img/logo-mark.svg"
         alt="Loop Memory"
         class="logo-mark logo-mark-dark" />
    <img src="static/img/logo-light.svg"
         alt="Loop Memory"
         class="logo-mark logo-mark-light" />
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

  <div class="group-right topbar-command-bar">
    <!--
      Model entry — always shows provider + status so users know
      (a) which model is in use, (b) whether an API key has been set,
      and (c) that clicking opens the configurator. The previous
      design had transparent background + transparent border, which
      made the entry visually disappear into the topbar.
    -->
    <button class="model-chip" id="model-chip"
            :data-reach="store.modelInfo.reachability"
            role="button" type="button"
            :title="modelChipTip"
            @click="onOpenLlmConfig">
      <span class="m-icon">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <circle cx="8" cy="8" r="3"/>
          <path d="M2.5 8h2M11.5 8h2M8 2.5v2M8 11.5v2"/>
          <path d="M3.8 3.8l1.4 1.4M10.8 10.8l1.4 1.4M3.8 12.2l1.4-1.4M10.8 5.2l1.4-1.4"/>
        </svg>
      </span>
      <span class="m-info">
        <span class="m-name">{{ store.modelInfo.model || 'rules' }}</span>
        <span class="m-dot" :data-reach="store.modelInfo.reachability" :aria-label="t('model.dot.' + store.modelInfo.reachability)">
          <span class="m-dot-inner"></span>
        </span>
      </span>
    </button>

    <div class="tb-command-group">
      <div class="tb-ingest">
        <button class="tb-action tb-ingest-trigger" :class="{ active: ingestOpen }"
                :title="t('action.ingestTip')" @click.stop="toggleIngest">
          <svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1l3.5 4H9v6H7V5H4.5L8 1zM2 13h12v1.5H2z"/></svg>
          <span>{{ t('action.ingest') }}</span>
          <svg class="caret" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M5 6.5L8 9.5l3-3"/></svg>
        </button>
        <IngestPopover v-show="ingestOpen" @close="ingestOpen=false" />
      </div>
    </div>

    <div class="tb-tools">
      <button class="tb-action tb-tools-trigger" :class="{ active: toolsOpen }"
              :title="t('topbar.toolsTip')" @click.stop="toggleTools">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 4h10M3 8h10M3 12h10"/><circle cx="6" cy="4" r="1.5" fill="var(--surface)"/><circle cx="10" cy="8" r="1.5" fill="var(--surface)"/><circle cx="7" cy="12" r="1.5" fill="var(--surface)"/></svg>
        <span>{{ t('topbar.tools') }}</span>
        <svg class="caret" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M5 6.5L8 9.5l3-3"/></svg>
      </button>
      <div class="tb-tools-menu" v-show="toolsOpen" @click.stop>
        <div class="tb-tools-heading">{{ t('topbar.maintenance') }}</div>
        <button class="tb-tools-item" @click="onRescore">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M13 8a5 5 0 11-1.5-3.6"/><path d="M10 2.5h3v3"/></svg>
          <span><b>{{ t('action.rescore') }}</b><small>{{ t('action.rescoreTip') }}</small></span>
        </button>
        <button class="tb-tools-item" @click="onConsolidate">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 4h10M4.5 8h7M6 12h4"/></svg>
          <span><b>{{ t('action.consolidate') }}</b><small>{{ t('topbar.consolidateTip') }}</small></span>
        </button>
        <button class="tb-tools-item" @click="onRebuildGraph">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="5.5"/><path d="M8 4.5v3.8l2.6 1.5"/></svg>
          <span><b>{{ t('topbar.rebuildGraph') }}</b><small>{{ t('topbar.rebuildGraphTip') }}</small></span>
        </button>
        <div class="tb-tools-heading">{{ t('topbar.system') }}</div>
        <button class="tb-tools-item" @click="onOpenDiag">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2.5 8h2l1.3-3 2.4 6 1.5-3H13.5"/></svg>
          <span><b>{{ t('topbar.doctor') }}</b><small>⌘D</small></span>
        </button>
      </div>
    </div>

    <div class="tb-utility-group">
      <div class="tb-seg lang" :title="t('topbar.language')">
        <button :class="{ active: store.lang === 'zh' }" @click="setLang('zh')">中</button>
        <button :class="{ active: store.lang === 'en' }" @click="setLang('en')">EN</button>
      </div>
      <div class="tb-seg theme" :title="t('topbar.theme')">
        <button :class="{ active: store.theme === 'auto' }" :title="t('topbar.themeAuto')" @click="setTheme('auto')">A</button>
        <button :class="{ active: store.theme === 'light' }" :title="t('topbar.themeLight')" @click="setTheme('light')">☀</button>
        <button :class="{ active: store.theme === 'dark' }" :title="t('topbar.themeDark')" @click="setTheme('dark')">☾</button>
      </div>
      <button class="icon-btn-circle settings-shortcut" :title="t('topbar.settingsTip')" :aria-label="t('action.settings')" @click="onOpenSettings">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <!-- cog/gear — distinct from the sun icon used for theme toggle -->
          <path d="M8 1.6l.6 1.4 1.4-.2.4 1.3 1.3.5-.2 1.4 1 .9-.7 1.2.6 1.2-1.2.6-.2 1.4-1.4.2-.5 1.3-1.3-.2-.9 1-1.2-.7-1.2.6-.6-1.2L2 11.7l-.2-1.4-1.3-.5.2-1.4-1-.9.7-1.2-.6-1.2 1.2-.6.2-1.4 1.4-.2.5-1.3 1.3.2.9-1z"/>
          <circle cx="8" cy="8" r="2.4"/>
        </svg>
      </button>
    </div>
  </div>
</header>
  `,
});
