/**
 * KnowledgeGraph — 3D Fibonacci-sphere knowledge graph.
 *
 * The drawing logic (canvas, force simulation, rotation, label rendering)
 * is a direct port of the legacy vanilla-JS code. We mount it onto a
 * <canvas> ref so Vue handles the lifecycle (resize observer, tab
 * visibility, mount/unmount) while the drawing code stays a single
 * self-contained IIFE inside the component.
 *
 * The component is intentionally heavy — the canvas code is by far the
 * most complex piece of the UI. Putting it all in one file keeps the
 * math together and makes future tweaks (new label rules, different
 * projection) easy to find.
 */
import { defineComponent, ref, onMounted, onUnmounted, watch, nextTick } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t } from '../store.js';
import { api } from '../api.js';

const KIND_COLOR = {
  concept:   '#6366f1',
  acronym:   '#10b981',
  cjk:       '#f59e0b',
  tag:       '#ec4899',
  url:       '#0ea5e9',
  path:      '#a855f7',
  wiki_page: '#f59e0b',
};

export const KnowledgeGraph = defineComponent({
  name: 'KnowledgeGraph',
  setup() {
    const canvasRef = ref(null);
    const wrapRef = ref(null);
    const loading = ref(false);
    const stats = ref({ entities: 0, relations: 0 });
    const filterText = ref('');
    const filterKind = ref('');

    // Draw state
    let graphData = { entities: [], relations: [] };
    let graphNodes = {};
    let graphEdges = [];
    let graphLayout = { scale: 1, tx: 0, ty: 0, dragging: null, hover: null };
    let graphSelected = null;
    let graphInitTried = false;
    let sim = null;
    let animating = false;
    let globalTick = 0;
    let warm = false;

    async function loadGraph() {
      loading.value = true;
      try {
        const entLimit = window._graphEntLimit || 120;
        const relLimit = Math.min(entLimit * 4, 600);
        const g = await api.graph({ limit_entities: entLimit, limit_relations: relLimit });
        graphData = g;
        stats.value = { entities: (g.entities || []).length, relations: (g.relations || []).length };

        graphEdges = (g.relations || []).map((r, i) => ({
          src: r.src, dst: r.dst, weight: r.weight,
          evidence: r.evidence, id: r.id, _i: i,
          edgeKind: r.kind || 'related',
        }));
        graphNodes = {};
        for (const e of (g.entities || [])) {
          graphNodes[e.name] = {
            name: e.name, kind: e.kind, weight: e.weight,
            mention: e.mention_count, _i: graphNodes.length || 0,
            x: 0, y: 0, vx: 0, vy: 0,
          };
        }
        seedFibonacci();
        ensureSim();
        fitToCanvas();
        refreshStats();
        drawCanvas();
      } catch (e) {
        console.error('graph load failed', e);
      } finally {
        loading.value = false;
      }
    }

    function stableHash(s) {
      let h = 0x811c9dc5 >>> 0;
      for (let i = 0; i < s.length; i++) {
        h ^= s.charCodeAt(i);
        h = Math.imul(h, 0x01000193) >>> 0;
      }
      return h;
    }

    function seedFibonacci() {
      const names = Object.keys(graphNodes);
      names.sort((a, b) => {
        const ma = graphNodes[a].mention || 0;
        const mb = graphNodes[b].mention || 0;
        if (ma !== mb) return mb - ma;
        return stableHash(a) - stableHash(b);
      });
      const N = names.length;
      const R = Math.max(220, Math.sqrt(N) * 28);
      const PHI = Math.PI * (3 - Math.sqrt(5));
      // Interleave high/low mention so the high-mention nodes spread
      // across the sphere instead of clustering at one pole.
      const ordered = new Array(N);
      const halfHigh = Math.min(N, Math.ceil(N * 0.5));
      let hi = 0, lo = halfHigh;
      for (let i = 0; i < N; i++) {
        if (i % 2 === 0 && hi < halfHigh) ordered[i] = names[hi++];
        else if (lo < N) ordered[i] = names[lo++];
        else ordered[i] = names[hi++];
      }
      ordered.forEach((name, i) => {
        const lat = Math.asin(1 - 2 * (i + 0.5) / N);
        const lon0 = ((i * PHI) % (Math.PI * 2));
        const node = graphNodes[name];
        node.x = R * Math.cos(lat) * Math.sin(lon0);
        node.y = R * Math.sin(lat);
        node.lat0 = lat;
        node.shellR = R;
        node.lon = lon0;
      });
    }

    // Force-simulation is intentionally minimal — the Fibonacci sphere is
    // already a sphere, so we only need a few ticks to settle user drags.
    function makeSim() {
      const params = {
        repulsion: 250, springLen: 120, springK: 0.012, centerK: 0.018,
        collideR: 18, collideK: 0.5, damping: 0.82, alphaDecay: 0.04,
        baseOmega: 0.0014, shellBoost: 0.6, minAlpha: 0.05, maxSpeed: 6,
      };
      let alpha = 0;
      const nodes = () => Object.values(graphNodes);
      return {
        params: () => params,
        tick() {
          if (alpha < 0.001) return;
          for (const n of nodes()) {
            for (const m of nodes()) {
              if (m === n) continue;
              const dx = n.x - m.x, dy = n.y - m.y;
              const d2 = dx*dx + dy*dy + 0.01;
              const f = params.repulsion / d2;
              n.vx += dx / Math.sqrt(d2) * f * 0.01;
              n.vy += dy / Math.sqrt(d2) * f * 0.01;
            }
          }
          for (const e of graphEdges) {
            const a = graphNodes[e.src], b = graphNodes[e.dst];
            if (!a || !b) continue;
            const dx = b.x - a.x, dy = b.y - a.y;
            const d = Math.sqrt(dx*dx + dy*dy) || 0.01;
            const target = params.springLen * (e.weight || 0.5);
            const f = (d - target) * params.springK;
            a.vx += dx / d * f;
            a.vy += dy / d * f;
            b.vx -= dx / d * f;
            b.vy -= dy / d * f;
          }
          // Gentle pull back to Fibonacci surface
          for (const n of nodes()) {
            if (n.shellR == null) continue;
            n.vx += -n.x * 0.0008;
            n.vy += -n.y * 0.0008;
          }
          for (const n of nodes()) {
            n.x += n.vx; n.y += n.vy;
            n.vx *= params.damping; n.vy *= params.damping;
          }
          // Sphere rotation
          for (const n of nodes()) {
            if (n.lat0 == null) continue;
            n.lon = (n.lon || 0) + params.baseOmega;
          }
          alpha *= (1 - params.alphaDecay);
        },
        restart() { alpha = 1; },
        setAlpha(v) { alpha = Math.max(0, Math.min(1, v)); },
        alpha() { return alpha; },
        pause() {},
        resume() {},
        isRotating() { return true; },
      };
    }

    function ensureSim() {
      if (!sim) sim = makeSim();
      sim.restart();
      for (let i = 0; i < 20; i++) sim.tick();
      sim.setAlpha(0);
      // Nudge nodes back to Fibonacci positions
      for (const n of Object.values(graphNodes)) {
        if (n.lat0 !== undefined) {
          n.x = n.shellR * Math.cos(n.lat0) * Math.sin(n.lon);
          n.y = n.shellR * Math.sin(n.lat0);
        }
        n.vx = 0; n.vy = 0;
      }
      warm = true;
    }

    function fitToCanvas() {
      const c = canvasRef.value; if (!c) return;
      const wrap = wrapRef.value; if (!wrap) return;
      const rect = wrap.getBoundingClientRect();
      const W = rect.width > 100 ? rect.width : 1000;
      const H = rect.height > 100 ? rect.height : 700;
      const nodes = Object.values(graphNodes);
      if (!nodes.length) return;
      let rMax = 0;
      for (const n of nodes) {
        const r = Math.sqrt(n.x*n.x + n.y*n.y);
        if (r > rMax) rMax = r;
      }
      const R = rMax + 60;
      const w = R * 2, h = R * 2;
      graphLayout.scale = Math.min(W / w, H / h) * 0.96;
      graphLayout.tx = W / 2;
      graphLayout.ty = H / 2;
    }

    function refreshStats() {
      // Stats pill on top of canvas
      store.stats.graph = `${stats.value.entities}/${stats.value.entities}`;
    }

    function resizeCanvas() {
      const c = canvasRef.value; if (!c) return;
      const wrap = wrapRef.value; if (!wrap) return;
      const dpr = window.devicePixelRatio || 1;
      const rect = wrap.getBoundingClientRect();
      c.width = Math.max(100, rect.width) * dpr;
      c.height = Math.max(100, rect.height) * dpr;
      c.style.width = rect.width + 'px';
      c.style.height = rect.height + 'px';
    }

    function hexA(hex, a) {
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r}, ${g}, ${b}, ${a})`;
    }

    function roundRect(ctx, x, y, w, h, rad) {
      ctx.beginPath();
      ctx.moveTo(x + rad, y);
      ctx.lineTo(x + w - rad, y);
      ctx.quadraticCurveTo(x + w, y, x + w, y + rad);
      ctx.lineTo(x + w, y + h - rad);
      ctx.quadraticCurveTo(x + w, y + h, x + w - rad, y + h);
      ctx.lineTo(x + rad, y + h);
      ctx.quadraticCurveTo(x, y + h, x, y + h - rad);
      ctx.lineTo(x, y + rad);
      ctx.quadraticCurveTo(x, y, x + rad, y);
      ctx.closePath();
    }

    function drawCanvas() {
      const c = canvasRef.value; if (!c) return;
      const ctx = c.getContext('2d');
      if (!ctx) return;
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, c.width, c.height);
      ctx.scale(dpr, dpr);

      const themeAttr = document.documentElement.getAttribute('data-theme') || 'light';
      const isDark = themeAttr === 'dark';
      const textFaint = isDark ? 'rgba(238,242,255,0.55)' : 'rgba(15,23,42,0.55)';
      const halo = '#ec4899';

      const cssW = c.clientWidth, cssH = c.clientHeight;
      ctx.save();
      ctx.translate(graphLayout.tx, graphLayout.ty);
      ctx.scale(graphLayout.scale, graphLayout.scale);

      const visibleNodes = Object.values(graphNodes).filter(n => {
        if (filterKind.value && n.kind !== filterKind.value) return false;
        if (filterText.value && !n.name.toLowerCase().includes(filterText.value.toLowerCase())) return false;
        return true;
      });
      const visibleNames = new Set(visibleNodes.map(n => n.name));

      // Project nodes from (lat, lon) into 2D
      const projected = [];
      for (const n of visibleNodes) {
        const lat = (n.lat0 !== undefined) ? n.lat0 : Math.atan2(n.y, n.x);
        const R = (n.shellR !== undefined) ? n.shellR : (Math.sqrt(n.x*n.x + n.y*n.y) || 1);
        const lon = (n.lon !== undefined) ? n.lon : 0;
        const sinLat = Math.sin(lat), cosLat = Math.cos(lat);
        const sinLon = Math.sin(lon), cosLon = Math.cos(lon);
        const px = R * cosLat * sinLon;
        const py = R * sinLat;
        const pz = R * cosLat * cosLon;
        const front = (pz + R) / (2 * R);
        projected.push({ name: n.name, x: px, y: py, depth: front, node: n });
      }
      projected.sort((a, b) => a.depth - b.depth);
      const projMap = {};
      for (const p of projected) projMap[p.name] = p;

      // Draw edges
      for (const e of graphEdges) {
        const a = graphNodes[e.src], b = graphNodes[e.dst];
        if (!a || !b) continue;
        if (!visibleNames.has(a.name) && !visibleNames.has(b.name)) continue;
        const pa = projMap[a.name], pb = projMap[b.name];
        if (!pa || !pb) continue;
        const depthAvg = (pa.depth + pb.depth) / 2;
        const alpha = 0.32 * (0.45 + 0.55 * depthAvg);
        const edgeKind = e.edgeKind || 'related';
        let strokeColor;
        if (edgeKind === 'tagged_with') strokeColor = hexA('#ec4899', alpha * 0.9);
        else if (edgeKind === 'mentions') strokeColor = hexA('#6366f1', alpha * 0.7);
        else if (edgeKind === 'related_to') strokeColor = hexA('#f59e0b', alpha * 0.85);
        else strokeColor = hexA('#6366f1', alpha);
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(pa.x, pa.y);
        ctx.lineTo(pb.x, pb.y);
        ctx.stroke();
      }

      // Draw nodes
      for (const p of projected) {
        const n = p.node;
        const depth = p.depth;
        const sizeMul = 0.55 + 0.55 * depth;
        const kindBonus = (n.kind === 'wiki_page') ? 2.2 : (n.kind === 'tag') ? 0.7 : 1.0;
        const r = (4 + Math.sqrt(n.mention || 1) * 1.5) * sizeMul * kindBonus;
        const baseFill = KIND_COLOR[n.kind] || KIND_COLOR.concept;
        const isSel = graphSelected && graphSelected.name === n.name;
        const isHover = graphLayout.hover && graphLayout.hover.name === n.name;
        const depthAlpha = 0.40 + 0.60 * depth;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.fillStyle = hexA(baseFill, depthAlpha);
        ctx.fill();
        ctx.strokeStyle = 'rgba(0,0,0,0.18)';
        ctx.lineWidth = 1;
        ctx.stroke();

        // Label — show wiki_page / tag / high-mention always
        const mention = n.mention || 0;
        const importantLabel = n.kind === 'wiki_page' || mention >= 6;
        const mediumLabel = n.kind === 'tag' || mention >= 3;
        const showLabel = isSel || isHover || importantLabel || mediumLabel;
        if (showLabel) {
          const label = n.name.length > 34 ? n.name.slice(0, 32) + '…' : n.name;
          ctx.font = '12px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
          const m = ctx.measureText(label);
          const lw = m.width + 12, lh = 18;
          const dropBelow = (isSel || isHover) ? false : (p.y < 0);
          const ly = dropBelow ? (p.y + r + 6) : (p.y - r - 14);
          if (isSel || isHover) {
            ctx.fillStyle = isDark ? '#a5b4fc' : '#eef2ff';
            roundRect(ctx, p.x - lw/2, ly, lw, lh, 9);
            ctx.fill();
            ctx.fillStyle = isDark ? '#1d2433' : '#1e1b4b';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(label, p.x, ly + lh/2);
          } else {
            ctx.fillStyle = textFaint;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(label, p.x, ly + lh/2);
          }
        }
      }
      ctx.restore();

      // HUD
      ctx.save();
      ctx.font = '11px -apple-system, sans-serif';
      ctx.fillStyle = isDark ? 'rgba(238,242,255,0.45)' : 'rgba(15,23,42,0.45)';
      ctx.textAlign = 'left';
      ctx.fillText(
        `${Object.keys(graphNodes).length} / ${Object.keys(graphNodes).length} nodes`,
        10, cssH - 18
      );
      ctx.restore();
    }

    function renderLoop() {
      globalTick++;
      if (sim) sim.tick();
      drawCanvas();
      requestAnimationFrame(renderLoop);
    }

    function onWheel(e) {
      e.preventDefault();
      const rect = canvasRef.value.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const factor = Math.exp(-e.deltaY * 0.0015);
      const wx = (cx - graphLayout.tx) / graphLayout.scale;
      const wy = (cy - graphLayout.ty) / graphLayout.scale;
      graphLayout.scale *= factor;
      graphLayout.tx = cx - wx * graphLayout.scale;
      graphLayout.ty = cy - wy * graphLayout.scale;
      drawCanvas();
    }

    function pickNode(cx, cy) {
      let best = null, bestD = 12;
      for (const n of Object.values(graphNodes)) {
        const wx = (cx - graphLayout.tx) / graphLayout.scale;
        const wy = (cy - graphLayout.ty) / graphLayout.scale;
        const dx = n.x - wx, dy = n.y - wy;
        const d = Math.sqrt(dx*dx + dy*dy);
        if (d < bestD) { bestD = d; best = n; }
      }
      return best;
    }

    function onCanvasMouseDown(e) {
      const rect = canvasRef.value.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const node = pickNode(cx, cy);
      if (node) {
        graphLayout.dragging = { kind: 'node', node };
        graphSelected = node;
      } else {
        graphLayout.dragging = { kind: 'pan', x0: e.clientX, y0: e.clientY, tx0: graphLayout.tx, ty0: graphLayout.ty };
      }
    }
    function onCanvasMouseMove(e) {
      const rect = canvasRef.value.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const node = pickNode(cx, cy);
      graphLayout.hover = node;
      if (graphLayout.dragging) {
        if (graphLayout.dragging.kind === 'pan') {
          graphLayout.tx = graphLayout.dragging.tx0 + (e.clientX - graphLayout.dragging.x0);
          graphLayout.ty = graphLayout.dragging.ty0 + (e.clientY - graphLayout.dragging.y0);
        } else if (graphLayout.dragging.kind === 'node') {
          const wx = (cx - graphLayout.tx) / graphLayout.scale;
          const wy = (cy - graphLayout.ty) / graphLayout.scale;
          graphLayout.dragging.node.x = wx;
          graphLayout.dragging.node.y = wy;
          graphLayout.dragging.node.fixed = true;
        }
      }
      drawCanvas();
    }
    function onCanvasMouseUp() {
      if (graphLayout.dragging && graphLayout.dragging.kind === 'node') {
        graphLayout.dragging.node.fixed = false;
      }
      graphLayout.dragging = null;
    }
    function onCanvasClick(e) {
      const rect = canvasRef.value.getBoundingClientRect();
      const node = pickNode(e.clientX - rect.left, e.clientY - rect.top);
      if (node) graphSelected = node;
      else graphSelected = null;
    }

    let resizeObserver = null;
    function onResize() {
      resizeCanvas();
      fitToCanvas();
      drawCanvas();
    }

    onMounted(async () => {
      await nextTick();
      resizeCanvas();
      canvasRef.value.addEventListener('wheel', onWheel, { passive: false });
      canvasRef.value.addEventListener('mousedown', onCanvasMouseDown);
      canvasRef.value.addEventListener('mousemove', onCanvasMouseMove);
      window.addEventListener('mouseup', onCanvasMouseUp);
      canvasRef.value.addEventListener('click', onCanvasClick);
      window.addEventListener('resize', onResize);
      if (window.ResizeObserver) {
        resizeObserver = new ResizeObserver(onResize);
        if (wrapRef.value) resizeObserver.observe(wrapRef.value);
      }
      await loadGraph();
      animating = true;
      requestAnimationFrame(renderLoop);
    });

    onUnmounted(() => {
      animating = false;
      if (resizeObserver) resizeObserver.disconnect();
      window.removeEventListener('resize', onResize);
      window.removeEventListener('mouseup', onCanvasMouseUp);
    });

    return { canvasRef, wrapRef, loading, stats, filterText, filterKind, t,
             rebuild: loadGraph };
  },
  template: /* html */ `
<div class="tab-pane" id="pane-graph">
  <div class="graph-toolbar">
    <button class="tb-action primary" @click="rebuild">{{ t('action.rebuild') }}</button>
    <select v-model="filterKind">
      <option value="">{{ t('graph.allKinds') }}</option>
      <option value="wiki_page">{{ t('graph.kind.wiki_page') }}</option>
      <option value="tag">{{ t('graph.kind.tag') }}</option>
      <option value="concept">{{ t('graph.kind.concept') }}</option>
    </select>
    <input v-model="filterText" :placeholder="t('graph.filterPlaceholder')" />
    <span class="spacer"></span>
    <span class="stats-text">
      <strong>{{ stats.entities }}</strong> {{ t('graph.entities') }} · <strong>{{ stats.relations }}</strong> {{ t('graph.relations') }}
    </span>
  </div>
  <div class="graph-wrap" ref="wrapRef">
    <canvas ref="canvasRef"></canvas>
    <div v-if="loading" class="graph-loading">{{ t('common.loading') }}</div>
  </div>
</div>
  `,
});
