/**
 * WikiEditor — minimal new/edit modal for a wiki page.
 *
 * The full editor (markdown preview, evidence picker, etc.) is huge in
 * the legacy code; here we ship a focused 3-field form so users can fix
 * typos and create new pages. Future iterations can grow the editor
 * without rewriting the surrounding list view.
 */
import { defineComponent, ref, computed, watch } from '../lib/vue.esm-browser.prod.js';
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
    // Scope — which clients should see this wiki page on recall.
    // 'global' is exclusive (mutually exclusive with per-client chips).
    const SCOPE_TOKENS = ['auto', 'global', 'codex', 'claude', 'hermes', 'openclaw'];
    const scope = ref(['auto']);
    // Auto-save draft to localStorage so edits survive refresh
    const DRAFT_KEY = 'loop_wiki_draft_v1';
    let saveTimer = null;
    function saveDraft() {
      if (props.pageId !== 'new') return;  // only draft new pages
      try {
        localStorage.setItem(DRAFT_KEY, JSON.stringify({
          title: title.value, summary: summary.value, body: body.value,
          tags: tags.value, importance: importance.value, scope: scope.value,
          savedAt: Date.now(),
        }));
      } catch (_) {}
    }
    function loadDraft() {
      if (props.pageId !== 'new') return;
      try {
        const raw = localStorage.getItem(DRAFT_KEY);
        if (!raw) return;
        const d = JSON.parse(raw);
        // Only restore if draft is < 24h old
        if (d.savedAt && Date.now() - d.savedAt < 86400000) {
          title.value = d.title || '';
          summary.value = d.summary || '';
          body.value = d.body || '';
          tags.value = d.tags || '';
          importance.value = d.importance ?? 0.5;
          scope.value = Array.isArray(d.scope) ? d.scope : (d.scope ? [d.scope] : ['auto']);
        }
      } catch (_) {}
    }
    function clearDraft() {
      try { localStorage.removeItem(DRAFT_KEY); } catch (_) {}
    }
    // Debounced auto-save on any field change
    function scheduleDraftSave() {
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(saveDraft, 1200);
    }
    watch([title, summary, body, tags, importance, scope], scheduleDraftSave, { deep: true });

    async function load() {
      if (props.pageId === 'new') {
        // Try to restore draft first, then clear it
        loadDraft();
        clearDraft();
        return;
      }
      clearDraft();  // clear any stale draft on successful load
      loading.value = true;
      try {
        const p = await api.getWiki(props.pageId);
        title.value = p.title || '';
        summary.value = p.summary || '';
        body.value = p.body || '';
        tags.value = (p.tags || []).join(', ');
        importance.value = p.importance || 0.5;
        const rawScope = (p.scope || 'auto').toString().toLowerCase();
        const tokens = rawScope.split(',').map(s => s.trim()).filter(Boolean);
        scope.value = tokens.length ? tokens : ['auto'];
      } catch (e) {
        // ignore
      } finally {
        loading.value = false;
      }
    }

    watch(() => props.pageId, load, { immediate: true });

    function toggleScope(token) {
      const cur = new Set(scope.value);
      if (token === 'auto') {
        scope.value = ['auto'];
        return;
      }
      if (token === 'global') {
        // global is exclusive: clicking it clears the per-client list
        scope.value = ['global'];
        return;
      }
      cur.delete('auto');
      cur.delete('global');  // any per-client click leaves global
      if (cur.has(token)) cur.delete(token); else cur.add(token);
      // Fallback: if user clears everything, let the classifier decide.
      scope.value = cur.size ? Array.from(cur) : ['auto'];
    }

    function onSave() {
      const payload = {
        title: title.value.trim(),
        summary: summary.value.trim(),
        body: body.value,
        tags: tags.value.split(',').map(s => s.trim()).filter(Boolean),
        importance: Number(importance.value) || 0.5,
        scope: scope.value.includes('auto') ? 'auto' : scope.value.join(','),
      };
      emit('save', payload);
    }

    return {
      loading, title, summary, body, tags, importance, scope,
      SCOPE_TOKENS, toggleScope, t, onSave, onCancel: () => emit('cancel'),
    };
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
      <label class="row-scope">
        <span>
          {{ t('wiki.field.scope') }}
          <em class="sec-hint">— {{ t('wiki.scope.hint') }}</em>
        </span>
        <div class="scope-chips" role="group">
          <button
            v-for="tok in SCOPE_TOKENS" :key="tok"
            type="button"
            class="scope-chip"
            :class="{ active: scope.includes(tok), 'is-global': tok === 'global' }"
            :aria-pressed="scope.includes(tok) ? 'true' : 'false'"
            @click="toggleScope(tok)">
            {{ t('wiki.scope.' + tok) }}
          </button>
        </div>
      </label>
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
