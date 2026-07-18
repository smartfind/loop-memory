/**
 * Sidebar — left-rail session list.
 *
 * In the legacy code the session list was hand-rolled HTML with manual
 * diffing on every refresh. Here it's a Vue list bound to a reactive
 * `sessions` array; Vue's virtual DOM handles the diff.
 */
import { defineComponent, ref, computed, onMounted, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, timeAgo } from '../store.js';
import { api } from '../api.js';

export const Sidebar = defineComponent({
  name: 'Sidebar',
  setup() {
    const sessions = ref([]);
    const filter = ref('all');
    const loading = ref(false);

    async function refresh() {
      loading.value = true;
      try {
        const params = filter.value && filter.value !== 'all' ? { source: filter.value } : {};
        const data = await api.listSessions(params);
        sessions.value = Array.isArray(data) ? data : (data.sessions || []);
      } catch (e) {
        sessions.value = [];
      } finally {
        loading.value = false;
      }
    }

    const sources = computed(() => {
      const set = new Set();
      for (const s of sessions.value) {
        if (s.source) set.add(s.source);
      }
      return Array.from(set).sort();
    });

    onMounted(() => {
      refresh();
      // Refresh when stats update (signals a new session may have arrived)
      watch(() => store.stats.sessions, refresh);
    });

    function onClickSession(s) {
      store.activeSession = s.id;
    }

    return { sessions, filter, sources, loading, refresh, onClickSession, t, store, timeAgo };
  },
  template: /* html */ `
<aside class="sidebar">
  <div class="sidebar-head">
    <h2>{{ t('sidebar.sessions') }}</h2>
    <div style="flex:1"></div>
    <select v-model="filter" @change="refresh">
      <option value="all">{{ t('sidebar.allSources') }}</option>
      <option v-for="src in sources" :key="src" :value="src">{{ src }}</option>
    </select>
  </div>
  <div class="sessions" v-if="sessions.length">
    <div v-for="s in sessions" :key="s.id"
         class="session"
         :class="{ active: store.activeSession === s.id }"
         @click="onClickSession(s)">
      <div class="src-badge" :data-source="s.source">{{ s.source || '—' }}</div>
      <div class="meta">
        <div class="title">{{ s.title || s.id.slice(0, 12) }}</div>
        <div class="row">
          <span class="count">{{ s.count || 0 }}</span>
          <span class="ago">{{ timeAgo(s.last_seen || s.updated_at) }}</span>
        </div>
      </div>
    </div>
  </div>
  <div class="empty" v-else-if="!loading">{{ t('sidebar.empty') }}</div>
  <div class="loading" v-else>{{ t('common.loading') }}</div>
</aside>
  `,
});
