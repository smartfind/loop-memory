/**
 * Sidebar — left-rail session list.
 *
 * Improvements over the previous version:
 *   - Source filter is a pill strip (Codex / Claude / Hermes / OpenClaw
 *     / All) instead of a dropdown; the count badge next to each pill
 *     tells you how many sessions are in that bucket.
 *   - Time labels use ended_at (which is what the API actually returns)
 *     instead of the missing `last_seen` field.
 *   - Limit raised from 100 → 300 so Codex sessions aren't hidden
 *     under heavier OpenClaw traffic.
 *   - Auto-refresh whenever the global ingest event fires so users see
 *     new Codex/OpenClaw sessions land without a manual reload.
 *   - Visual polish: rounded cards, source color stripe on the left,
 *     hollow badge for the "active" session, friendly empty state,
 *     inline clear filter on the active session.
 */
import { defineComponent, ref, computed, onMounted, onUnmounted, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, timeAgo, fmtTime } from '../store.js';
import { api } from '../api.js';

const SOURCE_META = {
  codex: { label: 'Codex',    tone: '#10b981', glyph: '◧' },
  openclaw: { label: 'OpenClaw', tone: '#f59e0b', glyph: '✜' },
  claude: { label: 'Claude',  tone: '#8b5cf6', glyph: '◎' },
  hermes: { label: 'Hermes',  tone: '#ec4899', glyph: '⚒' },
  'codex-desktop-thread-2026-07-10': { label: 'Codex (legacy)', tone: '#64748b', glyph: '◧' },
};

function metaFor(source) {
  return SOURCE_META[source] || { label: source || '—', tone: '#64748b', glyph: '·' };
}

function shortTitle(s) {
  const t0 = (s.title || '').trim();
  if (t0) {
    // Prefix from cron/job wrappers: "[cron:...] …" → strip the bracket.
    return t0.replace(/^\[cron:[^\]]+\]\s*/, '').slice(0, 70);
  }
  return (s.id || '').slice(0, 14);
}

export const Sidebar = defineComponent({
  name: 'Sidebar',
  setup() {
    const sessions = ref([]);
    const counts = ref({});              // source -> { sessions, turns }
    const filter = ref('all');
    const loading = ref(false);
    const lastRefreshedAt = ref(0);

    async function refresh() {
      loading.value = true;
      try {
        const params = { limit: 300 };
        if (filter.value && filter.value !== 'all') params.source = filter.value;
        const data = await api.listSessions(params);
        sessions.value = Array.isArray(data) ? data : (data.sessions || []);
      } catch (e) {
        sessions.value = [];
      } finally {
        loading.value = false;
        lastRefreshedAt.value = Date.now();
      }
    }

    async function refreshCounts() {
      try {
        const c = await api.fetchJSON('/api/sessions/counts');
        counts.value = c && c.by_source ? c : { by_source: c || {} };
      } catch (e) {
        // silent — counts are decorative
      }
    }

    const sourcePills = computed(() => {
      const seen = new Set();
      const list = [];
      list.push({ id: 'all', label: t('sidebar.allSources'), tone: '#475569', glyph: '⊞', count: counts.value.all?.sessions });
      for (const s of sessions.value) {
        if (!s.source || seen.has(s.source)) continue;
        seen.add(s.source);
        const meta = metaFor(s.source);
        const c = (counts.value.by_source || {})[s.source];
        list.push({
          id: s.source,
          label: meta.label,
          tone: meta.tone,
          glyph: meta.glyph,
          count: c ? c.sessions : undefined,
        });
      }
      // Make sure Codex / Claude / Hermes / OpenClaw are visible even when
      // no records are returned yet (otherwise the pill disappears and the
      // user can't pre-select it).
      for (const id of ['codex', 'openclaw', 'claude', 'hermes']) {
        if (seen.has(id)) continue;
        const meta = metaFor(id);
        const c = (counts.value.by_source || {})[id];
        list.push({
          id,
          label: meta.label,
          tone: meta.tone,
          glyph: meta.glyph,
          count: c ? c.sessions : 0,
        });
      }
      return list;
    });

    function onPickSource(id) {
      filter.value = id;
      refresh();
    }

    function onClearSession() {
      store.activeSession = '';
      refresh();
    }

    function onClickSession(s) {
      store.activeSession = store.activeSession === s.id ? '' : s.id;
    }

    onMounted(() => {
      refresh();
      refreshCounts();
      watch(() => store.stats.sessions, refresh);
      // Refresh when IngestPopover reports new sessions were imported so the
      // left list reflects them without a manual reload.
      const onIngest = () => { refresh(); refreshCounts(); };
      window.addEventListener('loop-memory:ingest-done', onIngest);
      // Periodic auto-refresh every 30s so new files dropped into the watch
      // directory show up even when the user clicks nothing.
      const poller = setInterval(() => { refresh(); refreshCounts(); }, 30_000);
      onUnmounted(() => {
        window.removeEventListener('loop-memory:ingest-done', onIngest);
        clearInterval(poller);
      });
    });

    return {
      sessions, counts, filter, loading, sourcePills, lastRefreshedAt,
      onPickSource, onClickSession, onClearSession,
      metaFor, shortTitle, timeAgo, fmtTime, t, store,
    };
  },
  template: /* html */ `
<aside class="sidebar">
  <div class="sidebar-head">
    <h2>{{ t('sidebar.sessions') }}</h2>
    <button v-if="store.activeSession" class="sidebar-clear" :title="t('sidebar.clearFilter')" @click="onClearSession">×</button>
  </div>

  <div class="source-pills">
    <button v-for="p in sourcePills" :key="p.id"
            class="src-pill" :class="{ active: filter === p.id }"
            :style="{ '--tone': p.tone }"
            @click="onPickSource(p.id)">
      <span class="glyph">{{ p.glyph }}</span>
      <span class="label">{{ p.label }}</span>
      <span class="cnt" v-if="p.count != null">{{ p.count }}</span>
    </button>
  </div>

  <div v-if="store.activeSession" class="active-banner">
    <span class="dot"></span>
    <span class="lbl">{{ t('sidebar.filteringBySession') }}</span>
    <button class="x" @click="onClearSession" :title="t('sidebar.clearFilter')">×</button>
  </div>

  <div class="sessions" v-if="sessions.length">
    <button v-for="s in sessions" :key="s.id"
            class="session"
            :class="{ active: store.activeSession === s.id }"
            :data-source="s.source"
            @click="onClickSession(s)">
      <div class="src-stripe"></div>
      <div class="src-glyph">{{ metaFor(s.source).glyph }}</div>
      <div class="meta">
        <div class="title">{{ shortTitle(s) }}</div>
        <div class="row">
          <span class="src-name">{{ metaFor(s.source).label }}</span>
          <span class="dot-sep">·</span>
          <span class="msgs" :title="t('sidebar.memoryCount')">{{ s.message_count || 0 }} {{ t('sidebar.turns') }}</span>
          <span class="dot-sep">·</span>
          <span class="ago" :title="fmtTime(s.ended_at)">{{ timeAgo(s.ended_at) }}</span>
        </div>
      </div>
    </button>
  </div>
  <div class="empty" v-else-if="!loading">
    <div class="empty-icon">◌</div>
    <div class="empty-text">{{ t('sidebar.empty') }}</div>
    <div class="empty-hint">{{ t('sidebar.emptyHint') }}</div>
  </div>
  <div class="loading" v-else>{{ t('common.loading') }}</div>
</aside>
  `,
});
