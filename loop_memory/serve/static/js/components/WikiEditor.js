/**
 * WikiEditor — minimal new/edit modal for a wiki page.
 *
 * The full editor (markdown preview, evidence picker, etc.) is huge in
 * the legacy code; here we ship a focused 3-field form so users can fix
 * typos and create new pages. Future iterations can grow the editor
 * without rewriting the surrounding list view.
 */
import { defineComponent, ref, computed, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t } from '../store.js';
import { api } from '../api.js';

export const WikiEditor = defineComponent({
  name: 'WikiEditor',
  props: {
    pageId: { type: [String, null], required: true },
  },
  emits: ['save', 'cancel'],
  setup(props, { emit }) {
    const loading = ref(false);
    const title = ref('');
    const summary = ref('');
    const body = ref('');
    const tags = ref('');
    const importance = ref(0.5);

    async function load() {
      if (props.pageId === 'new') {
        title.value = ''; summary.value = ''; body.value = '- '; tags.value = ''; importance.value = 0.5;
        return;
      }
      loading.value = true;
      try {
        const p = await api.getWiki(props.pageId);
        title.value = p.title || '';
        summary.value = p.summary || '';
        body.value = p.body || '';
        tags.value = (p.tags || []).join(', ');
        importance.value = p.importance || 0.5;
      } catch (e) {
        // ignore
      } finally {
        loading.value = false;
      }
    }

    watch(() => props.pageId, load, { immediate: true });

    function onSave() {
      const payload = {
        title: title.value.trim(),
        summary: summary.value.trim(),
        body: body.value,
        tags: tags.value.split(',').map(s => s.trim()).filter(Boolean),
        importance: Number(importance.value) || 0.5,
      };
      emit('save', payload);
    }

    return { loading, title, summary, body, tags, importance, t, onSave, onCancel: () => emit('cancel') };
  },
  template: /* html */ `
<div class="modal-backdrop" @click.self="onCancel">
  <div class="modal wiki-editor">
    <header class="modal-head">
      <h3>{{ pageId === 'new' ? t('wiki.new') : t('wiki.edit') }}</h3>
      <button class="x" @click="onCancel">×</button>
    </header>
    <div class="modal-body" v-if="!loading">
      <label>
        <span>{{ t('wiki.field.title') }}</span>
        <input type="text" v-model="title" :placeholder="t('wiki.titlePlaceholder')" />
      </label>
      <label>
        <span>{{ t('wiki.field.summary') }}</span>
        <textarea v-model="summary" rows="2" :placeholder="t('wiki.summaryPlaceholder')"></textarea>
      </label>
      <label>
        <span>{{ t('wiki.field.body') }}</span>
        <textarea v-model="body" rows="14" :placeholder="t('wiki.bodyPlaceholder')"></textarea>
      </label>
      <div class="row-2">
        <label>
          <span>{{ t('wiki.field.tags') }}</span>
          <input type="text" v-model="tags" :placeholder="t('wiki.tagsPlaceholder')" />
        </label>
        <label>
          <span>{{ t('wiki.field.importance') }} ({{ Math.round(importance * 100) }}%)</span>
          <input type="range" v-model.number="importance" min="0" max="1" step="0.05" />
        </label>
      </div>
    </div>
    <div class="loading" v-else>{{ t('common.loading') }}</div>
    <footer class="modal-foot">
      <button class="btn ghost" @click="onCancel">{{ t('action.cancel') }}</button>
      <button class="btn primary" @click="onSave">{{ t('action.save') }}</button>
    </footer>
  </div>
</div>
  `,
});
