/**
 * Tab bar — Timeline / Dashboard / Wiki / Knowledge graph.
 *
 * The active tab lives in `store.activeTab` so other components can react
 * to it. URL `?tab=` is read on app boot and written back when the user
 * switches, so a deep-link to a specific view round-trips.
 *
 * Each tab is rendered as a real <button role="tab"> (instead of a plain
 * <div>) so screen readers, keyboard focus, and the native Enter/Space
 * activation all work without any extra JS. The container carries
 * role="tablist" so the group is announced as a tab list.
 */
import { defineComponent, computed, watch } from '../lib/vue.esm-browser.prod.js';
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
      // App.js owns the URL sync via a watcher on store.activeTab,
      // so all writers (this component, Sidebar session picks,
      // Open-wiki from graph) reach the URL through the same path.
      store.activeTab = id;
    }

    return { tabs, store, setTab };
  },
  template: /* html */ `
<nav class="tabs" role="tablist">
  <button v-for="tb in tabs" :key="tb.id"
          type="button"
          class="tab" :class="{ active: store.activeTab === tb.id }"
          :data-tab="tb.id"
          role="tab"
          :aria-selected="store.activeTab === tb.id"
          @click="setTab(tb.id)">
    <span>{{ tb.label }}</span>
    <span class="badge" v-if="tb.badge">{{ tb.badge }}</span>
  </button>
</nav>
  `,
});
