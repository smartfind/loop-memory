/**
 * Timeline — the "memory list" view, the default tab.
 *
 * Shows a reverse-chronological list of memories with kind badges, scores,
 * source labels and inline tags. Search uses the unified `/api/recall`
 * endpoint so Chinese tokenisation + importance ranking both apply.
 *
 * Filters (kind, score, date range) live in component-local state. The
 * query string is passed straight to the API.
 */
import { defineComponent, ref, computed, onMounted, watch, h } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, escapeHtml, timeAgo } from '../store.js';
import { api } from '../api.js';

const KIND_LABELS = {
  episode:  'Episode',
  fact:     'Fact',
  rule:     'Rule',
  scratch:  'Scratch',
  summary:  'Summary',
};

export const Timeline = defineComponent({
  name: 'Timeline',
  setup() {
    const memories = ref([]);
    const recallMeta = ref(null);
    const loading = ref(false);
    const q = ref('');
    const kind = ref('');
    const minScore = ref(0);
    const since = ref('');
    const until = ref('');

    const sources = computed(() => {
      const set = new Set();
      for (const m of memories.value) {
        if (m.source) set.add(m.source);
      }
      return Array.from(set).sort();
    });

    async function refresh() {
      loading.value = true;
      recallMeta.value = null;
      try {
        let rows;
        if (q.value.trim()) {
          const r = await api.recall(q.value.trim(), 50);
          rows = (r.memories || []).map(m => ({ ...m, recall_score: m.score }));
          recallMeta.value = { wiki: r.wiki, entities: r.entities, tokens: r.tokens };
        } else {
          const data = await api.listMemories({
            kind: kind.value,
            min_score: minScore.value || undefined,
            since: since.value ? new Date(since.value).getTime() / 1000 : undefined,
            until: until.value ? new Date(until.value).getTime() / 1000 : undefined,
            limit: 200,
          });
          rows = Array.isArray(data) ? data : (data.memories || data.items || []);
        }
        // Apply client-side filters that the API doesn't expose yet
        if (kind.value) rows = rows.filter(r => r.kind === kind.value);
        if (minScore.value) rows = rows.filter(r => (r.score || r.importance || 0) >= Number(minScore.value));
        memories.value = rows;
      } catch (e) {
        memories.value = [];
      } finally {
        loading.value = false;
      }
    }

    function resetFilters() {
      q.value = ''; kind.value = ''; minScore.value = 0; since.value = ''; until.value = '';
      refresh();
    }

    function onSearchSubmit(e) {
      e.preventDefault();
      refresh();
    }

    function scoreFmt(s) {
      if (s == null) return '—';
      return (s * 100).toFixed(0) + '%';
    }

    function onClickMemory(m) {
      store.activeMemory = m.id;
    }

    onMounted(() => refresh());
    // Refresh on stats changes (new memories ingested)
    watch(() => store.stats.memories, refresh);

    return {
      store, t, memories, recallMeta, loading, q, kind, minScore, since, until,
      sources, KIND_LABELS,
      refresh, resetFilters, onSearchSubmit, scoreFmt, onClickMemory, timeAgo,
    };
  },
  template: /* html */ `
<div class="tab-pane" id="pane-timeline">
  <div class="tl-wrap">
    <form class="tl-toolbar" @submit="onSearchSubmit">
      <input class="tl-q" type="text" v-model="q" :placeholder="t('timeline.searchPlaceholder')" />
      <select v-model="kind" @change="refresh">
        <option value="">{{ t('timeline.allKinds') }}</option>
        <option value="episode">{{ t('kind.episode') }}</option>
        <option value="fact">{{ t('kind.fact') }}</option>
        <option value="rule">{{ t('kind.rule') }}</option>
        <option value="summary">{{ t('kind.summary') }}</option>
      </select>
      <input type="number" v-model.number="minScore" min="0" max="1" step="0.05" :placeholder="t('timeline.minScore')" @change="refresh" />
      <input type="date" v-model="since" @change="refresh" :title="t('timeline.since')" />
      <input type="date" v-model="until" @change="refresh" :title="t('timeline.until')" />
      <button type="button" class="tl-btn ghost" @click="resetFilters">{{ t('timeline.reset') }}</button>
    </form>

    <div v-if="recallMeta" class="recall-meta">
      <span v-if="recallMeta.wiki && recallMeta.wiki.length" class="recall-hint">
        {{ t('recall.wikiMatches', { n: recallMeta.wiki.length }) }}
      </span>
      <span v-if="recallMeta.entities && recallMeta.entities.length" class="recall-hint">
        {{ t('recall.entityMatches', { n: recallMeta.entities.length }) }}
      </span>
    </div>

    <div class="tl-list" v-if="memories.length">
      <article v-for="m in memories" :key="m.id" class="tl-card"
               :class="{ active: store.activeMemory === m.id }"
               @click="onClickMemory(m)">
        <div class="tl-head">
          <span class="src-badge" :data-source="m.source">{{ m.source || '—' }}</span>
          <span class="kind" :class="'kind-' + (m.kind || '')">{{ t('kind.' + (m.kind || 'episode')) }}</span>
          <span class="score" :title="t('timeline.score')">{{ scoreFmt(m.score ?? m.importance) }}</span>
          <span class="ts">{{ timeAgo(m.created_at) }}</span>
        </div>
        <div class="tl-body">{{ m.text }}</div>
        <div class="tl-tags" v-if="m.tags && m.tags.length">
          <span v-for="tag in m.tags" :key="tag" class="tag">#{{ tag }}</span>
        </div>
      </article>
    </div>
    <div class="empty" v-else-if="!loading">{{ t('timeline.empty') }}</div>
    <div class="loading" v-else>{{ t('common.loading') }}</div>
  </div>
</div>
  `,
});
