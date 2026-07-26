// Minimal fetch helper + toast used by all pages.
async function api(url, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

// Prices in the registry are stored as USD per single token. Humans compare them
// far more easily as USD per 1,000,000 tokens, with a fixed number of decimals so
// the columns line up. Returns a string with uniform width (3 decimals).
function pricePerMillion(perToken) {
  return ((Number(perToken) || 0) * 1e6).toFixed(3);
}

// --- safe DOM building -------------------------------------------------
// Everything the console renders passes through untrusted input at some point:
// external_user_id is free-form (a stats key, never a trust boundary),
// request_text is the caller's prompt, fallback_reason quotes a provider's
// error. Building rows by interpolating those into innerHTML executes whatever
// the caller stored. These helpers set text as text; no page may assemble
// markup from data.
function el(tag, text, opts = {}) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (opts.className) node.className = opts.className;
  if (opts.title) node.title = String(opts.title);
  return node;
}

// A table row from a list of cell descriptors. A cell is a primitive (rendered
// as text), or {text, className, title, node} for anything richer.
function row(cells) {
  const tr = document.createElement('tr');
  for (const cell of cells) {
    if (cell instanceof Node) { tr.appendChild(cell); continue; }
    const spec = (cell === null || typeof cell !== 'object') ? { text: cell } : cell;
    const td = el('td', spec.node === undefined ? spec.text : undefined, spec);
    if (spec.node !== undefined) td.appendChild(spec.node);
    tr.appendChild(td);
  }
  return tr;
}

function toast(message, isError = false) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = isError ? 'show error' : 'show';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = ''; }, 3000);
}
