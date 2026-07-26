// ---- HTML sanitizer for v-html (XSS protection) ----
// Audit M1: the previous regex-based version was bypassable via
// HTML-entity-encoded javascript: URLs (``java&#x73;cript:``),
// unusual whitespace in attribute values, mixed-case tag names,
// and several mutation-XSS variants. We now parse the HTML in a
// sandboxed DOM (``DOMParser``), walk the tree, and drop any
// element/attribute that isn't on the explicit allow-list. The
// CSP middleware (``script-src 'self' 'unsafe-eval'``) already
// blocks inline event handlers, but js: URLs are *navigation*
// vectors and are not covered by CSP script-src, so we still
// need this layer.
//
// Safe tag whitelist (a strict Markdown-rendered subset):
const _ALLOWED_TAGS = new Set([
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'p', 'br', 'hr',
  'ul', 'ol', 'li',
  'strong', 'em', 'b', 'i', 'u', 's', 'code', 'pre',
  'blockquote', 'details', 'summary',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'a', 'img', 'sub', 'sup', 'span', 'div',
]);
// Attributes allowed per tag. Anything not on this list is stripped.
const _ALLOWED_ATTRS = {
  'a':   new Set(['href', 'title', 'rel', 'target']),
  'img': new Set(['src', 'alt', 'title', 'width', 'height']),
  '*':   new Set(['id']),
};
// URL schemes accepted on href / src. Everything else is stripped.
const _SAFE_SCHEMES = /^(?:https?:|mailto:|tel:|#|\/)/i;
// Static-image data: URLs are allowed; everything else is killed.
const _SAFE_DATA_IMG = /^data:image\/(?:png|jpe?g|gif|webp|svg\+xml);/i;

function _scrubAttrs(el) {
  const tag = el.tagName.toLowerCase();
  if (!_ALLOWED_TAGS.has(tag)) return false;        // drop whole element
  const allowed = new Set([...(_ALLOWED_ATTRS['*'] || []), ...(_ALLOWED_ATTRS[tag] || [])]);
  const attrs = Array.from(el.attributes);
  for (const attr of attrs) {
    const name = attr.name.toLowerCase();
    if (!allowed.has(name)) { el.removeAttribute(attr.name); continue; }
    let val = attr.value || '';
    // URL-bearing attributes get extra scrutiny.
    if (name === 'href' || name === 'src' ||
        name === 'xlink:href' || name === 'formaction') {
      val = val.trim();
      // Strip control chars / whitespace that browsers interpret as
      // part of the scheme (e.g. ``java	script:``).
      const cleaned = val.replace(/[\x00-\x1f\x7f\s]+/g, '');
      const ok = _SAFE_SCHEMES.test(cleaned) ||
                 (name === 'src' && _SAFE_DATA_IMG.test(cleaned));
      if (!ok) {
        if (name === 'href') el.setAttribute(attr.name, '#');
        else el.removeAttribute(attr.name);
        continue;
      }
      // Force ``rel="noopener noreferrer"`` on outbound links.
      if (tag === 'a' && /^https?:/i.test(cleaned)) {
        el.setAttribute('rel', 'noopener noreferrer');
      }
    }
    // Defensive: kill any surviving ``style`` attribute.
    if (name === 'style') el.removeAttribute(attr.name);
  }
  return true;
}

function _domAvailable() {
  try { return typeof DOMParser !== 'undefined' && typeof document !== 'undefined'; }
  catch (e) { return false; }
}

export function sanitizeHtml(dirty) {
  if (dirty == null) return '';
  const src = String(dirty);
  if (!src) return '';
  if (!_domAvailable()) {
    // No DOM means there's no v-html sink either (Node / tests).
    // Just escape everything to be safe.
    return escapeHtml(src);
  }
  const doc = new DOMParser().parseFromString(src, 'text/html');
  _walk(doc.body);
  return doc.body.innerHTML;
}

function _walk(node) {
  // Snapshot first because removals mutate live HTMLCollection.
  const kids = Array.from(node.childNodes);
  for (const child of kids) {
    if (child.nodeType === 1 /* ELEMENT_NODE */) {
      const keep = _scrubAttrs(child);
      if (!keep) { child.remove(); continue; }
      _walk(child);
    } else if (child.nodeType === 3 /* TEXT_NODE */) {
      continue;  // text is always safe
    } else if (child.nodeType === 8 /* COMMENT_NODE */) {
      child.remove();
    } else {
      child.remove();
    }
  }
}


// Lightweight HTML escape used as a fallback when no DOM is
// available (SSR, Node tests). The leading ``escapeHtml`` from
// store.js was originally exported separately; the sanitizer is the
// only consumer in this module, so we inline a tiny copy.
export function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
