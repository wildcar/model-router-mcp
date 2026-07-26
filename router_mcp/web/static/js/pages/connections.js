// «Подключения»: providers, their live state, and the catalog sync.
//
// Moved off the old registry screen when the navigation split in two. The one
// behavioural rule kept from §12.1: a provider's state is shown by name of its
// environment variable and never by its value, and the sync report stays on
// screen until the next action instead of vanishing into a toast.

import { api, ApiError } from '../core/api.js';
import { el, row, clear, toast } from '../core/dom.js';

const $ = (id) => document.getElementById(id);
let caps = null;

// enabled × configured is four states, and they need four different answers
// from the operator, so they get four different words.
function stateCell(provider) {
  if (!provider.enabled) {
    return el('td', 'выключен вручную', { className: 'muted' });
  }
  if (!provider.configured) {
    return el('td', 'включён, но не настроен', {
      className: 'err',
      title: 'Диспетчер откажет: у адаптера нет ключа или бинарника',
    });
  }
  return el('td', 'рабочий', {
    className: 'ok',
    // `configured` checks that a key is present, not that it still works. A
    // revoked key looks exactly like a good one here until wave 2 adds the probe.
    title: 'Ключ на месте. Живая проба ключа появится во второй волне',
  });
}

async function load() {
  caps = await api('/capabilities');
  const body = document.querySelector('#providers tbody');
  clear(body);
  for (const provider of caps.providers) {
    const tr = row([
      { text: provider.id, className: 'mono' },
      provider.modalities.join(', ') || '—',
    ]);

    const toggleCell = el('td');
    const toggle = el('input');
    toggle.type = 'checkbox';
    toggle.checked = provider.enabled;
    toggle.addEventListener('change', async () => {
      try {
        await api(`/registry/providers/${provider.id}`,
          { method: 'PATCH', body: { enabled: toggle.checked } });
        toast(toggle.checked ? `${provider.id} включён` : `${provider.id} выключен`);
        load();
      } catch (error) {
        toggle.checked = !toggle.checked;
        toast(error.message, true);
      }
    });
    toggleCell.appendChild(toggle);
    tr.appendChild(toggleCell);

    tr.appendChild(stateCell(provider));
    tr.appendChild(el('td', provider.env_var || '—', { className: 'mono muted' }));

    const priceCell = el('td');
    if (provider.pricing_url) {
      const link = el('a', 'прайс', { href: provider.pricing_url });
      link.target = '_blank';
      link.rel = 'noreferrer';
      priceCell.appendChild(link);
    } else {
      priceCell.textContent = '—';
    }
    tr.appendChild(priceCell);
    body.appendChild(tr);
  }
}

// The report is the answer to "что изменилось", so it stays on the page until
// the next action rather than flashing past in a toast.
function renderReports(reports) {
  const host = $('reports');
  clear(host);
  for (const report of reports) {
    const block = el('div', undefined, { className: 'sync-report' });
    block.appendChild(el('h4', report.provider_id, { className: 'mono' }));
    if (report.skipped) {
      block.appendChild(el('p', `пропущен: ${report.skipped}`, { className: 'muted' }));
    } else {
      block.appendChild(el('p',
        `видно ${report.seen} · добавлено ${report.added} · обновлено ${report.updated} `
        + `· снято ${report.deprecated}`));
      if (report.deprecated) {
        block.appendChild(el('a', 'снятые модели →', { href: '/models?source=catalog' }));
      }
    }
    host.appendChild(block);
  }
}

$('sync').addEventListener('click', async () => {
  const button = $('sync');
  button.disabled = true;
  try {
    const answer = await api('/registry/refresh', { method: 'POST', body: {} });
    renderReports(answer.providers);
    load();
  } catch (error) {
    toast(error instanceof ApiError ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
});

load().catch((error) => toast(`Не удалось загрузить поставщиков: ${error.message}`, true));
