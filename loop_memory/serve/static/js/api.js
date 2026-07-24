/**
 * API client for loop-memory.
 *
 * Single fetchJSON wrapper used by every component. Returns the parsed JSON
 * body on 2xx, throws a structured error otherwise. Errors carry the HTTP
 * status, response body, and the original URL so the UI can show useful
 * toasts and so the test endpoint can tell the user exactly what failed.
 */
const API_BASE = '';

function buildUrl(path, params) {
  let url = path.startsWith('http') ? path : API_BASE + path;
  if (params && Object.keys(params).length > 0) {
    const usp = new URLSearchParams();
    for (const k of Object.keys(params)) {
      if (params[k] === undefined || params[k] === null || params[k] === '') continue;
      usp.set(k, String(params[k]));
    }
    const qs = usp.toString();
    if (qs) url += (url.includes('?') ? '&' : '?') + qs;
  }
  return url;
}

export async function fetchJSON(path, opts = {}) {
  const { method = 'GET', params, body, headers = {}, timeoutMs = 30000, cache } = opts;
  const url = buildUrl(path, params);
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), timeoutMs);
  const finalHeaders = { 'Accept': 'application/json', ...headers };
  if (body !== undefined && !(body instanceof FormData)) {
    finalHeaders['Content-Type'] = 'application/json';
  }
  let res;
  try {
    // Pass ``cache`` straight through to the underlying fetch() call so
    // callers can override the default cache mode (e.g. ``'no-store'``
    // for high-volatility read endpoints like /api/sessions/counts).
    const fetchOpts = {
      method,
      headers: finalHeaders,
      body: body === undefined ? undefined
            : body instanceof FormData ? body
            : JSON.stringify(body),
      signal: ctrl.signal,
    };
    if (cache) fetchOpts.cache = cache;
    res = await fetch(url, fetchOpts);
  } catch (e) {
    clearTimeout(tid);
    if (e.name === 'AbortError') {
      throw new ApiError(0, { error: { message: 'Request timed out' } }, url, 'timeout');
    }
    throw new ApiError(0, { error: { message: e.message || 'Network error' } }, url, 'network');
  }
  clearTimeout(tid);
  let data = null;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    try { data = await res.json(); } catch { data = null; }
  } else {
    try { data = await res.text(); } catch { data = null; }
  }
  if (!res.ok) {
    throw new ApiError(res.status, data, url);
  }
  return data;
}

export class ApiError extends Error {
  constructor(status, body, url, kind) {
    const detail = (body && (body.detail || body.error?.message)) || `HTTP ${status}`;
    super(typeof detail === 'string' ? detail : JSON.stringify(detail));
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    this.url = url;
    this.kind = kind || (status === 0 ? 'network' : status >= 500 ? 'server' : 'client');
  }
}

