/**
 * KnowledgeGraph — 3D Fibonacci-sphere knowledge graph.
 *
 * Renders entities on a Fibonacci-sphere (even distribution) and rotates
 * the whole sphere around its vertical (Y) axis. Each tick:
 *   1. Increment every node's longitude by baseOmega (independent of
 *      force-sim alpha — auto-rotation never freezes).
 *   2. Project (lat, lon) → 3D → 2D screen position with depth.
 *   3. Draw edges back-to-front, then nodes back-to-front, then HUD.
 *
 * Interaction model:
 *   - Hover   → pause rotation, jiggle the hovered node, dim others.
 *   - Drag    → drag a node, or pan the canvas (drag empty space).
 *   - Wheel   → zoom toward cursor.
 *   - Click   → select a node (highlight + show full label + side panel).
 *   - Dblclk  → wiki_page node jumps to Wiki tab and opens editor.
 *
 * Toolbar parity (legacy):
 *   [Rebuild] [wiki/memory] [stats] ___ [search] [kind ⌄] [Fit] [⟳] [+] [-]
 *
 * Below the canvas:
 *   - Side panel (when node selected) — title, kind, mention count,
 *     connected edges (top 20 by weight) and evidence memories.
 *   - Bottom legend strip (Wiki/Tag/Concept/Acronym colored dots).
 */
import { defineComponent, ref, onMounted, onUnmounted, watch, nextTick, computed } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store, t, toast } from '../store.js';
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

const KIND_LABEL = {
  concept:   'Concept',
  acronym:   'Acronym',
  cjk:       'CJK',
  tag:       'Tag',
  url:       'URL',
  path:      'Path',
  wiki_page: 'Wiki',
};
const KIND_LABEL_ZH = {
  concept:   '概念',
  acronym:   '缩写',
  cjk:       '中文',
  tag:       '标签',
  url:       '链接',
  path:      '路径',
  wiki_page: '知识库',
};

function kindLabel(kind) {
  return store.lang === 'zh'
    ? (KIND_LABEL_ZH[kind] || kind)
    : (KIND_LABEL[kind] || kind);
}

