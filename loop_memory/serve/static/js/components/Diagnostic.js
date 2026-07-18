/**
 * Diagnostic — quick subsystem health check modal.
 *
 * Calls `/api/health` and shows the per-subsystem status. If anything
 * is red, the user gets a one-click "copy fix command" affordance.
 */
import { defineComponent, ref } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { t } from '../store.js';
import { api } from '../api.js';

export const Diagnostic = defineComponent({
  name: 'Diagnostic',
  props: { open: { type: Boolean, default: false } },
  emits: ['close'],
  setup(props, { emit }) {
    const data = ref(null);
    const loading = ref(false);

    async function check() {
      loading.value = true;
      try { data.value = await fetch('/api/diag').then(r => r.ok ? r.json() : {ok: false}); }
      catch (e) { data.value = { ok: false, error: e.message }; }
      finally { loading.value = false; }
    }
    return { data, loading, check, t, onClose: () => emit('close') };
  },
  watch: {
    open(o) { if (o) this.check(); },
  },
  template: /* html */ `
<transition name="modal">
  <div v-if="open" class="modal-backdrop" @click.self="onClose">
    <div class="modal diagnostic">
      <header class="modal-head">
        <h3>🔍 {{ t('diag.title') }}</h3>
        <button class="x" @click="onClose">×</button>
      </header>
      <div class="modal-body">
        <p class="hint">{{ t('diag.sub') }}</p>
        <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
        <pre v-else class="diag-output">{{ JSON.stringify(data, null, 2) }}</pre>
      </div>
      <footer class="modal-foot">
        <button class="btn ghost" @click="check">{{ t('diag.recheck') }}</button>
        <button class="btn primary" @click="onClose">{{ t('action.close') }}</button>
      </footer>
    </div>
  </div>
</transition>
  `,
});