/** Domain endpoints — short, named functions for clarity. */
export const api = {
  // Generic — also re-export the raw fetchJSON helper for the few
  // components that need to call a route the api object doesn't
  // wrap (e.g. Sidebar's /api/sessions/counts).
  fetchJSON,
  diag:           () => fetchJSON('/api/diag'),
  stats:          () => fetchJSON('/api/stats'),

  // Memories
  listMemories:   (params) => fetchJSON('/api/memories', { params }),
  getMemory:      (id) => fetchJSON(`/api/memories/${id}`),
  deleteMemory:   (id) => fetchJSON(`/api/memories/${id}`, { method: 'DELETE' }),
  recall:         (query, limit = 50) => fetchJSON('/api/recall', { params: { query, limit } }),

  // Sessions
  listSessions:   (params) => fetchJSON('/api/sessions', { params }),

  // Wiki
  listWiki:       () => fetchJSON('/api/wiki'),
  getWiki:        (id) => fetchJSON(`/api/wiki/${id}`),
  listContradictions: () => fetchJSON('/api/wiki/contradictions'),
  scanContradictions: (params) => fetchJSON('/api/wiki/contradictions/scan', { method: 'POST', params }),
  mergeWiki:      (winnerId, payload) => fetchJSON(`/api/wiki/${winnerId}/merge`, { method: 'POST', body: payload }),
  resolveWikiContradiction: (pageId) => fetchJSON(`/api/wiki/${pageId}/resolve`, { method: 'POST' }),
  createWiki:     (payload) => fetchJSON('/api/wiki', { method: 'POST', body: payload }),
  updateWiki:     (id, payload) => fetchJSON(`/api/wiki/${id}`, { method: 'PUT', body: payload }),
  deleteWiki:     (id) => fetchJSON(`/api/wiki/${id}`, { method: 'DELETE' }),
  // Bulk-set ``scope`` on one or many wiki pages in a single
  // round-trip. ``payload.page_ids`` is optional — omitting it
  // applies to every page (used by the master "全局" toggle's
  // bulk-ON path).
  bulkScopeWiki:  (payload) => fetchJSON('/api/wiki/bulk-scope', { method: 'POST', body: payload }),

  // Graph
  graph:          (params) => fetchJSON('/api/graph', { params }),

  // LLM admin
  llmProviders:   () => fetchJSON('/api/admin/llm/providers'),
  llmConfig:      async () => {
    const r = await fetchJSON('/api/admin/llm/config');
    return r.config || r;
  },
  llmTest:        (payload) => fetchJSON('/api/admin/llm/test', { method: 'POST', body: payload }),
  llmStatus:      () => fetchJSON('/api/admin/llm/status'),
  llmRun:         (payload) => fetchJSON('/api/admin/llm/run', { method: 'POST', body: payload }),
  llmSchedule:    (payload) => fetchJSON('/api/admin/llm/schedule', { method: 'POST', body: payload }),
  // Full save — writes the entire {provider, model, base_url,
  // schedule, behaviour, api_key} tuple to the settings store. The
  // ``/api/admin/llm/schedule`` endpoint stays around for genuine
  // quick-toggle callers (the dashboard button, hooks, etc.) and
  // merges into ``cfg.schedule`` flat — sending the full form to
  // it would put the entire schedule object under
  // ``cfg.schedule.schedule`` and silently drop the top-level
  // ``enabled``/``mode`` flags, which is the persistence bug.
  saveLlm:         (payload) => fetchJSON('/api/admin/llm/config', { method: 'PUT', body: payload }),

  // Ingest / score
  rescore:        () => fetchJSON('/api/admin/rescore', { method: 'POST' }),
  rebuildGraph:   () => fetchJSON('/api/admin/graph/rebuild', { method: 'POST' }),
  ingest:         (source, path) => fetchJSON('/api/admin/ingest', { method: 'POST', params: { source, path } }),
  // Watcher ingest-cadence settings. ``getIngestConfig`` returns the
  // current values plus the defaults and bounds so the Settings UI
  // can render hint text without hardcoding them.
  getIngestConfig: () => fetchJSON('/api/admin/ingest/config'),
  saveIngestConfig: (payload) => fetchJSON('/api/admin/ingest/config', { method: 'POST', body: payload }),
  // Redaction toggle + preview. The preview endpoint runs the same
  // pipeline the live ingest uses, so the UI can show exactly what
  // a pasted snippet will look like after storage.
  getRedactConfig:    () => fetchJSON('/api/admin/redact'),
  saveRedactConfig:   (payload) => fetchJSON('/api/admin/redact', { method: 'POST', body: payload }),
  redactPreview:      (payload) => fetchJSON('/api/admin/redact/preview', { method: 'POST', body: payload }),
  // Storage budget + manual compact trigger. The dashboard polls
  // getStorage() to show live db size and the compactor's last
  // run timestamp.
  getStorage:         () => fetchJSON('/api/admin/storage'),
  saveStorageBudget:  (payload) => fetchJSON('/api/admin/storage/budget', { method: 'POST', body: payload }),
  runCompact:         (params) => fetchJSON('/api/admin/compact', { method: 'POST', params }),
  // Force-ingest endpoint: skips the watcher's idle window. Used by
  // the IngestPopover "Force active session" button when the user
  // has a long-running conversation and doesn't want to wait for the
  // 60s idle timer.
  forceIngest:    (params) => fetchJSON('/api/admin/watcher/force-ingest', { method: 'POST', params }),
  activeSession:  (source) => fetchJSON('/api/admin/watcher/active-session', { params: { source } }),

  // Audit / runs
  llmAudit:       () => fetchJSON('/api/llm-audit'),
  llmRuns:        (params) => fetchJSON('/api/admin/llm/runs', { params }),

  // Other
  contradiction:  (params) => fetchJSON('/api/contradiction', { params }),
  exportData:     (format) => fetchJSON('/api/export', { params: { format } }),

  // Graph entity memories (memories that mention this entity).
  graphEntityMemories: (name, limit = 10) =>
    fetchJSON(`/api/graph/entity/${encodeURIComponent(name)}/memories`, { params: { limit } }),
};
