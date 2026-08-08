// The inspector: one component, three places.
//
// It renders a `Run` — the envelope that looks the same whether the run just
// executed, was read back from the log, or is one cell of the multi-model
// table. That is the whole reason the envelope has two constructors pinned
// together by a test: without it this component would need a branch per source,
// and the branches would drift.
//
// Five tabs (§10.4): Запрос · Ответ · Шаги · JSON · Сырьё. There is no "Лог" tab
// — a link goes there instead, because a tab that duplicates another screen ages
// into a worse copy of it. «Ответ» opens first: after a send that is what the
// operator came for, and the request is the thing they already know.

import { el, clear, toast } from '../core/dom.js';
import { costCell, duration, localTime, money, tokens } from '../core/format.js';
import { jsonBlock, jsonView } from './json-view.js';
import { timeline } from './timeline.js';

function copyButton(text, what) {
  return el('button', 'Копировать', {
    className: 'small',
    onClick: () => {
      navigator.clipboard?.writeText(text);
      toast(`${what} скопировано`);
    },
  });
}

function kvTable(pairs) {
  const table = el('table', undefined, { className: 'kv' });
  const body = el('tbody');
  for (const [key, value, opts = {}] of pairs) {
    if (value === undefined) continue;
    const tr = el('tr');
    tr.appendChild(el('th', key));
    const td = el('td', undefined, { className: opts.mono === false ? '' : 'mono' });
    if (value instanceof Node) td.appendChild(value);
    else td.textContent = value === null || value === '' ? '—' : String(value);
    if (opts.copy) {
      td.appendChild(document.createTextNode(' '));
      td.appendChild(copyButton(String(value), key));
    }
    tr.appendChild(td);
    body.appendChild(tr);
  }
  table.appendChild(body);
  return table;
}

// Level 0: the line an operator reads first and, most of the time, only.
function summary(run, labels) {
  const head = el('div', undefined, { className: 'insp-summary' });
  const status = el('span', labels?.statuses?.[run.status]?.label || run.status, {
    className: run.status === 'ok' ? 'chip ok' : 'chip err',
  });
  head.appendChild(status);
  head.appendChild(el('span', duration(run.latency_ms), { className: 'num' }));
  head.appendChild(costCell(run.cost.value, run.cost.source, labels));
  // The provider's own figure, when it named one. Two numbers apart by more
  // than rounding is the signal a registry rate is stale or incomplete (§7.4).
  const provider = run.cost.provider_value;
  if (provider != null && provider > 0
      && run.cost.source !== 'provider' && run.cost.source !== 'provider_notional') {
    const apart = Math.abs((run.cost.value || 0) - provider)
      > Math.max(0.0001, 0.02 * provider);
    head.appendChild(el('span', `поставщик: ${money(provider)}${apart ? ' ≠' : ''}`, {
      className: apart ? 'err' : 'muted',
      title: 'Сумма, которую назвал сам поставщик (usage.cost)',
    }));
  }
  head.appendChild(el('span', labels?.channels?.[run.channel]?.label || run.channel || '—', {
    className: 'muted',
  }));
  if (run.log_id) {
    head.appendChild(el('a', `#${run.log_id}`, { href: `/logs?log=${run.log_id}` }));
  }

  const pair = el('div', undefined, { className: 'insp-pair mono' });
  pair.appendChild(el('span', `${run.model.provider ?? '—'} · ${run.model.model_id ?? '—'}`));
  const wrap = el('div');
  wrap.appendChild(head);
  wrap.appendChild(pair);

  // A substitution planned before the call and one that followed a failure cost
  // differently: a temporary rate limit under the "дороже" policy quietly moves
  // traffic to an expensive model.
  if (run.fallback) {
    const when = run.fallback.kind === 'after_failure' ? 'после отказа' : 'до вызова';
    wrap.appendChild(el(
      'div',
      `⇄ запрошена ${run.fallback.trigger} · подмена ${when}: ${run.fallback.reason || ''}`,
      { className: 'warn' },
    ));
  }
  if (run.ignored_params && run.ignored_params.length) {
    wrap.appendChild(el(
      'div',
      `⚠ адаптер не читает параметры: ${run.ignored_params.join(', ')}`,
      { className: 'warn' },
    ));
  }
  if (run.error) {
    wrap.appendChild(el('div', run.error.message, { className: 'err' }));
    if (run.error.hint?.env_var) {
      wrap.appendChild(el('div', `Переменная ${run.error.hint.env_var} не задана`, {
        className: 'muted mono',
      }));
    }
  }
  return wrap;
}

