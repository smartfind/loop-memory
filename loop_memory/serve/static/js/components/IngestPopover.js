/**
 * IngestPopover — choose which conversation sources to ingest.
 *
 * Why: the old TopBar 导入 button called /api/ingest with source=manual
 * (404 + invalid source), so it never worked. This popover lets the user
 * pick one or more of the registered loaders (codex / claude / hermes /
 * openclaw) and runs them, surfacing per-source results.
 */
import { defineComponent, ref, computed, onMounted, onUnmounted, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, toast } from '../store.js';
import { api } from '../api.js';

const SOURCES = [
  { id: 'codex',   label: 'Codex',   desc: '~/.codex/sessions',     icon: '◧' },
  { id: 'openclaw', label: 'OpenClaw', desc: '~/.openclaw  (clawx)', icon: '✜' },
  { id: 'claude',  label: 'Claude',  desc: '~/.claude',             icon: '◎' },
  { id: 'hermes',  label: 'Hermes',  desc: '~/.hermes',             icon: '⚒' },
];

export const IngestPopover = defineComponent({
  name: 'IngestPopover',
  emits: ['close'],
  setup(_, { emit }) {
    // Default selection: only sources present in the local filesystem.
    const selected = ref(new Set());
    const running = ref(false);
    const progress = ref('');     // human-readable status
    const results = ref(null);    // last batch results {source: {files, root, error}}

    // Per-source active-session cache. The "⚡ ingest now" affordance
    // is per-source rather than a separate dropdown, so the layout
    // stays one coherent checklist instead of two competing panels.
    const activeBySource = ref({});     // { codex: {name,size,mtime,age_seconds,path} | null, ... }
    const forcePending = ref(new Set()); // source ids currently being force-ingested

    async function refreshAllActive() {
      const out = {};
      await Promise.all(SOURCES.map(async (s) => {
        try {
          const r = await api.activeSession(s.id);
          out[s.id] = (r && r.active) || null;
        } catch (_e) { out[s.id] = null; }
      }));
      activeBySource.value = out;
    }

    async function forceActive(sourceId) {
      if (forcePending.value.has(sourceId)) return;
      const next = new Set(forcePending.value);
      next.add(sourceId); forcePending.value = next;
      try {
        const r = await api.forceIngest({
          source: sourceId,
          active_only: 'true',
        });
        const ok = r && r.ingested ? r.ingested : 0;
        const err = r && r.errors ? r.errors : 0;
        toast(
          ok > 0
            ? `立即摄入完成 · ${SOURCES.find(s=>s.id===sourceId)?.label || sourceId} · ${ok} 个会话`
            : (err > 0 ? `摄入失败: ${err} 个错误` : '当前活跃会话暂无新内容'),
          2800,
        );
        window.dispatchEvent(new CustomEvent('loop-memory:ingest-done', { detail: r }));
        store._refreshStats = (store._refreshStats || 0) + 1;
        await refreshAllActive();
      } catch (e) {
        toast(`摄入失败: ${e.message || e}`, 3500);
      } finally {
        const m = new Set(forcePending.value); m.delete(sourceId); forcePending.value = m;
      }
    }

    onMounted(() => {
      refreshAllActive();
      // Re-poll every 30s so the active badge stays accurate while
      // the popover stays open.
      const t = setInterval(refreshAllActive, 30000);
      // Clean up when component unmounts.
      _cleanupTimer = t;
    });
    let _cleanupTimer = null;
    onUnmounted(() => { if (_cleanupTimer) clearInterval(_cleanupTimer); });

    function toggle(id) {
      const s = new Set(selected.value);
      if (s.has(id)) s.delete(id); else s.add(id);
      selected.value = s;
    }
    function selectAll() {
      selected.value = new Set(SOURCES.map(s => s.id));
    }
    function clearSelection() {
      selected.value = new Set();
    }

    function sourceDisabled(id) {
      return running.value && !selected.value.has(id);
    }

    async function runAll() {
      if (running.value) return;
      const ids = [...selected.value];
      if (!ids.length) {
        toast(t('action.ingestNoSource') || '请先选择至少一个数据源', 2200);
        return;
      }
      running.value = true;
      results.value = null;
      const out = {};
      let total = 0;
      try {
        for (const id of ids) {
          progress.value = `正在导入 ${id}…`;
          try {
            const r = await api.ingest(id);
            out[id] = r || { files: 0 };
            total += Number(r?.files || r?.ingested || 0);
          } catch (e) {
            out[id] = { error: e?.message || 'failed' };
          }
        }
        results.value = out;
        progress.value = `导入完成 · ${total} 个会话`;
        toast((t('action.ingestStarted', { n: total }) || `导入完成 · ${total} 个`), 2400);
        // Sidebar refresh hook: emit a global event the Sidebar listens to.
        window.dispatchEvent(new CustomEvent('loop-memory:ingest-done', { detail: out }));
        store._refreshStats = (store._refreshStats || 0) + 1;
      } finally {
        running.value = false;
      }
    }

    function formatSize(bytes) {
      if (!bytes && bytes !== 0) return '—';
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024*1024) return (bytes/1024).toFixed(0) + ' KB';
      return (bytes/1024/1024).toFixed(1) + ' MB';
    }
    function formatAge(seconds) {
      if (seconds == null) return '—';
      if (seconds < 60) return Math.round(seconds) + 's 前';
      if (seconds < 3600) return Math.round(seconds/60) + 'm 前';
      return Math.round(seconds/3600) + 'h 前';
    }

    return {
      SOURCES, store, t,
      selected, running, progress, results,
      activeBySource, forcePending, forceActive,
      toggle, selectAll, clearSelection, runAll,
      sourceDisabled,
      formatSize, formatAge,
    };
  },
  template: /* html */ `
<div class="tb-ingest-menu" @click.stop>
  <div class="ingest-head">
    <div class="ingest-title">{{ t('action.ingest') }}</div>
    <button class="ingest-close" @click="$emit('close')" aria-label="close">×</button>
  </div>
  <div class="ingest-sub">{{ t('action.ingestTip') }}</div>

  <div class="ingest-list">
    <div v-for="src in SOURCES" :key="src.id"
         class="ingest-row" :class="{ dim: sourceDisabled(src.id), active: activeBySource[src.id], sel: selected.has(src.id) }">
      <label class="ingest-row-main">
        <input type="checkbox"
               :checked="selected.has(src.id)"
               :disabled="sourceDisabled(src.id)"
               @change="toggle(src.id)" />
        <span class="ingest-ico">{{ src.icon }}</span>
        <span class="ingest-name">{{ src.label }}</span>
        <span class="ingest-path">{{ src.desc }}</span>
        <span v-if="results && results[src.id] && results[src.id].error" class="ingest-status err">×</span>
        <span v-else-if="results && results[src.id]" class="ingest-status ok">{{ results[src.id].files || 0 }}</span>
      </label>
      <!-- contextual "ingest active now" affordance: only appears when
           this source has an active session. Lives inline with the row
           instead of as a separate competing CTA at the top. -->
      <div v-if="activeBySource[src.id]" class="ingest-active" :title="activeBySource[src.id].path">
        <span class="ingest-active-name">{{ activeBySource[src.id].name }}</span>
        <span class="ingest-active-meta">
          {{ formatSize(activeBySource[src.id].size) }} ·
          {{ formatAge(activeBySource[src.id].age_seconds) }}
        </span>
        <button type="button" class="ingest-active-run"
                :disabled="forcePending.has(src.id)"
                @click.stop="forceActive(src.id)"
                :title="t('action.forceTip') || '立即把当前活跃会话的更新摄入'">
          <span v-if="forcePending.has(src.id)">…</span>
          <span v-else>⚡</span>
        </button>
      </div>
    </div>
  </div>

  <div class="ingest-bar">
    <button type="button" class="ingest-mini ghost" @click="selectAll" :disabled="running">{{ t('action.ingestAll') || '全选' }}</button>
    <button type="button" class="ingest-mini ghost" @click="clearSelection" :disabled="running">{{ t('action.ingestNone') || '清空' }}</button>
    <button type="button" class="ingest-run" :disabled="running || selected.size === 0" @click="runAll">
      <span v-if="running">{{ progress || '…' }}</span>
      <span v-else>{{ t('action.ingestRun') }} ({{ selected.size }})</span>
    </button>
  </div>
</div>
  `,
});
