/**
 * RingMeter — circular progress ring with a label and optional sub-label.
 *
 * Used by the Dashboard's "ring meters" row (Occupation / Citation / Decay).
 * The geometry is intentionally hard-coded here so the legacy CSS gradients
 * / classnames continue to live in layout.css without a per-instance prop.
 *
 * The component is the textual+svg body of a .ins-ring-card; the consumer
 * provides the label / value (0-100) / colour / sub-label.
 */
import { defineComponent, computed } from '../lib/vue.esm-browser.prod.js';

export const RingMeter = defineComponent({
  name: 'RingMeter',
  props: {
    label: { type: String, required: true },
    value: { type: Number, default: 0 },
    color: { type: String, default: '#6366f1' },
    sub: { type: String, default: '' },
    icon: { type: String, default: '' },
  },
  setup(props) {
    const dashArray = computed(() => {
      const r = 22, c = 2 * Math.PI * r;
      const v = Math.max(0, Math.min(1, (props.value || 0) / 100));
      return { dash: c, offset: c * (1 - v) };
    });
    const displayValue = computed(() => {
      const v = Number(props.value || 0);
      return Number.isFinite(v) ? v.toFixed(0) : '0';
    });
    return { dashArray, displayValue };
  },
  template: /* html */ `
<div class="ins-ring-card">
  <svg class="irc-svg" viewBox="0 0 64 64">
    <circle cx="32" cy="32" r="22" class="ins-ring-track"></circle>
    <circle cx="32" cy="32" r="22" class="ins-ring-arc"
      :stroke="color"
      :stroke-dasharray="dashArray.dash"
      :stroke-dashoffset="dashArray.offset"></circle>
    <text x="32" y="34" class="ins-ring-text">{{ displayValue }}%</text>
  </svg>
  <div class="irc-info">
    <div class="irc-label"><span class="ico" v-if="icon">{{ icon }}</span>{{ label }}</div>
    <div class="irc-val">{{ displayValue }}%</div>
    <div class="irc-sub" v-if="sub">{{ sub }}</div>
  </div>
</div>
  `,
});