// The policy in the same words the «Прогон» select offers, so the entry and the
// form name one thing one way. The codes stay visible for a policy the glossary
// does not know.
function fallbackWords(policy, labels) {
  const choice = (labels?.fallback_choices || []).find(
    (c) => c.policy === policy.policy && c.scope === policy.scope);
  const words = choice ? choice.label : `${policy.policy}, область ${policy.scope}`;
  return policy.policy === 'off' ? words : `${words} · шагов ${policy.max_steps}`;
}

// The question, in the same shape as the answer. Without it the entry says what
// came back and nothing about what was asked, and «почему ответ такой» is a guess.
function requestTab(run, labels) {
  const req = run.request || {};
  const pane = el('div');

  if (req.text) {
    const head = el('div', undefined, { className: 'pane-head' });
    head.appendChild(copyButton(req.text, 'Запрос'));
    if (req.text_truncated) {
      head.appendChild(el('span', 'текст сохранён не полностью', { className: 'muted' }));
    }
    pane.appendChild(head);
    pane.appendChild(el('pre', req.text));
  }

  // A chat history is not a prompt: the roles are what explains the answer.
  if (req.messages && req.messages.length) {
    pane.appendChild(el('h4', `Сообщения ${req.messages.length}`));
    for (const message of req.messages) {
      const block = el('div', undefined, { className: 'insp-message' });
      block.appendChild(el('span', message.role || '—', { className: 'chip' }));
      const content = typeof message.content === 'string'
        ? message.content
        : JSON.stringify(message.content, null, 2);
      block.appendChild(el('pre', content ?? ''));
      if (message.content_truncated) {
        block.appendChild(el('div', 'сохранено не полностью', { className: 'muted' }));
      }
      pane.appendChild(block);
    }
    if (req.dropped_messages) {
      pane.appendChild(el('div', `ещё ${req.dropped_messages} сообщени(й) не сохранено`, {
        className: 'muted',
      }));
    }
  }

  // What was sent along with the file, and the file itself: a transcription
  // opened a week later is unreadable without the audio.
  for (const att of req.attachments || []) {
    const size = att.bytes ? `, ${Math.round(att.bytes / 1024)} КБ` : '';
    const link = el('a', `Скачать вложение (${att.mime || att.kind || 'файл'}${size})`, {
      href: att.download_url,
    });
    if ((att.mime || '').startsWith('audio/')) {
      const player = el('audio');
      player.controls = true;
      player.src = att.download_url;
      pane.appendChild(player);
    }
    if (att.download_url) pane.appendChild(el('div', undefined, { children: [link] }));
  }

  // What the caller asked for, as opposed to what ran — the pair that ran is in
  // the summary line above, and the difference is the interesting part.
  const policy = req.fallback;
  pane.appendChild(el('h4', 'Как спросили'));
  pane.appendChild(kvTable([
    ['Задача', labels?.tasks?.[run.task]?.label || run.task || '—'],
    ['Группа тарификации', labels?.modalities?.[run.modality]?.label || run.modality],
    ['Запрошенная модель', req.model_id],
    ['Уровень', req.tier],
    ['Поставщик (подсказка)', req.provider_hint],
    ['Подмена модели', policy ? fallbackWords(policy, labels) : 'выключена'],
    ['Канал', labels?.channels?.[run.channel]?.label || run.channel],
    ['Инструмент', run.tool],
    ['Клиент', run.client_id],
    ['Ключ идемпотентности', req.client_request_id,
      { copy: Boolean(req.client_request_id) }],
    ['Пакет', req.batch_id],
  ]));

  const params = req.params || {};
  pane.appendChild(el('h4', 'Параметры'));
  if (Object.keys(params).length) {
    // The ones the adapter does not read are already named in the summary.
    pane.appendChild(kvTable(Object.entries(params).map(
      ([key, value]) => [key, typeof value === 'object' && value !== null
        ? JSON.stringify(value)
        : String(value)],
    )));
  } else {
    pane.appendChild(el('p', 'по умолчанию', { className: 'muted' }));
  }
  return pane;
}

