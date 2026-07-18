/**
 * Composable that returns the i18n `t()` function bound to the current
 * language. Components use:
 *
 *     const { t } = useI18n();
 *     const label = t('topbar.stats');
 *
 * Re-renders automatically because `t` reads `store.lang` reactively
 * (Vue's reactivity tracks the dependency).
 */
import { store, t } from '../store.js';

export function useI18n() {
  return { t, lang: () => store.lang };
}
