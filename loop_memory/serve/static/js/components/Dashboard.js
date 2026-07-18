/**
 * Dashboard — Insights tab.
 *
 * Renders 11 KPI tiles (with sparklines) + 3 ring meters + lifecycle +
 * pulse + compression + granularity + distribution + sources + pipeline
 * latency + health/weekly + LLM audit + write-guard + architecture loop.
 * Every section defends against missing fields so a partial /api/insights
 * payload still renders.
 *
 * Faithful to the legacy vanilla-JS dashboard (pre-Vue commit 8498eca):
 * - 11 KPIs each with an `ik-spark` SVG fed by a rolling 60-sample history.
 * - Sub-labels include live numbers (`from N sources`, `N/M of total`, …).
 * - Each ring card has both the SVG centre value AND an `irc-val` below.
 * - Architecture diagram has a title + subtitle above the ring, emoji
 *   icons inside each node, and a file-anchor strip below.
 * - WriteGuard header shows uptime (time since first audit record).
 */
import { defineComponent, ref, computed, onMounted, onUnmounted } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, timeAgo, toast } from '../store.js';
import { api } from '../api.js';

const SVGNS = 'http://www.w3.org/2000/svg';
const KIND_TONE = {
  episode: 'blue', fact: 'green', rule: 'amber', summary: 'purple',
  scratch: 'slate', concept: 'cyan', plan: 'rose', reflection: 'violet',
};
const STATUS_TONE = {
  active: 'green', decayed: 'amber', forgotten: 'rose', archived: 'slate',
};
const SOURCE_COLORS = ['#6366f1', '#10b981', '#f59e0b', '#ec4899', '#06b6d4', '#a855f7', '#f43f5e'];

// 7 stages with both an `icon` (emoji) and a `file` anchor used in the
// bottom strip of the architecture diagram.
const STAGE_DEFS = [
  { key: 'capture',  tone: 'blue',   icon: '📥', file: 'cli/main.py hook' },
  { key: 'reflect',  tone: 'green',  icon: '🪞', file: 'engine/reflect.py' },
  { key: 'score',    tone: 'amber',  icon: '⚖', file: 'jobs/consolidate.py' },
  { key: 'store',    tone: 'purple', icon: '🗄', file: 'storage/sqlite_store.py' },
  { key: 'recall',   tone: 'cyan',   icon: '🔍', file: 'serve/app.py /api/recall' },
  { key: 'surface',  tone: 'rose',   icon: '📖', file: 'mcp/ + graph/' },
  { key: 'loopback', tone: 'amber',  icon: '🔁', file: 'cli/main.py install-hooks' },
];

// Pre-computed geometry for the architecture ring. R = ring radius, RR =
// arc radius (slightly outside the ring so the animated arrow doesn't
// cross the node cards).
const ARCH = (() => {
  const W = 1200, H = 660;
  const cx = 600, cy = 330, R = 220, RR = R + 14;
  const positions = STAGE_DEFS.map((s, i) => {
    const a = -Math.PI / 2 + i * (2 * Math.PI / STAGE_DEFS.length);
    return { ...s, idx: i, a, x: cx + Math.cos(a) * R, y: cy + Math.sin(a) * R };
  });
  const arcs = positions.map((p, i) => {
    const n = STAGE_DEFS.length;
    const a1 = p.a + 0.32;
    const a2 = positions[(i + 1) % n].a - 0.32;
    const x1 = cx + Math.cos(a1) * RR;
    const y1 = cy + Math.sin(a1) * RR;
    const x2 = cx + Math.cos(a2) * RR;
    const y2 = cy + Math.sin(a2) * RR;
    return { d: 'M' + x1.toFixed(1) + ' ' + y1.toFixed(1)
      + ' A' + RR + ' ' + RR + ' 0 0 1 '
      + x2.toFixed(1) + ' ' + y2.toFixed(1) };
  });
  // Spokes from each node to the hub edge.
  const spokes = positions.map((p) => {
    const dx = cx - p.x, dy = cy - p.y;
    const d = Math.sqrt(dx * dx + dy * dy);
    const ux = dx / d, uy = dy / d;
    const x1 = p.x + ux * 67;     // NODE_W/2 + 2
    const y1 = p.y + uy * 30;     // NODE_H/2 + 2
    const x2 = cx - ux * 80;      // hub radius 78
    const y2 = cy - uy * 80;
    return { x1, y1, x2, y2 };
  });
  // File anchor strip at the bottom: 7 columns.
  const legendY = H - 34;
  const colW = W / STAGE_DEFS.length;
  const anchors = STAGE_DEFS.map((s, i) => ({
    key: s.key,
    file: s.file,
    cx: colW * i + colW / 2,
    y: legendY,
  }));
  return { W, H, cx, cy, R, positions, arcs, spokes, anchors };
})();

