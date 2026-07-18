/**
 * Timeline — the "memory list" view, the default tab.
 *
 * Faithful to the legacy vanilla-JS renderTimeline (pre-Vue 8498eca):
 * - Each card shows: kind-icon (emoji), kind label, polish-spark ✨ if
 *   the memory was AI-distilled (updated_at > created_at), full kind
 *   label, source chip, relative time, full timestamp, score %, body,
 *   visual score-bar, importance %, tags, and a copy-to-clipboard
 *   action button.
 * - Search uses /api/recall so Chinese tokenisation + importance
 *   ranking both apply. Filters (kind, score, date range) live in
 *   component-local state.
 */
import { defineComponent, ref, computed, onMounted, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, escapeHtml, timeAgo, fmtTime, toast } from '../store.js';
import { api } from '../api.js';

const KIND_ICON = {
  fact: '#',
  episode: '⏵',
  plan: '✱',
  reflection: '✦',
  turn: '↻',
  rule: '§',
  summary: '∑',
  scratch: '·',
  concept: '◇',
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
      return (s * 100).toFixed(1) + '%';
    }

    function kindIcon(k) { return KIND_ICON[k] || '·'; }

    function isPolished(m) {
      // AI-distilled if the memory was rewritten after creation
      return m && m.updated_at && m.created_at && (m.updated_at - m.created_at) > 1;
    }

    async function onCopy(m) {
      try {
        await navigator.clipboard?.writeText(m.text || '');
        toast(t('toast.copied'));
      } catch (e) { /* ignore */ }
    }

    function onClickMemory(m) {
      store.activeMemory = m.id;
    }

    onMounted(() => refresh());
    watch(() => store.stats.memories, refresh);

    return {
      store, t, memories, recallMeta, loading, q, kind, minScore, since, until,
      refresh, resetFilters, onSearchSubmit, scoreFmt, kindIcon, isPolished,
      onCopy, onClickMemory, timeAgo, fmtTime, KIND_ICON,
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
        <option value="plan">{{ t('kind.plan') }}</option>
        <option value="reflection">{{ t('kind.reflection') }}</option>
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
      <article v-for="m in memories" :key="m.id"
               class="bubble kind-{{ m.kind || 'turn' }}"
               :class="{ active: store.activeMemory === m.id, polished: isPolished(m) }"
               :data-id="m.id"
               @click="onClickMemory(m)">
        <div class="head">
          <span class="kind-icon">{{ kindIcon(m.kind) }}</span>
          <span style="font-weight:600;color:var(--text-mute);">{{ t('kind.' + (m.kind || 'episode')) }}</span>
          <span v-if="isPolished(m)" class="polish-spark">✨ {{ store.lang === 'zh' ? '已浓缩' : 'AI' }}</span>
          <span>·</span>
          <span :title="fmtTime(m.created_at)">{{ timeAgo(m.created_at) }}</span>
          <span v-if="m.source">·</span>
          <span v-if="m.source" class="src-chip" :class="m.source">{{ m.source }}</span>
          <span style="flex:1"></span>
          <span>{{ scoreFmt(m.score ?? m.importance) }}</span>
        </div>
        <div class="text">{{ m.text }}</div>
        <div class="score-bar"><span :style="{ width: scoreFmt(m.score ?? m.importance) }"></span></div>
        <div class="foot">
          <span class="meta-item">{{ t('common.importance') }} <strong>{{ Math.round((m.importance || 0) * 100) }}%</strong></span>
          <span v-if="m.tags && m.tags.length" class="meta-item">
            <code>{{ m.tags.slice(0, 4).join(', ') }}</code>
          </span>
          <span class="meta-item" :title="fmtTime(m.created_at)">{{ fmtTime(m.created_at) }}</span>
        </div>
        <div class="actions">
          <button type="button" :title="t('timeline.copy')" @click.stop="onCopy(m)">⧉</button>
        </div>
      </article>
    </div>
    <div class="empty" v-else-if="!loading">{{ t('timeline.empty') }}</div>
    <div class="loading" v-else>{{ t('common.loading') }}</div>
  </div>
</div>
  `,
});
