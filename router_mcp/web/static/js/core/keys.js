// Keyboard shortcuts.
//
// One hard rule (§8.12 of docs/UI_CONCEPT_v2.md): a single keystroke never
// spends money. Sending a request is Ctrl+Enter or the button — never `r`.

const handlers = [];

export function bind(spec, handler) {
  handlers.push({ ...spec, handler });
}

function isTyping(target) {
  const tag = (target.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || target.isContentEditable;
}

window.addEventListener('keydown', (event) => {
  for (const h of handlers) {
    if (h.key !== event.key) continue;
    if (Boolean(h.ctrl) !== (event.ctrlKey || event.metaKey)) continue;
    // Bare keys are shortcuts only outside text fields, where they are letters.
    if (!h.ctrl && isTyping(event.target)) continue;
    event.preventDefault();
    h.handler(event);
    return;
  }
});
