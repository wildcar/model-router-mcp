// The only module that talks to the network.
//
// Components never fetch and never read the address bar; page modules do both
// and hand components plain data (§18 of docs/UI_CONCEPT_v2.md). Everything
// goes to /api/v1 and every failure arrives in the same error envelope, so
// error handling is written once here instead of once per call site.

const BASE = '/api/v1';

// Carried on every request so the log can answer "who called this".
const CHANNEL_HEADERS = { 'X-Router-Channel': 'ui' };

export class ApiError extends Error {
  constructor(envelope, status) {
    super(envelope.message || `HTTP ${status}`);
    this.code = envelope.code || 'http_error';
    this.status = status;
    this.logId = envelope.log_id ?? null;
    this.provider = envelope.provider ?? null;
    this.modelId = envelope.model_id ?? null;
    this.retriable = Boolean(envelope.retriable);
    this.retryAfterS = envelope.retry_after_s ?? null;
    this.providerStatus = envelope.provider_status ?? null;
    this.hint = envelope.hint ?? null;
  }
}

export async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: { ...CHANNEL_HEADERS } };
  if (opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  if (opts.formData !== undefined) init.body = opts.formData;
  const res = await fetch(path.startsWith('/') ? BASE + path : path, init);
  if (res.status === 204) return null;
  let payload = null;
  try { payload = await res.json(); } catch (_) { /* empty or non-JSON body */ }
  if (!res.ok) {
    throw new ApiError((payload && payload.error) || { message: res.statusText }, res.status);
  }
  return payload;
}

// Build a query string, dropping empty values so the address stays readable.
export function query(params) {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) continue;
    q.set(key, String(value));
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}
