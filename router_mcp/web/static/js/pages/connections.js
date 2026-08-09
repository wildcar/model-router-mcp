// «Подключения»: providers on one tab, MCP on the other.
//
// Two rules from §12. A provider's state is four things — enabled × configured
// — and it gets four different words, plus a live probe, because `configured`
// only says a key exists and a revoked key looks identical to a good one. And
// the sync report stays on screen until the next action rather than flashing
// past in a toast: it is the answer to "что изменилось".

import { api, ApiError } from '../core/api.js';
import { el, row, clear, fill, toast } from '../core/dom.js';
import { readParams, writeParams } from '../core/state.js';
import { jsonView } from '../components/json-view.js';

const $ = (id) => document.getElementById(id);
let caps = null;
let summary = null;

const PROBE_WORDS = {
  ok: ['проверен', 'ok'],
  unauthorized: ['ключ отозван', 'err'],
  unavailable: ['поставщик не ответил', 'warn'],
  not_configured: ['ключа нет', 'err'],
  no_probe: ['проверить нечем', 'muted'],
};

function stateCell(provider) {
  if (!provider.enabled) return el('td', 'выключен вручную', { className: 'muted' });
  if (!provider.configured) {
    return el('td', 'включён, но не настроен', {
      className: 'err',
      title: 'Диспетчер откажет: у адаптера нет ключа или бинарника',
    });
  }
  // Key present, and still unable to do what it was added for. Its own word,
  // because «рабочий» here is how an operator finds out only from the first
  // failed request that files never had a way to reach the provider.
  if (provider.warnings?.length) {
    return el('td', 'настроен наполовину', {
      className: 'warn',
      title: provider.warnings.join('\n'),
    });
  }
  return el('td', 'рабочий', { className: 'ok' });
}

function probeCell(providerId) {
  const cell = el('td');
  const probe = (summary?.providers || []).find((p) => p.id === providerId)?.probe;
  if (probe) {
    const [word, cls] = PROBE_WORDS[probe.status] || [probe.status, ''];
    const age = probe.age_seconds < 60
      ? 'только что'
      : `${Math.round(probe.age_seconds / 60)} мин назад`;
    cell.appendChild(el('span', `${word}, ${age}`, { className: cls, title: probe.detail }));
  } else {
    cell.appendChild(el('span', 'не проверялся', { className: 'muted' }));
  }
  cell.appendChild(document.createTextNode(' '));
  cell.appendChild(el('button', 'Проверить', {
    className: 'small',
    onClick: () => probeOne(providerId),
  }));
  return cell;
}

async function probeOne(providerId) {
  try {
    const result = await api(`/providers/${providerId}/probe?force=true`, { method: 'POST' });
    toast(`${providerId}: ${result.detail}`, result.status !== 'ok');
    await load();
  } catch (error) {
    toast(error.message, true);
  }
}

async function load() {
  caps = await api('/capabilities');
  summary = await api('/health/summary');
  const modelsBy = Object.fromEntries(
    summary.providers.map((p) => [p.id, p.models]));
  const body = document.querySelector('#providers tbody');
  clear(body);
  for (const provider of caps.providers) {
    const tr = row([
      { text: provider.id, className: 'mono' },
      (provider.tasks || provider.modalities).join(', ') || '—',
      { text: modelsBy[provider.id] ?? 0, className: 'num' },
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
    tr.appendChild(probeCell(provider.id));
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
        block.appendChild(el('a', 'снятые модели →', { href: '/models?state=retired' }));
      }
    }
    host.appendChild(block);
  }
}

