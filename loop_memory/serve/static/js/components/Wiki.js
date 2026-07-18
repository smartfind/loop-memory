/**
 * Wiki — distilled knowledge pages.
 *
 * Cards in a responsive grid; each card shows title, summary, top bullets,
 * tags, importance, and last-updated. Click a card to open the editor
 * (or just expand inline). The legacy code had a 200-line renderWiki
 * function that hand-built HTML strings — Vue's template syntax is
 * significantly easier to scan and edit.
 */
import { defineComponent, ref, computed, onMounted, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, escapeHtml, fmtTime } from '../store.js';
import { api } from '../api.js';

export const Wiki = defineComponent({
  name: 'Wiki',
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

    function bulletsOf(p) {
      const lines = (p.body || '').split('\n');
      return lines.filter(l => l.startsWith('- ')).slice(0, 6);
    }

    function expand(id) { expanded.value = expanded.value === id ? null : id; }
    function edit(id) { editing.value = id; }

    function onNew() { editing.value = 'new'; }

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

    function bodyPreview(p) {
      const lines = (p.body || '').split('\n').filter(l => l.startsWith('- '));
      if (lines.length === 0) return p.body || '';
      return lines.slice(0, 3).join('\n');
    }

    onMounted(() => refresh());
    watch(() => store.stats.wiki_pages, refresh);

    return {
      store, t, pages, q, sort, loading, visible, expanded, editing, bulletsOf,
      refresh, expand, edit, onNew, saveEdit, removePage, bodyPreview, fmtTime,
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
