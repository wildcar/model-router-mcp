// A DOM small enough to read, big enough to run the console's page modules.
//
// There is no browser in the test environment and no bundler in this project,
// so the page modules would otherwise only ever be checked by hand. This stub
// gives them the handful of APIs they actually use — element creation, ids,
// classes, events, fetch, localStorage, history — and records what they built.
//
// It is deliberately not jsdom: the point is to catch a mistyped element id, a
// handler bound to a node that does not exist, and a page reading a field the
// API does not send. A full DOM implementation would add a node toolchain to a
// project whose production host has none.

class ClassList {
  constructor(node) { this.node = node; this.items = new Set(); }
  add(...names) { names.forEach((n) => n && this.items.add(n)); this.sync(); }
  remove(...names) { names.forEach((n) => this.items.delete(n)); this.sync(); }
  toggle(name, force) {
    const on = force === undefined ? !this.items.has(name) : force;
    if (on) this.items.add(name); else this.items.delete(name);
    this.sync();
  }
  contains(name) { return this.items.has(name); }
  sync() { this.node._className = [...this.items].join(' '); }
}

class Node {
  constructor(tag, doc) {
    this.tagName = (tag || '').toUpperCase();
    this.ownerDocument = doc;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this._className = '';
    this._textContent = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.checked = false;
    this.options = [];
    this.files = [];
    this.classList = new ClassList(this);
  }

  // Elements only, the way a browser's `children` works: the fixture parser
  // now emits text nodes for static markup, and counting those would shift
  // every `children[i]` a page or a test relies on.
  get children() {
    return this.childNodes.filter((n) => n instanceof Node && n.tagName !== '#TEXT');
  }
  get firstChild() { return this.childNodes[0] || null; }

  // Properties a browser reflects onto the attribute of the same name. Pages
  // set them either way (`node.href = …` or `setAttribute`), and a stub that
  // kept the two apart would report a link as empty when it is not.
  get href() { return this.attributes.href ?? ''; }
  set href(value) { this.attributes.href = String(value); }
  get src() { return this.attributes.src ?? ''; }
  set src(value) { this.attributes.src = String(value); }

  get className() { return this._className; }
  set className(value) {
    this._className = value || '';
    this.classList.items = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }

  get textContent() {
    if (this.childNodes.length) {
      return this.childNodes.map((n) => n.textContent).join('');
    }
    return this._textContent;
  }
  // Assigning textContent replaces the children with a real text node, the way
  // a browser does. Storing the string beside the child list instead would hide
  // it as soon as anything was appended afterwards — which is exactly the
  // pattern `<td>text + copy button</td>` uses.
  set textContent(value) {
    this.childNodes = [];
    const text = value === null || value === undefined ? '' : String(value);
    this._textContent = text;
    if (text !== '' && this.tagName !== '#TEXT') {
      this.appendChild(this.ownerDocument.createTextNode(text));
    }
  }

  // The property the escaping rule forbids for data. Present so a page that
  // reaches for it fails loudly here instead of silently in production.
  set innerHTML(value) {
    if (value !== '') throw new Error(`innerHTML assigned ${JSON.stringify(value)}`);
    this.childNodes = [];
  }
  get innerHTML() { return ''; }

  appendChild(child) {
    child.parentNode = this;
    this.childNodes.push(child);
    if (child.id) this.ownerDocument._byId.set(child.id, child);
    return child;
  }
  removeChild(child) {
    this.childNodes = this.childNodes.filter((n) => n !== child);
    return child;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  dispatch(type, event = {}) {
    for (const handler of this.listeners[type] || []) handler({ stopPropagation() {}, ...event });
  }
  scrollIntoView() {}

  get id() { return this._id; }
  set id(value) {
    this._id = value;
    if (value) this.ownerDocument._byId.set(value, this);
  }

  // Enough of a selector engine for `#table tbody` and `#table thead th`.
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const parts = selector.trim().split(/\s+/);
    let scope = [this];
    for (const part of parts) {
      const next = [];
      for (const node of scope) next.push(...matchIn(node, part));
      scope = next;
    }
    return scope;
  }
  get textDump() { return this.textContent; }
}

