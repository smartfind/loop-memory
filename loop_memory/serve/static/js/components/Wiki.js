/**
 * Wiki — distilled knowledge pages.
 *
 * Cards in a responsive grid; each card shows title, summary, top bullets,
 * tags, importance, and last-updated. Click a card to open the editor
 * (or just expand inline). The legacy code had a 200-line renderWiki
 * function that hand-built HTML strings — Vue's template syntax is
 * significantly easier to scan and edit.
 */
import { defineComponent, ref, computed, onMounted, onUnmounted, watch } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, toast, escapeHtml, fmtTime } from '../store.js';
import { api } from '../api.js';
import { WikiEditor } from './WikiEditor.js';

export const Wiki = defineComponent({
  name: 'Wiki',
  components: { WikiEditor },
  setup() {
    const pages = ref([]);
    const q = ref('');
    const sort = ref('updated_desc');
    const scopeFilter = ref('all');  // 'all'|'global'|'codex'|...
    const loading = ref(false);
    const expanded = ref(null);
    const editing = ref(null);
    // Scope tokens mirror WikiEditor.SCOPE_TOKENS
    const SCOPE_TOKENS = ['global', 'codex', 'claude', 'hermes', 'openclaw'];
    // Default scope applied to every page when the master 全局 switch
    // is flipped OFF. ``codex`` is the most common client in this
    // workspace, so it's a safe "scoped to one client" default that
    // matches the per-card toggle's fallback. Users can refine each
    // card afterwards via the per-card toggle or the WikiEditor.
    const SCOPE_OFF_DEFAULT = 'codex';

    async function refresh() {
      loading.value = true;
      try {
        const data = await api.listWiki();
        pages.value = Array.isArray(data) ? data : (data.pages || []);
      } catch (e) {
        pages.value = [];
      } finally {
        loading.value = false;
      }
    }

    const visible = computed(() => {
      let rows = pages.value;
      if (q.value.trim()) {
        const needle = q.value.trim().toLowerCase();
        rows = rows.filter(p =>
          (p.title || '').toLowerCase().includes(needle) ||
          (p.summary || '').toLowerCase().includes(needle) ||
          (p.slug || '').toLowerCase().includes(needle));
      }
      // Scope filter: 'all' = everything; otherwise the page must
      // either be 'global' (visible to everyone) OR include this
      // client token in its comma-separated scope list.
      if (scopeFilter.value && scopeFilter.value !== 'all') {
        const tok = scopeFilter.value;
        rows = rows.filter(p => {
          const s = (p.scope || 'global').toString().toLowerCase();
          if (s === 'global') return true;  // global visible to all
          const tokens = s.split(',').map(x => x.trim()).filter(Boolean);
          return tokens.includes(tok);
        });
      }
      const cmp = (a, b) => {
        if (sort.value === 'updated_desc') return (b.updated_at || 0) - (a.updated_at || 0);
        if (sort.value === 'importance_desc') return (b.importance || 0) - (a.importance || 0);
        if (sort.value === 'title_asc') return (a.title || '').localeCompare(b.title || '');
        return 0;
      };
      return [...rows].sort(cmp);
    });

    /**
     * Extract every bullet ("- ...") from a wiki body so the card can show the
     * full list. Previously this was capped at the first 6 lines, which made
     * freshly-distilled pages look truncated. The new distillation policy is
     * "completeness over compactness", so the card needs to surface every
     * atomic fact the LLM produced.
     */
    function bulletsOf(p) {
      const lines = (p.body || '').split('\n');
      return lines.filter(l => l.startsWith('- '));
    }
    /** Render the scope chips that show which clients a page is
     *  visible to. Always returns an array of strings; consumers
     *  iterate as-is. */
    function scopeTokensOf(p) {
      const s = (p.scope || 'global').toString().toLowerCase();
      return s.split(',').map(x => x.trim()).filter(Boolean);
    }
    function scopeChipLabel(tok) {
      return ('wiki.scope.chip.' + tok);
    }

    function expand(id) { expanded.value = expanded.value === id ? null : id; }
    function edit(id) { editing.value = id; }

    function onNew() { editing.value = 'new'; }

    const importing = ref(false);
    const exporting = ref(false);

    async function onExport() {
      exporting.value = true;
      try {
        const res = await fetch('/api/wiki/export?format=json&limit=2000');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
        a.href = url;
        a.download = `loop-memory-wiki-${ts}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      } catch (e) {
        toast(t('wiki.exportFail', { msg: e.message }), 4000);
      } finally {
        exporting.value = false;
      }
    }

    function onImportClick() {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.json,.md,.markdown,application/json,text/markdown,text/plain';
      input.addEventListener('change', async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        importing.value = true;
        try {
          const text = await file.text();
          let body;
          if (/\.md(?:arkdown)?$/i.test(file.name) || text.trim().startsWith('#')) {
            body = { format: 'markdown', markdown: text };
          } else {
            try {
              const parsed = JSON.parse(text);
              const pages = Array.isArray(parsed) ? parsed : (parsed.pages || []);
              body = { format: 'json', pages };
            } catch (e) {
              throw new Error('not a valid JSON file: ' + e.message);
            }
          }
          const r = await fetch('/api/wiki/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!r.ok) {
            const txt = await r.text();
            throw new Error(`HTTP ${r.status} — ${txt.slice(0, 200)}`);
          }
          const out = await r.json();
          if (out.total === 0) {
            toast(t('wiki.importEmpty'), 3000);
          } else {
            toast(t('wiki.importSuccess', {
              created: out.created, updated: out.updated, skipped: out.skipped,
            }), 3500);
            await refresh();
          }
        } catch (e) {
          toast(t('wiki.importError', { msg: e.message }), 4000);
        } finally {
          importing.value = false;
        }
      });
      input.click();
    }

    async function saveEdit(payload) {
      try {
        if (editing.value === 'new') {
          await api.createWiki(payload);
        } else {
          await api.updateWiki(editing.value, payload);
        }
        editing.value = null;
        await refresh();
      } catch (e) {
        alert(t('wiki.saveError') + ': ' + e.message);
      }
    }

    async function removePage(p) {
      if (!confirm(t('wiki.confirmDelete'))) return;
      try {
        await api.deleteWiki(p.id);
        await refresh();
      } catch (e) {
        alert(t('wiki.deleteError') + ': ' + e.message);
      }
    }

    async function openWikiBySlug(slug) {
      if (!slug) return;
      try {
        const list = await api.listWiki();
        const found = (list || []).find(x => x.slug === slug || x.title === slug);
        if (found) editing.value = found.id;
      } catch (e) { /* ignore */ }
    }
    function onOpenWiki(e) {
      const slug = (e && e.detail && e.detail.slug) || '';
      store.activeTab = 'wiki';
      openWikiBySlug(slug);
    }
    onMounted(() => {
      refresh();
      window.addEventListener('loop-memory:open-wiki', onOpenWiki);
    });
    onUnmounted(() => {
      window.removeEventListener('loop-memory:open-wiki', onOpenWiki);
    });
    watch(() => store.stats.wiki_pages, refresh);
    // When the user navigates back to the wiki tab, refresh in case the
    // list went stale (distillation may have added/removed pages).
    watch(() => store.activeTab, (id) => { if (id === 'wiki') refresh(); });

    // ------------------------------------------------------------------
    // Per-card 全局 toggle + toolbar master 全局 toggle.
    //
    // Design contract:
    //   - Each wiki page carries a ``scope`` field (existing schema)
    //     where 'global' means "shared with every agent", otherwise a
    //     comma-list of client tokens ('codex', 'claude', …).
    //   - Default is NOT global: distilled pages get the source-client
    //     of the evidence memories (see ``_scope_for_evidence`` in
    //     ``llm_consolidate.py``), so the per-card toggle is OFF on
    //     newly-distilled pages.
    //   - The toolbar master toggle has three visible states:
    //       * on  (every page is global — bulk ON)
    //       * off (no automatic bulk change)
    //       * "mixed" (some pages are global, some are not) — shown
    //         as a half-tinted knob so the user knows they have
    //         already diverged from the master.
    //   - Master is OPTIMISTIC: flipping it ON calls ``bulk-scope``
    //     to write 'global' to every page in one round-trip, then
    //     refreshes the local list. The visible "mixed" state
    //     resolves itself automatically as the response lands.
    //   - When the user manually flips a SINGLE card OFF while the
    //     master is ON, the master auto-flips to "mixed" — that's
    //     the rule "关闭某一个知识全局生效后，上面的全局生效自动关闭".
    // ------------------------------------------------------------------
    const masterGlobal = ref(false);   // local optimistic state
    const bulkBusy = ref(false);       // disable toggle while inflight

    /** True iff the page is shared with every client. */
    function isGlobal(p) {
      const s = (p && p.scope || '').toString().toLowerCase();
      if (!s) return true;  // schema default = global
      // "global" alone, or starting with "global," => global.
      return s === 'global' || s.split(',').map(x => x.trim()).includes('global');
    }

    /**
     * Toggle a single card's 全局 switch. Persists the change via
     * ``bulk-scope`` with the explicit ``page_ids`` list — avoids a
     * per-page PUT round-trip. Also nudges ``masterGlobal`` to
     * reflect the partial-state rule.
     */
    async function toggleCardGlobal(p) {
      if (bulkBusy.value) return;
      // If the page is currently global, flipping OFF means we
      // switch to a per-client scope. The natural fallback is the
      // page's existing scope tokens (e.g. 'codex,claude'); if it
      // was 'global', fall back to the single most-recent source
      // recorded in ``tags``/``evidence`` or just 'codex' so the
      // page is no longer shared with every agent.
      const cur = (p.scope || '').toString().toLowerCase();
      let nextScope;
      if (isGlobal(p)) {
        // Going global → not-global. Preserve whatever non-global
        // tokens were there, otherwise fall back to 'codex' so the
        // page is at least scoped to one client.
        const nonGlobal = cur.split(',')
          .map(x => x.trim())
          .filter(x => x && x !== 'global' && SCOPE_TOKENS.includes(x));
        nextScope = nonGlobal.length ? nonGlobal.join(',') : 'codex';
      } else {
        nextScope = 'global';
      }
      bulkBusy.value = true;
      // Optimistic local update so the UI reflects the flip
      // immediately, before the network round-trip.
      const idx = pages.value.findIndex(x => x.id === p.id);
      if (idx >= 0) {
        pages.value[idx] = { ...pages.value[idx], scope: nextScope };
      }
      // Partial-state rule: if the master was ON and the user
      // flipped a single card OFF, the master has to follow.
      if (masterGlobal.value && nextScope !== 'global') {
        masterGlobal.value = false;
      }
      try {
        await api.bulkScopeWiki({ scope: nextScope, page_ids: [p.id] });
      } catch (e) {
        // Roll back the optimistic update on failure.
        if (idx >= 0) {
          pages.value[idx] = p;
        }
        toast(t('wiki.globalToggleFail', { msg: e.message }), 4000);
      } finally {
        bulkBusy.value = false;
      }
    }

    /**
     * Toggle the master 全局 switch in the toolbar. Both directions
     * are now REAL bulk writes (single round-trip each):
     *
     *   * ON  → every page becomes ``scope='global'``. Master UI
     *           flips ON, every per-card knob follows.
     *   * OFF → every page loses its global flag. Each page is set
     *           to the SCOPE_OFF_DEFAULT scope (``'codex'``) so it
     *           is no longer shared with every client. Users who
     *           want a different client can still flip individual
     *           cards afterwards; the per-card toggle continues to
     *           work as before.
     *
     * Previously, master OFF was a pure local state change and
     * pages stayed global — the user reported this as confusing:
     * "若拨动关闭全局按钮，要全部关闭所有的全局设置" — flipping
     * the master OFF should actually turn off global everywhere.
     */
    async function toggleMasterGlobal() {
      if (bulkBusy.value) return;
      const turningOn = !masterGlobal.value;
      const targetScope = turningOn ? 'global' : SCOPE_OFF_DEFAULT;
      bulkBusy.value = true;
      // Optimistic local state flip so the knob reacts instantly;
      // the bulk write below will reconcile once it returns.
      masterGlobal.value = turningOn;
      try {
        const r = await api.bulkScopeWiki({ scope: targetScope });
        await refresh();
        toast(
          turningOn
            ? t('wiki.masterGlobalOn', { n: r.updated || 0 })
            : t('wiki.masterGlobalOff', { n: r.updated || 0 }),
          2200,
        );
      } catch (e) {
        // Roll back the optimistic flip on failure.
        masterGlobal.value = !turningOn;
        toast(t('wiki.globalToggleFail', { msg: e.message }), 4000);
      } finally {
        bulkBusy.value = false;
      }
    }

    // Whenever the page list changes (refresh / new distillation),
    // re-derive the master state. ON iff EVERY page is currently
    // global; OFF otherwise (the "mixed" partial-state rule).
    watch(pages, (rows) => {
      if (!Array.isArray(rows) || !rows.length) {
        masterGlobal.value = false;
        return;
      }
      // Don't clobber a "true" master while the user is mid-bulk-ON.
      if (bulkBusy.value && masterGlobal.value) return;
      masterGlobal.value = rows.every(isGlobal);
    }, { deep: true });

    return {
      store, t, pages, q, sort, scopeFilter, SCOPE_TOKENS,
      loading, visible, expanded, editing, bulletsOf,
      scopeTokensOf, scopeChipLabel,
      refresh, expand, edit, onNew, onExport, onImportClick, importing, exporting, saveEdit, removePage, fmtTime,
      isGlobal, toggleCardGlobal, toggleMasterGlobal, masterGlobal, bulkBusy,
    };
  },
  template: /* html */ `
<div class="tab-pane" id="pane-wiki">
  <div class="wiki-wrap">
    <div class="wiki-toolbar">
      <input class="wiki-q" type="text" v-model="q" :placeholder="t('wiki.searchPlaceholder')" />
      <select v-model="sort">
        <option value="updated_desc">{{ t('wiki.sort.updated') }}</option>
        <option value="importance_desc">{{ t('wiki.sort.importance') }}</option>
        <option value="title_asc">{{ t('wiki.sort.title') }}</option>
      </select>
      <select v-model="scopeFilter" :title="t('wiki.scope.hint')">
        <option value="all">{{ t('wiki.scope.filter.all') }}</option>
        <option value="global">{{ t('wiki.scope.filter.global') }}</option>
        <option value="codex">{{ t('wiki.scope.filter.codex') }}</option>
        <option value="claude">{{ t('wiki.scope.filter.claude') }}</option>
        <option value="hermes">{{ t('wiki.scope.filter.hermes') }}</option>
        <option value="openclaw">{{ t('wiki.scope.filter.openclaw') }}</option>
      </select>
      <span class="spacer"></span>
      <label class="master-global-toggle"
             :class="{ on: masterGlobal, busy: bulkBusy }"
             :title="t('wiki.masterGlobal.hint')">
        <input type="checkbox"
               :checked="masterGlobal"
               :disabled="bulkBusy"
               @change="toggleMasterGlobal" />
        <span class="mgt-knob" aria-hidden="true"></span>
        <span class="mgt-text">{{ t('wiki.masterGlobal.label') }}</span>
      </label>
      <button class="tb-action ghost" :title="t('wiki.exportTip')" :disabled="exporting" @click="onExport">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M8 1v9M4.5 6.5L8 10l3.5-3.5M2 12v2.5h12V12"/></svg>
        <span>{{ exporting ? '…' : t('wiki.export') }}</span>
      </button>
      <button class="tb-action ghost" :title="t('wiki.importTip')" :disabled="importing" @click="onImportClick">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" width="14" height="14"><path d="M8 15V6M4.5 9.5L8 6l3.5 3.5M2 2.5V0h12v2.5"/></svg>
        <span>{{ importing ? '…' : t('wiki.import') }}</span>
      </button>
      <button class="tb-action primary" @click="onNew">
        <svg viewBox="0 0 16 16" fill="currentColor" width="14" height="14"><path d="M8 1v14M1 8h14" stroke="currentColor" stroke-width="2"/></svg>
        <span>{{ t('wiki.new') }}</span>
      </button>
    </div>

    <div v-if="visible.length" class="wiki-grid">
      <article v-for="p in visible" :key="p.id" class="wiki-card">
        <div class="wc-head">
          <h3 class="wc-title">{{ p.title || p.slug }}</h3>
          <span class="wc-imp" :title="t('wiki.importance')">
            {{ Math.round((p.importance || 0) * 100) }}%
          </span>
        </div>
        <div class="wc-summary">{{ p.summary }}</div>
        <div class="wc-scopes" v-if="scopeTokensOf(p).length">
          <span class="scope-pill" v-for="tok in scopeTokensOf(p)" :key="tok"
                :class="'scope-pill-' + tok">
            {{ t(scopeChipLabel(tok)) }}
          </span>
        </div>
        <label class="card-global-toggle"
               :class="{ on: isGlobal(p), busy: bulkBusy }"
               :title="isGlobal(p) ? t('wiki.cardGlobal.onHint') : t('wiki.cardGlobal.offHint')">
          <input type="checkbox"
                 :checked="isGlobal(p)"
                 :disabled="bulkBusy"
                 @change="toggleCardGlobal(p)" />
          <span class="cgt-knob" aria-hidden="true"></span>
          <span class="cgt-text">{{ t('wiki.cardGlobal.label') }}</span>
        </label>
        <ul class="wc-bullets">
          <li v-for="(b, i) in bulletsOf(p)" :key="i">{{ b.replace(/^-\\s*/, '') }}</li>
        </ul>
        <div class="wc-tags" v-if="p.tags && p.tags.length">
          <span v-for="tag in p.tags" :key="tag" class="tag">#{{ tag }}</span>
        </div>
        <div class="wc-foot">
          <span class="wc-meta">
            <span :data-source="p.tags && p.tags.includes('fact') ? 'fact' : 'episode'">
              {{ fmtTime(p.updated_at) }}
            </span>
          </span>
          <div class="wc-actions">
            <button class="wc-btn" @click="expand(p.id)">
              {{ expanded === p.id ? t('action.close') : t('wiki.preview') }}
            </button>
            <button class="wc-btn" @click="edit(p.id)">{{ t('action.edit') }}</button>
            <button class="wc-btn danger" @click="removePage(p)">{{ t('action.delete') }}</button>
          </div>
        </div>
        <pre class="wc-body" v-if="expanded === p.id">{{ p.body }}</pre>
      </article>
    </div>
    <div class="empty" v-else-if="!loading">{{ t('wiki.empty') }}</div>
    <div class="loading" v-else>{{ t('common.loading') }}</div>

    <WikiEditor v-if="editing"
                :page-id="editing"
                @save="saveEdit"
                @cancel="editing = null" />
  </div>
</div>
  `,
});
