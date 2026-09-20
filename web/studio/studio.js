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
  layers: ['m12 2 9 5-9 5-9-5z', 'm3 12 9 5 9-5', 'm3 17 9 5 9-5'],
  text: ['M4 6V4h16v2', 'M12 4v16', 'M9 20h6'],
  barcode: ['M3 4v16', 'M6 4v16', 'M10 4v10', 'M14 4v16', 'M18 4v16', 'M21 4v16'],
  qr: ['M3 3h7v7H3z', 'M14 3h7v7h-7z', 'M3 14h7v7H3z', 'M14 14h3v3h-3z', 'M19 19h2v2h-2z'],
  line: ['M3 12h18'],
  square: ['M3 3h18v18H3z'],
  eye: ['M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7', 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6'],
  eyeoff: ['M10.7 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a18 18 0 0 1-2.6 3.5', 'M6.6 6.6A18 18 0 0 0 2 12s3.5 7 10 7a10 10 0 0 0 4-.8', 'M3 3l18 18'],
  up: ['m18 15-6-6-6 6'],
  down: ['m6 9 6 6 6-6'],
  list: ['M8 6h13', 'M8 12h13', 'M8 18h13', 'M3 6h.01', 'M3 12h.01', 'M3 18h.01'],
  redo: ['M21 12a9 9 0 1 1-2.6-6.4', 'M21 3v6h-6'],
  stop: ['M6 6h12v12H6z'],
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
  { href: '/studio/templates', label: 'Templates', icon: 'layers', key: 'templates' },
  { href: '/studio/data', label: 'Data sources', icon: 'database', key: 'data' },
  { href: '/studio/printers', label: 'Printers', icon: 'printer', key: 'printers' },
  { href: '/studio/jobs', label: 'Job history', icon: 'list', key: 'jobs' },
  { href: '/docs', label: 'API reference', icon: 'book', key: 'docs' },
];

function renderShell(current, footNote) {
  const aside = $('shell-nav');
  if (!aside) return;
  // replaceChildren stringifies a null, so the optional foot note is filtered
  // out rather than passed through as one
  aside.replaceChildren(...[
    el('div', { class: 'wordmark' },
      el('img', { src: '/studio/static/q7-logo-128.png', alt: 'Q7Technology Logo',
                  width: 32, height: 28 }),
      el('span', {}, el('b', { text: 'PLATEN' }), el('small', { text: 'Label Studio' }))),
    el('nav', { 'aria-label': 'Studio' }, ...NAV.map((n) =>
      el('a', { href: n.href, 'aria-current': n.key === current ? 'page' : null },
        icon(n.icon), el('span', { text: n.label })))),
    footNote ? el('p', { class: 'foot', text: footNote }) : null,
  ].filter(Boolean));
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
