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
 *   - Click   → select a node (highlight + show full label).
 *
 * The previous version tied rotation to the simulation's alpha, so the
 * sphere froze once the force-sim settled. Now rotation is decoupled and
 * always running (paused only while hovering so picking is easier).
 */
import { defineComponent, ref, onMounted, onUnmounted, watch, nextTick, computed } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
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
    const hoverNode = ref(null);

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
      baseOmega: 0.00055,         // ~0.032°/frame @60fps → ~190s per revolution
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

    /**
     * Distribute N nodes on a sphere via the Fibonacci-lattice method.
     * Even spacing (no clustering at the poles). Interleave high-mention
     * nodes so they spread across the sphere rather than concentrating
     * at one latitude.
     */
    function seedFibonacci() {
      const names = Object.keys(graphNodes);
      names.sort((a, b) => {
        const ma = graphNodes[a].mention || 0;
        const mb = graphNodes[b].mention || 0;
        if (ma !== mb) return mb - ma;
        return stableHash(a) - stableHash(b);
      });
      const N = names.length;
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
        const node = graphNodes[name];
        node.lat0 = lat;
        node.lon = lon0;
        node.shellR = R;
        // 3D coords (z positive toward viewer)
        node.x = R * Math.cos(lat) * Math.sin(lon0);
        node.y = R * Math.sin(lat);
        node.z = R * Math.cos(lat) * Math.cos(lon0);
      });
    }

    /**
     * Force-sim only handles drag-settle + collision. Rotation is decoupled
     * (handled in renderLoop directly) so it never freezes after settling.
     */
    function makeSim() {
      let alpha = 0;
      const nodes = () => Object.values(graphNodes);
      return {
        tick() {
          if (alpha < 0.001) return;
          for (const n of nodes()) {
            for (const m of nodes()) {
              if (m === n) continue;
              const dx = n.x - m.x, dy = n.y - m.y;
              const d2 = dx * dx + dy * dy + 0.01;
              const f = params.repulsion / d2;
              n.vx += (dx / Math.sqrt(d2)) * f * 0.01;
              n.vy += (dy / Math.sqrt(d2)) * f * 0.01;
            }
          }
          for (const e of graphEdges) {
            const a = graphNodes[e.src], b = graphNodes[e.dst];
            if (!a || !b) continue;
            const dx = b.x - a.x, dy = b.y - a.y;
            const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
            const target = params.springLen * (e.weight || 0.5);
            const f = (d - target) * params.springK;
            a.vx += (dx / d) * f; a.vy += (dy / d) * f;
            b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
          }
          for (const n of nodes()) {
            if (n.shellR == null) continue;
            n.vx += -n.x * 0.0008;
            n.vy += -n.y * 0.0008;
          }
          for (const n of nodes()) {
            n.x += n.vx; n.y += n.vy;
            n.vx *= params.damping; n.vy *= params.damping;
          }
          alpha *= 1 - params.alphaDecay;
        },
        restart() { alpha = 1; },
        setAlpha(v) { alpha = Math.max(0, Math.min(1, v)); },
        alpha() { return alpha; },
      };
    }

    function ensureSim() {
      if (!sim) sim = makeSim();
      sim.restart();
      for (let i = 0; i < 20; i++) sim.tick();
      sim.setAlpha(0);
      for (const n of Object.values(graphNodes)) {
        if (n.lat0 !== undefined) {
          n.x = n.shellR * Math.cos(n.lat0) * Math.sin(n.lon);
          n.y = n.shellR * Math.sin(n.lat0);
          n.z = n.shellR * Math.cos(n.lat0) * Math.cos(n.lon);
        }
        n.vx = 0; n.vy = 0;
      }
    }

    function fitToCanvas() {
      const wrap = wrapRef.value; if (!wrap) return;
      const rect = wrap.getBoundingClientRect();
      const W = rect.width > 100 ? rect.width : 1000;
      const H = rect.height > 100 ? rect.height : 700;
      const nodes = Object.values(graphNodes);
      if (!nodes.length) return;
      const sphereR = Math.max(...nodes.map(n => n.shellR || 0), 220);
      const usableW = Math.max(260, W - 96);
      const usableH = Math.max(260, H - 76);
      graphLayout.scale = Math.min(usableW / (sphereR * 2), usableH / (sphereR * 2)) * 0.92;
      graphLayout.tx = W / 2;
      graphLayout.ty = (H - 14) / 2;
    }

    function resizeCanvas() {
      const c = canvasRef.value; if (!c) return;
      const wrap = wrapRef.value; if (!wrap) return;
      const dpr = window.devicePixelRatio || 1;
      const rect = wrap.getBoundingClientRect();
      const width = Math.max(100, Math.round(c.clientWidth || rect.width));
      const height = Math.max(100, Math.round(c.clientHeight || rect.height));
      const pixelWidth = Math.round(width * dpr);
      const pixelHeight = Math.round(height * dpr);
      if (c.width !== pixelWidth) c.width = pixelWidth;
      if (c.height !== pixelHeight) c.height = pixelHeight;
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

    /**
     * Project one (lat, lon) to screen coords with depth.
     * pz > 0 = front of sphere (toward viewer).
     */
    function project(n) {
      const lat = n.lat0;
      const lon = n.lon;
      const R = n.shellR;
      const px = R * Math.cos(lat) * Math.sin(lon);
      const py = R * Math.sin(lat);
      const pz = R * Math.cos(lat) * Math.cos(lon);
      return { px, py, pz, depth: (pz + R) / (2 * R) };  // 0..1
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

    /**
     * Per-frame rotation + re-projection.
     * Decoupled from sim.alpha — the sphere always rotates unless the user
     * is hovering over the canvas (so picking is easy).
     */
    function stepRotation(ts) {
      const frameScale = lastFrame ? Math.min(2.4, Math.max(0.25, (ts - lastFrame) / 16.67)) : 1;
      const paused = (mouseInCanvas && hoveredName) || graphLayout.dragging;
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
      const textFaint = isDark ? 'rgba(238,242,255,0.55)' : 'rgba(15,23,42,0.55)';
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

      // Sphere halo (outer glow)
      const halo = ctx.createRadialGradient(0, 0, sphereR * 0.9, 0, 0, sphereR * 1.5);
      halo.addColorStop(0, isDark ? 'rgba(99,102,241,0.0)' : 'rgba(99,102,241,0.0)');
      halo.addColorStop(0.7, isDark ? 'rgba(99,102,241,0.06)' : 'rgba(99,102,241,0.05)');
      halo.addColorStop(1, isDark ? 'rgba(99,102,241,0)' : 'rgba(99,102,241,0)');
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(0, 0, sphereR * 1.5, 0, Math.PI * 2);
      ctx.fill();

      // Sphere shadow (gives 3D depth)
      const shadow = ctx.createRadialGradient(-sphereR * 0.4, -sphereR * 0.4, sphereR * 0.3, 0, 0, sphereR);
      shadow.addColorStop(0, isDark ? 'rgba(255,255,255,0.04)' : 'rgba(255,255,255,0.18)');
      shadow.addColorStop(1, isDark ? 'rgba(0,0,0,0.45)' : 'rgba(15,23,42,0.06)');
      ctx.fillStyle = shadow;
      ctx.beginPath();
      ctx.arc(0, 0, sphereR, 0, Math.PI * 2);
      ctx.fill();

      // Sphere outline + meridians (latitude rings)
      ctx.strokeStyle = isDark ? 'rgba(129,140,248,0.18)' : 'rgba(99,102,241,0.22)';
      ctx.lineWidth = 0.8;
      // Outline circle
      ctx.beginPath();
      ctx.arc(0, 0, sphereR, 0, Math.PI * 2);
      ctx.stroke();
      // Equator (thicker)
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.ellipse(0, 0, sphereR, sphereR * 0.18, 0, 0, Math.PI * 2);
      ctx.stroke();
      ctx.lineWidth = 0.6;
      // Latitude rings at ±30°, ±60°
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
      // Tilted meridian (visual indicator of rotation axis)
      ctx.strokeStyle = isDark ? 'rgba(129,140,248,0.10)' : 'rgba(99,102,241,0.14)';
      ctx.beginPath();
      ctx.ellipse(0, 0, sphereR * 0.18, sphereR, Math.PI / 6, 0, Math.PI * 2);
      ctx.stroke();
      // Vertical axis (rotation pole)
      ctx.strokeStyle = isDark ? 'rgba(165,180,252,0.20)' : 'rgba(99,102,241,0.25)';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, -sphereR - 8);
      ctx.lineTo(0, sphereR + 8);
      ctx.stroke();
      ctx.setLineDash([]);
      // Pole caps
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
      // Sort back-to-front
      visible.sort((a, b) => a.pz - b.pz);
      const projMap = {};
      for (const v of visible) projMap[v.n.name] = v;
      const visibleNames = new Set(visible.map(v => v.n.name));

      // Hover dim
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
        // Dim if hover on another node
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

      // Nodes (back-to-front). Labels are collected and drawn later in
      // screen space so they stay readable at every zoom level.
      const labelCandidates = [];
      for (const v of visible) {
        const n = v.n;
        const depth = v.depth;  // 0..1, front=1
        const isHover = hoveredName === n.name;
        const isSel = graphSelected && graphSelected.name === n.name;
        const r = nodeRadius(n, depth);
        const baseFill = KIND_COLOR[n.kind] || KIND_COLOR.concept;
        // Stronger depth contrast: back nodes fade out so the sphere reads as 3D
        const depthAlpha = 0.25 + 0.75 * depth;
        const alpha = (dimNonHover && !isHover) ? depthAlpha * 0.20 : depthAlpha;

        // Halo for hover/selection
        if (isHover || isSel) {
          const haloR = r + 10;
          const halo = ctx.createRadialGradient(v.px, v.py, r * 0.6, v.px, v.py, haloR);
          halo.addColorStop(0, hexA(baseFill, 0.55));
          halo.addColorStop(1, hexA(baseFill, 0));
          ctx.fillStyle = halo;
          ctx.beginPath();
          ctx.arc(v.px, v.py, haloR, 0, Math.PI * 2);
          ctx.fill();
        }

        // Filled circle
        ctx.beginPath();
        ctx.arc(v.px, v.py, r, 0, Math.PI * 2);
        ctx.fillStyle = hexA(baseFill, alpha);
        ctx.fill();
        ctx.strokeStyle = isDark ? `rgba(255,255,255,${0.25 * alpha})` : `rgba(255,255,255,${0.85 * alpha})`;
        ctx.lineWidth = 1.2;
        ctx.stroke();

        const mention = n.mention || 0;
        const importantLabel = (n.kind === 'wiki_page' && depth > 0.24) || mention >= 5;
        const mediumLabel = n.kind === 'tag' || mention >= 2;
        const polarLabel = Math.abs(v.py) > sphereR * 0.62 && depth > 0.55;
        const showLabel = isSel || isHover || importantLabel || polarLabel || (mediumLabel && depth > 0.48);
        if (showLabel) {
          labelCandidates.push({
            n, depth, isHover, isSel, r, baseFill,
            sx: graphLayout.tx + v.px * graphLayout.scale,
            sy: graphLayout.ty + v.py * graphLayout.scale,
            below: v.py > sphereR * 0.04,
            priority: (isHover ? 10000 : 0) + (isSel ? 9000 : 0)
              + (n.kind === 'wiki_page' ? 180 : n.kind === 'tag' ? 45 : 0)
              + mention * 5 + depth * 120,
          });
        }
      }
      ctx.restore();

      // Labels: priority + collision culling keeps dense graphs legible while
      // preserving names on both the upper and lower hemispheres.
      labelCandidates.sort((a, b) => b.priority - a.priority);
      const labelBoxes = [];
      const maxLabels = Math.max(22, Math.min(40, Math.floor(cssW / 27)));
      let shownLabels = 0;
      for (const item of labelCandidates) {
        const focus = item.isHover || item.isSel;
        if (!focus && shownLabels >= maxLabels) continue;
        const maxChars = focus ? 42 : item.n.kind === 'wiki_page' ? 24 : 20;
        const label = item.n.name.length > maxChars
          ? item.n.name.slice(0, Math.max(1, maxChars - 1)) + '…'
          : item.n.name;
        ctx.font = focus
          ? '600 12px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif'
          : item.n.kind === 'wiki_page'
            ? '600 10.5px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif'
            : '10.5px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
        const padX = focus ? 9 : item.n.kind === 'wiki_page' ? 6 : 2;
        const lw = ctx.measureText(label).width + padX * 2;
        const lh = focus ? 24 : 18;
        const screenR = Math.max(3, item.r * graphLayout.scale);
        let ly = item.below ? item.sy + screenR + 7 : item.sy - screenR - lh - 7;
        if (ly < 8) ly = item.sy + screenR + 7;
        if (ly + lh > cssH - 30) ly = item.sy - screenR - lh - 7;
        const lx = Math.max(6, Math.min(cssW - lw - 6, item.sx - lw / 2));
        const box = { x: lx - 3, y: ly - 2, w: lw + 6, h: lh + 4 };
        const overlaps = labelBoxes.some(placed => (
          box.x < placed.x + placed.w && box.x + box.w > placed.x
          && box.y < placed.y + placed.h && box.y + box.h > placed.y
        ));
        if (overlaps && !focus) continue;

        if (focus || item.n.kind === 'wiki_page') {
          ctx.fillStyle = focus
            ? (isDark ? 'rgba(30,27,75,0.96)' : 'rgba(255,255,255,0.96)')
            : (isDark ? 'rgba(15,20,34,0.72)' : 'rgba(255,255,255,0.76)');
          roundRect(ctx, lx, ly, lw, lh, focus ? 8 : 6);
          ctx.fill();
          ctx.strokeStyle = hexA(item.baseFill, focus ? 0.68 : 0.22);
          ctx.lineWidth = 1;
          ctx.stroke();
        }
        const labelAlpha = Math.min(1, 0.5 + 0.55 * item.depth);
        ctx.fillStyle = focus
          ? (isDark ? '#eef2ff' : '#25205f')
          : (isDark ? `rgba(238,242,255,${labelAlpha})` : `rgba(15,23,42,${labelAlpha})`);
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(label, lx + lw / 2, ly + lh / 2);
        labelBoxes.push(box);
        shownLabels += 1;
      }

      // HUD
      ctx.save();
      ctx.font = '11px -apple-system, sans-serif';
      ctx.fillStyle = textFaint;
      ctx.textAlign = 'left';
      const total = Object.keys(graphNodes).length;
      const back = visible.filter(v => v.depth < 0.4).length;
      ctx.fillText(`${total} ${t('graph.nodes')} · ${back} ${t('graph.farSide')}`, 12, cssH - 14);
      ctx.textAlign = 'right';
      ctx.fillText(
        `${stats.value.entities} ${t('graph.entities')} · ${stats.value.relations} ${t('graph.relations')}`,
        cssW - 12,
        cssH - 14,
      );
      ctx.restore();
    }

    function renderLoop(ts) {
      if (!animating) return;
      if (sim) sim.tick();
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
      graphLayout.scale *= factor;
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
    }

    function onCanvasEnter() { mouseInCanvas = true; }
    function onCanvasLeave() {
      mouseInCanvas = false;
      hoveredName = null;
      hoverNode.value = null;
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
      const cv = canvasRef.value;
      cv.addEventListener('wheel', onWheel, { passive: false });
      cv.addEventListener('mousedown', onCanvasMouseDown);
      cv.addEventListener('mousemove', onCanvasMouseMove);
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
    });

    onUnmounted(() => {
      animating = false;
      if (rafHandle) cancelAnimationFrame(rafHandle);
      if (resizeObserver) resizeObserver.disconnect();
      window.removeEventListener('resize', onResize);
      window.removeEventListener('mouseup', onCanvasMouseUp);
    });

    const rotationLabel = computed(() => {
      // i18n loads asynchronously; these reactive reads invalidate the label
      // once the dictionaries are ready or the user switches language.
      void store.ready;
      void store.lang;
      if (hoverNode.value) return `⏸ ${t('graph.rotationPaused')}`;
      return `↻ ${t('graph.rotationSlow')}`;
    });

    return {
      canvasRef, wrapRef, loading, stats, filterText, filterKind,
      hoverNode, rotationLabel, t,
      rebuild: loadGraph,
      // expose internal counters for template
      total: computed(() => Object.keys(graphNodes).length),
    };
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
    <span class="rotation-status" :data-paused="!!(hoverNode)" :title="t('graph.rotateTip')">
      {{ rotationLabel }}
    </span>
    <span class="stats-text">
      <strong>{{ stats.entities }}</strong> {{ t('graph.entities') }} ·
      <strong>{{ stats.relations }}</strong> {{ t('graph.relations') }}
    </span>
  </div>
  <div class="graph-wrap" ref="wrapRef">
    <canvas ref="canvasRef"></canvas>
    <div v-if="loading" class="graph-loading">{{ t('common.loading') }}</div>
    <div v-if="hoverNode" class="graph-tooltip">
      <div class="gt-name">{{ hoverNode }}</div>
      <div class="gt-hint">{{ t('graph.hoverHint') }}</div>
    </div>
  </div>
</div>
  `,
});
