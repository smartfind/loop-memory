/**
 * Settings — the right-side drawer that holds LLM config + scheduler.
 *
 * The legacy `openSettings` function was 250 lines of imperative DOM
 * mutation: read form, write form, schedule visibility toggles, API key
 * state machine, test-connection result rendering, etc. Here it's a
 * single Vue component driven by a reactive `cfg` object. Saving triggers
 * one POST. The component never touches the DOM directly.
 */
import { defineComponent, ref, computed, onMounted, watch, reactive } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, toast } from '../store.js';
import { api, ApiError } from '../api.js';

export const Settings = defineComponent({
  name: 'Settings',
  props: {
    open: { type: Boolean, default: false },
  },
  emits: ['close'],
  setup(props, { emit }) {
    const providers = ref([]);
    const cfg = reactive({
      provider: 'rules', model: 'rules',
      base_url: '', api_key: '',
      api_key_set: false, api_key_account: '', api_key_fingerprint: '',
      schedule: {
        enabled: false, mode: 'off',
        interval_minutes: 60, hour: 3, minute: 0, weekday: 0,
        after_ingest_idle_sec: 30,
      },
      behaviour: {
        batch_size: 50, temperature: 0.3, max_output_tokens: 800,
        min_importance: 0.0, enable_filter: true, enable_score: true,
        enable_summarize: true, dry_run: false,
      },
    });
    const testing = ref(false);
    const testResult = ref(null);
    const saving = ref(false);
    const savedHint = ref(false);

    async function load() {
      try {
        const [p, c] = await Promise.all([api.llmProviders(), api.llmConfig()]);
        providers.value = p || [];
        // /api/admin/llm/config returns {config: {...}, warnings, ...}
        const actualCfg = (c && c.config) ? c.config : c;
        Object.assign(cfg, actualCfg);
      } catch (e) { /* ignore */ }
      try {
        const status = await api.llmStatus();
        if (status && status.provider) store.modelInfo = {
          provider: status.provider, model: status.model || 'rules',
          api_key_set: !!status.api_key_set, key_len: status.key_len || 0,
        };
      } catch (e) { /* ignore */ }
    }

    onMounted(load);
    watch(() => props.open, (o) => { if (o) load(); });

    const selectedProvider = computed(() => {
      return providers.value.find(p => p.id === cfg.provider) || {};
    });

    function onProviderChange() {
      const p = selectedProvider.value;
      if (p && p.default_model) cfg.model = p.default_model;
      if (p && p.default_base_url && !cfg.base_url) cfg.base_url = p.default_base_url;
    }

    async function onTest() {
      testing.value = true;
      testResult.value = null;
      try {
        const r = await api.llmTest({
          provider: cfg.provider, model: cfg.model, base_url: cfg.base_url,
          api_key: cfg.api_key || undefined,
        });
        testResult.value = r;
        if (r.ok) {
          toast(t('settings.test.ok', { ms: r.elapsed_ms || 0 }), 2500);
        } else {
          toast(t('settings.test.fail', { msg: r.error?.provider_message || r.error?.hint || 'unknown' }), 4000);
        }
      } catch (e) {
        testResult.value = { ok: false, error: { provider_message: e.message } };
      } finally {
        testing.value = false;
      }
    }

    async function onSave() {
      saving.value = true;
      try {
        // api_key is only sent if the user typed something new
        const payload = {
          provider: cfg.provider, model: cfg.model, base_url: cfg.base_url,
          schedule: cfg.schedule, behaviour: cfg.behaviour,
        };
        if (cfg.api_key) payload.api_key = cfg.api_key;
        await api.llmSchedule(payload);
        savedHint.value = true;
        setTimeout(() => { savedHint.value = false; }, 2500);
        cfg.api_key = '';
        await load();
      } catch (e) {
        toast(t('common.error') + ': ' + e.message, 4000);
      } finally {
        saving.value = false;
      }
    }

    async function onClearKey() {
      if (!confirm(t('settings.apiKey.confirmClear'))) return;
      try {
        await api.llmSchedule({ ...cfg, api_key: '' });
        cfg.api_key_set = false;
        toast(t('settings.apiKey.cleared'), 2000);
        await load();
      } catch (e) { /* ignore */ }
    }

    function onClose() { emit('close'); }

    return { cfg, providers, selectedProvider, onProviderChange,
             testing, testResult, onTest,
             saving, savedHint, onSave, onClearKey,
             store, t, onClose };
  },
  template: /* html */ `
<aside v-show="open" class="drawer" role="dialog" aria-label="Settings" @click.self="onClose">
    <div class="drawer-body">
      <header class="drawer-head">
        <h2>{{ t('settings.title') }}</h2>
        <button class="x" @click="onClose">×</button>
      </header>

      <section>
        <h3>{{ t('settings.section.provider') }}</h3>
        <label>
          <span>{{ t('settings.provider') }}</span>
          <select v-model="cfg.provider" @change="onProviderChange">
            <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.label }}</option>
          </select>
        </label>
        <p v-if="selectedProvider.description" class="hint">{{ selectedProvider.description }}</p>
        <label>
          <span>{{ t('settings.model') }}</span>
          <input v-model="cfg.model" :placeholder="selectedProvider.default_model || 'gpt-4o-mini'" />
        </label>
        <label v-if="selectedProvider.needs_base_url !== false">
          <span>{{ t('settings.baseUrl') }}</span>
          <input v-model="cfg.base_url" :placeholder="selectedProvider.default_base_url || ''" />
        </label>
        <label v-if="selectedProvider.needs_api_key !== false">
          <span>
            {{ t('settings.apiKey') }}
            <span v-if="cfg.api_key_set" class="key-status saved">
              <span class="key-dot"></span>{{ t('settings.apiKey.configured') }}
              <span v-if="cfg.api_key_fingerprint" class="key-fp">{{ cfg.api_key_fingerprint }}</span>
            </span>
            <span v-else class="key-status missing">
              <span class="key-dot"></span>{{ t('settings.apiKey.missing') }}
            </span>
          </span>
          <div class="api-key-row">
            <input type="password" v-model="cfg.api_key"
                   :placeholder="cfg.api_key_set ? t('settings.apiKey.edit') : t('settings.apiKey.placeholder')" />
            <button class="btn small ghost" v-if="cfg.api_key_set" @click="onClearKey" type="button">
              {{ t('settings.apiKey.clear') }}
            </button>
          </div>
          <p class="hint">{{ t('settings.apiKey.hint') }}</p>
        </label>

        <div class="test-row">
          <button class="btn small primary" :disabled="testing" @click="onTest">
            {{ testing ? t('common.testing') : t('settings.test') }}
          </button>
          <span v-if="testResult" class="test-result" :class="{ ok: testResult.ok, fail: !testResult.ok }">
            {{ testResult.ok ? t('settings.test.ok', { ms: testResult.elapsed_ms || 0 }) : (testResult.error?.provider_message || testResult.error?.hint || 'failed') }}
          </span>
        </div>
      </section>

      <section>
        <h3>{{ t('settings.section.schedule') }}</h3>
        <label class="row-inline">
          <input type="checkbox" v-model="cfg.schedule.enabled" />
          <span>{{ t('settings.schedule.mode') }}</span>
        </label>
        <label>
          <span>{{ t('settings.schedule.mode') }}</span>
          <select v-model="cfg.schedule.mode">
            <option value="off">{{ t('settings.schedule.off') }}</option>
            <option value="realtime">{{ t('settings.schedule.realtime') }}</option>
            <option value="hourly">{{ t('settings.schedule.hourly') }}</option>
            <option value="daily">{{ t('settings.schedule.daily') }}</option>
            <option value="weekly">{{ t('settings.schedule.weekly') }}</option>
            <option value="interval">{{ t('settings.schedule.everyN') }}</option>
          </select>
        </label>
        <label v-if="cfg.schedule.mode === 'interval'">
          <span>{{ t('settings.schedule.interval') }}</span>
          <input type="number" v-model.number="cfg.schedule.interval_minutes" min="5" />
        </label>
        <div v-if="cfg.schedule.mode === 'daily' || cfg.schedule.mode === 'weekly'" class="row-2">
          <label>
            <span>{{ t('settings.schedule.hour') }}</span>
            <input type="number" v-model.number="cfg.schedule.hour" min="0" max="23" />
          </label>
          <label>
            <span>{{ t('settings.schedule.minute') }}</span>
            <input type="number" v-model.number="cfg.schedule.minute" min="0" max="59" />
          </label>
        </div>
        <label v-if="cfg.schedule.mode === 'realtime'">
          <span>{{ t('settings.schedule.realtimeIdle') }}</span>
          <input type="number" v-model.number="cfg.schedule.after_ingest_idle_sec" min="5" />
        </label>
      </section>

      <section>
        <h3>{{ t('settings.section.actions') }}</h3>
        <div class="action-row">
          <button class="btn primary" :disabled="saving" @click="onSave">
            {{ saving ? t('common.saving') : t('action.save') }}
          </button>
          <span v-if="savedHint" class="saved-hint">✓ {{ t('settings.apiKey.saved') }}</span>
        </div>
      </section>
    </div>
  </aside>
  `,
});
