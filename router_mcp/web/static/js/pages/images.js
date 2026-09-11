// «Картинки»: the log's pictures as a wall, newest first.
//
// The journal answers «что спросили и что ответили» one row at a time; this
// screen answers «что вообще рисовали за последние дни» at a glance. Each
// card is one picture with the entry it belongs to: the number is a link into
// the journal's entry window, the text is the words that were asked.

import { api, ApiError, query } from '../core/api.js';
import { el, clear, toast } from '../core/dom.js';
import { localTime, timeZoneLabel } from '../core/format.js';
import { SIDE_LABEL } from '../core/media.js';

const $ = (id) => document.getElementById(id);
const PAGE_SIZE = 40;

let cursor = null;
let shownPictures = 0;
let shownEntries = 0;
let loading = false;
let exhausted = false;

function card(entry, image) {
  const figure = el('figure', undefined, { className: 'shot' });

  // The picture opens full size in its own tab; a gallery that navigates away
  // on a click loses the operator's place in it.
  const link = el('a', undefined, { href: image.url, className: 'shot-pic' });
  link.setAttribute('target', '_blank');
  link.setAttribute('rel', 'noopener');
  const img = el('img');
  img.src = image.thumb_url || image.url;
  img.setAttribute('loading', 'lazy');
  img.setAttribute('alt', `${SIDE_LABEL[image.side] || image.side}: ${entry.text || ''}`.trim());
  link.appendChild(img);
  figure.appendChild(link);

  const caption = el('figcaption');
  const head = el('div', undefined, { className: 'shot-head' });
  head.appendChild(el('a', `#${entry.log_id}`, {
    href: `/logs?log=${entry.log_id}`,
    className: 'num',
    title: 'Открыть запись в журнале',
  }));
  // Which side of the exchange the picture is from, and what it was sent as
  // when the caller named a role — two photos of one run otherwise read the same.
  const side = SIDE_LABEL[image.side] || image.side;
  head.appendChild(el('span', image.role ? `${side} · ${image.role}` : side, {
    className: `chip ${image.side === 'response' ? 'ok' : 'muted'}`,
  }));
  head.appendChild(el('span', localTime(entry.created_at), {
    className: 'muted', title: entry.created_at,
  }));
  caption.appendChild(head);

  const who = [entry.external_user_id, entry.model_id].filter(Boolean).join(' · ');
  caption.appendChild(el('div', who, { className: 'shot-meta mono muted', title: who }));
  if (entry.status && entry.status !== 'ok') {
    caption.appendChild(el('div', entry.status, { className: 'err' }));
  }
  caption.appendChild(el('div', entry.text || '(без текста)', {
    className: entry.text ? 'shot-text' : 'shot-text muted',
    title: entry.text || '',
  }));
  figure.appendChild(caption);
  return figure;
}

async function loadMore() {
  if (loading || exhausted) return;
  loading = true;
  try {
    const answer = await api(`/logs/images${query({ cursor, limit: PAGE_SIZE })}`);
    const host = $('gallery');
    for (const entry of answer.items) {
      shownEntries += 1;
      for (const image of entry.images) {
        host.appendChild(card(entry, image));
        shownPictures += 1;
      }
    }
    cursor = answer.next_cursor;
    exhausted = cursor === null;
    $('more').hidden = exhausted;
    $('empty').hidden = shownPictures > 0;
    $('count').textContent = shownPictures
      ? `показано ${shownPictures} картинок из ${shownEntries} записей`
        + (exhausted ? ', это всё' : '')
      : '';
    // A tall screen may still show the sentinel after one page: keep going
    // until the page is either full or the log is out of pictures.
    if (!exhausted && observer && sentinelVisible) queueMicrotask(loadMore);
  } catch (e) {
    toast(e instanceof ApiError ? e.message : String(e), true);
  } finally {
    loading = false;
  }
}

// Loading on scroll: the sentinel sits under the grid, and the next page is
// asked for as it comes into view. The button below it does the same by hand.
let observer = null;
let sentinelVisible = false;
if (typeof IntersectionObserver !== 'undefined') {
  observer = new IntersectionObserver((entries) => {
    sentinelVisible = entries.some((entry) => entry.isIntersecting);
    if (sentinelVisible) loadMore();
  }, { rootMargin: '600px 0px' });
  observer.observe($('sentinel'));
}
$('more').addEventListener('click', () => loadMore());
$('tz').textContent = timeZoneLabel();

loadMore();
