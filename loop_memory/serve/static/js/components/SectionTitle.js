/**
 * SectionTitle — the recurring "<icon> <title> <bar> <right>" header used
 * by every Dashboard section. The CSS classes (.ins-section-title, .ico,
 * .bar, .right) remain unchanged so the styling section in layout.css
 * keeps working without a per-instance prop.
 *
 * Pass `right` to show the trailing meta line; it is hidden when omitted.
 */
import { defineComponent } from '../lib/vue.esm-browser.prod.js';

export const SectionTitle = defineComponent({
  name: 'SectionTitle',
  props: {
    ico: { type: String, default: '' },
    title: { type: String, required: true },
    right: { type: String, default: '' },
  },
  template: /* html */ `
<div class="ins-section-title">
  <span class="ico" v-if="ico">{{ ico }}</span>
  <span>{{ title }}</span>
  <span class="bar"></span>
  <span class="right" v-if="right">{{ right }}</span>
</div>
  `,
});
