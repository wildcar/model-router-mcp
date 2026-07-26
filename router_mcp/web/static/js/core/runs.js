// Starting a run and watching it, without holding a connection open.
//
// The server accepts a run and answers with an id; this polls that id once a
// second until it finishes (§10.5). Polling rather than SSE is a deliberate
// choice recorded in the concept: it survives a closed tab, it needs nothing
// from nginx, and it cannot collide with the streamable-HTTP session manager
// that /mcp runs in the same process.

import { api } from './api.js';

export const POLL_MS = 1000;

/** A key that makes re-sending the same run harmless. */
export function idempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `k-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function isDone(state) {
  return state && (state.status === 'ok' || state.status === 'error'
    || state.status === 'cancelled');
}

/** Start a run. Returns the first state, before anything has happened. */
export function start(body) {
  return api('/runs', { method: 'POST', body });
}

/**
 * Poll until the run finishes. `onUpdate` is called with every state, the
 * first one included, and the promise resolves with the final state.
 *
 * A failed poll does not end the watch: a single dropped request during a
 * ten-minute run must not orphan a run that is still going perfectly well.
 * Two failures in a row do end it, because by then something is actually wrong.
 */
export function watch(runId, onUpdate, { intervalMs = POLL_MS } = {}) {
  let timer = null;
  let stopped = false;
  let misses = 0;

  const promise = new Promise((resolve, reject) => {
    const tick = async () => {
      if (stopped) return;
      try {
        const state = await api(`/runs/${encodeURIComponent(runId)}`);
        misses = 0;
        onUpdate(state);
        if (isDone(state)) { stopped = true; resolve(state); return; }
      } catch (error) {
        misses += 1;
        if (misses > 1) { stopped = true; reject(error); return; }
      }
      timer = setTimeout(tick, intervalMs);
    };
    tick();
  });

  return {
    promise,
    stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
    },
  };
}

/** Ask the server to stop a run. The row is closed as `cancelled` either way. */
export function cancel(runId) {
  return api(`/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
}

/** Upload a file and get the ref a JSON run body can carry. */
export function upload(file) {
  const form = new FormData();
  form.append('file', file);
  return api('/blobs', { method: 'POST', formData: form });
}