function answerTab(run) {
  const pane = el('div');
  const audio = (run.attachments || []).filter((a) => (a.mime || '').startsWith('audio/'));
  const images = (run.attachments || []).filter(
    (a) => a.kind === 'image' || (a.mime || '').startsWith('image/'));
  const others = (run.attachments || []).filter(
    (a) => !audio.includes(a) && !images.includes(a));

  if (run.text) {
    const head = el('div', undefined, { className: 'pane-head' });
    head.appendChild(copyButton(run.text, 'Ответ'));
    pane.appendChild(head);
    pane.appendChild(el('pre', run.text));
  }
  for (const image of images) {
    const img = el('img', undefined, { className: 'gen-image' });
    img.src = image.download_url;
    pane.appendChild(img);
  }
  for (const track of audio) {
    const player = el('audio');
    player.controls = true;
    player.src = track.download_url;
    pane.appendChild(player);
  }
  // A blob id is not something an operator wants; the file is.
  for (const file of others) {
    const link = el('a', 'Скачать файл ответа', { href: file.download_url });
    link.setAttribute('download', '');
    pane.appendChild(el('div', undefined, { children: [link] }));
  }
  if (!pane.childNodes.length) pane.appendChild(el('p', '(пусто)', { className: 'muted' }));
  return pane;
}

function rawTab(run, labels) {
  const pane = el('div');
  const finish = run.finish_reason;
  pane.appendChild(kvTable([
    ['Пара', `${run.model.provider ?? '—'} · ${run.model.model_id ?? '—'}`, { copy: true }],
    ['Токены', tokens(run.usage)],
    ['Время ответа', duration(run.latency_ms)],
    ['Повторов HTTP', run.attempts],
    ['Статус у провайдера', run.provider_status],
    ['id запроса у провайдера', run.provider_request_id, { copy: Boolean(run.provider_request_id) }],
    // Raw `length` reads as "the model said nothing"; spell it out.
    ['Причина остановки', finish
      ? `${labels?.finish_reasons?.[finish]?.label || finish} (${finish})`
      : null],
    ['Начат', localTime(run.created_at)],
    ['Завершён', localTime(run.finished_at)],
  ]));
  pane.appendChild(el('h4', 'usage_raw'));
  pane.appendChild(jsonView(el('div'), run.usage?.raw || {}));
  return pane;
}

function jsonTab(run) {
  const pane = el('div');
  // The request as the router received it — not the body it sent upstream. That
  // one is shown only under a debug flag, and request headers are never
  // serialised at all, because the keys live there (§16.4).
  pane.appendChild(jsonBlock('JSON (запрос)', run.request || {}));
  pane.appendChild(jsonBlock('JSON (ответ /api)', run));
  return pane;
}

/**
 * Render a Run into `container`.
 * `labels` is the glossary from /api/v1/capabilities; without it the raw codes
 * are shown, which is ugly but never wrong.
 */
export function inspector(container, run, { labels = null, activeTab = 'answer' } = {}) {
  clear(container);
  container.className = 'inspector';
  container.appendChild(summary(run, labels));

  const tabs = [
    ['request', 'Запрос', () => requestTab(run, labels)],
    ['answer', 'Ответ', () => answerTab(run)],
    ['steps', `Шаги ${(run.steps || []).length || ''}`.trim(),
      () => timeline(run.steps, run.latency_ms)],
    ['json', 'JSON', () => jsonTab(run)],
    ['raw', 'Сырьё', () => rawTab(run, labels)],
  ];

  const bar = el('nav', undefined, { className: 'tabs' });
  const pane = el('div', undefined, { className: 'tab-pane' });
  let current = activeTab;

  const select = (key) => {
    current = key;
    for (const button of bar.children) {
      button.classList.toggle('active', button.dataset.tab === key);
      button.setAttribute('aria-selected', String(button.dataset.tab === key));
    }
    clear(pane);
    pane.appendChild(tabs.find(([k]) => k === key)[2]());
  };

  for (const [key, label] of tabs) {
    const button = el('button', label, {
      className: 'tab',
      dataset: { tab: key },
      onClick: () => select(key),
    });
    button.setAttribute('role', 'tab');
    bar.appendChild(button);
  }
  bar.setAttribute('role', 'tablist');
  container.appendChild(bar);
  container.appendChild(pane);
  select(tabs.some(([k]) => k === current) ? current : 'answer');
  return container;
}
