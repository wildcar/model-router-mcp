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