function fmtNum(v) { return Number(v || 0).toLocaleString(); }
function truncate(s, n = 28) {
  if (!s) return '';
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
function safeArr(x) { return Array.isArray(x) ? x : []; }
function shortenPath(s, max = 22) {
  if (!s) return '';
  return s.length > max ? '…' + s.slice(-(max - 1)) : s;
}

// Format a duration in seconds as "Nd Nh" / "Nh Nm" / "Nm Ns".
function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export const Dashboard = defineComponent({
  name: 'Dashboard',
  setup() {
    const insights = ref(null);
    const weeklyReport = ref(null);
    const weeklyDays = ref(7);
    const weeklyLoading = ref(false);
    const weeklyError = ref('');
    const llmAudit = ref(null);
    const writeGuard = ref(null);
    const sourceHealth = ref(null);
    const loading = ref(false);
    const live = ref(false);
    const lastRefresh = ref(0);
    const resolvingId = ref('');

    // Rolling history arrays for sparklines (max 60 samples).
    const hist = {
      total: [], today: [], active: [], links: [], clusters: [],
      avg: [], decay: [], entities: [],
    };

    let pollHandle = null;
    const resolvedPairs = new Set();

    function contradictionKey(pair) {
      const ids = [pair?.a?.id || '', pair?.b?.id || ''].sort();
      return `${ids[0]}|${ids[1]}`;
    }

    function pushHistory(arr, v) {
      arr.push(v);
      if (arr.length > 60) arr.shift();
    }

    // Build an SVG `d` attribute for a sparkline.
    function sparkPath(series, w = 100, h = 18) {
      if (!series || series.length < 2) return '';
      const max = Math.max(...series, 1);
      const min = Math.min(...series, 0);
      const range = (max - min) || 1;
      const step = w / (series.length - 1);
      let d = '';
      series.forEach((v, i) => {
        const x = i * step;
        const y = h - ((v - min) / range) * (h - 4) - 2;
        d += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ' ' + y.toFixed(1) + ' ';
      });
      return d.trim();
    }

    async function refresh() {
      loading.value = true;
      try {
        const [stats, insightsData, health, audit, guard] = await Promise.all([
          api.stats().catch(() => null),
          fetch('/api/insights').then(r => r.ok ? r.json() : null).catch(() => null),
          fetch('/api/source-health').then(r => r.ok ? r.json() : null).catch(() => null),
          fetch('/api/llm-audit?limit=24').then(r => r.ok ? r.json() : null).catch(() => null),
          fetch('/api/write-guard').then(r => r.ok ? r.json() : null).catch(() => null),
        ]);
        if (stats) {
          store.stats = {
            ...store.stats,
            memories: stats.memories,
            sessions: stats.sessions,
            wiki_pages: stats.wiki_pages || 0,
            avg_score: stats.avg_score,
            graph: insightsData
              ? `${insightsData.overview?.entities || 0}/${insightsData.overview?.links || 0}`
              : store.stats.graph,
            dbPath: stats.path,
          };
        }
        if (insightsData?.pulse?.contradictions) {
          insightsData.pulse.contradictions = insightsData.pulse.contradictions
            .filter(pair => !resolvedPairs.has(contradictionKey(pair)));
        }
        insights.value = insightsData;
        live.value = !!insightsData;
        sourceHealth.value = health;
        llmAudit.value = audit;
        writeGuard.value = guard;
        lastRefresh.value = Date.now();

        // Update sparkline history when new insights arrive.
        if (insightsData?.overview) {
          const o = insightsData.overview;
          pushHistory(hist.total, o.total || 0);
          pushHistory(hist.today, o.today || 0);
          pushHistory(hist.active, o.active || 0);
          pushHistory(hist.links, o.links || 0);
          pushHistory(hist.clusters, o.clusters || 0);
          pushHistory(hist.avg, o.avg_score || 0);
          pushHistory(hist.decay, o.decay_pct || 0);
          pushHistory(hist.entities, o.entities || 0);
        }
      } catch (e) { live.value = false; }
      finally { loading.value = false; }
    }

    async function loadWeekly(days = 7) {
      weeklyLoading.value = true;
      weeklyError.value = '';
      try {
        const res = await fetch(`/api/weekly-report?days=${days}&nocache=${Date.now()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        weeklyReport.value = await res.json();
        weeklyDays.value = days;
      } catch (e) { weeklyError.value = e?.message || 'failed'; }
      finally { weeklyLoading.value = false; }
    }

    async function resolvePair(pair, action) {
      const aId = pair?.a?.id, bId = pair?.b?.id;
      if (!aId || !bId) return;
      const key = `${contradictionKey(pair)}|${action}`;
      resolvingId.value = key;
      try {
        const url = `/api/contradictions/resolve?a=${encodeURIComponent(aId)}&b=${encodeURIComponent(bId)}&action=${encodeURIComponent(action)}`;
        const res = await fetch(url, { method: 'POST' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        resolvedPairs.add(contradictionKey(pair));
        if (insights.value?.pulse?.contradictions) {
          insights.value.pulse.contradictions = insights.value.pulse.contradictions
            .filter(item => contradictionKey(item) !== contradictionKey(pair));
        }
        toast(action === 'ignore' ? t('dash.pulse.resolvedIgnore') : t('dash.pulse.resolvedDel'), 2400);
        void refresh();
      } catch (e) {
        toast(t('dash.pulse.resolvedErr') + (e?.message || 'resolve failed'), 3200);
      }
      finally { resolvingId.value = ''; }
    }

    async function copyWeekly() {
      const md = weeklyReport.value?.markdown || '';
      if (!md) return;
      try { await navigator.clipboard?.writeText(md); } catch (e) { /* ignore */ }
    }

    onMounted(() => {
      refresh();
      pollHandle = setInterval(refresh, 6000);
      // Weekly report is intentionally NOT refreshed in the polling loop
      // (it is heavy and flickers the markdown content). Fetch only on
      // explicit user action or when the user navigates back to the tab.
      loadWeekly(weeklyDays.value);
    });
    onUnmounted(() => { if (pollHandle) clearInterval(pollHandle); });

    function ring(pct) {
      const v = Math.max(0, Math.min(1, (pct || 0) / 100));
      const r = 22, c = 2 * Math.PI * r;
      return { dash: c, offset: c * (1 - v) };
    }

    function donutArcPaths(rows) {
      const items = safeArr(rows);
      const total = items.reduce((a, b) => a + (b.count || 0), 0) || 1;
      const r = 46, c = 2 * Math.PI * r;
      let acc = 0;
      return items.map((row, idx) => {
        const start = acc / total;
        acc += row.count;
        const end = acc / total;
        return {
          color: SOURCE_COLORS[idx % SOURCE_COLORS.length],
          dasharray: `${c * (end - start)} ${c}`,
          dashoffset: -c * start,
        };
      });
    }

    function trendPoints(rows, w = 300, h = 140) {
      const items = safeArr(rows);
      if (!items.length) return { line: '', area: '', ticks: [], max: 1 };
      const max = Math.max(...items.map(r => r.count), 1);
      const step = w / Math.max(1, items.length - 1);
      const pts = items.map((r, i) => [i * step, h - (r.count / max) * (h - 8) - 4]);
      const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
      const area = `${line} L${w},${h} L0,${h} Z`;
      const ticks = items.map((r, i) => ({ x: i * step, label: r.date ? r.date.slice(5) : '', value: r.count }));
      return { line, area, ticks, max };
    }

    function ingestBars(rows, w = 360, h = 118) {
      // 24 buckets. Layout: 24 bars in w, but with breathing room. We expose
      // x/y/width/height plus an `axis` position and a `tier` flag so the
      // template can render the small hour ticks on every 4-hour mark.
      const items = safeArr(rows);
      const max = Math.max(...items.map(r => r.count || 0), 1);
      const peak = Math.max(...items.map(r => r.count || 0), 0);
      const plot = { left: 4, right: 4, top: 8, bottom: 18 };
      const innerW = w - plot.left - plot.right;
      const innerH = h - plot.top - plot.bottom;
      const baseline = plot.top + innerH;
      const bw = innerW / Math.max(1, items.length);
      return items.map((r, i) => {
        const hourNum = Number((r.hour || '0').split(':')[0]) || 0;
        const barH = Math.max(2, ((r.count || 0) / max) * innerH);
        return {
          x: plot.left + i * bw + Math.max(0.5, (bw - Math.max(2, bw - 2)) / 2),
          w: Math.max(2, bw - 2),
          y: baseline - barH,
          h: barH,
          baseline,
          tickY: baseline + 4,
          lblY: baseline + 14,
          tick: hourNum % 4 === 0,           // major tick every 4h
          label: hourNum % 4 === 0 ? String(hourNum).padStart(2, '0') : '',
          isPeak: r.count === peak && peak > 0,
          active: r.count > 0,
          hour: r.hour,
          count: r.count,
        };
      });
    }

    function barsFor(items, w = 360, h = 230) {
      // 10 score bands. We give 3 layout regions inside the SVG height:
      //   y[top:28 .. baseline:182] : bars
      //   y[tickY:182 .. labelY:198] : axis tick + short label
      //   y[metaY:214]               : optional per-bar full range (rare)
      const list = safeArr(items);
      const max = Math.max(...list.map(r => r.count || 0), 1);
      const plot = { left: 14, right: 6, top: 28, bottom: 56 };
      const innerW = w - plot.left - plot.right;
      const innerH = h - plot.top - plot.bottom;
      const baseline = plot.top + innerH; // 174
      const bw = innerW / Math.max(1, list.length);
      const fmtShort = (v) => (v >= 1 ? '1' : v === 0 ? '0' : `.${Math.round(v * 10)}`);
      return list.map((r, i) => {
        const upper = r.range ? r.range[1] : null;
        const lower = r.range ? r.range[0] : null;
        return {
          x: plot.left + i * bw + (bw > 18 ? 4 : 1),
          w: Math.max(6, bw - (bw > 18 ? 8 : 2)),
          y: baseline - Math.max(2, ((r.count || 0) / max) * innerH),
          h: Math.max(2, ((r.count || 0) / max) * innerH),
          baseline,
          countY: baseline - Math.max(2, ((r.count || 0) / max) * innerH) - 7,
          tickY: baseline + 8,
          labelY: baseline + 22,
          // short label: just the upper boundary, displayed for every other
          // bucket to keep the axis readable at narrow widths.
          shortLabel: upper != null ? fmtShort(upper) : '',
          // full range label kept for the (rare) tooltip/full view use
          label: (lower != null && upper != null) ? `${lower.toFixed(1)}–${upper.toFixed(1)}` : '',
          showShortLabel: i % 2 === 1, // 0, 2, 4... hide; 1, 3, 5... show
          peak: r.count > 0 && r.count === max,
          count: r.count,
        };
      });
    }

    function scoreDistributionTotal(items) {
      return safeArr(items).reduce((sum, item) => sum + (item.count || 0), 0);
    }

    function peakScoreRange(items) {
      const list = safeArr(items);
      if (!list.length) return '—';
      const peak = list.reduce((best, item) => (item.count || 0) > (best.count || 0) ? item : best, list[0]);
      return peak.range ? `${peak.range[0].toFixed(1)}–${peak.range[1].toFixed(1)}` : '—';
    }

    function sourceBars(sources) {
      const items = safeArr(sources);
      const total = items.reduce((a, b) => a + (b.count || 0), 0) || 1;
      return items.map((s, i) => ({
        ...s, pct: (s.count / total) * 100,
        color: SOURCE_COLORS[i % SOURCE_COLORS.length],
      }));
    }

    function pipelineBars(rows) {
      const items = safeArr(rows);
      const max = Math.max(...items.map(r => r.avg_ms || 0), 1);
      return items.map(r => ({ ...r, pct: ((r.avg_ms || 0) / max) * 100 }));
    }

    function ingestPeakHour(rows) {
      const items = safeArr(rows);
      if (!items.length) return null;
      const peak = items.reduce((best, r) => (r.count || 0) > (best.count || 0) ? r : best, items[0]);
      if (!peak || !(peak.count > 0)) return null;
      const total = items.reduce((a, b) => a + (b.count || 0), 0) || 1;
      return `${peak.hour} · ${peak.count} (${Math.round(((peak.count || 0) / total) * 100)}%)`;
    }

    function lifecycleSegments(stages) {
      const stg = stages || {};
      const labels = {
        extracted: t('dash.lc.extracted'), active: t('dash.lc.active'), decayed: t('dash.lc.decayed'),
        merged: t('dash.lc.merged'), archived: t('dash.lc.archived'), forgotten: t('dash.lc.forgotten'),
      };
      const tones = {
        extracted: 'blue', active: 'green', decayed: 'amber',
        merged: 'purple', archived: 'slate', forgotten: 'rose',
      };
      const icons = {
        extracted: '📥', active: '⚡', decayed: '📉',
        merged: '⊕', archived: '🗄', forgotten: '🗑',
      };
      const order = ['extracted', 'active', 'decayed', 'merged', 'archived', 'forgotten'];
      const total = order.reduce((a, k) => a + (stg[k] || 0), 0) || 1;
      let acc = 0;
      return order.map(k => {
        const pct = ((stg[k] || 0) / total) * 100;
        const seg = { key: k, label: labels[k], tone: tones[k], icon: icons[k], count: stg[k] || 0, pct, x: acc };
        acc += pct;
        return seg;
      });
    }

    // Uptime derived from the oldest LLM audit record (matches the
    // legacy behaviour of "server has been recording for N").
    const guardUptime = computed(() => {
      const recent = llmAudit.value?.recent || [];
      if (!recent.length) return '—';
      const oldest = Math.min(...recent.map(r => r.ts || 0));
      if (!oldest) return '—';
      return fmtDuration(Date.now() / 1000 - oldest);
    });

    // reactive aliases for i18n
    void computed(() => store.lang);

    return {
      store, t, insights, loading, live, lastRefresh,
      weeklyReport, weeklyLoading, weeklyError, weeklyDays, loadWeekly, copyWeekly,
      llmAudit, writeGuard, sourceHealth,
      resolvingId, resolvePair, contradictionKey,
      KIND_TONE, STATUS_TONE, SOURCE_COLORS, STAGE_DEFS, SVGNS, ARCH,
      fmtNum, truncate, timeAgo, sparkPath, fmtDuration, shortenPath,
      guardUptime, hist,
      ring, donutArcPaths, trendPoints, ingestBars, ingestPeakHour, barsFor,
      scoreDistributionTotal, peakScoreRange,
      sourceBars, pipelineBars, lifecycleSegments,
      onRefresh: refresh,
      onRunEvolution: () => window.dispatchEvent(new CustomEvent('loop:llm-run')),
    };
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

    <!-- 1. KPI tiles (11) + ring meters -->
    <div class="ins-section" v-if="insights">
      <div class="ins-section-title">
        <span class="ico">📊</span>
        <span>{{ t('dash.ins.statsTitle') }}</span>
        <span class="bar"></span>
        <span class="right">{{ fmtNum(insights.overview && insights.overview.total) }} {{ t('dash.kpi.totalLabel') }}</span>
      </div>

      <div class="ins-kpi-11">
        <div class="ins-kpi-primary">
          <div class="ins-kpi primary-metric" data-tone="blue">
            <div class="ik-label">{{ t('dash.kpi.total') }}</div>
            <div class="ik-val">{{ fmtNum(insights.overview && insights.overview.total) }}</div>
            <div class="ik-sub">{{ t('dash.kpi.totalSub2') }}{{ (insights.sources || []).length }} {{ t('dash.kpi.sourceCountUnit') }}</div>
            <svg class="ik-spark" viewBox="0 0 100 18" preserveAspectRatio="none"><path :d="sparkPath(hist.total)"></path></svg>
          </div>
          <div class="ins-kpi primary-metric" data-tone="green">
            <div class="ik-label">{{ t('dash.kpi.today') }}</div>
            <div class="ik-val">{{ fmtNum(insights.overview && insights.overview.today) }}</div>
            <div class="ik-sub">{{ t('dash.kpi.todaySub') }}</div>
            <svg class="ik-spark" viewBox="0 0 100 18" preserveAspectRatio="none"><path :d="sparkPath(hist.today)"></path></svg>
          </div>
          <div class="ins-kpi primary-metric" data-tone="amber">
            <div class="ik-label">{{ t('dash.kpi.active') }}</div>
            <div class="ik-val">{{ fmtNum(insights.overview && insights.overview.active) }}</div>
            <div class="ik-sub">{{ fmtNum(insights.overview && insights.overview.active) }}/{{ fmtNum(insights.overview && insights.overview.total) }} {{ t('dash.kpi.ofTotal') }}</div>
            <svg class="ik-spark" viewBox="0 0 100 18" preserveAspectRatio="none"><path :d="sparkPath(hist.active)"></path></svg>
          </div>
          <div class="ins-kpi primary-metric" data-tone="purple">
            <div class="ik-label">{{ t('dash.kpi.avg') }}</div>
            <div class="ik-val">{{ ((insights.overview && insights.overview.avg_score || 0) * 100).toFixed(0) }}%</div>
            <div class="ik-sub">{{ t('dash.kpi.avgSub') }}</div>
            <svg class="ik-spark" viewBox="0 0 100 18" preserveAspectRatio="none"><path :d="sparkPath(hist.avg)"></path></svg>
          </div>
        </div>
        <div class="ins-kpi-secondary">
          <div class="ins-kpi compact-metric" data-tone="purple">
            <div class="ik-label">{{ t('dash.kpi.links') }}</div>
            <div class="ik-val">{{ fmtNum(insights.overview && insights.overview.links) }}</div>
            <div class="ik-sub">{{ t('dash.kpi.linksSub') }}</div>
          </div>
          <div class="ins-kpi compact-metric" data-tone="cyan">
            <div class="ik-label">{{ t('dash.kpi.clusters') }}</div>
            <div class="ik-val">{{ fmtNum(insights.overview && insights.overview.clusters) }}</div>
            <div class="ik-sub">{{ fmtNum(insights.overview && insights.overview.clusters) }} {{ t('dash.kpi.groups') }}</div>
          </div>
          <div class="ins-kpi compact-metric" data-tone="rose">
            <div class="ik-label">{{ t('dash.kpi.decay') }}</div>
            <div class="ik-val">{{ ((insights.overview && insights.overview.decay_pct) || 0).toFixed(0) }}%</div>
            <div class="ik-sub">{{ t('dash.kpi.decaySub') }}</div>
          </div>
          <div class="ins-kpi compact-metric" data-tone="purple">
            <div class="ik-label">{{ t('dash.kpi.entities') }}</div>
            <div class="ik-val">{{ fmtNum(insights.overview && insights.overview.entities) }}</div>
            <div class="ik-sub">{{ t('dash.kpi.entitiesSub') }}</div>
          </div>
          <div class="ins-kpi compact-metric" data-tone="cyan">
            <div class="ik-label">{{ t('dash.kpi.sources') }}</div>
            <div class="ik-val">{{ fmtNum((insights.sources || []).reduce((a, b) => a + (b.count || 0), 0)) }}</div>
            <div class="ik-sub">{{ (insights.sources || []).length }} {{ t('dash.kpi.distinct') }}</div>
          </div>
          <div class="ins-kpi compact-metric" data-tone="amber">
            <div class="ik-label">{{ t('dash.kpi.wikiHealth') }}</div>
            <div class="ik-val">{{ fmtNum(insights.wiki_health && insights.wiki_health.pages) }}</div>
            <div class="ik-sub">{{ fmtNum(insights.wiki_health && insights.wiki_health.pages) }} {{ t('dash.kpi.pages') }} · {{ t('dash.src.wikiImp') }} {{ insights.wiki_health && insights.wiki_health.avg_importance ? Math.round(insights.wiki_health.avg_importance * 100) + '%' : '—' }}</div>
          </div>
          <div class="ins-kpi compact-metric" data-tone="rose">
            <div class="ik-label">{{ t('dash.kpi.recall24h') }}</div>
            <div class="ik-val">{{ fmtNum(insights.recall_24h && insights.recall_24h.total) }}</div>
            <div class="ik-sub">{{ fmtNum(insights.recall_24h && insights.recall_24h.unique_memories) }} {{ t('dash.kpi.uniqueMems') }}</div>
          </div>
        </div>
      </div>

      <div class="ins-rings">
        <div class="ins-ring-card">
          <svg class="irc-svg" viewBox="0 0 64 64">
            <circle cx="32" cy="32" r="22" class="ins-ring-track"></circle>
            <circle cx="32" cy="32" r="22" class="ins-ring-arc green"
              :stroke-dasharray="ring(insights.overview && insights.overview.occupation).dash"
              :stroke-dashoffset="ring(insights.overview && insights.overview.occupation).offset"></circle>
            <text x="32" y="34" class="ins-ring-text">{{ ((insights.overview && insights.overview.occupation) || 0).toFixed(0) }}%</text>
          </svg>
          <div class="irc-info">
            <div class="irc-label"><span class="ico">💎</span>{{ t('dash.ring.occ') }}</div>
            <div class="irc-val">{{ ((insights.overview && insights.overview.occupation) || 0).toFixed(0) }}%</div>
            <div class="irc-sub">{{ t('dash.ring.occSub') }}</div>
          </div>
        </div>
        <div class="ins-ring-card">
          <svg class="irc-svg" viewBox="0 0 64 64">
            <circle cx="32" cy="32" r="22" class="ins-ring-track"></circle>
            <circle cx="32" cy="32" r="22" class="ins-ring-arc blue"
              :stroke-dasharray="ring(insights.overview && insights.overview.citation).dash"
              :stroke-dashoffset="ring(insights.overview && insights.overview.citation).offset"></circle>
            <text x="32" y="34" class="ins-ring-text">{{ ((insights.overview && insights.overview.citation) || 0).toFixed(0) }}%</text>
          </svg>
          <div class="irc-info">
            <div class="irc-label"><span class="ico">🔗</span>{{ t('dash.ring.cite') }}</div>
            <div class="irc-val">{{ ((insights.overview && insights.overview.citation) || 0).toFixed(0) }}%</div>
            <div class="irc-sub">{{ t('dash.ring.citeSub') }}</div>
          </div>
        </div>
        <div class="ins-ring-card">
          <svg class="irc-svg" viewBox="0 0 64 64">
            <circle cx="32" cy="32" r="22" class="ins-ring-track"></circle>
            <circle cx="32" cy="32" r="22" class="ins-ring-arc rose"
              :stroke-dasharray="ring(insights.overview && insights.overview.decay).dash"
              :stroke-dashoffset="ring(insights.overview && insights.overview.decay).offset"></circle>
            <text x="32" y="34" class="ins-ring-text">{{ ((insights.overview && insights.overview.decay) || 0).toFixed(0) }}%</text>
          </svg>
          <div class="irc-info">
            <div class="irc-label"><span class="ico">📉</span>{{ t('dash.ring.decay') }}</div>
            <div class="irc-val">{{ ((insights.overview && insights.overview.decay) || 0).toFixed(0) }}%</div>
            <div class="irc-sub">{{ t('dash.ring.decaySub') }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 2. Lifecycle -->
    <div class="ins-section" v-if="insights">
      <div class="ins-section-title">
        <span class="ico">🔁</span>
        <span>{{ t('dash.lc.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.lc.totalLabel') }} {{ fmtNum(Object.values(insights.stages || {}).reduce((a,b)=>a+(b||0),0)) }}</span>
      </div>
      <div class="ins-lifecycle">
        <div v-for="(seg, index) in lifecycleSegments(insights.stages)" :key="seg.key"
             class="ins-lc-stage" :class="{ active: seg.count > 0 }" :data-tone="seg.tone">
          <div class="ins-lc-icon" :data-tone="seg.tone">{{ seg.icon }}</div>
          <div class="ins-lc-val">{{ fmtNum(seg.count) }}</div>
          <div class="ins-lc-label">{{ seg.label }}</div>
          <div class="ins-lc-pct">{{ seg.pct.toFixed(0) }}%</div>
          <div v-if="index < 5" class="ins-lc-flow" aria-hidden="true">
            <span class="ins-lc-particle p1"></span>
            <span class="ins-lc-particle p2"></span>
            <span class="ins-lc-particle p3"></span>
          </div>
        </div>
      </div>
    </div>

    <!-- 3. Self-improvement Pulse -->
    <div class="ins-section" v-if="insights && insights.pulse">
      <div class="ins-section-title">
        <span class="ico">🧬</span>
        <span>{{ t('dash.pulse.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.pulse.sub') }}</span>
      </div>
      <div class="ins-pulse">
        <div class="ins-pulse-card">
          <div class="ins-pulse-head">
            <span class="ins-pulse-title"><span class="label-icon">⚠</span>{{ t('dash.pulse.contradict') }}</span>
            <span class="ins-pulse-count">{{ (insights.pulse.contradictions || []).length }} {{ t('dash.pulse.pairs') }}</span>
          </div>
          <div v-if="!(insights.pulse.contradictions || []).length" class="ins-pulse-empty">
            {{ t('dash.pulse.noConflicts') }}
          </div>
          <div v-else class="ins-citem-list">
            <div v-for="pair in (insights.pulse.contradictions || []).slice(0, 4)"
                 :key="(pair.a && pair.a.id || '') + '|' + (pair.b && pair.b.id || '')"
                 class="ins-citem" :class="{ resolving: resolvingId.startsWith(contradictionKey(pair) + '|') }">
              <div class="ins-citem-a"><b>A</b> · {{ truncate(pair.a && pair.a.text, 50) }}</div>
              <div class="ins-citem-b"><b>B</b> · {{ truncate(pair.b && pair.b.text, 50) }}</div>
              <div class="ins-citem-meta">
                <span class="ins-citem-sim">sim {{ ((pair.similarity || 0) * 100).toFixed(0) }}%</span>
                <div class="ins-citem-actions">
                  <button class="btn xs" @click="resolvePair(pair, 'merge')" :disabled="resolvingId.startsWith(contradictionKey(pair) + '|')">{{ t('dash.pulse.merge') }}</button>
                  <button class="btn xs ghost" @click="resolvePair(pair, 'keepA')" :disabled="resolvingId.startsWith(contradictionKey(pair) + '|')">{{ t('dash.pulse.keepA') }}</button>
                  <button class="btn xs ghost" @click="resolvePair(pair, 'keepB')" :disabled="resolvingId.startsWith(contradictionKey(pair) + '|')">{{ t('dash.pulse.keepB') }}</button>
                  <button class="btn xs ghost" @click="resolvePair(pair, 'ignore')" :disabled="resolvingId.startsWith(contradictionKey(pair) + '|')">{{ t('dash.pulse.ignore') }}</button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="ins-pulse-card decay-card">
          <div class="ins-pulse-head">
            <span class="ins-pulse-title"><span class="label-icon">▥</span>{{ t('dash.pulse.decayDist') }}</span>
          </div>
          <div class="ins-decay-meta" v-if="(insights.pulse.score_distribution || []).length">
            <span>{{ t('dash.pulse.total') }} {{ fmtNum(scoreDistributionTotal(insights.pulse.score_distribution)) }} {{ t('dash.pulse.records') }}</span>
            <span>{{ t('dash.pulse.peak') }} <b>{{ peakScoreRange(insights.pulse.score_distribution) }}</b></span>
          </div>
          <svg class="ins-decay-svg" viewBox="0 0 360 230" preserveAspectRatio="xMidYMid meet" v-if="(insights.pulse.score_distribution || []).length">
            <defs>
              <linearGradient id="decay-bar-grad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.95"></stop>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0.5"></stop>
              </linearGradient>
            </defs>
            <line v-for="y in [60,93,128,165]" :key="y" x1="14" :y1="y" x2="354" :y2="y" class="ins-decay-grid"></line>
            <line x1="14" :y1="174" x2="354" y2="174" class="ins-decay-axis" />
            <g v-for="(b, i) in barsFor(insights.pulse.score_distribution)" :key="i">
              <rect :x="b.x - 0.5" :y="b.tickY" width="1" height="4" class="ins-decay-tick" />
              <rect :x="b.x" :y="b.y" :width="b.w" :height="b.h" class="ins-decay-bar" :class="{ peak: b.peak }" :data-empty="!b.count" rx="4">
                <title v-if="b.label">{{ b.label }} · {{ b.count }}</title>
              </rect>
              <text v-if="b.count" :x="b.x + b.w / 2" :y="b.countY" class="ins-decay-value">{{ b.count }}</text>
              <text v-if="b.showShortLabel" :x="b.x + b.w / 2" :y="b.labelY" class="ins-decay-label">{{ b.shortLabel }}</text>
            </g>
          </svg>
          <div v-else class="ins-pulse-empty">—</div>
        </div>
      </div>
    </div>

    <!-- 4. Compression + Granularity -->
    <div class="ins-section" v-if="insights">
      <div class="ins-section-title">
        <span class="ico">🗜</span>
        <span>{{ t('dash.cmp.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.cmp.sub') }}</span>
      </div>
      <div class="ins-grid-2">
        <div class="ins-compression">
          <div class="ins-pulse-title" style="margin-bottom:8px;"><span class="label-icon">🗜</span>{{ t('dash.cmp.memComp') }}</div>
          <div v-if="insights.compression" class="ins-cmp-stat-row">
            <div class="ins-cmp-stat">
              <div class="ins-cmp-stat-val">{{ fmtNum(insights.compression.compressible_count) }}</div>
              <div class="ins-cmp-stat-lbl">{{ t('dash.cmp.compressible') }}</div>
            </div>
            <div class="ins-cmp-stat">
              <div class="ins-cmp-stat-val">{{ fmtNum(insights.compression.avg_length) }}</div>
              <div class="ins-cmp-stat-lbl">{{ t('dash.cmp.avgLen') }}</div>
            </div>
            <div class="ins-cmp-stat">
              <div class="ins-cmp-stat-val">{{ Math.round(insights.compression.compression_progress || 0) }}%</div>
              <div class="ins-cmp-stat-lbl">{{ t('dash.cmp.prog') }}</div>
            </div>
          </div>
          <div v-if="(insights.compression && insights.compression.items || []).length" class="ins-cmp-list">
            <div v-for="item in (insights.compression.items || []).slice(0, 5)" :key="item.id" class="ins-cmp-item">
              <span class="ins-kind" :class="'kind-' + (item.kind || 'episode')">{{ item.kind || 'episode' }}</span>
              <span class="ins-cmp-text">{{ truncate(item.text, 60) }}</span>
              <span class="ins-cmp-meta">
                <span class="ins-cmp-imp">{{ Math.round((item.importance || 0) * 100) }}%</span>
                <span v-if="item.in_wiki" class="ins-cmp-wiki">{{ t('dash.cmp.inWiki') }}</span>
              </span>
            </div>
          </div>
          <div v-else class="ins-pulse-empty">{{ t('dash.cmp.empty') }}</div>
        </div>

        <div class="ins-gran" v-if="insights.granularity">
          <div class="ins-pulse-title" style="margin-bottom:8px;"><span class="label-icon">🔬</span>{{ t('dash.gran.title') }}</div>
          <div class="ins-gran-cols">
            <div class="ins-gran-col" data-tone="rose">
              <div class="ins-gran-col-head">
                <div class="ins-gran-col-title">🧠 {{ t('dash.gran.core') }}</div>
                <div class="ins-gran-col-count">{{ fmtNum(insights.granularity.core_count) }}</div>
              </div>
              <div v-for="r in (insights.granularity.core || []).slice(0, 4)" :key="r.id" class="ins-gran-card">
                <div class="ins-gran-card-text">{{ truncate(r.text, 50) }}</div>
                <div class="ins-gran-card-meta">
                  <span class="ins-gran-chip k-imp">i {{ (r.importance || 0).toFixed(2) }}</span>
                  <span class="ins-gran-chip k-score">s {{ (r.score || 0).toFixed(2) }}</span>
                </div>
              </div>
            </div>
            <div class="ins-gran-col" data-tone="amber">
              <div class="ins-gran-col-head">
                <div class="ins-gran-col-title">📋 {{ t('dash.gran.working') }}</div>
                <div class="ins-gran-col-count">{{ fmtNum(insights.granularity.working_count) }}</div>
              </div>
              <div v-for="r in (insights.granularity.working || []).slice(0, 4)" :key="r.id" class="ins-gran-card">
                <div class="ins-gran-card-text">{{ truncate(r.text, 50) }}</div>
                <div class="ins-gran-card-meta">
                  <span class="ins-gran-chip k-imp">i {{ (r.importance || 0).toFixed(2) }}</span>
                  <span class="ins-gran-chip k-score">s {{ (r.score || 0).toFixed(2) }}</span>
                </div>
              </div>
            </div>
            <div class="ins-gran-col" data-tone="cyan">
              <div class="ins-gran-col-head">
                <div class="ins-gran-col-title">📝 {{ t('dash.gran.scratch') }}</div>
                <div class="ins-gran-col-count">{{ fmtNum(insights.granularity.scratch_count) }}</div>
              </div>
              <div v-for="r in (insights.granularity.scratch || []).slice(0, 4)" :key="r.id" class="ins-gran-card">
                <div class="ins-gran-card-text">{{ truncate(r.text, 50) }}</div>
                <div class="ins-gran-card-meta">
                  <span class="ins-gran-chip k-imp">i {{ (r.importance || 0).toFixed(2) }}</span>
                  <span class="ins-gran-chip k-score">s {{ (r.score || 0).toFixed(2) }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 5. Distribution -->
    <div class="ins-section" v-if="insights && insights.distribution">
      <div class="ins-section-title">
        <span class="ico">📈</span>
        <span>{{ t('dash.dist.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.dist.sub') }}</span>
      </div>
      <div class="ins-distrib">
        <div class="ins-dist-card">
          <div class="ins-dist-title">📊 {{ t('common.types') }}</div>
          <svg class="ins-donut-svg" viewBox="0 0 130 130">
            <g transform="rotate(-90 65 65)">
              <circle cx="65" cy="65" r="46" fill="none" stroke="var(--surface-2)" stroke-width="14"></circle>
              <circle v-for="(a, i) in donutArcPaths(insights.distribution.types)" :key="i"
                cx="65" cy="65" r="46" fill="none"
                :stroke="a.color" stroke-width="14"
                :stroke-dasharray="a.dasharray" :stroke-dashoffset="a.dashoffset"></circle>
            </g>
            <text x="65" y="65" text-anchor="middle" font-size="11" fill="var(--text-faint)">{{ t('common.types') }}</text>
            <text x="65" y="83" text-anchor="middle" font-size="14" font-weight="700" fill="var(--text)">{{ fmtNum((insights.distribution.types || []).reduce((a,b)=>a+(b.count||0),0)) }}</text>
          </svg>
          <div class="ins-donut-legend">
            <div v-for="(row, i) in sourceBars(insights.distribution.types)" :key="row.kind" class="ins-legend-item">
              <span class="sw" :style="{ background: SOURCE_COLORS[i % SOURCE_COLORS.length] }"></span>
              <span class="nm">{{ row.kind }}</span>
              <span class="ct">{{ fmtNum(row.count) }}</span>
            </div>
          </div>
        </div>
        <div class="ins-dist-card">
          <div class="ins-dist-title">📈 {{ t('dash.dist.statusTitle') }}</div>
          <div class="dist-list">
            <div v-for="row in (insights.distribution.status || [])" :key="row.status" class="dist-row">
              <span class="dist-label">
                <span class="status-dot" :data-tone="STATUS_TONE[row.status] || 'slate'"></span>
                {{ row.status }}
              </span>
              <span class="dist-bar">
                <span class="dist-fill" :class="'tone-' + (STATUS_TONE[row.status] || 'slate')"
                  :style="{ width: ((row.count / Math.max(1, ...(insights.distribution.status || []).map(r => r.count || 0))) * 100) + '%' }"></span>
              </span>
              <span class="dist-val">{{ fmtNum(row.count) }}</span>
            </div>
          </div>
        </div>
        <div class="ins-dist-card">
          <div class="ins-dist-title">📈 {{ t('dash.dist.trendTitle') }}</div>
          <svg class="ins-trend-svg" viewBox="0 0 300 140" preserveAspectRatio="none">
            <defs>
              <linearGradient id="dist-trend-grad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.5"></stop>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"></stop>
              </linearGradient>
            </defs>
            <path :d="trendPoints(insights.distribution.trend).area" fill="url(#dist-trend-grad)"></path>
            <path :d="trendPoints(insights.distribution.trend).line" class="ins-trend-line" fill="none" stroke="var(--accent)" stroke-width="2"></path>
            <g v-for="(t, i) in trendPoints(insights.distribution.trend).ticks" :key="i">
              <text :x="t.x" y="135" text-anchor="middle" font-size="9" fill="var(--text-faint)">{{ t.label }}</text>
              <circle :cx="t.x" :cy="(140 - (t.value / Math.max(1, trendPoints(insights.distribution.trend).max)) * 130) - 4" r="2.4" fill="var(--accent)"></circle>
            </g>
          </svg>
        </div>
      </div>
    </div>

    <!-- 6. Sources + Wiki + Ingest -->
    <div class="ins-section" v-if="insights">
      <div class="ins-section-title">
        <span class="ico">📡</span>
        <span>{{ t('dash.src.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.src.sub') }}</span>
      </div>
      <div class="ins-sources-row">
        <div class="ins-src-card">
          <div class="ins-src-title">📡 {{ t('dash.src.bySource') }}</div>
          <div class="ins-src-donut-wrap">
            <svg class="ins-src-donut" viewBox="0 0 130 130">
              <g transform="rotate(-90 65 65)">
                <circle cx="65" cy="65" r="46" fill="none" stroke="var(--surface-2)" stroke-width="14"></circle>
                <circle v-for="(a, i) in donutArcPaths(insights.sources)" :key="i"
                  cx="65" cy="65" r="46" fill="none"
                  :stroke="a.color" stroke-width="14"
                  :stroke-dasharray="a.dasharray" :stroke-dashoffset="a.dashoffset"></circle>
              </g>
              <text x="65" y="65" text-anchor="middle" font-size="10" fill="var(--text-faint)">{{ t('common.sources') }}</text>
              <text x="65" y="83" text-anchor="middle" font-size="14" font-weight="700" fill="var(--text)">{{ (insights.sources || []).reduce((a,b)=>a+(b.count||0),0).toLocaleString() }}</text>
            </svg>
            <div class="ins-src-legend">
              <div v-for="(row, i) in sourceBars(insights.sources)" :key="row.source" class="ins-src-legend-item">
                <span class="sw" :style="{ background: SOURCE_COLORS[i % SOURCE_COLORS.length] }"></span>
                <span class="nm">{{ row.source }}</span>
                <span class="ct">{{ fmtNum(row.count) }}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="ins-src-card">
          <div class="ins-src-title">📖 {{ t('dash.src.wikiHealth') }}</div>
          <div class="ins-wiki-stats" v-if="insights.wiki_health">
            <div class="ins-wiki-stat"><div class="ins-wiki-val">{{ fmtNum(insights.wiki_health.pages) }}</div><div class="ins-wiki-lbl">{{ t('dash.src.wikiPages') }}</div></div>
            <div class="ins-wiki-stat"><div class="ins-wiki-val">{{ insights.wiki_health.avg_importance ? Math.round(insights.wiki_health.avg_importance * 100) + '%' : '—' }}</div><div class="ins-wiki-lbl">{{ t('dash.src.wikiImp') }}</div></div>
            <div class="ins-wiki-stat"><div class="ins-wiki-val">{{ fmtNum(insights.wiki_health.total_chars) }}</div><div class="ins-wiki-lbl">{{ t('dash.src.wikiChars') }}</div></div>
            <div class="ins-wiki-stat"><div class="ins-wiki-val">{{ fmtNum(insights.wiki_health.referenced_memories) }}</div><div class="ins-wiki-lbl">{{ t('dash.src.wikiRefs') }}</div></div>
          </div>
          <div class="ins-wiki-bar-track" v-if="insights.wiki_health">
            <div class="ins-wiki-bar-fill" :style="{ width: Math.min(100, Math.round((insights.wiki_health.referenced_memories / Math.max(1, (insights.overview && insights.overview.total))) * 100)) + '%' }"></div>
          </div>
          <div class="ins-wiki-coverage-lbl">
            <span>{{ t('dash.src.coverage') }}</span>
            <span>{{ Math.min(100, Math.round((insights.wiki_health.referenced_memories / Math.max(1, insights.overview && insights.overview.total)) * 100)) }}%</span>
          </div>
        </div>
        <div class="ins-src-card">
          <div class="ins-src-title">📥 {{ t('dash.src.ingest') }}<span class="ins-src-peak" v-if="ingestPeakHour(insights.ingest_rate)">★ {{ ingestPeakHour(insights.ingest_rate) }}</span></div>
          <svg class="ins-ingest-svg" viewBox="0 0 360 118" preserveAspectRatio="xMidYMid meet">
            <defs>
              <linearGradient id="ins-ingest-grad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.85"></stop>
                <stop offset="100%" stop-color="var(--accent)" stop-opacity="0.18"></stop>
              </linearGradient>
            </defs>
            <line v-for="y in [40, 70]" :key="y" x1="4" :y1="y" x2="356" y2="y" class="ins-ingest-grid"></line>
            <line x1="4" y1="100" x2="356" y2="100" class="ins-ingest-axis" />
            <g v-for="(b, i) in ingestBars(insights.ingest_rate)" :key="i">
              <rect v-if="b.tick" :x="b.x + b.w / 2 - 0.5" :y="b.tickY" width="1" height="3" class="ins-ingest-tick"></rect>
              <rect :x="b.x" :y="b.y" :width="b.w" :height="b.h" :class="['ins-ingest-bar', { peak: b.isPeak, idle: !b.active }]" rx="2">
                <title v-if="b.count">{{ b.hour }} · {{ b.count }}</title>
              </rect>
              <text v-if="b.label" :x="b.x + b.w / 2" :y="b.lblY" text-anchor="middle" class="ins-ingest-lbl">{{ b.label }}</text>
            </g>
          </svg>
          <div class="ins-ingest-total">
            <span>{{ t('dash.src.last24h') }} <b>{{ fmtNum((insights.ingest_rate || []).reduce((a, b) => a + (b.count || 0), 0)) }}</b></span>
            <span>{{ t('dash.src.recall24h') }} <b>{{ fmtNum(insights.recall_24h && insights.recall_24h.total) }}</b></span>
          </div>
        </div>
      </div>
    </div>

    <!-- 7. Pipeline latency -->
    <div class="ins-section" v-if="insights && (insights.pipeline || []).length">
      <div class="ins-section-title">
        <span class="ico">⏱️</span>
        <span>{{ t('dash.pipe.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.pipe.sub') }}</span>
      </div>
      <div class="ins-pipe">
        <div v-for="row in pipelineBars(insights.pipeline)" :key="row.stage" class="ins-pipe-row">
          <div class="ins-pipe-name"><span class="ico">📦</span>{{ t('dash.pipe.stage.' + row.stage, row.stage) }}</div>
          <div class="ins-pipe-bar-track"><div class="ins-pipe-bar-fill green" :style="{ width: row.pct + '%' }"></div></div>
          <div class="ins-pipe-ms">{{ row.avg_ms }} ms</div>
          <div class="ins-pipe-cnt">×{{ row.count }}</div>
        </div>
      </div>
    </div>

    <!-- 8. Health + Weekly -->
    <div class="ins-section" v-if="insights">
      <div class="ins-section-title">
        <span class="ico">🩺</span>
        <span>{{ t('dash.health.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.health.sub') }}</span>
      </div>
      <div class="ins-health-row">
        <div class="ins-health-card">
          <div class="ins-health-title">
            <span>📡 {{ t('dash.health.sources') }}</span>
            <span v-if="sourceHealth" class="ins-overall-pill" :class="sourceHealth.overall">{{ sourceHealth.overall }}</span>
          </div>
          <div class="ins-health-list" v-if="(sourceHealth && sourceHealth.sources || []).length">
            <div v-for="s in sourceHealth.sources" :key="s.source" class="ins-health-row-item">
              <span class="ins-overall-pill" :class="s.status" style="margin-left:0;">{{ s.status }}</span>
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{{ s.source }}</span>
              <span style="font-size:10.5px;color:var(--text-faint);">{{ s.hint }}</span>
            </div>
          </div>
          <div v-if="Array.isArray(sourceHealth && sourceHealth.hooks) && sourceHealth.hooks.length" class="ins-hooks-strip">
            <div class="muted">{{ t('dash.health.hooksLabel') }}</div>
            <div v-for="h in sourceHealth.hooks" :key="h" class="ins-hook">{{ h }}</div>
          </div>
        </div>
        <div class="ins-wreport-card">
          <div class="ins-wreport-title">
            <span>📝 {{ t('dash.health.weekly') }}</span>
            <span class="ins-window-pills">
              <button v-for="d in [3, 7, 14]" :key="d"
                class="ins-window-pill" :class="{ active: weeklyDays === d }"
                @click="loadWeekly(d)">{{ d }}d</button>
            </span>
            <button class="btn xs ghost" @click="copyWeekly" :disabled="!weeklyReport">{{ t('dash.health.copy') }}</button>
            <button class="btn xs ghost" @click="loadWeekly(weeklyDays)">↻</button>
          </div>
          <div v-if="weeklyError" class="ins-wreport-error">⚠ {{ weeklyError }}</div>
          <div v-else-if="weeklyLoading" class="ins-wreport-loading">{{ t('dash.health.weeklyLoading') }}</div>
          <div v-else-if="weeklyReport" class="ins-wreport-md">{{ weeklyReport.markdown }}</div>
          <div v-else class="ins-wreport-md muted">—</div>
        </div>
      </div>
    </div>

    <!-- 9. LLM Audit + WriteGuard -->
    <div class="ins-section" v-if="insights">
      <div class="ins-section-title">
        <span class="ico">🔬</span>
        <span>{{ t('dash.audit.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.audit.sub') }}</span>
      </div>
      <div class="ins-audit-row">
        <div class="ins-audit-card">
          <div class="ins-audit-title"><span>🤖 {{ t('dash.audit.llm') }}</span></div>
          <div class="ins-audit-stats" v-if="llmAudit">
            <div class="ins-audit-stat"><div class="ins-audit-stat-val">{{ fmtNum((llmAudit.stats && llmAudit.stats.calls) || llmAudit.total_calls) }}</div><div class="ins-audit-stat-lbl">{{ t('dash.audit.calls') }}</div></div>
            <div class="ins-audit-stat"><div class="ins-audit-stat-val">{{ fmtNum((llmAudit.stats && llmAudit.stats.total_tokens) || llmAudit.total_tokens) }}</div><div class="ins-audit-stat-lbl">{{ t('dash.audit.tokens') }}</div></div>
            <div class="ins-audit-stat"><div class="ins-audit-stat-val">{{ (llmAudit.stats && llmAudit.stats.avg_latency_ms) ? Math.round(llmAudit.stats.avg_latency_ms) : 0 }}</div><div class="ins-audit-stat-lbl">{{ t('dash.audit.avgMs') }}</div></div>
            <div class="ins-audit-stat"><div class="ins-audit-stat-val">{{ fmtNum((llmAudit.stats && llmAudit.stats.failures) || llmAudit.failures) }}</div><div class="ins-audit-stat-lbl">{{ t('dash.audit.fails') }}</div></div>
          </div>
          <div v-if="(llmAudit && llmAudit.recent || []).length" class="ins-audit-list">
            <div v-for="(it, i) in (llmAudit.recent || []).slice(0, 6)" :key="i" class="ins-audit-row-item">
              <span class="ins-audit-stage">{{ it.kind || it.stage || '?' }}</span>
              <span class="ins-audit-meta">{{ fmtNum(it.total_tokens || it.tokens) }} tok · {{ Math.round(it.latency_ms || it.elapsed_ms) }} ms</span>
              <span class="ins-audit-status" :data-tone="(it.ok === 1 || it.ok === true || it.error == null) ? 'green' : 'rose'">{{ (it.ok === 1 || it.ok === true || it.error == null) ? '✓' : '✕' }}</span>
            </div>
          </div>
          <div v-else class="muted" style="font-size:11px;">{{ t('dash.audit.empty') }}</div>
        </div>
        <div class="ins-guard-card">
          <div class="ins-guard-title">
            <span>🛡 {{ t('dash.audit.guard') }}</span>
            <span style="margin-left:auto;font-size:10.5px;color:var(--text-faint);">uptime {{ guardUptime }}</span>
          </div>
          <div class="ins-guard-stats" v-if="writeGuard">
            <div class="ins-guard-stat" data-tone="duplicate"><div class="ins-guard-stat-val">{{ fmtNum((writeGuard.totals && writeGuard.totals.duplicate) || writeGuard.duplicate) }}</div><div class="ins-guard-stat-lbl">{{ t('dash.audit.duplicate') }}</div></div>
            <div class="ins-guard-stat" data-tone="too_long"><div class="ins-guard-stat-val">{{ fmtNum((writeGuard.totals && writeGuard.totals.too_long) || writeGuard.too_long) }}</div><div class="ins-guard-stat-lbl">{{ t('dash.audit.tooLong') }}</div></div>
            <div class="ins-guard-stat" data-tone="too_short"><div class="ins-guard-stat-val">{{ fmtNum((writeGuard.totals && writeGuard.totals.too_short) || writeGuard.too_short) }}</div><div class="ins-guard-stat-lbl">{{ t('dash.audit.tooShort') }}</div></div>
            <div class="ins-guard-stat" data-tone="low_signal"><div class="ins-guard-stat-val">{{ fmtNum((writeGuard.totals && writeGuard.totals.low_signal) || writeGuard.low_signal) }}</div><div class="ins-guard-stat-lbl">{{ t('dash.audit.lowSignal') }}</div></div>
          </div>
          <div class="muted" style="margin-top:8px;font-size:10.5px;">{{ t('dash.audit.guardHint') }}</div>
        </div>
      </div>
    </div>

    <!-- 10. Architecture loop diagram -->
    <div class="ins-section">
      <div class="ins-section-title">
        <span class="ico">🏗</span>
        <span>{{ t('dash.arch.title') }}</span>
        <span class="bar"></span>
        <span class="right">{{ t('dash.arch.sub') }}</span>
      </div>
      <div class="ins-arch-wrap">
        <svg class="ins-arch-svg" viewBox="0 0 1200 660" preserveAspectRatio="xMidYMid meet">
          <defs>
            <marker id="arch-arrow" markerWidth="10" markerHeight="10" refX="6" refY="3" orient="auto" markerUnits="strokeWidth">
              <path d="M0,0 L0,6 L7,3 z" fill="var(--accent)"></path>
            </marker>
            <linearGradient id="arch-hub-grad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="color-mix(in srgb, var(--accent) 28%, var(--surface))"></stop>
              <stop offset="100%" stop-color="var(--surface)"></stop>
            </linearGradient>
          </defs>

          <!-- Title + subtitle above the ring -->
          <text :x="ARCH.cx" y="38" text-anchor="middle" font-size="14" font-weight="700" fill="var(--text)">
            {{ t('dash.arch.loopTitle') }}
          </text>
          <text :x="ARCH.cx" y="56" text-anchor="middle" font-size="11" fill="var(--text-faint)">
            {{ t('dash.arch.loopSub') }}
          </text>

          <!-- Outer ring path (dashed) -->
          <circle :cx="ARCH.cx" :cy="ARCH.cy" :r="ARCH.R" fill="none" stroke="var(--border)" stroke-width="1.5" stroke-dasharray="4 4"></circle>

          <!-- Center hub -->
          <circle :cx="ARCH.cx" :cy="ARCH.cy" r="78" fill="url(#arch-hub-grad)" stroke="var(--accent)" stroke-width="2"></circle>
          <text :x="ARCH.cx" :y="ARCH.cy - 6" text-anchor="middle" font-size="13" font-weight="700" fill="var(--text)">loop_memory</text>
          <text :x="ARCH.cx" :y="ARCH.cy + 12" text-anchor="middle" font-size="10" fill="var(--text-faint)">{{ t('dash.arch.coreStore') }}</text>
          <text :x="ARCH.cx" :y="ARCH.cy + 26" text-anchor="middle" font-size="10" fill="var(--text-faint)">{{ t('dash.arch.coreData') }}</text>

          <!-- 7 nodes + spokes + labels -->
          <g v-for="(s, i) in ARCH.positions" :key="s.key">
            <rect :x="s.x - 67" :y="s.y - 28" width="134" height="56" rx="10" :class="'ins-arch-node-bg tone-' + s.tone"></rect>
            <text :x="s.x - 50" :y="s.y + 5" text-anchor="middle" font-size="16">{{ s.icon }}</text>
            <text :x="s.x + 8" :y="s.y - 4" text-anchor="middle" font-size="11" font-weight="600" fill="var(--text)">
              {{ t('dash.arch.' + s.key, s.key) }}
            </text>
            <text :x="s.x + 8" :y="s.y + 10" text-anchor="middle" font-size="9.5" fill="var(--text-faint)">
              {{ t('dash.arch.' + s.key + 'Sub', s.file) }}
            </text>
            <line :x1="ARCH.spokes[i].x1" :y1="ARCH.spokes[i].y1" :x2="ARCH.spokes[i].x2" :y2="ARCH.spokes[i].y2" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 3"></line>
          </g>

          <!-- 7 animated arc edges (clockwise flow) -->
          <path v-for="(a, i) in ARCH.arcs" :key="'arc-' + i" class="ins-arch-edge animated" :d="a.d" marker-end="url(#arch-arrow)"></path>

          <!-- File anchor strip at the bottom -->
          <g v-for="(a, i) in ARCH.anchors" :key="'anchor-' + a.key">
            <rect :x="a.cx - (ARCH.W / ARCH.positions.length) / 2 + 6" :y="a.y - 18" :width="ARCH.W / ARCH.positions.length - 12" height="28" rx="6" fill="var(--surface-2)" stroke="var(--border)" stroke-width="1"></rect>
            <text :x="a.cx" :y="a.y - 5" text-anchor="middle" font-size="10" font-weight="600" fill="var(--text)">
              {{ i + 1 }}. {{ t('dash.arch.' + a.key, a.key) }}
            </text>
            <text :x="a.cx" :y="a.y + 6" text-anchor="middle" font-size="9" fill="var(--text-faint)">{{ shortenPath(a.file) }}</text>
          </g>
        </svg>
      </div>
      <div class="ins-arch-caption">{{ t('dash.arch.loopSub') }}</div>
    </div>

    <div v-if="loading && !insights" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="!insights && !loading" class="empty">{{ t('dash.ins.offline') }}</div>
  </div>
</div>
`,
});
