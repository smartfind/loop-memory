/**
 * Dashboard — Insights tab.
 *
 * Pulls the full payload from `/api/insights` (cheap, polled every 5s)
 * and renders it as a multi-pane dashboard:
 *   - KPI grid (10 metrics from overview)
 *   - Pipeline data flow (capture → recall → surface, animated arrows)
 *   - Distribution (types / status)
 *   - 24h ingest sparkline
 *   - Wiki health
 *   - Compression candidates
 *   - Per-source health
 */
import { defineComponent, ref, computed, onMounted, onUnmounted } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, fmtTime, timeAgo } from '../store.js';
import { api } from '../api.js';

const KIND_TONE = {
  episode: 'blue', fact: 'green', rule: 'amber', summary: 'purple',
  scratch: 'slate', concept: 'cyan', plan: 'rose', reflection: 'violet',
};
const STATUS_TONE = {
  active: 'green', decayed: 'amber', forgotten: 'rose', archived: 'slate',
};

export const Dashboard = defineComponent({
  name: 'Dashboard',
  setup() {
    const insights = ref(null);
    const loading = ref(false);
    const live = ref(false);
    const lastRefresh = ref(0);
    let pollHandle = null;

    async function refresh() {
      loading.value = true;
      try {
        const [stats, insightsData] = await Promise.all([
          api.stats(),
          fetch('/api/insights').then(r => r.ok ? r.json() : null).catch(() => null),
        ]);
        store.stats = {
          ...store.stats,
          memories: stats.memories,
          sessions: stats.sessions,
          wiki_pages: stats.wiki_pages || 0,
          avg_score: stats.avg_score,
          graph: insightsData
            ? `${insightsData.overview?.entities || 0}/${insightsData.overview?.links || 0}`
            : '0/0',
          dbPath: stats.path,
        };
        insights.value = insightsData;
        live.value = !!insightsData;
        lastRefresh.value = Date.now();
      } catch (e) {
        live.value = false;
      } finally {
        loading.value = false;
      }
    }

    onMounted(() => {
      refresh();
      pollHandle = setInterval(refresh, 5000);
    });
    onUnmounted(() => {
      if (pollHandle) clearInterval(pollHandle);
    });

    function onRefresh() { refresh(); }
    function onRunEvolution() { window.dispatchEvent(new CustomEvent('loop:llm-run')); }

    // Sparkline helpers
    function sparkPath(values, w = 120, h = 28) {
      if (!values || !values.length) return '';
      const nums = values.map(Number).filter(n => !isNaN(n));
      if (!nums.length) return '';
      const max = Math.max(...nums, 1);
      const step = w / Math.max(1, nums.length - 1);
      return nums.map((v, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(1)},${(h - (v / max) * h).toFixed(1)}`).join(' ');
    }
    function sparkFill(values, w = 120, h = 28) {
      const top = sparkPath(values, w, h);
      if (!top) return '';
      return `${top} L${w},${h} L0,${h} Z`;
    }
    function maxOf(arr, key) {
      if (!arr || !arr.length) return 0;
      return Math.max(...arr.map(x => key ? x[key] : x), 1);
    }

    // 7-stage pipeline labels (Chinese) for the data flow widget
    const stageLabels = {
      extracted: '采集',
      active: '活跃',
      decayed: '衰减',
      merged: '合并',
      archived: '归档',
      forgotten: '遗忘',
    };

    const pipelineSteps = computed(() => {
      const s = insights.value?.stages || {};
      const total = Object.values(s).reduce((a, b) => a + (b || 0), 0) || 1;
      return Object.entries(s).map(([k, v]) => ({
        key: k,
        label: stageLabels[k] || k,
        count: v || 0,
        pct: ((v || 0) / total * 100),
      }));
    });

    const ingest24 = computed(() => {
      const arr = insights.value?.ingest_rate || [];
      return arr.map(r => Number(r.count) || 0);
    });

    return { store, t, insights, loading, live, lastRefresh, onRefresh, fmtTime, timeAgo,
             onRunEvolution, sparkPath, sparkFill, KIND_TONE, STATUS_TONE,
             pipelineSteps, ingest24, maxOf };
  },
  template: /* html */ `
<div class="tab-pane" id="pane-dashboard">
  <div class="ins-wrap">
    <div class="ins-head">
      <div>
        <h2>{{ t('dash.ins.title') }}</h2>
        <div class="ins-sub">{{ t('dash.ins.sub') }}</div>
      </div>
      <div class="ins-actions">
        <span class="pill" :class="live ? 'live' : 'off'">
          <span class="live-dot" v-if="live"></span>
          {{ live ? t('dash.ins.live') : t('dash.ins.offline') }}
        </span>
        <button class="btn small ghost" @click="onRefresh">{{ t('dash.ins.refresh') }}</button>
        <button class="btn small primary" @click="onRunEvolution">
          <span class="ev-ico">⚡</span> {{ t('dash.ins.runEvolution') }}
        </button>
      </div>
    </div>

    <!-- 1. KPI grid: 10 live metrics -->
    <div class="ins-section" v-if="insights">
      <div class="ins-section-title">
        <span class="ico">📊</span>
        <span>{{ t('dash.ins.statsTitle') }}</span>
        <span class="bar"></span>
      </div>
      <div class="ins-kpi-10">
        <div class="ins-kpi" data-tone="blue">
          <div class="ik-label">{{ t('dash.kpi.total') }}</div>
          <div class="ik-val">{{ insights.overview.total || 0 }}</div>
          <div class="ik-sub">{{ t('dash.kpi.totalSub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="amber">
          <div class="ik-label">{{ t('dash.kpi.today') }}</div>
          <div class="ik-val">{{ insights.overview.today || 0 }}</div>
          <div class="ik-sub">{{ t('dash.kpi.todaySub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="green">
          <div class="ik-label">{{ t('dash.kpi.active') }}</div>
          <div class="ik-val">{{ insights.overview.active || 0 }}</div>
          <div class="ik-sub">{{ t('dash.kpi.activeSub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="cyan">
          <div class="ik-label">{{ t('dash.kpi.links') }}</div>
          <div class="ik-val">{{ insights.overview.links || 0 }}</div>
          <div class="ik-sub">{{ t('dash.kpi.linksSub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="purple">
          <div class="ik-label">{{ t('dash.kpi.entities') }}</div>
          <div class="ik-val">{{ insights.overview.entities || 0 }}</div>
          <div class="ik-sub">{{ t('dash.kpi.entitiesSub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="rose">
          <div class="ik-label">{{ t('dash.kpi.clusters') }}</div>
          <div class="ik-val">{{ insights.overview.clusters || 0 }}</div>
          <div class="ik-sub">{{ t('dash.kpi.clustersSub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="violet">
          <div class="ik-label">{{ t('dash.kpi.avg') }}</div>
          <div class="ik-val">{{ insights.overview.avg_score ? Math.round(insights.overview.avg_score * 100) + '%' : '—' }}</div>
          <div class="ik-sub">{{ t('dash.kpi.avgSub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="slate">
          <div class="ik-label">{{ t('dash.kpi.decay') }}</div>
          <div class="ik-val">{{ insights.overview.decay_pct ? insights.overview.decay_pct.toFixed(0) + '%' : '—' }}</div>
          <div class="ik-sub">{{ t('dash.kpi.decaySub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="emerald">
          <div class="ik-label">{{ t('dash.kpi.wikiHealth') }}</div>
          <div class="ik-val">{{ store.stats.wiki_pages || 0 }}</div>
          <div class="ik-sub">{{ t('dash.kpi.wikiHealthSub') }}</div>
        </div>
        <div class="ins-kpi" data-tone="blue-soft">
          <div class="ik-label">{{ t('dash.kpi.recall24h') }}</div>
          <div class="ik-val">{{ insights.recall_24h?.total || 0 }}</div>
          <div class="ik-sub">{{ insights.recall_24h?.unique_memories || 0 }} {{ t('dash.kpi.recall24hSub') }}</div>
        </div>
      </div>
    </div>

    <!-- 2. Data flow pipeline -->
    <div class="ins-section" v-if="insights && pipelineSteps.length">
      <div class="ins-section-title">
        <span class="ico">🔄</span>
        <span>数据流转 · {{ t('dash.pipe.title') }}</span>
        <span class="bar"></span>
      </div>
      <div class="data-flow">
        <div v-for="(step, i) in pipelineSteps" :key="step.key" class="flow-step"
             :class="'tone-' + (['blue','green','amber','violet','slate','rose'][i % 6])">
          <div class="flow-num">{{ step.count }}</div>
          <div class="flow-label">{{ step.label }}</div>
          <div class="flow-bar"><div class="flow-fill" :style="{ width: step.pct + '%' }"></div></div>
          <div v-if="i < pipelineSteps.length - 1" class="flow-arrow">→</div>
        </div>
      </div>
    </div>

    <!-- 3. Two-column: Distribution + Ingest rate -->
    <div class="ins-grid-2" v-if="insights">
      <!-- Memory type distribution -->
      <div class="ins-section">
        <div class="ins-section-title">
          <span class="ico">🧩</span>
          <span>记忆类型分布</span>
        </div>
        <div class="dist-list" v-if="insights.distribution && insights.distribution.types && insights.distribution.types.length">
          <div v-for="row in insights.distribution.types" :key="row.kind" class="dist-row">
            <span class="dist-label">
              <span class="kind" :class="'kind-' + row.kind">{{ row.kind }}</span>
            </span>
            <span class="dist-bar">
              <span class="dist-fill"
                    :style="{ width: ((row.count / Math.max(...insights.distribution.types.map(r => r.count), 1)) * 100) + '%' }"></span>
            </span>
            <span class="dist-val">{{ row.count }}</span>
          </div>
        </div>
        <div v-else class="empty">—</div>
      </div>

      <!-- Status distribution -->
      <div class="ins-section">
        <div class="ins-section-title">
          <span class="ico">📈</span>
          <span>状态分布</span>
        </div>
        <div class="dist-list" v-if="insights.distribution && insights.distribution.status && insights.distribution.status.length">
          <div v-for="row in insights.distribution.status" :key="row.status" class="dist-row">
            <span class="dist-label">
              <span class="status-dot" :data-tone="STATUS_TONE[row.status] || 'slate'"></span>
              {{ row.status }}
            </span>
            <span class="dist-bar">
              <span class="dist-fill"
                    :class="'tone-' + (STATUS_TONE[row.status] || 'slate')"
                    :style="{ width: ((row.count / Math.max(...insights.distribution.status.map(r => r.count), 1)) * 100) + '%' }"></span>
            </span>
            <span class="dist-val">{{ row.count }}</span>
          </div>
        </div>
        <div v-else class="empty">—</div>
      </div>
    </div>

    <!-- 4. Sparkline row: 24h ingest + sources + wiki health -->
    <div class="ins-grid-3" v-if="insights">
      <div class="ins-section">
        <div class="ins-section-title">
          <span class="ico">⏱️</span>
          <span>近 24h 采集</span>
        </div>
        <svg class="sparkline" viewBox="0 0 120 32" preserveAspectRatio="none" v-if="ingest24.length">
          <path :d="sparkFill(ingest24, 120, 30)" class="spark-fill" />
          <path :d="sparkPath(ingest24, 120, 30)" class="spark-line" />
        </svg>
        <div class="spark-total">
          <strong>{{ ingest24.reduce((a, b) => a + b, 0) }}</strong>
          <span>总采集 (近24h)</span>
        </div>
      </div>

      <div class="ins-section">
        <div class="ins-section-title">
          <span class="ico">📡</span>
          <span>来源健康度</span>
        </div>
        <div class="src-list" v-if="insights.sources && insights.sources.length">
          <div v-for="s in insights.sources" :key="s.source || s.name" class="src-row">
            <span class="src-badge" :data-source="s.source">{{ s.source || '—' }}</span>
            <span class="src-count">{{ s.count || 0 }}</span>
            <span class="src-last">{{ s.last_seen ? timeAgo(s.last_seen) : '—' }}</span>
          </div>
        </div>
        <div v-else class="empty">—</div>
      </div>

      <div class="ins-section">
        <div class="ins-section-title">
          <span class="ico">📚</span>
          <span>知识库健康度</span>
        </div>
        <div v-if="insights.wiki_health" class="wiki-health">
          <div class="wh-row"><span>页面数</span><strong>{{ insights.wiki_health.pages || 0 }}</strong></div>
          <div class="wh-row"><span>平均重要度</span><strong>{{ insights.wiki_health.avg_importance ? (insights.wiki_health.avg_importance * 100).toFixed(0) + '%' : '—' }}</strong></div>
          <div class="wh-row"><span>字符数</span><strong>{{ insights.wiki_health.total_chars || 0 }}</strong></div>
          <div class="wh-row"><span>被引用记忆</span><strong>{{ insights.wiki_health.referenced_memories || 0 }}</strong></div>
        </div>
      </div>
    </div>

    <!-- 5. Compression candidates (top items) -->
    <div class="ins-section" v-if="insights && insights.compression && insights.compression.items">
      <div class="ins-section-title">
        <span class="ico">🗜️</span>
        <span>待压缩记忆 · {{ insights.compression.compressible_count || 0 }} 条</span>
        <span class="bar"></span>
        <span class="muted">平均长度 {{ insights.compression.avg_length || 0 }} 字符</span>
      </div>
      <div class="cmp-list">
        <div v-for="item in insights.compression.items.slice(0, 5)" :key="item.id" class="cmp-item">
          <span class="kind" :class="'kind-' + (item.kind || 'episode')">{{ item.kind }}</span>
          <span class="cmp-preview">{{ item.preview }}</span>
          <span class="cmp-meta">
            <span class="cmp-imp" :title="'importance ' + (item.importance || 0)">
              {{ Math.round((item.importance || 0) * 100) }}%
            </span>
            <span v-if="item.in_wiki" class="cmp-wiki-tag">已入库</span>
          </span>
        </div>
      </div>
    </div>

    <div v-if="loading && !insights" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="!insights && !loading" class="empty">{{ t('dash.ins.offline') }}</div>
  </div>
</div>
`,
});
