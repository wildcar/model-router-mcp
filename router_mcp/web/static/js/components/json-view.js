// A JSON viewer that never builds markup from data.
//
// Syntax highlighting is normally done by replacing patterns inside an HTML
// string — which is exactly the hole §16.1 closes, because the values here are
// provider output and caller input. This walks the parsed value and creates one
// node per token instead, so a string containing `<script>` is a string.
//
// Collapsing uses native <details>: no state to keep, keyboard and find-in-page
// work for free. Anything past the threshold starts collapsed — highlighting a
// megabyte document hangs the tab.

import { el, clear, toast } from '../core/dom.js';

const COLLAPSE_BYTES = 256 * 1024;

function typeOf(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function scalarNode(value) {
  const kind = typeOf(value);
  const text = kind === 'string' ? JSON.stringify(value) : String(value);
  return el('span', text, { className: `json-${kind}` });
}

function pathOf(parent, key, isIndex) {
  if (!parent) return isIndex ? `[${key}]` : String(key);
  return isIndex ? `${parent}[${key}]` : `${parent}.${key}`;
}

// The path of a value is what somebody pastes into a filter or a jq expression,
// so every key is a click away from the clipboard.
function keyNode(key, path) {
  return el('span', key, {
    className: 'json-key',
    title: `${path} — щёлкните, чтобы скопировать путь`,
    onClick: (event) => {
      event.stopPropagation();
      navigator.clipboard?.writeText(path);
      toast(`Скопировано: ${path}`);
    },
  });
}

function entryNode(key, value, path, isIndex, depth, collapsed) {
  const kind = typeOf(value);
  if (kind !== 'object' && kind !== 'array') {
    const line = el('div', undefined, { className: 'json-line' });
    if (key !== null) {
      line.appendChild(keyNode(key, path));
      line.appendChild(el('span', ': ', { className: 'json-punct' }));
    }
    line.appendChild(scalarNode(value));
    return line;
  }

  const entries = kind === 'array'
    ? value.map((v, i) => [i, v, true])
    : Object.entries(value).map(([k, v]) => [k, v, false]);
  const brackets = kind === 'array' ? ['[', ']'] : ['{', '}'];

  const details = el('details', undefined, { className: 'json-node' });
  details.open = !collapsed && depth < 3;
  const summary = el('summary');
  if (key !== null) {
    summary.appendChild(keyNode(key, path));
    summary.appendChild(el('span', ': ', { className: 'json-punct' }));
  }
  summary.appendChild(el('span', brackets[0], { className: 'json-punct' }));
  summary.appendChild(el('span', ` ${entries.length} `, { className: 'json-count' }));
  summary.appendChild(el('span', brackets[1], { className: 'json-punct' }));
  details.appendChild(summary);

  const body = el('div', undefined, { className: 'json-body' });
  for (const [childKey, childValue, childIsIndex] of entries.slice(0, 2000)) {
    body.appendChild(entryNode(
      childKey, childValue, pathOf(path, childKey, childIsIndex),
      childIsIndex, depth + 1, collapsed,
    ));
  }
  if (entries.length > 2000) {
    body.appendChild(el('div', `…ещё ${entries.length - 2000}`, { className: 'muted' }));
  }
  details.appendChild(body);
  return details;
}

/** Render `value` into `container`. Returns the container. */
export function jsonView(container, value, { rootLabel = null } = {}) {
  clear(container);
  container.className = 'json-view';
  let size = 0;
  try {
    size = JSON.stringify(value)?.length ?? 0;
  } catch (_) {
    size = 0;
  }
  const collapsed = size > COLLAPSE_BYTES;
  if (collapsed) {
    container.appendChild(el(
      'div',
      `Документ большой (${Math.round(size / 1024)} КБ), узлы свёрнуты`,
      { className: 'hint' },
    ));
  }
  container.appendChild(entryNode(rootLabel, value, rootLabel || '', false, 0, collapsed));
  return container;
}

/** A block with a copy button above it — used for request/response bodies. */
export function jsonBlock(title, value) {
  const wrap = el('section', undefined, { className: 'json-block' });
  const head = el('div', undefined, { className: 'json-block-head' });
  head.appendChild(el('h4', title));
  head.appendChild(el('button', 'Копировать', {
    onClick: () => {
      navigator.clipboard?.writeText(JSON.stringify(value, null, 2));
      toast('JSON скопирован');
    },
  }));
  wrap.appendChild(head);
  wrap.appendChild(jsonView(el('div'), value));
  return wrap;
}
