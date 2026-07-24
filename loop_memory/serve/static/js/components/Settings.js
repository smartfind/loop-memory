/**
 * Settings — the right-side drawer that holds LLM config + scheduler.
 *
 * Faithful to the legacy vanilla-JS settings drawer (pre-Vue 8498eca):
 * - 5 sections: Provider, Schedule, Behaviour, Actions, Recent runs.
 * - Behaviour section lets the user tune batch size / temperature /
 *   max output / min importance / filter / score / summarise / dry-run.
 * - Recent runs section shows the latest 20 LLM runs with status pill,
 *   trigger, timestamp and stats summary.
 * - Drawer foot has Reset / Cancel / Save buttons (legacy parity).
 * - Schedule includes weekday selector (visible when mode=weekly).
 */
import { defineComponent, ref, computed, onMounted, watch, reactive } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, toast, fmtTime, callAction } from '../store.js';
import { api, ApiError } from '../api.js';

const WEEKDAYS = [
  { v: 0, k: 'weekday.mon' }, { v: 1, k: 'weekday.tue' },
  { v: 2, k: 'weekday.wed' }, { v: 3, k: 'weekday.thu' },
  { v: 4, k: 'weekday.fri' }, { v: 5, k: 'weekday.sat' },
  { v: 6, k: 'weekday.sun' },
];

