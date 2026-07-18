/**
 * Wiki — distilled knowledge pages.
 *
 * Cards in a responsive grid; each card shows title, summary, top bullets,
 * tags, importance, and last-updated. Click a card to open the editor
 * (or just expand inline). The legacy code had a 200-line renderWiki
 * function that hand-built HTML strings — Vue's template syntax is
 * significantly easier to scan and edit.
 */
import { defineComponent, ref, computed, onMounted, onUnmounted, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, escapeHtml, fmtTime } from '../store.js';
import { api } from '../api.js';
import { WikiEditor } from './WikiEditor.js';

export const Wiki = defineComponent({
  name: 'Wiki',
  components: { WikiEditor },
  setup() {
    const pages = ref([]);
    const q = ref('');
    const sort = ref('updated_desc');
    const loading = ref(false);
    const expanded = ref(null);
    const editing = ref(null);

    async function refresh() {
      loading.value = true;
      try {
        const data = await api.listWiki();
        pages.value = Array.isArray(data) ? data : (data.pages || []);
      } catch (e) {
        pages.value = [];
      } finally {
        loading.value = false;
      }
    }

    const visible = computed(() => {
      let rows = pages.value;
      if (q.value.trim()) {
        const needle = q.value.trim().toLowerCase();
        rows = rows.filter(p =>
          (p.title || '').toLowerCase().includes(needle) ||
          (p.summary || '').toLowerCase().includes(needle) ||
          (p.slug || '').toLowerCase().includes(needle));
      }
      const cmp = (a, b) => {
        if (sort.value === 'updated_desc') return (b.updated_at || 0) - (a.updated_at || 0);
        if (sort.value === 'importance_desc') return (b.importance || 0) - (a.importance || 0);
        if (sort.value === 'title_asc') return (a.title || '').localeCompare(b.title || '');
        return 0;
      };
      return [...rows].sort(cmp);
    });

    /**
     * Extract every bullet ("- ...") from a wiki body so the card can show the
     * full list. Previously this was capped at the first 6 lines, which made
     * freshly-distilled pages look truncated. The new distillation policy is
     * "completeness over compactness", so the card needs to surface every
     * atomic fact the LLM produced.
     */
    function bulletsOf(p) {
      const lines = (p.body || '').split('\n');
      return lines.filter(l => l.startsWith('- '));
    }

    function expand(id) { expanded.value = expanded.value === id ? null : id; }
    function edit(id) { editing.value = id; }

    function onNew() { editing.value = 'new'; }

    const importing = ref(false);
    const exporting = ref(false);

    async function onExport() {
      exporting.value = true;
      try {
        const res = await fetch('/api/wiki/export?format=json&limit=2000');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
        a.href = url;
        a.download = `loop-memory-wiki-${ts}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      } catch (e) {
        toast(t('wiki.exportFail', { msg: e.message }), 4000);
      } finally {
        exporting.value = false;
      }
    }

    function onImportClick() {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,.md,.markdown,application/json,text/markdown,text/plain';
      input.addEventListener('change', async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        importing.value = true;
        try {
          const text = await file.text();
          let body;
          if (/\.md(?:arkdown)?$/i.test(file.name) || text.trim().startsWith('#')) {
            body = { format: 'markdown', markdown: text };
          } else {
            try {
              const parsed = JSON.parse(text);
              const pages = Array.isArray(parsed) ? parsed : (parsed.pages || []);
              body = { format: 'json', pages };
            } catch (e) {
              throw new Error('not a valid JSON file: ' + e.message);
            }
          }
          const r = await fetch('/api/wiki/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!r.ok) {
            const txt = await r.text();
            throw new Error(`HTTP ${r.status} — ${txt.slice(0, 200)}`);
          }
          const out = await r.json();
          if (out.total === 0) {
            toast(t('wiki.importEmpty'), 3000);
          } else {
            toast(t('wiki.importSuccess', {
              created: out.created, updated: out.updated, skipped: out.skipped,
            }), 3500);
            await refresh();
          }
        } catch (e) {
          toast(t('wiki.importError', { msg: e.message }), 4000);
        } finally {
          importing.value = false;
        }
      });
      input.click();
    }

    async function saveEdit(payload) {
      try {
        if (editing.value === 'new') {
          await api.createWiki(payload);
        } else {
          await api.updateWiki(editing.value, payload);
        }
        editing.value = null;
        await refresh();
      } catch (e) {
        alert(t('wiki.saveError') + ': ' + e.message);
      }
    }

    async function removePage(p) {
      if (!confirm(t('wiki.confirmDelete'))) return;
      try {
        await api.deleteWiki(p.id);
        await refresh();
      } catch (e) {
        alert(t('wiki.deleteError') + ': ' + e.message);
      }
    }

    async function openWikiBySlug(slug) {
      if (!slug) return;
      try {
        const list = await api.listWiki();
        const found = (list || []).find(x => x.slug === slug || x.title === slug);
        if (found) editing.value = found.id;
      } catch (e) { /* ignore */ }
    }
    function onOpenWiki(e) {
      const slug = (e && e.detail && e.detail.slug) || '';
      store.activeTab = 'wiki';
      openWikiBySlug(slug);
    }
    onMounted(() => {
      refresh();
      window.addEventListener('loop-memory:open-wiki', onOpenWiki);
    });
    onUnmounted(() => {
      window.removeEventListener('loop-memory:open-wiki', onOpenWiki);
    });
    watch(() => store.stats.wiki_pages, refresh);
    // When the user navigates back to the wiki tab, refresh in case the
    // list went stale (distillation may have added/removed pages).
    watch(() => store.activeTab, (id) => { if (id === 'wiki') refresh(); });

    return {
      store, t, pages, q, sort, loading, visible, expanded, editing, bulletsOf,
      refresh, expand, edit, onNew, onExport, onImportClick, importing, exporting, saveEdit, removePage, fmtTime,
    };
  },
  template: /* html */ `
<div class="tab-pane" id="pane-wiki">
  <div class="wiki-wrap">
    <div class="wiki-toolbar">
      <input class="wiki-q" type="text" v-model="q" :placeholder="t('wiki.searchPlaceholder')" />
      <select v-model="sort">
        <option value="updated_desc">{{ t('wiki.sort.updated') }}</option>
        <option value="importance_desc">{{ t('wiki.sort.importance') }}</option>
        <option value="title_asc">{{ t('wiki.sort.title') }}</option>
      </select>
      <span class="spacer"></span>
      <button class="tb-action ghost" :title="t('wiki.exportTip')" :disabled="exporting" @click="onExport">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M8 1v9M4.5 6.5L8 10l3.5-3.5M2 12v2.5h12V12"/></svg>
        <span>{{ exporting ? '…' : t('wiki.export') }}</span>
      </button>
      <button class="tb-action ghost" :title="t('wiki.importTip')" :disabled="importing" @click="onImportClick">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M8 15V6M4.5 9.5L8 6l3.5 3.5M2 2.5V0h12v2.5"/></svg>
        <span>{{ importing ? '…' : t('wiki.import') }}</span>
      </button>
      <button class="tb-action primary" @click="onNew">
        <svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14"><path d="M8 1v14M1 8h14" stroke="currentColor" stroke-width="2"/></svg>
        <span>{{ t('wiki.new') }}</span>
      </button>
    </div>

    <div v-if="visible.length" class="wiki-grid">
      <article v-for="p in visible" :key="p.id" class="wiki-card">
        <div class="wc-head">
          <h3 class="wc-title">{{ p.title || p.slug }}</h3>
          <span class="wc-imp" :title="t('wiki.importance')">
            {{ Math.round((p.importance || 0) * 100) }}%
          </span>
        </div>
        <div class="wc-summary">{{ p.summary }}</div>
        <ul class="wc-bullets">
          <li v-for="(b, i) in bulletsOf(p)" :key="i">{{ b.replace(/^-\\s*/, '') }}</li>
        </ul>
        <div class="wc-tags" v-if="p.tags && p.tags.length">
          <span v-for="tag in p.tags" :key="tag" class="tag">#{{ tag }}</span>
        </div>
        <div class="wc-foot">
          <span class="wc-meta">
            <span :data-source="p.tags && p.tags.includes('fact') ? 'fact' : 'episode'">
              {{ fmtTime(p.updated_at) }}
            </span>
          </span>
          <div class="wc-actions">
            <button class="wc-btn" @click="expand(p.id)">
              {{ expanded === p.id ? t('action.close') : t('wiki.preview') }}
            </button>
            <button class="wc-btn" @click="edit(p.id)">{{ t('action.edit') }}</button>
            <button class="wc-btn danger" @click="removePage(p)">{{ t('action.delete') }}</button>
          </div>
        </div>
        <pre class="wc-body" v-if="expanded === p.id">{{ p.body }}</pre>
      </article>
    </div>
    <div class="empty" v-else-if="!loading">{{ t('wiki.empty') }}</div>
    <div class="loading" v-else>{{ t('common.loading') }}</div>

    <WikiEditor v-if="editing"
                :page-id="editing"
                @save="saveEdit"
                @cancel="editing = null" />
  </div>
</div>
  `,
});
