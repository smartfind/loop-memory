/**
 * IngestPopover — choose which conversation sources to ingest.
 *
 * Why: the old TopBar 导入 button called /api/ingest with source=manual
 * (404 + invalid source), so it never worked. This popover lets the user
 * pick one or more of the registered loaders (codex / claude / hermes /
 * openclaw) and runs them, surfacing per-source results.
 */
import { defineComponent, ref, computed } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
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

    return {
      SOURCES, store, t,
      selected, running, progress, results,
      toggle, selectAll, clearSelection, runAll,
      sourceDisabled,
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
    <label v-for="src in SOURCES" :key="src.id"
           class="ingest-row" :class="{ dim: sourceDisabled(src.id) }">
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
