// What kind of thing a stored payload is, from what the envelope says about it.
//
// The server fills `kind` and `mime` from the blob ref's extension, so a
// picture read from the log is a picture (views.py `_media_of`). The suffix
// check here is the belt to that brace: an envelope from before that change,
// or a fixture written by hand, still gets its thumbnail.

const IMAGE_REF = /\.(png|jpe?g|webp|gif)$/i;

export function isImageAttachment(att) {
  if (!att) return false;
  if (att.kind === 'image') return true;
  if ((att.mime || '').startsWith('image/')) return true;
  return IMAGE_REF.test(att.ref || '');
}

export function isAudioAttachment(att) {
  if (!att) return false;
  return att.kind === 'audio' || (att.mime || '').startsWith('audio/');
}

// The small copy for a strip or a grid; the full-size picture is `fullUrl`.
export function thumbUrl(att) {
  if (att.thumb_url) return att.thumb_url;
  if (att.ref) return `/blobs/${att.ref}/thumb`;
  return att.download_url || att.url || '';
}

export function fullUrl(att) {
  if (att.ref) return `/blobs/${att.ref}`;
  return att.download_url || att.url || '';
}

// Every picture a run carried, request side first, each tagged with its side
// so a strip of two can say which one was the question.
export function runImages(run) {
  const out = [];
  for (const att of run.request?.attachments || []) {
    if (isImageAttachment(att)) out.push({ ...att, side: 'request' });
  }
  for (const att of run.attachments || []) {
    if (isImageAttachment(att)) out.push({ ...att, side: 'response' });
  }
  return out;
}

export const SIDE_LABEL = { request: 'запрос', response: 'ответ' };
