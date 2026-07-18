/**
 * RunStrip — the bottom-of-page strip that shows when an AI distill
 * is in progress (current/total progress bar + cancel button).
 */
import { defineComponent, computed } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t } from '../store.js';

export const RunStrip = defineComponent({
  name: 'RunStrip',
  emits: ['dismiss'],
  setup(props, { emit }) {
    const visible = computed(() => store.runStatus?.is_running && !store.stripDismissed);
    const pct = computed(() => {
      const p = store.runStatus?.progress || {};
      if (!p.total) return 0;
      return Math.min(100, Math.round((p.current / p.total) * 100));
    });
    return { visible, pct, store, t, dismiss: () => emit('dismiss') };
  },
  template: /* html */ `
<div v-if="visible" class="run-strip" :data-state="store.runStatus?.is_running ? 'running' : 'idle'">
  <div class="rs-left">
    <span class="rs-dot"></span>
    <span class="rs-label">{{ store.runStatus?.progress?.message || t('run.running') }}</span>
  </div>
  <div class="rs-bar"><div class="rs-fill" :style="{ width: pct + '%' }"></div></div>
  <div class="rs-pct">{{ pct }}%</div>
  <button class="x" @click="dismiss">{{ t('action.dismiss') }}</button>
</div>
  `,
});
