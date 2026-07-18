/**
 * Tab bar — Timeline / Dashboard / Wiki / Knowledge graph.
 *
 * The active tab lives in `store.activeTab` so other components can react
 * to it. URL `?tab=` is read on app boot and written back when the user
 * switches, so a deep-link to a specific view round-trips.
 */
import { defineComponent, computed, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t } from '../store.js';

export const Tabs = defineComponent({
  name: 'Tabs',
  setup() {
    const tabs = computed(() => ([
      { id: 'timeline',  label: t('tab.timeline') },
      { id: 'dashboard', label: t('tab.dashboard') },
      { id: 'wiki',      label: t('tab.wiki'),      badge: store.stats.wiki_pages },
      { id: 'graph',     label: t('tab.graph'),     badge: (typeof store.stats.graph === 'string' ? store.stats.graph.split('/')[1] : 0) || 0 },
    ]));

    function setTab(id) {
      store.activeTab = id;
      // Persist to URL so the view is bookmarkable
      const url = new URL(window.location.href);
      url.searchParams.set('tab', id);
      window.history.replaceState({}, '', url.toString());
    }

    return { tabs, store, setTab };
  },
  template: /* html */ `
<nav class="tabs">
  <div v-for="tb in tabs" :key="tb.id"
       class="tab" :class="{ active: store.activeTab === tb.id }"
       :data-tab="tb.id"
       @click="setTab(tb.id)">
    <span>{{ tb.label }}</span>
    <span class="badge" v-if="tb.badge">{{ tb.badge }}</span>
  </div>
</nav>
  `,
});