export const KnowledgeGraph = defineComponent({
  name: 'KnowledgeGraph',
  emits: ['open-wiki'],
  setup(_, { emit }) {
    const canvasRef = ref(null);
    const wrapRef = ref(null);
    const shellRef = ref(null);
    const loading = ref(false);
    const stats = ref({ entities: 0, relations: 0 });
    const filterText = ref('');
    const filterKind = ref('');
    const rebuildMode = ref('wiki');                 // 'wiki' | 'memory'
    const rotationEnabled = ref(true);               // ⟳ toggle
    const hoverNode = ref(null);

    // Side panel state
    const selectedNode = ref(null);                  // node name (string) or null
    const sideConnected = ref([]);                   // [{other, weight}]
    const sideEvidence = ref([]);                     // [{text, ...}]
    const sideLoading = ref(false);
    const sideMentions = ref(0);
    let graphSelectedKind = '';

    // ---- Mutable draw state (plain objects, no Vue reactivity) ----
    let graphData = { entities: [], relations: [] };
    let graphNodes = {};
    let graphEdges = [];
    let graphLayout = { scale: 1, tx: 0, ty: 0, dragging: null };
    let graphSelected = null;
    let sim = null;
    let animating = false;
    let hoveredName = null;
    let mouseInCanvas = false;
    let lastFrame = 0;
    let rafHandle = null;

    // ---- Tuning ----
    const params = {
      baseOmega: 0.00055,
      repulsion: 250,
      springLen: 120,
      springK: 0.012,
      damping: 0.82,
      alphaDecay: 0.04,
      collideR: 18,
      centerK: 0.018,
    };

    async function loadGraph() {
      loading.value = true;
      try {
        const entLimit = window._graphEntLimit || 120;
        const relLimit = Math.min(entLimit * 4, 600);
        const g = await api.graph({ limit_entities: entLimit, limit_relations: relLimit });
        graphData = g;
        stats.value = {
          entities: (g.entities || []).length,
          relations: (g.relations || []).length,
        };

        graphEdges = (g.relations || []).map((r, i) => ({
          src: r.src, dst: r.dst, weight: r.weight,
          evidence: r.evidence, id: r.id, _i: i,
          edgeKind: r.kind || 'related',
        }));
        graphNodes = {};
        for (const e of (g.entities || [])) {
          graphNodes[e.name] = {
            name: e.name,
            kind: e.kind,
            weight: e.weight,
            mention: e.mention_count,
            x: 0, y: 0, z: 0,
            vx: 0, vy: 0,
            lat0: 0, lon: 0, shellR: 1,
            jitter: 0,
          };
        }
        seedFibonacci();
        ensureSim();
        fitToCanvas();
        drawCanvas();
        if (selectedNode.value && graphNodes[selectedNode.value]) {
          showNodeDetails(graphNodes[selectedNode.value]);
        }
      } catch (e) {
        console.error('graph load failed', e);
      } finally {
        loading.value = false;
      }
    }

    async function rebuildGraph() {
      loading.value = true;
      try {
        const modeLabel = rebuildMode.value === 'wiki'
          ? (store.lang === 'zh' ? '知识库' : 'distilled wiki')
          : (store.lang === 'zh' ? '原始记忆' : 'raw memories');
        toast(
          (store.lang === 'zh' ? `正在从${modeLabel}重建图谱…` : `Rebuilding graph from ${modeLabel}…`),
          2500,
        );
        const r = await fetch(`/api/admin/graph/rebuild?clear=true&mode=${encodeURIComponent(rebuildMode.value)}`, {
          method: 'POST',
        });
        if (!r.ok) throw new Error('rebuild failed: HTTP ' + r.status);
        const data = await r.json();
        toast(
          (store.lang === 'zh'
            ? `图谱已重建：${data.entities} 实体 / ${data.relations} 关系`
            : `Graph rebuilt: ${data.entities} entities / ${data.relations} relations`),
          3000,
        );
        await loadGraph();
      } catch (e) {
        toast((store.lang === 'zh' ? '重建失败：' : 'Rebuild failed: ') + e.message, 4000);
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
      if (N === 0) return;
      const R = Math.max(220, Math.sqrt(N) * 32);
      const PHI = Math.PI * (3 - Math.sqrt(5));
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
        const n = graphNodes[name];
        n.lat0 = lat;
        n.lon = lon0;
        n.shellR = R;
        const p = project(n);
        n.x = p.px; n.y = p.py; n.z = p.pz;
      });
    }

    function project(n) {
      const R = n.shellR || 220;
      const lat = n.lat0 || 0;
      const lon = n.lon || 0;
      const px = R * Math.cos(lat) * Math.sin(lon);
      const py = R * Math.sin(lat);
      const pz = R * Math.cos(lat) * Math.cos(lon);
      return { px, py, pz, depth: (pz + R) / (2 * R) };
    }

    function nodeRadius(n, depth) {
      const sizeMul = 0.55 + 0.85 * depth;
      const kindBonus = n.kind === 'wiki_page' ? 2.25 : n.kind === 'tag' ? 0.72 : 1;
      return (3.5 + Math.sqrt(n.mention || 1) * 1.6) * sizeMul * kindBonus;
    }

    function pickNode(cx, cy) {
      let best = null;
      let bestScore = Infinity;
      for (const n of Object.values(graphNodes)) {
        if (filterKind.value && n.kind !== filterKind.value) continue;
        if (filterText.value && !n.name.toLowerCase().includes(filterText.value.toLowerCase())) continue;
        const p = project(n);
        const sx = graphLayout.tx + p.px * graphLayout.scale;
        const sy = graphLayout.ty + p.py * graphLayout.scale;
        const dx = sx - cx, dy = sy - cy;
        const d = Math.sqrt(dx * dx + dy * dy);
        const hitRadius = Math.max(14, Math.min(28, nodeRadius(n, p.depth) * graphLayout.scale + 8));
        if (d > hitRadius) continue;
        const score = d / hitRadius - p.depth * 0.12;
        if (score < bestScore) {
          bestScore = score;
          best = n;
        }
      }
      return best;
    }

    function stepRotation(ts) {
      const frameScale = lastFrame ? Math.min(2.4, Math.max(0.25, (ts - lastFrame) / 16.67)) : 1;
      const paused = !(rotationEnabled.value) || (mouseInCanvas && hoveredName) || graphLayout.dragging;
      const rot = paused ? 0 : params.baseOmega * frameScale;
      for (const n of Object.values(graphNodes)) {
        if (n.lat0 == null) continue;
        n.lon = (n.lon || 0) + rot;
        const p = project(n);
        n.x = p.px;
        n.y = p.py;
        n.z = p.pz;
      }
      lastFrame = ts;
    }

    function hexA(hex, alpha) {
      const a = Math.round(Math.max(0, Math.min(1, alpha)) * 255).toString(16).padStart(2, '0');
      return hex + a;
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
      const bg = isDark ? '#0b0e16' : '#fafbfd';
      const cssW = c.clientWidth, cssH = c.clientHeight;

      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, cssW, cssH);

      // Sphere outline + atmospheric shading
      ctx.save();
      ctx.translate(graphLayout.tx, graphLayout.ty);
      ctx.scale(graphLayout.scale, graphLayout.scale);
      const nodes = Object.values(graphNodes);
      let rMax = 0;
      for (const n of nodes) {
        const r = Math.sqrt(n.x * n.x + n.y * n.y);
        if (r > rMax) rMax = r;
      }
      const sphereR = rMax + 4;

      const halo = ctx.createRadialGradient(0, 0, sphereR * 0.9, 0, 0, sphereR * 1.5);
      halo.addColorStop(0, isDark ? 'rgba(99,102,241,0.0)' : 'rgba(99,102,241,0.0)');
      halo.addColorStop(0.7, isDark ? 'rgba(99,102,241,0.06)' : 'rgba(99,102,241,0.05)');
      halo.addColorStop(1, isDark ? 'rgba(99,102,241,0)' : 'rgba(99,102,241,0)');
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(0, 0, sphereR * 1.5, 0, Math.PI * 2);
      ctx.fill();

      const shadow = ctx.createRadialGradient(-sphereR * 0.4, -sphereR * 0.4, sphereR * 0.3, 0, 0, sphereR);
      shadow.addColorStop(0, isDark ? 'rgba(255,255,255,0.04)' : 'rgba(255,255,255,0.18)');
      shadow.addColorStop(1, isDark ? 'rgba(0,0,0,0.45)' : 'rgba(15,23,42,0.06)');
      ctx.fillStyle = shadow;
      ctx.beginPath();
      ctx.arc(0, 0, sphereR, 0, Math.PI * 2);
      ctx.fill();

      ctx.strokeStyle = isDark ? 'rgba(129,140,248,0.18)' : 'rgba(99,102,241,0.22)';
      ctx.lineWidth = 0.8;
      ctx.beginPath();
      ctx.arc(0, 0, sphereR, 0, Math.PI * 2);
      ctx.stroke();
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.ellipse(0, 0, sphereR, sphereR * 0.18, 0, 0, Math.PI * 2);
      ctx.stroke();
      ctx.lineWidth = 0.6;
      for (const latDeg of [-60, -30, 30, 60]) {
        const lat = latDeg * Math.PI / 180;
        const ry = sphereR * 0.18 * Math.cos(lat) * 0.5 + sphereR * 0.06;
        const rx = sphereR * Math.cos(lat);
        if (rx > 4) {
          ctx.beginPath();
          ctx.ellipse(0, sphereR * Math.sin(lat), rx, Math.max(2, ry), 0, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
      ctx.strokeStyle = isDark ? 'rgba(129,140,248,0.10)' : 'rgba(99,102,241,0.14)';
      ctx.beginPath();
      ctx.ellipse(0, 0, sphereR * 0.18, sphereR, Math.PI / 6, 0, Math.PI * 2);
      ctx.stroke();
      ctx.strokeStyle = isDark ? 'rgba(165,180,252,0.20)' : 'rgba(99,102,241,0.25)';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, -sphereR - 8);
      ctx.lineTo(0, sphereR + 8);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = isDark ? 'rgba(165,180,252,0.7)' : 'rgba(99,102,241,0.7)';
      ctx.beginPath(); ctx.arc(0, -sphereR - 6, 3, 0, Math.PI * 2); ctx.fill();
      ctx.beginPath(); ctx.arc(0,  sphereR + 6, 3, 0, Math.PI * 2); ctx.fill();
      ctx.restore();

      // Filtered visible nodes + projection
      const visible = [];
      for (const n of nodes) {
        if (filterKind.value && n.kind !== filterKind.value) continue;
        if (filterText.value && !n.name.toLowerCase().includes(filterText.value.toLowerCase())) continue;
        const p = project(n);
        visible.push({ n, ...p });
      }
      visible.sort((a, b) => a.pz - b.pz);
      const projMap = {};
      for (const v of visible) projMap[v.n.name] = v;
      const visibleNames = new Set(visible.map(v => v.n.name));

      const dimNonHover = !!(mouseInCanvas && hoveredName);

      // Edges (back-to-front)
      ctx.save();
      ctx.translate(graphLayout.tx, graphLayout.ty);
      ctx.scale(graphLayout.scale, graphLayout.scale);
      for (const e of graphEdges) {
        const a = graphNodes[e.src], b = graphNodes[e.dst];
        if (!a || !b) continue;
        if (!visibleNames.has(a.name) || !visibleNames.has(b.name)) continue;
        const pa = projMap[a.name], pb = projMap[b.name];
        if (!pa || !pb) continue;
        const depthAvg = (pa.pz + pb.pz) / (2 * (pa.n.shellR || 1));
        const depthAlpha = 0.45 + 0.55 * depthAvg;
        const edgeKind = e.edgeKind || 'related';
        let strokeColor;
        if (edgeKind === 'tagged_with') strokeColor = hexA('#ec4899', 0.7 * depthAlpha);
        else if (edgeKind === 'mentions') strokeColor = hexA('#6366f1', 0.55 * depthAlpha);
        else if (edgeKind === 'related_to') strokeColor = hexA('#f59e0b', 0.75 * depthAlpha);
        else strokeColor = hexA('#6366f1', 0.6 * depthAlpha);
        if (dimNonHover && a.name !== hoveredName && b.name !== hoveredName) {
          strokeColor = strokeColor.replace(/[\d.]+\)$/g, m => (parseFloat(m) * 0.18).toFixed(2) + ')');
        }
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(pa.px, pa.py);
        ctx.lineTo(pb.px, pb.py);
        ctx.stroke();
      }
      ctx.restore();

      // Nodes (back-to-front)
      for (const v of visible) {
        const n = v.n;
        const sx = graphLayout.tx + v.px * graphLayout.scale;
        const sy = graphLayout.ty + v.py * graphLayout.scale;
        const depthMul = 0.55 + 0.85 * v.depth;
        const r = nodeRadius(n, v.depth) * graphLayout.scale * depthMul;
        if (r <= 0.3) continue;
        const baseFill = KIND_COLOR[n.kind] || KIND_COLOR.concept;
        const isHover = hoveredName === n.name;
        const isSelected = graphSelected && graphSelected.name === n.name;
        const isFar = v.depth < 0.20;
        if (isFar && !isHover && !isSelected) {
          ctx.fillStyle = hexA(baseFill, 0.18);
          ctx.beginPath();
          ctx.arc(sx, sy, Math.max(1.2, r * 0.7), 0, Math.PI * 2);
          ctx.fill();
          continue;
        }
        const labelAlpha = (v.pz + (n.shellR || 1)) / (2 * (n.shellR || 1));
        const importantLabel = (n.kind === 'wiki_page' && labelAlpha > 0.24) || (n.mention || 0) >= 5;
        const mediumLabel = n.kind === 'tag' || (n.mention || 0) >= 2;
        if (isHover || isSelected) {
          ctx.fillStyle = hexA(baseFill, 0.30);
          ctx.beginPath();
          ctx.arc(sx, sy, r * (isHover ? 2.2 : 1.9), 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillStyle = isHover || isSelected ? baseFill : hexA(baseFill, 0.78 + 0.22 * labelAlpha);
        ctx.beginPath();
        ctx.arc(sx, sy, r, 0, Math.PI * 2);
        ctx.fill();
        if (isHover || isSelected) {
          ctx.lineWidth = 1.6;
          ctx.strokeStyle = isDark ? '#f8fafc' : '#ffffff';
          ctx.stroke();
        }
        if (v.pz > -50 && (importantLabel || mediumLabel || isHover || isSelected)) {
          const focus = isHover || isSelected;
          const maxChars = focus ? 42 : n.kind === 'wiki_page' ? 24 : 20;
          const text = n.name.length > maxChars ? n.name.slice(0, maxChars - 1) + '…' : n.name;
          const fontSize = focus ? 13 : n.kind === 'wiki_page' ? 12 : 10.5;
          ctx.font = (focus ? '600 ' : '500 ') + fontSize + 'px var(--ui-font, system-ui)';
          const m = ctx.measureText(text);
          const padX = focus ? 9 : n.kind === 'wiki_page' ? 6 : 2;
          const lw = m.width + padX * 2;
          const lh = 16;
          let lx = sx;
          let ly = sy - r - 8 - lh / 2;
          if (focus) ly -= 2;
          ctx.fillStyle = isDark ? 'rgba(15,23,42,0.85)' : 'rgba(255,255,255,0.92)';
          ctx.strokeStyle = baseFill;
          ctx.lineWidth = 1;
          ctx.beginPath();
          const x = lx - lw / 2;
          const y = ly - lh / 2;
          const rr = 6;
          ctx.moveTo(x + rr, y);
          ctx.arcTo(x + lw, y, x + lw, y + lh, rr);
          ctx.arcTo(x + lw, y + lh, x, y + lh, rr);
          ctx.arcTo(x, y + lh, x, y, rr);
          ctx.arcTo(x, y, x + lw, y, rr);
          ctx.closePath();
          ctx.fill();
          ctx.stroke();
          ctx.fillStyle = isDark ? '#fdf2f8' : '#1e1b4b';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText(text, lx, ly);
        }
      }
    }

    function fitToCanvas() {
      const wrap = wrapRef.value;
      if (!wrap) return;
      const rect = wrap.getBoundingClientRect();
      const nodes = Object.values(graphNodes);
      if (nodes.length === 0) return;
      let rMax = 0;
      for (const n of nodes) {
        const r = Math.sqrt(n.x * n.x + n.y * n.y);
        if (r > rMax) rMax = r;
      }
      const W = rect.width > 100 ? rect.width : 1000;
      const H = rect.height > 100 ? rect.height : 700;
      const R = rMax + 60;
      const w = R * 2, h = R * 2;
      graphLayout.scale = Math.min(W / w, H / h) * 0.96;
      graphLayout.tx = W / 2;
      graphLayout.ty = H / 2;
    }

    function zoomBy(factor, cx, cy) {
      const c = canvasRef.value;
      if (!c) return;
      const rect = c.getBoundingClientRect();
      const ccx = cx != null ? cx - rect.left : rect.width / 2;
      const ccy = cy != null ? cy - rect.top : rect.height / 2;
      const wx = (ccx - graphLayout.tx) / graphLayout.scale;
      const wy = (ccy - graphLayout.ty) / graphLayout.scale;
      graphLayout.scale = Math.max(0.2, Math.min(4, graphLayout.scale * factor));
      graphLayout.tx = ccx - wx * graphLayout.scale;
      graphLayout.ty = ccy - wy * graphLayout.scale;
      drawCanvas();
    }
    function zoomIn() { zoomBy(1.25); }
    function zoomOut() { zoomBy(1 / 1.25); }
    function fit() { fitToCanvas(); drawCanvas(); }

    function ensureSim() {
      if (sim) return;
      sim = { tick() {}, restart() {}, setAlpha() {}, alpha: () => 0 };
    }

    function renderLoop(ts) {
      if (!animating) return;
      stepRotation(ts);
      drawCanvas();
      rafHandle = requestAnimationFrame(renderLoop);
    }

    function onWheel(e) {
      e.preventDefault();
      const rect = canvasRef.value.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const factor = Math.exp(-e.deltaY * 0.0015);
      const wx = (cx - graphLayout.tx) / graphLayout.scale;
      const wy = (cy - graphLayout.ty) / graphLayout.scale;
      graphLayout.scale = Math.max(0.2, Math.min(4, graphLayout.scale * factor));
      graphLayout.tx = cx - wx * graphLayout.scale;
      graphLayout.ty = cy - wy * graphLayout.scale;
      drawCanvas();
    }

    function onCanvasMouseMove(e) {
      const rect = canvasRef.value.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const node = pickNode(cx, cy);
      hoveredName = node ? node.name : null;
      hoverNode.value = node ? node.name : null;
      if (graphLayout.dragging) {
        if (graphLayout.dragging.kind === 'pan') {
          graphLayout.tx = graphLayout.dragging.tx0 + (e.clientX - graphLayout.dragging.x0);
          graphLayout.ty = graphLayout.dragging.ty0 + (e.clientY - graphLayout.dragging.y0);
        } else if (graphLayout.dragging.kind === 'node') {
          const wx = (cx - graphLayout.tx) / graphLayout.scale;
          const wy = (cy - graphLayout.ty) / graphLayout.scale;
          const dragged = graphLayout.dragging.node;
          const radius = dragged.shellR || 220;
          const clampedY = Math.max(-radius * 0.98, Math.min(radius * 0.98, wy));
          dragged.lat0 = Math.asin(clampedY / radius);
          const latitudeRadius = Math.max(1, radius * Math.cos(dragged.lat0));
          const sinLon = Math.max(-1, Math.min(1, wx / latitudeRadius));
          const nearLon = Math.asin(sinLon);
          dragged.lon = Math.cos(dragged.lon) < 0 ? Math.PI - nearLon : nearLon;
        }
      }
    }

    function onCanvasMouseDown(e) {
      const rect = canvasRef.value.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const node = pickNode(cx, cy);
      if (node) {
        graphLayout.dragging = { kind: 'node', node, ox: node.x, oy: node.y };
        graphSelected = node;
        showNodeDetails(node);
      } else {
        graphLayout.dragging = { kind: 'pan', x0: e.clientX, y0: e.clientY, tx0: graphLayout.tx, ty0: graphLayout.ty };
      }
    }

    function onCanvasMouseUp() {
      if (graphLayout.dragging && graphLayout.dragging.kind === 'node') {
        const n = graphLayout.dragging.node;
        const p = project(n);
        n.x = p.px; n.y = p.py; n.z = p.pz;
      }
      graphLayout.dragging = null;
    }

    function onCanvasClick(e) {
      const rect = canvasRef.value.getBoundingClientRect();
      const node = pickNode(e.clientX - rect.left, e.clientY - rect.top);
      graphSelected = node || null;
      showNodeDetails(node);
      drawCanvas();
    }

    async function onCanvasDblClick(e) {
      const rect = canvasRef.value.getBoundingClientRect();
      const node = pickNode(e.clientX - rect.left, e.clientY - rect.top);
      if (!node) return;
      if (node.kind === 'wiki_page' && node.name.startsWith('wiki:')) {
        const slug = node.name.slice('wiki:'.length);
        emit('open-wiki', { slug });
      }
    }

    async function showNodeDetails(node) {
      if (!node) {
        selectedNode.value = null;
        sideConnected.value = [];
        sideEvidence.value = [];
        sideMentions.value = 0;
        graphSelectedKind = '';
        return;
      }
      selectedNode.value = node.name;
      graphSelectedKind = node.kind || '';
      const connected = graphEdges
        .filter(e => e.src === node.name || e.dst === node.name)
        .map(e => ({
          other: e.src === node.name ? e.dst : e.src,
          weight: e.weight,
          evidence: e.evidence,
        }));
      connected.sort((a, b) => (b.weight || 0) - (a.weight || 0));
      sideConnected.value = connected.slice(0, 20);
      // server-provided mention count (how many memories mention this entity).
      sideMentions.value = node.mention || 0;
      sideEvidence.value = [];
      sideLoading.value = true;
      try {
        const evs = await api.graphEntityMemories(node.name, 8);
        sideEvidence.value = (evs || []).map(m => ({
          id: m.id,
          text: (m.text || '').slice(0, 200),
        }));
      } catch (e) {
        sideEvidence.value = [];
      } finally {
        sideLoading.value = false;
      }
    }

    function dismissSide() {
      graphSelected = null;
      selectedNode.value = null;
      sideConnected.value = [];
      sideEvidence.value = [];
      sideMentions.value = 0;
      graphSelectedKind = '';
      drawCanvas();
    }

    function onCanvasEnter() { mouseInCanvas = true; }
    function onCanvasLeave() {
      mouseInCanvas = false;
      hoveredName = null;
      hoverNode.value = null;
    }

    function toggleRotation() {
      rotationEnabled.value = !rotationEnabled.value;
    }

    function resizeCanvas() {
      const c = canvasRef.value;
      if (!c) return;
      const dpr = window.devicePixelRatio || 1;
      const rect = c.getBoundingClientRect();
      c.width = Math.max(1, rect.width * dpr);
      c.height = Math.max(1, rect.height * dpr);
      c.style.width = rect.width + 'px';
      c.style.height = rect.height + 'px';
    }

    let resizeObserver = null;
    function onResize() {
      resizeCanvas();
      fitToCanvas();
      drawCanvas();
    }

    watch(() => store.lang, () => drawCanvas());
    watch(() => store.theme, () => drawCanvas());

    function onExternalRebuild() { rebuildGraph(); }
    onMounted(async () => {
      await nextTick();
      resizeCanvas();
      const cv = canvasRef.value;
      cv.addEventListener('wheel', onWheel, { passive: false });
      cv.addEventListener('mousedown', onCanvasMouseDown);
      cv.addEventListener('mousemove', onCanvasMouseMove);
      cv.addEventListener('click', onCanvasClick);
      cv.addEventListener('dblclick', onCanvasDblClick);
      cv.addEventListener('mouseenter', onCanvasEnter);
      cv.addEventListener('mouseleave', onCanvasLeave);
      window.addEventListener('mouseup', onCanvasMouseUp);
      window.addEventListener('resize', onResize);
      if (window.ResizeObserver) {
        resizeObserver = new ResizeObserver(onResize);
        if (wrapRef.value) resizeObserver.observe(wrapRef.value);
      }
      await loadGraph();
      animating = true;
      rafHandle = requestAnimationFrame(renderLoop);
      window.addEventListener('loop-memory:rebuild-graph', onExternalRebuild);
    });

    onUnmounted(() => {
      animating = false;
      if (rafHandle) cancelAnimationFrame(rafHandle);
      if (resizeObserver) resizeObserver.disconnect();
      window.removeEventListener('resize', onResize);
      window.removeEventListener('mouseup', onCanvasMouseUp);
      window.removeEventListener('loop-memory:rebuild-graph', onExternalRebuild);
    });

    const rotationLabel = computed(() => {
      void store.ready;
      void store.lang;
      if (!rotationEnabled.value) return '⏸ ' + t('graph.rotationPaused');
      if (hoverNode.value) return '⏸ ' + t('graph.rotationPaused');
      return '↻ ' + t('graph.rotationSlow');
    });

    const graphSelectedKindProxy = computed(() => graphSelectedKind);

    return {
      store, t,
      canvasRef, wrapRef, shellRef, loading, stats,
      filterText, filterKind, rebuildMode, rotationEnabled,
      hoverNode, rotationLabel,
      // side panel
      selectedNode, sideConnected, sideEvidence, sideLoading, sideMentions,
      graphSelectedKind: graphSelectedKindProxy,
      kindLabel,
      // actions
      rebuild: rebuildGraph,
      refresh: loadGraph,
      zoomIn, zoomOut, fit, toggleRotation,
      onFilterKindChange: () => drawCanvas(),
      dismissSide,
    };
  },
  template: /* html */ `
<div class="tab-pane" id="pane-graph">
  <div class="graph-toolbar">
    <button class="tb-action primary" @click="rebuild">{{ t('action.rebuild') }}</button>
    <select v-model="rebuildMode" class="kg-mode" :title="t('graph.modeTip')">
      <option value="wiki">{{ t('graph.mode.wiki') }}</option>
      <option value="memory">{{ t('graph.mode.memory') }}</option>
    </select>
    <span class="stat-pill">
      <strong>{{ stats.entities }}</strong> {{ t('graph.entities') }} ·
      <strong>{{ stats.relations }}</strong> {{ t('graph.relations') }}
    </span>
    <span class="spacer"></span>
    <input v-model="filterText" :placeholder="t('graph.filterPlaceholder')"
           @input="onFilterKindChange" />
    <select v-model="filterKind" class="kg-kind" @change="onFilterKindChange">
      <option value="">{{ t('graph.allKinds') }}</option>
      <option value="concept">{{ t('kind.concept') }}</option>
      <option value="acronym">{{ t('kind.acronym') }}</option>
      <option value="cjk">{{ t('kind.cjk') }}</option>
      <option value="tag">{{ t('kind.tag') }}</option>
      <option value="url">{{ t('kind.url') }}</option>
      <option value="path">{{ t('kind.path') }}</option>
      <option value="wiki_page">{{ t('kind.wiki') }}</option>
    </select>
    <button class="tb-action ghost" @click="fit" :title="t('graph.fit')">{{ t('graph.fit') }}</button>
    <button class="tb-action ghost" :class="{ active: rotationEnabled }"
            @click="toggleRotation" :title="t('graph.rotateTip')">⟳</button>
    <button class="tb-action ghost" @click="zoomIn" title="+">+</button>
    <button class="tb-action ghost" @click="zoomOut" title="−">−</button>
  </div>
  <div class="graph-shell" ref="shellRef">
    <div class="graph-wrap" ref="wrapRef">
      <canvas ref="canvasRef"></canvas>
      <div v-if="loading" class="graph-loading">{{ t('common.loading') }}</div>
      <div v-if="hoverNode" class="graph-tooltip">
        <div class="gt-name">{{ hoverNode }}</div>
        <div class="gt-hint">{{ t('graph.hoverHint') }}</div>
      </div>
      <div class="rotation-status" :data-paused="!rotationEnabled || !!hoverNode"
           :title="t('graph.rotateTip')">
        {{ rotationLabel }}
      </div>
    </div>
    <aside class="graph-side" v-if="selectedNode">
      <div class="graph-side-head">
        <h3>{{ selectedNode }}</h3>
        <button class="graph-side-close" @click="dismissSide">×</button>
      </div>
      <div class="graph-side-meta">
        <span class="badge kg-badge-kind">{{ kindLabel(graphSelectedKind) }}</span>
        <span class="badge kg-badge-mentions">
          {{ t('graph.mentions', { n: sideMentions || 0 }) }}
        </span>
      </div>
      <h4>{{ t('graph.connectedTo') }}</h4>
      <div class="rel-list">
        <div class="rel-row" v-for="c in sideConnected" :key="c.other">
          <span class="rel-name">{{ c.other }}</span>
          <span class="badge">{{ (c.weight || 0).toFixed(2) }}</span>
        </div>
        <div class="rel-empty" v-if="!sideConnected.length">—</div>
      </div>
      <h4>{{ t('graph.evidence') }}</h4>
      <div class="evidence-list">
        <div class="evidence-row" v-for="m in sideEvidence" :key="m.id">
          {{ m.text }}
        </div>
        <div class="evidence-empty" v-if="sideLoading">…</div>
        <div class="evidence-empty" v-else-if="!sideEvidence.length">—</div>
      </div>
    </aside>
    <div class="graph-legend">
      <span><span class="dot" style="background:#f59e0b"></span>{{ t('kind.wiki') }}</span>
      <span><span class="dot" style="background:#ec4899"></span>{{ t('kind.tag') }}</span>
      <span><span class="dot" style="background:#6366f1"></span>{{ t('kind.concept') }}</span>
      <span><span class="dot" style="background:#10b981"></span>{{ t('kind.acronym') }}</span>
    </div>
  </div>
</div>
  `,
});