// --- MCP tab ------------------------------------------------------------
async function loadMcp() {
  const mcp = await api('/mcp/summary');
  $('mcp-endpoint').textContent = `${window.location.origin}${mcp.endpoint}`;
  $('mcp-auth').textContent = mcp.auth_required
    ? 'требуется Bearer-токен · режим без сохранения состояния'
    : 'без токена · режим без сохранения состояния';

  const body = document.querySelector('#tools tbody');
  clear(body);
  for (const tool of mcp.tools) {
    const tr = row([
      { text: tool.name, className: 'mono' },
      { text: tool.calls, className: 'num' },
      { text: tool.errors, className: tool.errors ? 'num err' : 'num' },
      { text: tool.p50_ms === null ? '—' : `${tool.p50_ms} мс`, className: 'num' },
    ]);
    const schemaCell = el('td');
    const details = el('details');
    details.appendChild(el('summary', 'схема'));
    // Showing the schema next to the matching /api handle is the point of the
    // tab: a divergence between the two is visible at a glance.
    details.appendChild(jsonView(el('div'), tool.input_schema));
    if (tool.description) {
      details.appendChild(el('p', tool.description, { className: 'hint' }));
    }
    schemaCell.appendChild(details);
    tr.appendChild(schemaCell);
    body.appendChild(tr);
  }

  const retired = $('retired-tools');
  clear(retired);
  if (mcp.retired_tools_called.length) {
    retired.appendChild(el('p',
      'В журнале есть вызовы инструментов, которых в этой сборке нет — значит, '
      + 'какой-то клиент всё ещё просит переименованное или удалённое:',
      { className: 'hint warn' }));
    for (const item of mcp.retired_tools_called) {
      retired.appendChild(el('div', `${item.tool}: ${item.calls} вызовов`,
        { className: 'mono muted' }));
    }
  }

  fill(document.querySelector('#clients tbody'), mcp.clients.map((client) => row([
    { text: client.client, className: 'mono' },
    { text: client.calls, className: 'num' },
    { text: client.errors, className: client.errors ? 'num err' : 'num' },
    client.last_call_at,
  ])));

  const snippet = {
    mcpServers: {
      'model-router': {
        url: `${window.location.origin}/mcp`,
        ...(mcp.auth_required
          ? { headers: { Authorization: 'Bearer <токен>' } }
          : {}),
      },
    },
  };
  $('mcp-snippet').textContent = JSON.stringify(snippet, null, 2);
}

function showTab(name) {
  const isMcp = name === 'mcp';
  $('pane-mcp').hidden = !isMcp;
  $('pane-providers').hidden = isMcp;
  $('tab-mcp').classList.toggle('active', isMcp);
  $('tab-providers').classList.toggle('active', !isMcp);
  writeParams({ ...readParams(), tab: isMcp ? 'mcp' : undefined });
  if (isMcp) loadMcp().catch((e) => toast(`Ошибка: ${e.message}`, true));
}

$('tab-providers').addEventListener('click', () => showTab('providers'));
$('tab-mcp').addEventListener('click', () => showTab('mcp'));

$('sync').addEventListener('click', async () => {
  const button = $('sync');
  button.disabled = true;
  try {
    const answer = await api('/registry/refresh', { method: 'POST', body: {} });
    renderReports(answer.providers);
    await load();
  } catch (error) {
    toast(error instanceof ApiError ? error.message : String(error), true);
  } finally {
    button.disabled = false;
  }
});

$('probe-all').addEventListener('click', async () => {
  const button = $('probe-all');
  button.disabled = true;
  try {
    for (const provider of caps.providers) {
      await api(`/providers/${provider.id}/probe?force=true`, { method: 'POST' });
    }
    await load();
    toast('Ключи проверены');
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$('mcp-probe').addEventListener('click', async () => {
  try {
    const result = await api('/mcp/probe', { method: 'POST' });
    $('mcp-probe-result').textContent =
      `${result.status}, ${result.latency_ms} мс, ${result.detail}`;
  } catch (error) {
    $('mcp-probe-result').textContent = error.message;
  }
});

$('copy-snippet').addEventListener('click', () => {
  navigator.clipboard?.writeText($('mcp-snippet').textContent);
  toast('Фрагмент скопирован');
});

load()
  .then(() => { if (readParams().tab === 'mcp') showTab('mcp'); })
  .catch((error) => toast(`Не удалось загрузить поставщиков: ${error.message}`, true));
