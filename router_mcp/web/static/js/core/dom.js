// Safe DOM construction. The one place the console turns data into nodes.
//
// Everything the console renders passes through untrusted input at some point:
// `external_user_id` is free-form (a stats key, never a trust boundary),
// `request_text` is the caller's prompt, `fallback_reason` quotes a provider's
// error, model names come from a vendor catalog. Building rows by interpolating
// those into innerHTML executes whatever the caller stored — and the console
// sits behind Basic auth the browser replays on every request, so a stored
// payload would run with the operator's credentials.
//
// The rule (§16.1 of docs/UI_CONCEPT_v2.md): text is set as text, structure is
// built from nodes, and no module assembles markup from data. `tests/
// test_escaping.py` fails the build if innerHTML is ever assigned anything but
// an empty literal.

export function el(tag, text, opts = {}) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (opts.className) node.className = opts.className;
  if (opts.title) node.title = String(opts.title);
  if (opts.id) node.id = opts.id;
  if (opts.href) node.href = opts.href;
  if (opts.dataset) Object.assign(node.dataset, opts.dataset);
  if (opts.onClick) node.addEventListener('click', opts.onClick);
  for (const child of opts.children || []) node.appendChild(child);
  return node;
}

// A table row from a list of cell descriptors. A cell is a primitive (rendered
// as text), a Node (used as the cell itself), or {text, className, title, node}.
export function row(cells) {
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

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function fill(node, children) {
  clear(node);
  for (const child of children) node.appendChild(child);
}

export function toast(message, isError = false) {
  let node = document.getElementById('toast');
  if (!node) {
    node = document.createElement('div');
    node.id = 'toast';
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.className = isError ? 'show error' : 'show';
  clearTimeout(node._t);
  node._t = setTimeout(() => { node.className = ''; }, 3000);
}
