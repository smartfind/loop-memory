/**
 * Diagnostic — quick subsystem health check modal.
 *
 * Two surfaces:
 *   1. `/api/diag` JSON dump (legacy — kept for power users).
 *   2. **Client integration panel** — the entry point users were missing
 *      for "how do I enable knowledge on my Codex / Claude / Hermes".
 *      Shows a per-client status row + a single button that runs the
 *      full ``loop-memory install-hooks`` pipeline and prints the
 *      resulting actions.
 */
import { defineComponent, ref, computed } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { t, toast } from '../store.js';

const CLIENT_META = {
  codex:   { icon: '⌨', label: 'Codex CLI' },
  claude:  { icon: '✦', label: 'Claude Code' },
  hermes:  { icon: '◆', label: 'Hermes' },
  openclaw:{ icon: '◈', label: 'OpenClaw' },
};

export const Diagnostic = defineComponent({
  name: 'Diagnostic',
  props: { open: { type: Boolean, default: false } },
  emits: ['close'],
  setup(props, { emit }) {
    const data = ref(null);
    const loading = ref(false);
    const hooks = ref(null);          // /api/install-hooks GET response
    const hooksLoading = ref(false);
    const running = ref(false);
    const lastResult = ref(null);     // last POST result

    async function check() {
      loading.value = true;
      try { data.value = await fetch('/api/diag').then(r => r.ok ? r.json() : {ok: false}); }
      catch (e) { data.value = { ok: false, error: e.message }; }
      finally { loading.value = false; }
    }

    async function loadHooks() {
      hooksLoading.value = true;
      try {
        const r = await fetch('/api/install-hooks');
        hooks.value = r.ok ? await r.json() : { ok: false };
      } catch (e) {
        hooks.value = { ok: false, error: e.message };
      } finally {
        hooksLoading.value = false;
      }
    }

    async function runInstall() {
      running.value = true;
      try {
        const r = await fetch('/api/install-hooks', { method: 'POST' });
        const j = r.ok ? await r.json() : { ok: false, error: 'HTTP ' + r.status };
        lastResult.value = j;
        if (j.ok) {
          toast(t('diag.hooks.done'), 2400);
          await loadHooks();
        } else {
          toast((t('common.error') || 'Error') + ': ' + (j.error || '?'), 4000);
        }
      } catch (e) {
        lastResult.value = { ok: false, error: e.message };
      } finally {
        running.value = false;
      }
    }

    function copyActions() {
      const actions = (lastResult.value && lastResult.value.actions) || [];
      if (!actions.length) return;
      const text = '[loop-memory install-hooks]\n' + actions.map(a => '  · ' + a).join('\n');
      navigator.clipboard?.writeText(text).then(() => toast(t('diag.hooks.copied'), 1500));
    }

    const clientRows = computed(() => {
      const src = (hooks.value && hooks.value.clients) || (lastResult.value && lastResult.value.clients) || {};
      return Object.entries(src).map(([key, c]) => {
        const meta = CLIENT_META[key] || { icon: '●', label: key };
        const installed = !!c.installed;
        const mcpOk = !!c.mcp_configured;
        return {
          key,
          icon: meta.icon,
          name: meta.label,
          installed,
          status: !installed ? 'absent' : mcpOk ? 'ok' : 'pending',
        };
      });
    });

    const allOk = computed(() => clientRows.value.every(r => r.status === 'ok'));

    return {
      data, loading, check,
      hooks, hooksLoading, loadHooks,
      running, lastResult, runInstall, copyActions,
      clientRows, allOk,
      t, onClose: () => emit('close'),
    };
  },
  watch: {
    open(o) {
      if (o) {
        this.check();
        this.loadHooks();
      }
    },
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

        <!-- Client integration panel: the entry point for "enable knowledge" -->
        <section class="diag-hooks">
          <div class="diag-hooks-head">
            <div>
              <h4>🪝 {{ t('diag.hooks.title') }}</h4>
              <p class="muted">{{ t('diag.hooks.sub') }}</p>
            </div>
            <button class="btn primary" :disabled="running" @click="runInstall">
              <span v-if="running">…</span>
              <span v-else>{{ allOk ? t('diag.hooks.reinstall') : t('diag.hooks.run') }}</span>
            </button>
          </div>

          <div v-if="hooksLoading && !clientRows.length" class="loading">{{ t('common.loading') }}</div>
          <div v-else class="diag-hooks-grid">
            <div v-for="r in clientRows" :key="r.key" class="diag-hook-row" :class="r.status">
              <span class="diag-hook-ico">{{ r.icon }}</span>
              <span class="diag-hook-name">{{ r.name }}</span>
              <span class="diag-hook-state">
                <span v-if="r.status === 'ok'"     class="badge ok">{{ t('diag.hooks.stateOk') }}</span>
                <span v-else-if="r.status === 'pending'" class="badge warn">{{ t('diag.hooks.statePending') }}</span>
                <span v-else class="badge mute">{{ t('diag.hooks.stateAbsent') }}</span>
              </span>
            </div>
          </div>

          <div v-if="lastResult && lastResult.actions && lastResult.actions.length" class="diag-hooks-actions">
            <div class="diag-hooks-actions-head">
              <span class="muted">{{ t('diag.hooks.actionsLabel') }}</span>
              <button class="btn xs ghost" @click="copyActions">{{ t('action.copy') }}</button>
            </div>
            <ul>
              <li v-for="(a, i) in lastResult.actions" :key="i">{{ a }}</li>
            </ul>
            <p class="muted diag-hooks-note">{{ lastResult.note }}</p>
          </div>

          <details class="diag-hooks-raw">
            <summary>{{ t('diag.hooks.rawToggle') }}</summary>
            <pre class="diag-output">{{ JSON.stringify(data, null, 2) }}</pre>
          </details>
        </section>

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
