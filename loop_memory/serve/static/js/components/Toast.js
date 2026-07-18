/**
 * Toast — bottom-center transient notification.
 */
import { defineComponent, computed } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { store } from '../store.js';

export const Toast = defineComponent({
  name: 'Toast',
  setup() {
    const visible = computed(() => !!store.toast);
    return { visible, store };
  },
  template: /* html */ `
<div class="toast" :class="{ show: visible }">{{ store.toast?.msg || '' }}</div>
  `,
});
