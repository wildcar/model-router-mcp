// Screen state that belongs in the address bar.
//
// Filters, search, page, sorting, the selected row, the active tab and `?log=`
// live in the URL: a screen an operator is looking at must be a link they can
// send to a colleague, and a reload must not lose it. What does *not* live
// there: the live tail, collapsed nodes, the prompt draft, the theme (§18).

export function readParams(defaults = {}) {
  const params = new URLSearchParams(window.location.search);
  const out = { ...defaults };
  for (const [key, value] of params.entries()) out[key] = value;
  return out;
}

export function writeParams(values, { replace = true } = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === null || value === '' || value === false) continue;
    params.set(key, String(value));
  }
  const search = params.toString();
  const url = search ? `${window.location.pathname}?${search}` : window.location.pathname;
  if (replace) window.history.replaceState(null, '', url);
  else window.history.pushState(null, '', url);
}

// Local, per-screen scratch state: prompt drafts and parameter presets. Kept
// out of the URL on purpose — a half-typed prompt is not a shareable location.
export function loadLocal(key, fallback = null) {
  try {
    const raw = window.localStorage.getItem(`router:${key}`);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (_) {
    return fallback;
  }
}

export function saveLocal(key, value) {
  try {
    window.localStorage.setItem(`router:${key}`, JSON.stringify(value));
  } catch (_) {
    /* private mode or a full quota must not break the page */
  }
}
