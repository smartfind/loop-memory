/**
 * App — top-level shell that owns the global layout.
 *
 * Renders: TopBar, Sidebar, Tabs, the active tab pane, Settings drawer,
 * RunStrip, Toast, Diagnostic modal. Listens for cross-component events
 * (ingest, rescore, llm-run, run-now, rebuild-graph) and calls the API.
 *
 * The store is the cross-component bus: every component reads from it
 * (lang, theme, stats, runStatus, activeTab) and a few write to it
 * (TopBar writes stats, TopBar writes modelInfo, App writes runStatus).
 * Components DO NOT call each other directly — the App listens to user
 * events emitted by TopBar and orchestrates API calls.
 */
import { defineComponent, ref, computed, onMounted, onUnmounted, watch, nextTick } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, applyTheme, applyLang, loadI18n, toast } from './store.js';
import { api } from './api.js';

import { TopBar } from './components/TopBar.js';
import { Sidebar } from './components/Sidebar.js';
import { Tabs } from './components/Tabs.js';
import { Timeline } from './components/Timeline.js';
import { Dashboard } from './components/Dashboard.js';
import { Wiki } from './components/Wiki.js';
import { KnowledgeGraph } from './components/KnowledgeGraph.js';
import { Settings } from './components/Settings.js';
import { RunStrip } from './components/RunStrip.js';
import { Toast } from './components/Toast.js';
import { Diagnostic } from './components/Diagnostic.js';

export const App = defineComponent({
  name: 'App',
  components: { TopBar, Sidebar, Tabs, Timeline, Dashboard, Wiki, KnowledgeGraph, Settings, RunStrip, Toast, Diagnostic },
  setup() {
    const settingsOpen = ref(false);
    const diagOpen = ref(false);
    let statusPoll = null;
    let modelPoll = null;

    async function refreshStats() {
      try {
        const data = await api.stats();
        store.stats = {
          ...store.stats,
          memories: data.memories, sessions: data.sessions,
          wiki_pages: data.wiki_pages || 0, avg_score: data.avg_score,
          graph: data.entities ? `${data.entities}/${data.entities}` : '0/0',
          dbPath: data.path,
        };
      } catch (e) { /* ignore */ }
    }

    async function refreshRunStatus() {
      try {
        const r = await api.llmStatus();
        store.runStatus = r || store.runStatus;
        store.modelInfo = {
          provider: r?.provider || 'rules',
          model: r?.model || 'rules',
          api_key_set: !!r?.api_key_set,
          key_len: r?.key_len || 0,
        };
        if (r?.last_run && r.last_run !== store.lastRunId) {
          store.lastRunId = r.last_run;
          refreshStats();
        }
      } catch (e) { /* ignore */ }
    }

    function readTabFromUrl() {
      try {
        const usp = new URLSearchParams(location.search);
        const tab = usp.get('tab');
        if (tab && ['timeline', 'dashboard', 'wiki', 'graph'].includes(tab)) {
          store.activeTab = tab;
        }
      } catch (e) { /* ignore */ }
    }

    onMounted(async () => {
      applyTheme();
      applyLang();
      readTabFromUrl();
      await loadI18n();
      applyTheme();
      applyLang();
      store.ready = true;
      refreshStats();
      refreshRunStatus();
      statusPoll = setInterval(refreshRunStatus, 3000);
      modelPoll = setInterval(refreshStats, 8000);
      // Cmd+D shortcut — open Doctor diagnostic modal.
      window.addEventListener('keydown', onGlobalKeydown);
    });

    onUnmounted(() => {
      if (statusPoll) clearInterval(statusPoll);
      if (modelPoll) clearInterval(modelPoll);
      window.removeEventListener('keydown', onGlobalKeydown);
    });

    function onGlobalKeydown(e) {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'd' || e.key === 'D')) {
        e.preventDefault();
        diagOpen.value = true;
      }
    }

    // --- Action handlers ---
    async function onIngest() {
      try {
        const r = await api.ingest({ source: 'manual' });
        toast(t('action.ingestStarted', { n: r.ingested || 0 }), 2500);
        refreshStats();
      } catch (e) { toast(t('common.error') + ': ' + e.message, 4000); }
    }
    async function onRescore() {
      try {
        await api.rescore();
        toast(t('action.rescoreDone'), 2000);
        refreshStats();
      } catch (e) { toast(t('common.error') + ': ' + e.message, 4000); }
    }
    async function onLlmRun() {
      try {
        const r = await api.llmRun({});
        if (r.queued) toast(t('action.llmRunQueued'), 2000);
      } catch (e) { toast(t('common.error') + ': ' + e.message, 4000); }
    }
    async function onRunNow() {
      try { await api.llmRun({ force: true }); toast(t('action.runNowQueued'), 2000); }
      catch (e) { toast(t('common.error') + ': ' + e.message, 4000); }
    }
    function onOpenSettings() { settingsOpen.value = true; }
    function onOpenDiag() { diagOpen.value = true; }
    function onOpenStats() { /* legacy stats popover — delegated to TopBar */ }

    async function onRebuildGraph() {
      // Switch to the graph tab first so the user sees progress; the
      // KnowledgeGraph component itself owns the rebuild request now.
      store.activeTab = 'graph';
      // Wait a tick so the KG is mounted, then trigger its handler.
      await nextTick();
      window.dispatchEvent(new CustomEvent('loop-memory:rebuild-graph'));
    }

    async function onConsolidate() {
      // Trigger LLM-driven consolidation (a.k.a. AI Run) — uses the same
      // endpoint as the topbar's "AI Run" button, just navigated via kebab.
      try {
        const r = await api.llmRun({});
        if (r.queued) toast(t('action.llmRunQueued'), 2000);
      } catch (e) {
        toast(t('common.error') + ': ' + e.message, 4000);
      }
    }

    async function onOpenWiki(payload) {
      // From graph dblclick: switch to wiki tab and request the editor to open.
      store.activeTab = 'wiki';
      await nextTick();
      window.dispatchEvent(new CustomEvent('loop-memory:open-wiki', { detail: payload || {} }));
    }

    return {
      store, t, settingsOpen, diagOpen,
      onIngest, onRescore, onLlmRun, onRunNow,
      onOpenSettings, onOpenStats, onOpenDiag,
      onRebuildGraph, onOpenWiki,
      dismissStrip: () => { store.stripDismissed = true; },
    };
  },
  template: /* html */ `
<div class="app-shell">
  <TopBar @ingest="onIngest" @rescore="onRescore"
          @llm-run="onLlmRun" @run-now="onRunNow"
          @open-settings="onOpenSettings" @open-diag="onOpenDiag"
          @rebuild-graph="onRebuildGraph"
          @consolidate="onConsolidate" />
  <div class="app-body">
    <Sidebar />
    <main class="content">
      <Tabs />
      <div class="tab-panes">
        <Timeline v-show="store.activeTab === 'timeline'" />
        <Dashboard v-show="store.activeTab === 'dashboard'" />
        <Wiki v-show="store.activeTab === 'wiki'" />
        <KnowledgeGraph v-show="store.activeTab === 'graph'"
                        @open-wiki="onOpenWiki" />
      </div>
    </main>
  </div>
  <RunStrip @dismiss="dismissStrip" />
  <Settings :open="settingsOpen" @close="settingsOpen = false" />
  <Diagnostic :open="diagOpen" @close="diagOpen = false" />
  <Toast />
</div>
  `,
});