export const Settings = defineComponent({
  name: 'Settings',
  props: {
    open: { type: Boolean, default: false },
    // Drawer mode:
    //   - "llm"     : only the LLM Connection section (entered via
    //                 the topbar model-chip — user clicked on the
    //                 model name to configure it).
    //   - "settings": full drawer — Connection + Ingest + Schedule
    //                 (entered via the gear icon — user wants to
    //                 tweak anything, not just the LLM).
    mode: { type: String, default: 'settings' },
  },
  emits: ['close'],
  setup(props, { emit }) {
    const providers = ref([]);
    const cfg = reactive({
      provider: 'rules', model: 'rules',
      base_url: '', api_key: '',
      api_key_set: false, api_key_account: '', api_key_fingerprint: '',
      schedule: {
        enabled: false, mode: 'off',
        interval_minutes: 60, hour: 3, minute: 0, weekday: 0,
        after_ingest_idle_sec: 30,
      },
      behaviour: {
        batch_size: 50, temperature: 0.3, max_output_tokens: 800,
        min_importance: 0.0, enable_filter: true, enable_score: true,
        enable_summarize: true, dry_run: false,
      },
    });
    // Ingest cadence — drives the background file watcher that
    // auto-ingests finished transcripts from Codex / Claude / Hermes
    // / OpenClaw. Kept SEPARATE from the LLM ``cfg`` block above
    // because:
    //   * it lives in a different settings key (``ingest`` not
    //     ``llm_consolidator``);
    //   * its save round-trip is independent (the watcher is a
    //     separate process that hot-reloads every few ticks);
    //   * it has no api_key handling so it doesn't share the LLM
    //     save flow's fingerprint / key lifecycle.
    const ingestCfg = reactive({
      idle_seconds: 300,    // size-stable wait before ingesting
      poll_seconds: 5,      // directory scan period
      defaults: { idle_seconds: 300, poll_seconds: 5 },
      notes: { min_idle_seconds: 30, min_poll_seconds: 1,
               max_idle_seconds: 3600, max_poll_seconds: 60 },
    });
    const ingestSaving = ref(false);
    const ingestHint = ref(false);
    // ---- Redaction ----
    // Default ON. Loaded from /api/admin/redact; persist via the
    // same endpoint. ``redactText`` is bound to the preview input
    // so the user can paste any text and see what would land in
    // long-term storage before actually writing it.
    const redactCfg = reactive({
      enabled: true,
      private_spans: true,
      kinds: [],
      notes: {},
    });
    const redactSaving = ref(false);
    const redactHint = ref(false);
    const redactPreview = reactive({
      input: '',
      output: '',
      counts: {},
      total: 0,
      total_chars: 0,
      busy: false,
    });
    const testing = ref(false);
    const testResult = ref(null);
    const saving = ref(false);
    const savedHint = ref(false);

    // Recent runs + next-run + preview state (legacy parity)
    const runs = ref([]);
    const runsLoading = ref(false);
    const nextRun = ref(null);
    const previewItems = ref([]);
    const previewLoading = ref(false);
    const previewOpen = ref(false);

    async function load() {
      let ing = null;
      try {
        const [p, c] = await Promise.all([
          api.llmProviders(), api.llmConfig(),
        ]);
        providers.value = p || [];
        // /api/admin/llm/config returns {config: {...}, warnings, ...}
        const actualCfg = (c && c.config) ? c.config : c;
        Object.assign(cfg, actualCfg);
      } catch (e) { /* ignore */ }
      try {
        ing = await api.getIngestConfig();
      } catch (e) { ing = null; }
      // Hydrate ingestCfg from the dedicated endpoint. Falls back to
      // the reactive defaults if the endpoint is unreachable (older
      // server builds, or first paint before loadI18n lands).
      if (ing && typeof ing === 'object') {
        if (typeof ing.idle_seconds === 'number') ingestCfg.idle_seconds = ing.idle_seconds;
        if (typeof ing.poll_seconds === 'number') ingestCfg.poll_seconds = ing.poll_seconds;
        if (ing.defaults && typeof ing.defaults === 'object') {
          ingestCfg.defaults = { ...ing.defaults };
        }
        if (ing.notes && typeof ing.notes === 'object') {
          ingestCfg.notes = { ...ing.notes };
        }
      }
      try {
        const rd = await api.getRedactConfig().catch(() => null);
        if (rd && typeof rd === 'object') {
          redactCfg.enabled = (rd.enabled !== false);
          redactCfg.private_spans = (rd.private_spans !== false);
          if (Array.isArray(rd.kinds)) redactCfg.kinds = rd.kinds;
          if (rd.notes) redactCfg.notes = rd.notes;
        }
      } catch (e) { /* ignore */ }
      try {
        const status = await api.llmStatus();
        if (status && status.provider) store.modelInfo = {
          provider: status.provider, model: status.model || 'rules',
          api_key_set: !!status.api_key_set, key_len: status.key_len || 0,
        };
        nextRun.value = status?.next_run || null;
      } catch (e) { /* ignore */ }
      await refreshRuns();
    }

    async function refreshRuns() {
      runsLoading.value = true;
      try {
        const data = await api.llmRuns({ limit: 20 });
        runs.value = Array.isArray(data) ? data : (data.runs || []);
      } catch (e) {
        runs.value = [];
      } finally {
        runsLoading.value = false;
      }
    }

    onMounted(load);
    watch(() => props.open, (o) => { if (o) load(); });

    const selectedProvider = computed(() => {
      return providers.value.find(p => p.id === cfg.provider) || {};
    });

    function onProviderChange() {
      const p = selectedProvider.value;
      if (p && p.default_model) cfg.model = p.default_model;
      if (p && p.default_base_url && !cfg.base_url) cfg.base_url = p.default_base_url;
    }

    async function onTest() {
      testing.value = true;
      testResult.value = null;
      try {
        const r = await api.llmTest({
          provider: cfg.provider, model: cfg.model, base_url: cfg.base_url,
          api_key: cfg.api_key || undefined,
        });
        testResult.value = r;
        if (r.ok) {
          toast(t('settings.test.ok', { ms: r.elapsed_ms || 0 }), 2500);
        } else {
          toast(t('settings.test.fail', { msg: r.error?.provider_message || r.error?.hint || 'unknown' }), 4000);
        }
      } catch (e) {
        testResult.value = { ok: false, error: { provider_message: e.message } };
      } finally {
        testing.value = false;
      }
    }

    async function onSave() {
      saving.value = true;
      try {
        const payload = {
          provider: cfg.provider, model: cfg.model, base_url: cfg.base_url,
          schedule: cfg.schedule, behaviour: cfg.behaviour,
        };
        if (cfg.api_key) payload.api_key = cfg.api_key;
        // Use the full PUT endpoint so the entire config tuple
        // (provider, model, base_url, schedule, behaviour, api_key)
        // is written atomically. The ``/api/admin/llm/schedule`` POST
        // endpoint only flat-merges keys into ``cfg.schedule``, so
        // sending the full form there would nest ``schedule`` and
        // ``behaviour`` under ``cfg.schedule.schedule`` /
        // ``cfg.schedule.behaviour`` and silently leave the top-level
        // ``enabled`` / ``mode`` flags stale — the "saved but still
        // shows unconfigured" persistence bug.
        await api.saveLlm(payload);
        savedHint.value = true;
        // Reload first so the in-memory ``cfg`` reflects what was
        // actually persisted (handles ``api_key`` fingerprint, etc.)
        // before we close the drawer.
        await load();
        toast(t('settings.saved') || t('common.saved') || '\u5df2\u4fdd\u5b58', 1800);
        cfg.api_key = '';
        // Auto-close the drawer so the user does not have to scroll
        // back to the top to hit the close button after saving.
        // The toast confirms the save; reopening shows the new state.
        setTimeout(() => {
          savedHint.value = false;
          emit('close');
        }, 700);
      } catch (e) {
        toast(t('common.error') + ': ' + e.message, 4000);
      } finally {
        saving.value = false;
      }
    }

    async function onSaveIngest() {
      ingestSaving.value = true;
      try {
        const r = await api.saveIngestConfig({
          idle_seconds: Number(ingestCfg.idle_seconds),
          poll_seconds: Number(ingestCfg.poll_seconds),
        });
        ingestHint.value = true;
        toast(t('settings.ingest.saved', {
          idle: ingestCfg.idle_seconds, poll: ingestCfg.poll_seconds,
        }), 2200);
        // Re-pull so any server-side normalization is reflected.
        if (r && r.ingest) {
          ingestCfg.idle_seconds = r.ingest.idle_seconds;
          ingestCfg.poll_seconds = r.ingest.poll_seconds;
        }
        setTimeout(() => { ingestHint.value = false; }, 2500);
      } catch (e) {
        toast(t('settings.ingest.saveFail', { msg: e.message }), 4000);
      } finally {
        ingestSaving.value = false;
      }
    }

    // ---- Redaction handlers ----
    async function onSaveRedact() {
      redactSaving.value = true;
      try {
        await api.saveRedactConfig({
          enabled: redactCfg.enabled,
          private_spans: redactCfg.private_spans,
          kinds: redactCfg.kinds,
        });
        redactHint.value = true;
        toast(t('settings.redact.saved'), 1800);
        setTimeout(() => { redactHint.value = false; }, 2500);
      } catch (e) {
        toast(t('settings.redact.saveFail', { msg: e.message }), 4000);
      } finally {
        redactSaving.value = false;
      }
    }

    async function onRunRedactPreview() {
      if (!redactPreview.input || !redactPreview.input.trim()) {
        toast(t('settings.redact.emptyPreview'), 2000);
        return;
      }
      redactPreview.busy = true;
      try {
        const r = await api.redactPreview({ text: redactPreview.input });
        redactPreview.output = r.text || '';
        redactPreview.counts = r.counts || {};
        redactPreview.total = r.total || 0;
        redactPreview.total_chars = r.total_chars || 0;
      } catch (e) {
        toast(t('settings.redact.saveFail', { msg: e.message }), 4000);
      } finally {
        redactPreview.busy = false;
      }
    }

    function clearRedactPreview() {
      redactPreview.input = '';
      redactPreview.output = '';
      redactPreview.counts = {};
      redactPreview.total = 0;
      redactPreview.total_chars = 0;
    }

    async function onClearKey() {
      if (!confirm(t('settings.apiKey.confirmClear'))) return;
      try {
        // ``__clear__`` is the sentinel the PUT endpoint understands:
        // it deletes the secret from the backend and flips
        // ``api_key_set`` to false. We must NOT use the
        // ``/api/admin/llm/schedule`` POST endpoint here — it
        // would re-introduce the same persistence bug as
        // ``onSave`` (nested ``schedule`` / ``behaviour``).
        const payload = {
          provider: cfg.provider, model: cfg.model, base_url: cfg.base_url,
          schedule: cfg.schedule, behaviour: cfg.behaviour,
          api_key: '__clear__',
        };
        await api.saveLlm(payload);
        cfg.api_key_set = false;
        toast(t('settings.apiKey.cleared'), 2000);
        await load();
      } catch (e) {
        toast(t('toast.fail', { msg: e.message }), 4000);
      }
    }

    async function onReset() {
      try {
        await fetch('/api/admin/llm/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provider: 'echo', model: 'rules',
            schedule: { enabled: false, mode: 'off' },
          }),
        });
        await load();
        toast(t('settings.saved'));
      } catch (e) {
        toast(t('toast.fail', { msg: e.message }), 4000);
      }
    }

    async function onRunNow() {
      try {
        await api.llmRun({});
        toast(t('action.runNowQueued') || t('action.llmRunQueued') || t('action.runNow'), 2000);
        setTimeout(refreshRuns, 1500);
      } catch (e) {
        toast(t('toast.fail', { msg: e.message }), 4000);
      }
    }

    async function onPreview() {
      previewOpen.value = true;
      previewLoading.value = true;
      previewItems.value = [];
      try {
        const r = await fetch('/api/admin/llm/run?dry_run=true&limit=20', { method: 'POST' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        previewItems.value = data.preview || [];
      } catch (e) {
        previewItems.value = [];
        toast(t('toast.fail', { msg: e.message }), 4000);
      } finally {
        previewLoading.value = false;
      }
    }

    function statusKey(s) { return 'settings.run.status.' + (s || ''); }
    function triggerKey(s) { return 'settings.run.trigger.' + (s || ''); }
    function statLine(stats) {
      if (!stats) return '';
      return t('settings.run.stats', {
        kept: stats.kept || 0,
        dropped: stats.dropped || 0,
        rescored: stats.importance_updated || 0,
        merged: stats.resummarized || 0,
      });
    }

    const WEEKDAY_NAMES_ZH = ['一','二','三','四','五','六','日'];
    const WEEKDAY_NAMES_EN = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    function nextRunText() {
      if ((cfg.schedule.mode || 'off') === 'off') {
        return t('settings.schedule.statusOff');
      }
      if (!nextRun.value) return '';
      const ts = nextRun.value;
      const d = new Date(ts * 1000);
      if (Number.isNaN(d.getTime())) return '';
      const when = fmtTime(ts);
      const mode = t('settings.schedule.' + (cfg.schedule.mode || 'off'));
      return t('settings.schedule.statusOn', { when, mode });
    }
    function scheduleModeHint() {
      const m = cfg.schedule.mode || 'off';
      const isZh = (store.lang || 'zh') === 'zh';
      const wdName = (isZh ? WEEKDAY_NAMES_ZH : WEEKDAY_NAMES_EN)[cfg.schedule.weekday || 0];
      if (m === 'realtime') return t('settings.schedule.realtimeHint', { sec: cfg.schedule.after_ingest_idle_sec || 30 });
      if (m === 'weekly')   return t('settings.schedule.weeklyHint',   { weekday: wdName, hour: cfg.schedule.hour, minute: String(cfg.schedule.minute).padStart(2,'0') });
      if (m === 'daily')    return t('settings.schedule.dailyHint',    { hour: cfg.schedule.hour, minute: String(cfg.schedule.minute).padStart(2,'0') });
      if (m === 'hourly')   return t('settings.schedule.hourlyHint');
      if (m === 'interval') return t('settings.schedule.intervalHint', { n: cfg.schedule.interval_minutes });
      return '';
    }

    // The UI no longer shows an explicit "enabled" checkbox — the
    // mode dropdown's 'off' option IS the disable. Auto-derive
    // schedule.enabled from mode so scheduler.py keeps seeing a
    // coherent state.
    watch(() => cfg.schedule.mode, (m) => {
      cfg.schedule.enabled = (m || 'off') !== 'off';
    }, { immediate: true });

    // --- storage budget + manual compact ---------------------------
    const storageCfg = reactive({
      max_bytes_mb: 200,
      max_memories: 8000,
      auto_compact: false,
      compact_interval_hours: 24,
      defaults: { max_bytes_mb: 200, max_memories: 8000, compact_interval_hours: 24 },
      current_mb: 0,
      current_memories: 0,
      last_compact_text: '—',
    });
    const storageSaving = ref(false);
    const storageHint = ref('');
    const storageCompacting = ref(false);
    const storageStats = ref({ memories: 0, db_size_bytes: 0 });
    const DEFAULT_STORAGE = {
      max_bytes_mb: 200, max_memories: 8000,
      auto_compact: false, compact_interval_hours: 24,
    };

    function bytesToMb(b) {
      if (!b) return 0;
      return Math.round((Number(b) / (1024 * 1024)) * 10) / 10;
    }
    function _fmtLastCompact(ts) {
      if (!ts) return t('settings.storage.never');
      try {
        const d = new Date(Number(ts) * 1000);
        return d.toLocaleString();
      } catch (_e) {
        return '—';
      }
    }

    async function refreshStorage() {
      try {
        const data = await api.getStorage();
        const budget = (data && data.budget) || {};
        storageCfg.max_bytes_mb = Math.round((Number(budget.max_bytes || DEFAULT_STORAGE.max_bytes_mb * 1024 * 1024) / (1024 * 1024)) || DEFAULT_STORAGE.max_bytes_mb);
        storageCfg.max_memories = Number(budget.max_memories || DEFAULT_STORAGE.max_memories);
        storageCfg.auto_compact = !!budget.auto_compact;
        storageCfg.compact_interval_hours = Number(budget.compact_interval_hours || DEFAULT_STORAGE.compact_interval_hours);
        const br = (data && data.breakdown) || {};
        storageCfg.current_mb = bytesToMb(br.db_size_bytes || data.db_size_bytes);
        storageCfg.current_memories = Number(br.memories || 0);
        storageStats.value = {
          memories: storageCfg.current_memories,
          db_size_bytes: Number(br.db_size_bytes || data.db_size_bytes || 0),
        };
        const last = (data && data.last_compact) || {};
        storageCfg.last_compact_text = _fmtLastCompact(last.finished_at);
      } catch (e) {
        // best-effort — leave defaults in place
      }
    }

    async function onSaveStorage() {
      storageSaving.value = true;
      storageHint.value = '';
      try {
        const payload = {
          max_bytes: Math.max(0, Math.round(Number(storageCfg.max_bytes_mb || 0) * 1024 * 1024)),
          max_memories: Math.max(0, Math.round(Number(storageCfg.max_memories || 0))),
          auto_compact: !!storageCfg.auto_compact,
          compact_interval_hours: Math.max(1, Math.round(Number(storageCfg.compact_interval_hours || 24))),
        };
        await api.saveStorageBudget(payload);
        storageHint.value = t('settings.storage.savedHint');
        setTimeout(() => { storageHint.value = ''; }, 2500);
        await refreshStorage();
      } catch (e) {
        storageHint.value = t('settings.storage.saveFailed', { error: String(e && e.message || e) });
      } finally {
        storageSaving.value = false;
      }
    }

    async function onRunCompact() {
      storageCompacting.value = true;
      storageHint.value = t('settings.storage.compacting');
      try {
        const r = await api.runCompact({ force: true, mode: "heuristic" });
        const result = (r && r.result) || r || {};
        const d = result.result || result;
        storageHint.value = t('settings.storage.compactDone', {
          deleted: d.deleted_memories || 0,
          sessions: d.digested_sessions || 0,
        });
        await refreshStorage();
      } catch (e) {
        storageHint.value = t('settings.storage.compactFailed', { error: String(e && e.message || e) });
      } finally {
        storageCompacting.value = false;
        setTimeout(() => { if (storageHint.value && storageHint.value.startsWith(t('settings.storage.compactDone', { deleted: 0, sessions: 0 }).slice(0, 5))) storageHint.value = ''; }, 4000);
      }
    }

    watch(() => props.open, (v) => { if (v) refreshStorage(); });

    function onClose() { emit('close'); }
    function openClientHooksPanel() {
      // Close the drawer first, then ask App.js to open the diagnostic
      // modal which now owns the per-client "Configure all" button.
      emit('close');
      try { callAction('openDiag'); } catch (_e) {}
    }

    return { cfg, providers, selectedProvider, onProviderChange,
             testing, testResult, onTest,
             saving, savedHint, onSave, onClearKey, onReset, onRunNow, onPreview,
             ingestCfg, ingestSaving, ingestHint, onSaveIngest,
             redactCfg, redactSaving, redactHint, onSaveRedact,
             redactPreview, onRunRedactPreview, clearRedactPreview,
             storageCfg, storageSaving, storageHint, onSaveStorage,
             storageCompacting, onRunCompact, storageStats,
             runs, runsLoading, refreshRuns, nextRun, nextRunText, scheduleModeHint,
             previewItems, previewLoading, previewOpen,
             statusKey, triggerKey, statLine,
             WEEKDAYS, store, t, onClose, openClientHooksPanel };
  },
  template: /* html */ `
<aside v-show="open" class="drawer" role="dialog" aria-label="Settings" @click.self="onClose">
  <div class="drawer-body">
    <header class="drawer-head">
      <div class="drawer-head-text">
        <h2>{{ t(mode === 'llm' ? 'settings.title.llm' : 'settings.title') }}</h2>
        <p class="drawer-subtitle">{{ t(mode === 'llm' ? 'settings.subtitle.llm' : 'settings.subtitle') }}</p>
      </div>
      <button class="icon-btn" @click="onClose" type="button" aria-label="Close">
        <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M3 3l10 10M13 3L3 13"/>
        </svg>
      </button>
    </header>

    <!-- Client integration entry point — discoverable from the top of
         settings so users find it without reading the README.
         Hidden in 'mode=llm' so the LLM config view stays focused. -->
    <button v-if="mode !== 'llm'" class="drawer-link-cta" type="button" @click="openClientHooksPanel"
            :title="t('settings.hooks.tooltip')">
      <span class="drawer-link-ico" aria-hidden="true">🪝</span>
      <span class="drawer-link-text">
        <strong>{{ t('settings.hooks.ctaTitle') }}</strong>
        <small>{{ t('settings.hooks.ctaSub') }}</small>
      </span>
      <span class="drawer-link-arrow" aria-hidden="true">→</span>
    </button>

    <!-- Connection (LLM info — global, used by every LLM feature) -->
    <section class="sec-connection">
      <h3>{{ t('settings.section.connection') }}</h3>
      <p class="sec-scope">{{ t('settings.connection.usedBy') }}</p>
      <label>
        <span>{{ t('settings.provider') }}</span>
        <select v-model="cfg.provider" @change="onProviderChange">
          <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.label }}</option>
        </select>
      </label>
      <p v-if="selectedProvider.description" class="hint">{{ selectedProvider.description }}</p>
      <label>
        <span>{{ t('settings.model') }}</span>
        <input v-model="cfg.model" :placeholder="selectedProvider.default_model || 'gpt-4o-mini'" />
      </label>
      <label v-if="selectedProvider.needs_base_url !== false">
        <span>{{ t('settings.baseUrl') }}</span>
        <input v-model="cfg.base_url" :placeholder="selectedProvider.default_base_url || ''" />
      </label>
      <label v-if="selectedProvider.needs_api_key !== false">
        <span>
          {{ t('settings.apiKey') }}
          <span v-if="cfg.api_key_set" class="key-status saved">
            <span class="key-dot"></span>{{ t('settings.apiKey.configured') }}
            <span v-if="cfg.api_key_fingerprint" class="key-fp">{{ cfg.api_key_fingerprint }}</span>
          </span>
          <span v-else class="key-status missing">
            <span class="key-dot"></span>{{ t('settings.apiKey.missing') }}
          </span>
        </span>
        <div class="api-key-row">
          <input type="password" v-model="cfg.api_key"
                 :placeholder="cfg.api_key_set ? t('settings.apiKey.edit') : t('settings.apiKey.placeholder')"
                 autocomplete="off" />
          <button class="btn small ghost" v-if="cfg.api_key_set" @click="onClearKey" type="button">
            {{ t('settings.apiKey.clear') }}
          </button>
        </div>
        <p class="hint">{{ t('settings.apiKey.hint') }}</p>
      </label>

      <div class="test-row">
        <button class="btn small primary" :disabled="testing" @click="onTest">
          {{ testing ? t('common.testing') : t('settings.test') }}
        </button>
        <span v-if="testResult" class="test-result" :class="{ ok: testResult.ok, fail: !testResult.ok }">
          {{ testResult.ok ? t('settings.test.ok', { ms: testResult.elapsed_ms || 0 }) : (testResult.error?.provider_message || testResult.error?.hint || 'failed') }}
        </span>
      </div>
    </section>

    <!-- Ingest — how often the background watcher scans / ingests.
         Hidden in 'mode=llm' (ingest cadence is independent of the LLM). -->
    <section v-if="mode !== 'llm'" class="sec-ingest">
      <h3>{{ t('settings.section.ingest') }}</h3>
      <p class="sec-scope">{{ t('settings.ingest.scope') }}</p>
      <div class="row-2">
        <label>
          <span>{{ t('settings.ingest.idle') }}</span>
          <input type="number" v-model.number="ingestCfg.idle_seconds"
                 :min="ingestCfg.notes.min_idle_seconds || 30"
                 :max="ingestCfg.notes.max_idle_seconds || 3600" />
          <small class="hint">
            {{ t('settings.ingest.idleHint', {
              def: ingestCfg.defaults.idle_seconds || 300,
              min: ingestCfg.notes.min_idle_seconds || 30,
              max: ingestCfg.notes.max_idle_seconds || 3600,
            }) }}
          </small>
        </label>
        <label>
          <span>{{ t('settings.ingest.poll') }}</span>
          <input type="number" v-model.number="ingestCfg.poll_seconds"
                 :min="ingestCfg.notes.min_poll_seconds || 1"
                 :max="ingestCfg.notes.max_poll_seconds || 60" />
          <small class="hint">
            {{ t('settings.ingest.pollHint', {
              def: ingestCfg.defaults.poll_seconds || 5,
            }) }}
          </small>
        </label>
      </div>
      <div class="action-row" style="gap:8px;flex-wrap:wrap;margin-top:8px;">
        <button class="btn primary" type="button" :disabled="ingestSaving"
                @click="onSaveIngest">
          {{ ingestSaving ? t('common.saving') : t('action.save') }}
        </button>
        <button class="btn ghost" type="button"
                @click="ingestCfg.idle_seconds = ingestCfg.defaults.idle_seconds;
                        ingestCfg.poll_seconds = ingestCfg.defaults.poll_seconds"
                :title="t('settings.ingest.resetToDefaults')">
          {{ t('settings.ingest.resetDefaults') }}
        </button>
        <span v-if="ingestHint" class="ingest-hint">{{ t('settings.ingest.liveHint') }}</span>
      </div>
    </section>

    <!-- Storage budget + compaction cadence. Hidden in 'mode=llm' —
         the LLM Connection drawer should not surface maintenance
         controls; compaction lives behind the gear icon. -->
    <section v-if="mode !== 'llm'" class="sec-storage">
      <h3>{{ t('settings.section.storage') }}</h3>
      <p class="sec-scope">{{ t('settings.storage.scope') }}</p>
      <div class="row-2">
        <label>
          <span>{{ t('settings.storage.maxBytes') }}</span>
          <input type="number" v-model.number="storageCfg.max_bytes_mb"
                 :min="0" :max="2048" />
          <small class="hint">{{ t('settings.storage.maxBytesHint', { def: storageCfg.defaults.max_bytes_mb, current: storageCfg.current_mb }) }}</small>
        </label>
        <label>
          <span>{{ t('settings.storage.maxMemories') }}</span>
          <input type="number" v-model.number="storageCfg.max_memories"
                 :min="0" :max="100000" />
          <small class="hint">{{ t('settings.storage.maxMemoriesHint', { def: storageCfg.defaults.max_memories, current: storageCfg.current_memories }) }}</small>
        </label>
      </div>
      <label class="row-toggle">
        <input type="checkbox" v-model="storageCfg.auto_compact" />
        <span>{{ t('settings.storage.autoCompact') }}</span>
      </label>
      <div v-if="storageCfg.auto_compact" class="row-2">
        <label>
          <span>{{ t('settings.storage.intervalHours') }}</span>
          <input type="number" v-model.number="storageCfg.compact_interval_hours"
                 :min="1" :max="720" />
          <small class="hint">{{ t('settings.storage.intervalHoursHint', { def: storageCfg.defaults.compact_interval_hours }) }}</small>
        </label>
        <div class="storage-meter">
          <span>{{ t('settings.storage.lastCompact') }}</span>
          <strong>{{ storageCfg.last_compact_text }}</strong>
        </div>
      </div>
      <div class="action-row" style="gap:8px;flex-wrap:wrap;margin-top:8px;">
        <button class="btn primary" type="button" :disabled="storageSaving"
                @click="onSaveStorage">
          {{ storageSaving ? t('common.saving') : t('action.save') }}
        </button>
        <button class="btn ghost" type="button" :disabled="storageCompacting"
                @click="onRunCompact">
          {{ storageCompacting ? t('settings.storage.compacting') : t('settings.storage.runCompact') }}
        </button>
        <span v-if="storageHint" class="ingest-hint">{{ storageHint }}</span>
      </div>
    </section>

    <!-- Redaction — secrets auto-stripped from stored memories and
         exported markdown. Hidden in 'mode=llm' (LLM Connection
         doesn't own privacy — redaction is project-wide). The
         preview textarea lets the user paste any text and see
         exactly what would land in long-term storage. -->
    <section v-if="mode !== 'llm'" class="sec-redact">
      <h3>{{ t('settings.section.redact') }}</h3>
      <p class="sec-scope">{{ t('settings.redact.scope') }}</p>
      <label class="row-toggle">
        <input type="checkbox" v-model="redactCfg.enabled" />
        <span>
          <strong>{{ t('settings.redact.enabled') }}</strong>
          <small>{{ t('settings.redact.enabledHint') }}</small>
        </span>
      </label>
      <label class="row-toggle">
        <input type="checkbox" v-model="redactCfg.private_spans" />
        <span>
          <strong>{{ t('settings.redact.privateSpans') }}</strong>
          <small>{{ t('settings.redact.privateSpansHint') }}</small>
        </span>
      </label>
      <details class="redact-kinds">
        <summary>{{ t('settings.redact.kindsSummary', { n: redactCfg.kinds.length }) }}</summary>
        <ul class="kind-list">
          <li v-for="k in redactCfg.kinds" :key="k">
            <code>{{ k }}</code>
          </li>
        </ul>
      </details>
      <div class="redact-preview">
        <label>
          <span>{{ t('settings.redact.previewLabel') }}</span>
          <textarea v-model="redactPreview.input" rows="3"
                    :placeholder="t('settings.redact.previewPlaceholder')"></textarea>
        </label>
        <div class="redact-preview-actions">
          <button class="btn small" type="button" :disabled="redactPreview.busy"
                  @click="onRunRedactPreview">
            {{ redactPreview.busy ? t('common.loading') : t('settings.redact.previewRun') }}
          </button>
          <button class="btn small ghost" type="button" @click="clearRedactPreview"
                  :disabled="!redactPreview.input && !redactPreview.output">
            {{ t('common.clear') }}
          </button>
        </div>
        <div v-if="redactPreview.output" class="redact-output">
          <div class="redact-output-text">
            <small class="hint">{{ t('settings.redact.previewResult') }}</small>
            <pre>{{ redactPreview.output }}</pre>
          </div>
          <div class="redact-output-meta" v-if="redactPreview.total > 0">
            <span class="redact-pill"
                  v-for="(n, kind) in redactPreview.counts" :key="kind">
              {{ kind }} × {{ n }}
            </span>
            <small class="hint">
              {{ t('settings.redact.previewTotal', { n: redactPreview.total, chars: redactPreview.total_chars }) }}
            </small>
          </div>
        </div>
      </div>
      <div class="action-row" style="gap:8px;flex-wrap:wrap;margin-top:8px;">
        <button class="btn primary" type="button" :disabled="redactSaving"
                @click="onSaveRedact">
          {{ redactSaving ? t('common.saving') : t('action.save') }}
        </button>
        <span v-if="redactHint" class="redact-hint">{{ t('settings.redact.liveHint') }}</span>
      </div>
    </section>

    <!-- Schedule — when the consolidation job auto-runs (LLM info above is global).
         Hidden in 'mode=llm' (schedule is independent of the LLM provider). -->
    <section v-if="mode !== 'llm'" class="sec-schedule">
      <h3>{{ t('settings.section.schedule') }}</h3>
      <p class="sec-scope">{{ t('settings.section.consolidationScope') }}</p>
      <label>
        <span>{{ t('settings.schedule.mode') }}</span>
        <select v-model="cfg.schedule.mode">
          <option value="off">{{ t('settings.schedule.off') }}</option>
          <option value="realtime">{{ t('settings.schedule.realtime') }}</option>
          <option value="hourly">{{ t('settings.schedule.hourly') }}</option>
          <option value="daily">{{ t('settings.schedule.daily') }}</option>
          <option value="weekly">{{ t('settings.schedule.weekly') }}</option>
          <option value="interval">{{ t('settings.schedule.everyN') }}</option>
        </select>
      </label>
      <p class="sched-hint mode-hint">{{ scheduleModeHint() || t('settings.schedule.offHint') }}</p>
      <label v-if="cfg.schedule.mode === 'interval'">
        <span>{{ t('settings.schedule.interval') }}</span>
        <input type="number" v-model.number="cfg.schedule.interval_minutes" min="1" max="1440" />
      </label>
      <div v-if="cfg.schedule.mode === 'daily' || cfg.schedule.mode === 'weekly'" class="row-2">
        <label>
          <span>{{ t('settings.schedule.hour') }}</span>
          <input type="number" v-model.number="cfg.schedule.hour" min="0" max="23" />
        </label>
        <label>
          <span>{{ t('settings.schedule.minute') }}</span>
          <input type="number" v-model.number="cfg.schedule.minute" min="0" max="59" />
        </label>
      </div>
      <label v-if="cfg.schedule.mode === 'weekly'">
        <span>{{ t('settings.schedule.weekday') }}</span>
        <select v-model.number="cfg.schedule.weekday">
          <option v-for="w in WEEKDAYS" :key="w.v" :value="w.v">{{ t(w.k) }}</option>
        </select>
      </label>
      <label v-if="cfg.schedule.mode === 'realtime'">
        <span>{{ t('settings.schedule.realtimeIdle') }}</span>
        <input type="number" v-model.number="cfg.schedule.after_ingest_idle_sec" min="5" max="600" />
      </label>
      <div class="sched-status" :class="{ on: cfg.schedule.enabled && (cfg.schedule.mode || 'off') !== 'off', off: !cfg.schedule.enabled || (cfg.schedule.mode || 'off') === 'off' }">
        <span class="dot"></span>
        <span class="text">{{ nextRunText() }}</span>
      </div>
    </section>

    <!-- Behaviour — consolidation-job-only knobs -->
    <!-- Behaviour — batch size / min importance / filters etc.
         Hidden in 'mode=llm'. -->
    <section v-if="mode !== 'llm'" class="sec-behaviour">
      <h3>{{ t('settings.section.behaviour') }}</h3>
      <p class="sec-scope">{{ t('settings.behaviour.scope') }}</p>
      <div class="row-2">
        <label>
          <span>{{ t('settings.batchSize') }}</span>
          <input type="number" v-model.number="cfg.behaviour.batch_size" min="1" max="500" />
        </label>
        <label>
          <span>{{ t('settings.temperature') }}</span>
          <input type="number" v-model.number="cfg.behaviour.temperature" step="0.1" min="0" max="2" />
        </label>
      </div>
      <div class="row-2">
        <label>
          <span>{{ t('settings.maxOutput') }}</span>
          <input type="number" v-model.number="cfg.behaviour.max_output_tokens" min="64" max="4096" />
        </label>
        <label>
          <span>{{ t('settings.minImp') }}</span>
          <input type="number" v-model.number="cfg.behaviour.min_importance" step="0.05" min="0" max="1" />
        </label>
      </div>
      <div class="behaviour-switches">
        <label class="switch">
          <input type="checkbox" v-model="cfg.behaviour.enable_filter" />
          <span>{{ t('settings.filter') }}</span>
        </label>
        <label class="switch">
          <input type="checkbox" v-model="cfg.behaviour.enable_score" />
          <span>{{ t('settings.score') }}</span>
        </label>
        <label class="switch">
          <input type="checkbox" v-model="cfg.behaviour.enable_summarize" />
          <span>{{ t('settings.summary') }}</span>
        </label>
        <label class="switch">
          <input type="checkbox" v-model="cfg.behaviour.dry_run" />
          <span>{{ t('settings.dryRun') }}</span>
        </label>
      </div>
    </section>

    <!-- Actions (Run now / Preview) -->
    <section>
      <h3>{{ t('settings.section.actions') }}</h3>
      <div class="action-row" style="gap:8px;flex-wrap:wrap;">
        <button class="btn primary" type="button" @click="onRunNow">{{ t('settings.runNow') }}</button>
        <button class="btn ghost" type="button" @click="onPreview">{{ t('settings.preview') }}</button>
      </div>
      <div v-if="previewOpen" style="margin-top:8px;">
        <div class="preview-list">
          <div v-if="previewLoading" class="preview-row" style="color:var(--text-faint);justify-content:center;">
            {{ t('common.loading') }}
          </div>
          <div v-else-if="!previewItems.length" class="preview-row" style="color:var(--text-faint);justify-content:center;">
            {{ t('settings.preview.empty') }}
          </div>
          <div v-for="p in previewItems" v-else :key="p.id" class="preview-row">
            <span class="badge" :class="p.would_drop ? 'drop' : 'keep'">
              {{ p.would_drop ? t('settings.drop') : t('settings.keep') }}
            </span>
            <span class="text" :title="p.text">{{ p.text }}</span>
            <span class="meta" style="color:var(--text-faint);">{{ Math.round((p.importance || 0) * 100) }}%</span>
          </div>
        </div>
      </div>
    </section>

    <!-- Recent runs -->
    <section>
      <h3>{{ t('settings.section.runs') }}</h3>
      <div v-if="runsLoading" class="status-line" style="font-size:11.5px;color:var(--text-faint);">
        {{ t('common.loading') }}
      </div>
      <div v-else-if="!runs.length" class="run-row" style="color:var(--text-faint);justify-content:center;">
        {{ t('settings.noRuns') }}
      </div>
      <div v-else class="run-list">
        <div v-for="r in runs" :key="r.id" class="run-row">
          <span class="pill" :class="r.status">{{ t(statusKey(r.status)) }}</span>
          <span class="meta">{{ t(triggerKey(r.trigger)) }} · {{ r.started_at ? new Date(r.started_at * 1000).toLocaleString() : '—' }}</span>
          <span class="stats">{{ statLine(r.stats) }}</span>
        </div>
      </div>
    </section>
  </div>

  <div class="drawer-foot">
    <button class="btn ghost" type="button" @click="onReset">{{ t('action.reset') }}</button>
    <div style="flex:1"></div>
    <button class="btn ghost" type="button" @click="onClose">{{ t('action.cancel') }}</button>
    <button class="btn primary" type="button" :disabled="saving" @click="onSave">
      {{ saving ? t('common.saving') : t('action.save') }}
    </button>
  </div>
</aside>
  `,
});
