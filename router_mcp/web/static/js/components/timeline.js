// The two-level timeline: provider attempts, and the HTTP retries inside them.
//
// This is the component that answers "почему ответ шёл восемь секунд". Without
// the second level the answer reads as "медленная модель"; with it the same run
// reads as two 429s and five seconds of sleep the provider asked for. The
// numbers were always in the response headers — they just never had anywhere to
// go (§10.4).

import { el } from '../core/dom.js';

function ms(value) {
  if (value === null || value === undefined) return '';
  return value < 1000
    ? `${value} мс`
    : `${(value / 1000).toFixed(2).replace('.', ',')} с`;
}

const KIND_TITLES = {
  plan: 'план построен',
  attempt: 'попытка',
  response: 'ответ разобран',
  blob: 'файл ответа сохранён',
  cost: 'цена посчитана',
  log: 'записан лог',
};

function retryLine(retry, index, total) {
  const parts = [];
  if (retry.status) parts.push(`HTTP ${retry.status}`);
  if (retry.error) parts.push(retry.error);
  if (retry.retry_after_s !== null && retry.retry_after_s !== undefined) {
    parts.push(`провайдер просил ${retry.retry_after_s} с`);
  } else if (retry.wait_s) {
    parts.push(`пауза ${retry.wait_s.toFixed(1)} с`);
  }
  const succeeded = retry.status && retry.status < 400;
  const line = el('div', undefined, { className: 'tl-retry' });
  line.appendChild(el('span', ms(retry.at_ms), { className: 'tl-at num' }));
  line.appendChild(el('span', succeeded && index === total - 1
    ? 'успех'
    : `попытка ${index + 1}/${total}`, { className: 'tl-what' }));
  line.appendChild(el('span', parts.join(' · '), {
    className: succeeded ? 'muted' : 'warn',
  }));
  return line;
}

function stepLine(step) {
  const row = el('div', undefined, { className: `tl-step tl-${step.kind}` });
  row.appendChild(el('span', ms(step.at_ms), { className: 'tl-at num' }));

  const title = KIND_TITLES[step.kind] || step.kind;
  const label = step.kind === 'attempt'
    ? `${title}: ${step.provider} · ${step.model_id}`
    : title;
  row.appendChild(el('span', label, { className: 'tl-what mono' }));

  const notes = [];
  if (step.kind === 'plan' && step.planned) {
    notes.push(`${step.planned.length} ${step.planned.length === 1 ? 'попытка' : 'попытки'}`);
  }
  if (step.kind === 'cost') notes.push(step.cost_source || '');
  if (step.kind === 'log' && step.log_id) notes.push(`#${step.log_id} · канал ${step.channel}`);
  if (step.kind === 'response') {
    notes.push(step.finish_reason ? `остановка: ${step.finish_reason}` : 'остановка: обычная');
  }
  if (step.reason) notes.push(step.reason);
  if (step.error) notes.push(step.error);
  row.appendChild(el('span', notes.filter(Boolean).join(' · '), {
    className: step.error ? 'err' : 'muted',
  }));
  return row;
}

/** Render `steps` (the Run envelope's `steps[]`) into a fresh node. */
export function timeline(steps, totalMs) {
  const wrap = el('section', undefined, { className: 'timeline' });
  if (!steps || !steps.length) {
    wrap.appendChild(el('p', 'Шаги не сохранены для этой записи', { className: 'hint' }));
    return wrap;
  }
  const head = el('div', undefined, { className: 'tl-head' });
  head.appendChild(el('span', 'ШАГИ'));
  head.appendChild(el('span', `всего ${ms(totalMs)}`, { className: 'num muted' }));
  wrap.appendChild(head);

  for (const step of steps) {
    wrap.appendChild(stepLine(step));
    const retries = step.retries || [];
    // One HTTP call is not a retry, so it stays folded into its attempt; two or
    // more are the whole point of this level.
    if (retries.length > 1) {
      const nested = el('div', undefined, { className: 'tl-nested' });
      retries.forEach((retry, i) => nested.appendChild(retryLine(retry, i, retries.length)));
      wrap.appendChild(nested);
    }
  }
  return wrap;
}
