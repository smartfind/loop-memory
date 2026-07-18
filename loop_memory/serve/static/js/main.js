/**
 * Entry point. Boots Vue 3 and mounts the App component.
 *
 * Vue 3 is loaded from a CDN (unpkg). We use the production ESM build
 * to keep the runtime tiny. No build step is required — every component
 * is a self-contained ES module that the browser imports directly.
 *
 * If a future iteration wants SFC + HMR, swap the CDN for the Vite
 * dev server; the component sources are already structured to fit.
 */
import { createApp } from 'https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js';
import { App } from './App.js';

const root = document.getElementById('app');
if (root) {
  createApp(App).mount(root);
} else {
  // Should never happen; helps debugging if the host page is misconfigured.
  console.error('loop-memory: #app root not found');
}