function matches(node, part) {
  if (part.startsWith('#')) return node.id === part.slice(1);
  if (part.startsWith('.')) return node.classList.contains(part.slice(1));
  if (part.startsWith('[')) {
    const name = part.slice(1, -1).split('=')[0].replace(/^data-/, '');
    return name in node.dataset;
  }
  return node.tagName === part.toUpperCase();
}

function matchIn(root, part) {
  const found = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (matches(child, part)) found.push(child);
      walk(child);
    }
  };
  walk(root);
  return found;
}

export function makeDocument() {
  const doc = {
    _byId: new Map(),
    createElement(tag) { return new Node(tag, doc); },
    createTextNode(text) {
      const node = new Node('#text', doc);
      node._textContent = String(text);
      return node;
    },
    getElementById(id) { return doc._byId.get(id) || null; },
    querySelector(selector) { return doc.body.querySelector(selector); },
    querySelectorAll(selector) { return doc.body.querySelectorAll(selector); },
  };
  doc.body = new Node('body', doc);
  doc.documentElement = doc.body;
  return doc;
}

/** Build a document from a tiny subset of HTML: tags, ids, classes, values,
 * and the text between tags — a table header spelled out in markup is part of
 * what the page shows, and a stub that drops it cannot check column names. */
export function parseFixture(html, doc) {
  const stack = [doc.body];
  const tagRe = /<(\/?)([a-zA-Z0-9]+)([^>]*)>/g;
  let match;
  let last = 0;
  while ((match = tagRe.exec(html)) !== null) {
    const between = html.slice(last, match.index).replace(/\s+/g, ' ').trim();
    if (between) stack[stack.length - 1].appendChild(doc.createTextNode(between));
    last = tagRe.lastIndex;
    const [, closing, tag, rawAttrs] = match;
    const lower = tag.toLowerCase();
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const node = doc.createElement(lower);
    for (const attr of rawAttrs.matchAll(/([a-zA-Z-]+)(?:="([^"]*)")?/g)) {
      const [, name, value = ''] = attr;
      if (name === 'id') node.id = value;
      else if (name === 'class') node.className = value;
      else if (name === 'value') node.value = value;
      else if (name === 'hidden') node.hidden = true;
      else node.setAttribute(name, value);
    }
    stack[stack.length - 1].appendChild(node);
    const selfClosing = ['input', 'br', 'img', 'meta', 'link'].includes(lower)
      || rawAttrs.trim().endsWith('/');
    if (!selfClosing) stack.push(node);
  }
  return doc;
}

/** Install the globals a page module expects. Returns the recorded calls. */
export function installGlobals(doc, routes) {
  const calls = [];
  const store = new Map();
  globalThis.document = doc;
  globalThis.Node = Node;
  globalThis.window = {
    location: { search: '', pathname: '/', href: '/' },
    history: { replaceState() {}, pushState() {} },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, v),
    },
    addEventListener() {},
    confirm: () => true,
    setTimeout: (fn) => fn(),
  };
  globalThis.localStorage = globalThis.window.localStorage;
  // Node 24 defines `navigator` as a getter, so it has to be replaced rather
  // than assigned.
  Object.defineProperty(globalThis, 'navigator', {
    value: { clipboard: { writeText() {} } },
    configurable: true,
    writable: true,
  });
  globalThis.FormData = class { append() {} };
  globalThis.setTimeout = (fn) => { fn(); return 0; };
  globalThis.clearTimeout = () => {};
  globalThis.fetch = async (url, init = {}) => {
    // The body is recorded too: what a page sends is as much a contract as
    // which endpoint it sends to (an idempotency key that never arrives is an
    // ordinary-looking POST).
    calls.push({ url, method: init.method || 'GET', body: init.body });
    const path = String(url).split('?')[0];
    const route = routes[`${init.method || 'GET'} ${path}`] ?? routes[path];
    if (route === undefined) {
      throw new Error(`no fixture for ${init.method || 'GET'} ${url}`);
    }
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => route,
    };
  };
  return calls;
}
