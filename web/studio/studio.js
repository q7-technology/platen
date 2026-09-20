/* Shared shell for the Label Studio screens. No build step, no framework —
   the pages are small enough that the DOM is the state you can see. */
'use strict';

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* not json */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  if (r.status === 204) return null;
  return r.headers.get('content-type')?.includes('json') ? r.json() : r;
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

/* lucide, 1.5px stroke, nothing else */
const ICONS = {
  play: ['M6 3l14 9-14 9z'],
  database: ['M3 5a9 3 0 0 0 18 0 9 3 0 0 0-18 0', 'M3 5v14a9 3 0 0 0 18 0V5', 'M3 12a9 3 0 0 0 18 0'],
  printer: ['M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2', 'M6 9V3h12v6', 'M6 14h12v8H6z'],
  book: ['M12 7v14', 'M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z'],
  check: ['M20 6 9 17l-5-5'],
  alert: ['m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3', 'M12 9v4', 'M12 17h.01'],
  plus: ['M5 12h14', 'M12 5v14'],
  trash: ['M3 6h18', 'M8 6V4h8v2', 'M19 6l-1 14H6L5 6'],
  plug: ['M12 22v-5', 'M9 8V2', 'M15 8V2', 'M18 8v3a6 6 0 0 1-12 0V8z'],
  image: ['M3 5h18v14H3z', 'm4 16 4.5-4.5 3.5 3.5 2.6-2.6L20 17', 'M15.5 9h.01'],
};

function icon(name, size = 16) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  const attrs = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
                  stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round',
                  'stroke-linejoin': 'round', 'aria-hidden': 'true' };
  for (const [k, v] of Object.entries(attrs)) svg.setAttribute(k, v);
  for (const d of ICONS[name] || []) {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', d);
    svg.append(p);
  }
  return svg;
}

const NAV = [
  { href: '/studio/print', label: 'Print run', icon: 'play', key: 'print' },
  { href: '/studio/data', label: 'Data sources', icon: 'database', key: 'data' },
  { href: '/studio/printers', label: 'Printers', icon: 'printer', key: 'printers' },
  { href: '/docs', label: 'API reference', icon: 'book', key: 'docs' },
];

function renderShell(current, footNote) {
  const aside = $('shell-nav');
  if (!aside) return;
  aside.replaceChildren(
    el('div', { class: 'wordmark' },
      el('img', { src: '/studio/static/q7-logo-128.png', alt: 'Q7Technology Logo',
                  width: 32, height: 28 }),
      el('span', {}, el('b', { text: 'PLATEN' }), el('small', { text: 'Label Studio' }))),
    el('nav', { 'aria-label': 'Studio' }, ...NAV.map((n) =>
      el('a', { href: n.href, 'aria-current': n.key === current ? 'page' : null },
        icon(n.icon), el('span', { text: n.label })))),
    footNote ? el('p', { class: 'foot', text: footNote }) : null,
  );
}

/* A failure the operator needs to read, in the gold that means "look here". */
function showError(box, message) {
  box.replaceChildren(el('div', { class: 'err' }, icon('alert', 18),
    el('span', { class: 'txt' }, el('code', { text: message }))));
  box.hidden = false;
}

function clearError(box) { box.replaceChildren(); box.hidden = true; }

function slug(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 64);
}
