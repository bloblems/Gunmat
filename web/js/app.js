// One-screen mat maker: open a STEP file, get an exploded blueprint on a mat.

import { parse as parseFont } from './vendor/opentype.js';

const $ = (s) => document.querySelector(s);
const NS = 'http://www.w3.org/2000/svg';
const MM = 25.4;
const r2 = (v) => Math.round(v * 100) / 100;

const SIZES = [
  ['31.5x15.5', '31.5 × 15.5 in desk mat'],
  ['36x18', '36 × 18 in desk mat'],
  ['36x12', '36 × 12 in mat'],
  ['24x14', '24 × 14 in'],
  ['17x11', '17 × 11 in mat'],
];
const HOME = { az: 30, el: 18 };

const DEFAULTS = () => ({
  view: { ...HOME, flip: false },
  spread: 1,
  hidden: [],
  pinned: {},
  names: {},
  labels: {},
  co: {},
  text: { title: '', subtitle: '', story: '', specs: '', tagline: '' },
  fonts: { title: null, text: null },
  textScale: 1,
  numScale: 1,
  images: [],
  mat: { size: '31.5x15.5', bg: '#0b0b0b', fg: '#ffffff' },
  show: { numbers: true, lines: true, list: false },
  asm: { on: false, corner: 'br', w: 720 }, // the whole thing, put together; corner of the right column, width in px
  logo: { corner: 'tr', h: 150 }, // logos in a row in this corner of the right column, height in px
  placementRegions: {}, // normalized sheet regions for explicitly placed groups
  layoutSeed: null, // previous automatic layout, used as a soft starting point
  groupSpread: {}, // sub-assembly (by its main part) -> its own spread
  sizes: {}, // text sizes in pt, per kind of text (unset = automatic)
  grid: { on: false, size: 0.5 }, // inches; logos snap to it
  area: null, // [x, y, w, h] mm: where the parts go (null = automatic)
});

let S = DEFAULTS(); // saved per STEP file
let model = null; // server model info
let drawing = null; // last drawing from the server
let L = null; // computed sheet layout
let selImage = null; // selected logo id

// ---------------------------------------------------------------- helpers
async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail || msg;
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}

const status = (msg) => ($('#status').textContent = msg || '');
function busy(msg) {
  $('#busy-msg').textContent = msg;
  $('#busy').hidden = !msg;
}

// ---------------------------------------------------------------- fonts
let FONTS = [];
const fontById = new Map();
const faceLoads = new Map();
const otFonts = new Map(); // id -> opentype font (for outlines on export)

async function loadFontList() {
  FONTS = await api('GET', '/api/fonts');
  for (const f of FONTS) fontById.set(f.id, f);
  $('#font-list').innerHTML = FONTS.map((f) => `<option value="${f.full.replace(/"/g, '&quot;')}">`).join('');
}

function findFont(full) {
  const q = String(full || '').trim().toLowerCase();
  return FONTS.find((f) => f.full.toLowerCase() === q) || null;
}

function defaultFonts() {
  const by = (fam, style) => FONTS.find((f) => f.family === fam && f.style === style);
  const title = by('Bebas Neue Pro', 'Bold') || by('Bebas Neue', 'Regular') || by('Bebas Neue', 'Bold') || by('DIN Condensed', 'Bold') || by('Impact', 'Regular') || FONTS[0];
  const text = by('Bebas Neue Pro', 'Regular') || by('Bebas Neue', 'Regular') || by('Helvetica Neue', 'Regular') || by('Arial', 'Regular') || FONTS[0];
  return { title: title?.id || null, text: text?.id || null };
}

const boldOf = (id) => fontById.get(id)?.bold || id;
const css = (id) => (id ? `"F_${id}", "Helvetica Neue", Arial, sans-serif` : '"Helvetica Neue", Arial, sans-serif');

// Load the exact font face into the page; re-draw when it arrives.
function useFace(id) {
  if (!id || faceLoads.has(id)) return;
  const ff = new FontFace(`F_${id}`, `url(/api/fontfile/${id})`);
  document.fonts.add(ff);
  faceLoads.set(id, ff.load().then(() => render(), () => {}));
}

function faces() {
  const t = S.fonts.title;
  const x = S.fonts.text;
  return { title: t, text: x, bold: boldOf(x) };
}

const ctx = document.createElement('canvas').getContext('2d');
function measure(str, fid, size) {
  ctx.font = `100px ${css(fid)}`;
  return (ctx.measureText(str).width / 100) * size;
}
function wrap(str, width, fid, size) {
  const out = [];
  for (const para of String(str || '').split('\n')) {
    const words = para.split(/\s+/).filter(Boolean);
    if (!words.length) {
      out.push('');
      continue;
    }
    let line = words[0];
    for (const w of words.slice(1)) {
      if (measure(`${line} ${w}`, fid, size) <= width) line += ` ${w}`;
      else {
        out.push(line);
        line = w;
      }
    }
    out.push(line);
  }
  return out;
}

// Text as live <text> on screen, as outlines in the export.
let OUTLINE = false;
let LIVE = false; // export with live text for Figma
function weightOf(style) {
  const st = String(style).toLowerCase().replace(/[\s-]/g, '');
  for (const [k, w] of [['extralight', 200], ['ultralight', 200], ['semibold', 600], ['demibold', 600], ['extrabold', 800], ['ultrabold', 800], ['thin', 100], ['hairline', 100], ['light', 300], ['book', 400], ['medium', 500], ['middle', 500], ['bold', 700], ['black', 900], ['heavy', 900]]) if (st.includes(k)) return w;
  return 400;
}
function text(parent, str, x, y, fid, size, fill, anchor = 'start') {
  if (!str) return;
  const ot = OUTLINE && otFonts.get(fid);
  if (ot) {
    let x0 = x;
    if (anchor !== 'start') {
      const w = ot.getAdvanceWidth(str, size, { kerning: true });
      x0 = anchor === 'middle' ? x - w / 2 : x - w;
    }
    const d = ot.getPath(str, x0, y, size, { kerning: true }).toPathData(3);
    if (d) el('path', { d, fill }, parent);
    return;
  }
  // LIVE (Figma): real family + weight so Figma keeps it as editable text in the right font
  const f = LIVE && fontById.get(fid);
  const family = f ? f.family : css(fid);
  const weight = f ? weightOf(f.style) : null;
  const italic = f && /italic|oblique/i.test(f.style) ? 'italic' : null;
  const t = el('text', { x: r2(x), y: r2(y), 'font-family': family, 'font-weight': weight, 'font-style': italic, 'font-size': r2(size), fill, 'text-anchor': anchor === 'start' ? null : anchor }, parent);
  t.textContent = str;
}

async function loadOutlines(ids) {
  await Promise.all([...new Set(ids)].filter(Boolean).map(async (id) => {
    if (otFonts.has(id)) return;
    try {
      const buf = await (await fetch(`/api/fontfile/${id}`)).arrayBuffer();
      otFonts.set(id, parseFont(buf));
    } catch {
      otFonts.set(id, null);
    }
  }));
}

// ---------------------------------------------------------------- parts
// "91290A252_Black-Oxide Socket Head Screw" -> "Black-Oxide Socket Head Screw"
function pretty(name) {
  return String(name).replace(/^\d{3,6}[A-Z]\d{1,5}_+/, '').replace(/[-_]stp$/i, '').replace(/_/g, ' ').replace(/\s+·\s+\d+$/, '').trim() || name;
}

const parts = () => (model ? model.parts : []);
const isHidden = (pid) => S.hidden.includes(pid);
const nameOf = (pid) => S.names[pid] ?? pretty(parts().find((p) => p.id === pid)?.name || '');

// Same name = same number. Numbers follow the order parts appear in the file.
function numbering() {
  const auto = {};
  const byName = new Map();
  let n = 0;
  for (const p of parts()) {
    if (isHidden(p.id)) continue;
    const key = nameOf(p.id).toLowerCase();
    if (!byName.has(key)) byName.set(key, String(++n));
    auto[p.id] = byName.get(key);
  }
  const label = (pid) => S.labels[pid] ?? auto[pid] ?? '';
  const shown = new Set();
  const callout = {};
  for (const p of parts()) {
    const lb = label(p.id);
    if (!lb || isHidden(p.id) || !drawing?.parts[p.id]?.anchor || shown.has(lb)) continue;
    shown.add(lb);
    callout[p.id] = lb;
  }
  const legend = new Map();
  for (const p of parts()) {
    const lb = label(p.id);
    if (!lb || isHidden(p.id)) continue;
    if (!legend.has(lb)) legend.set(lb, { label: lb, name: nameOf(p.id), count: 0 });
    legend.get(lb).count++;
  }
  const items = [...legend.values()].sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true }));
  return { label, callout, items };
}

// ---------------------------------------------------------------- saving
let saveTimer;
function save() {
  if (!model) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api('PUT', '/api/state', { source: model.source, state: S }).catch(() => {}), 600);
}

// ---------------------------------------------------------------- text sizes
// Every kind of text has its own size in pt. Nothing is ever below 12 pt
// (16 px), so it stays readable in print.
const PT = MM / 72;
const MIN_PT = 12;
const AUTO = { title: 0.115, subtitle: 0.032, story: 0.0165, heading: 0.048, specs: 0.0165, tagline: 0.06, list: 0.0145, numbers: 0.022 };
function autoPt(kind) {
  const [, H] = matSize();
  const scale = kind === 'numbers' ? S.numScale ?? 1 : S.textScale ?? 1;
  return (H * AUTO[kind] * scale) / PT;
}
const ptOf = (kind) => Math.max(MIN_PT, Math.round(S.sizes[kind] ?? autoPt(kind)));
const sz = (kind) => ptOf(kind) * PT; // mm

// ---------------------------------------------------------------- grid
const step = () => S.grid.size * MM;
const snap = (v, force = false) => (S.grid.on || force ? Math.round(v / step()) * step() : v);
const PXMM = MM / 96; // mm per px (96 px per inch)

// ---------------------------------------------------------------- sheet layout
function matSize() {
  const [w, h] = S.mat.size.split('x').map(Number);
  return [w * MM, h * MM];
}

// Parts list: one line per item, whole names. Shrinks and adds columns until it fits.
function fitList(items, w, maxH, base, fid) {
  const rows = items.map((it) => ({ label: it.label, prefix: `${it.label}.`, name: it.name.toUpperCase() + (it.count > 1 ? ` (×${it.count})` : '') }));
  let best = null;
  const min = MIN_PT * PT;
  for (let size = base; ; size = Math.max(min, size * 0.94)) {
    const lh = size * 1.3;
    const perCol = Math.max(1, Math.floor((maxH - size * 0.25) / lh));
    const cols = Math.ceil(rows.length / perCol);
    const n = Math.ceil(rows.length / cols); // balance the columns
    const gap = size * 1.5;
    const colW = (w - gap * (cols - 1)) / cols;
    const indent = Math.max(...rows.map((r) => measure(`${r.prefix} `, fid, size)));
    const widest = Math.max(...rows.map((r) => measure(r.name, fid, size)));
    best = { size, lh, indent, colW, cells: rows.map((r, i) => ({ ...r, x: Math.floor(i / n) * (colW + gap), row: i % n })) };
    if (indent + widest <= colW || size <= min) return best;
  }
}

function computeLayout() {
  const [W, H] = matSize();
  const f = faces();
  const m = Math.min(W, H) * 0.05;
  const colW = W * 0.27;
  const blocks = [];
  const add = (field, str, fid, size, lh, upper, gapAfter, y) => {
    if (!String(str || '').trim()) return y;
    const lines = wrap(upper ? str.toUpperCase() : str, colW, fid, size);
    blocks.push({ field, lines, fid, size, lh, y });
    return y + size * (0.78 + (lines.length - 1) * lh) + size * 0.22 + gapAfter;
  };
  const t = S.text;
  let y = m;
  y = add('title', t.title, f.title, sz('title'), 0.92, true, H * 0.012, y);
  y = add('subtitle', t.subtitle, f.title, sz('subtitle'), 1.05, true, H * 0.045, y);
  y = add('story', t.story, f.text, sz('story'), 1.35, false, H * 0.045, y);
  const specRows = String(t.specs || '').split('\n').map((s) => s.trim()).filter(Boolean);
  if (specRows.length) {
    y = add('specs', 'Technical details', f.title, sz('heading'), 1, true, H * 0.018, y);
    const size = sz('specs');
    blocks.push({ field: 'specs', specs: specRows, size, lh: 1.5, y });
    y += size * (0.78 + (specRows.length - 1) * 1.5) + size * 0.22 + H * 0.045;
  }
  let tagY = H - m;
  if (String(t.tagline || '').trim()) {
    const size = sz('tagline');
    const lines = wrap(t.tagline.toUpperCase(), colW, f.title, size);
    const th = size * (0.78 + (lines.length - 1)) + size * 0.22;
    tagY -= th;
    blocks.push({ field: 'tagline', lines, fid: f.title, size, lh: 1, y: tagY });
  }
  const list = S.show.list ? { y, bottom: tagY - H * 0.03 } : null;
  // the right column: logos and the assembled view sit in its corners, parts fill the rest
  const ax = m + colW + W * 0.03;
  const area = [ax, m, W - m - ax, H - 2 * m];
  const gap = H * 0.02;
  let logoBox = null;
  if (S.images.length) {
    const units = S.images.reduce((a, im) => a + im.aspect, 0) + 0.3 * (S.images.length - 1);
    const h = Math.min(S.logo.h * PXMM, area[2] / units, area[3]);
    const w = h * units;
    const c = S.logo.corner;
    logoBox = [c[1] === 'r' ? area[0] + area[2] - w : area[0], c[0] === 't' ? area[1] : area[1] + area[3] - h, w, h];
  }
  // the assembled box is placed by the server (it knows the drawing's shape)
  const asm = S.asm.on ? { corner: S.asm.corner, w: Math.min(S.asm.w * PXMM, area[2]), inset: logoBox && S.logo.corner === S.asm.corner ? logoBox[3] + gap : 0, pad: gap } : null;
  return { W, H, m, colW, blocks, list, area, logoBox, asm };
}

function fit() {
  if (!drawing) return null;
  const P = drawing.place;
  if (P) return { s: P.k, tx: P.c[0] - P.rc[0] * P.k, ty: P.c[1] - P.rc[1] * P.k };
  const [x0, y0, x1, y1] = drawing.bounds;
  const [ax, ay, aw, ah] = L.area;
  const s = Math.min(aw / (x1 - x0 || 1), ah / (y1 - y0 || 1)) * 0.97;
  return { s, tx: ax + aw / 2 - ((x0 + x1) / 2) * s, ty: ay + ah / 2 - ((y0 + y1) / 2) * s };
}
function asmFit() {
  const A = drawing?.assembled;
  if (!A || !drawing.asmBox) return null;
  const [x0, y0, x1, y1] = A.bbox;
  const [ax, ay, aw, ah] = drawing.asmBox;
  const s = Math.min(aw / (x1 - x0 || 1), ah / (y1 - y0 || 1)) * 0.95;
  return { s, tx: ax + aw / 2 - ((x0 + x1) / 2) * s, ty: ay + ah / 2 - ((y0 + y1) / 2) * s };
}
const toSheet = (p, f) => [p[0] * f.s + f.tx, p[1] * f.s + f.ty];

// ---------------------------------------------------------------- callouts
function placeCallouts(f, num) {
  const out = {};
  const size = sz('numbers');
  const fid = faces().bold;
  const len = size * 1.8;
  const pids = Object.keys(num.callout);
  const anchors = Object.fromEntries(pids.map((pid) => [pid, toSheet(drawing.parts[pid].anchor, f)]));
  const obstacles = [];
  for (const [pid, p] of Object.entries(drawing.parts)) {
    if (!p.bbox || isHidden(pid)) continue;
    const a = toSheet([p.bbox[0], p.bbox[1]], f);
    const b = toSheet([p.bbox[2], p.bbox[3]], f);
    obstacles.push([a[0], a[1], b[0], b[1]]);
  }
  const ab = asmFit();
  if (ab) {
    const [x0, y0, x1, y1] = drawing.assembled.bbox;
    obstacles.push([x0 * ab.s + ab.tx, y0 * ab.s + ab.ty, x1 * ab.s + ab.tx, y1 * ab.s + ab.ty]);
  }
  obstacles.push([0, 0, L.m + L.colW + 4, L.H]);
  for (const b of logoBoxes()) obstacles.push([b.x, b.y, b.x + b.w, b.y + b.h]);
  const overlap = (p, q) => Math.max(0, Math.min(p[2], q[2]) - Math.max(p[0], q[0])) * Math.max(0, Math.min(p[3], q[3]) - Math.max(p[1], q[1]));
  const [ax, ay, aw, ah] = L.area;
  const C = [ax + aw / 2, ay + ah / 2];
  const placed = [];
  const order = pids.slice().sort((a, b) => Math.hypot(anchors[b][0] - C[0], anchors[b][1] - C[1]) - Math.hypot(anchors[a][0] - C[0], anchors[a][1] - C[1]));
  for (const pid of order) {
    const A = anchors[pid];
    const tw = measure(num.callout[pid], fid, size);
    const hw = tw / 2 + size * 0.4;
    const hh = size * 0.55;
    if (S.co[pid]) {
      const P = [A[0] + S.co[pid][0], A[1] + S.co[pid][1]];
      placed.push([P[0] - hw, P[1] - hh, P[0] + hw, P[1] + hh]);
      out[pid] = { A, P, tw };
      continue;
    }
    let ox = A[0] - C[0];
    let oy = A[1] - C[1];
    const on = Math.hypot(ox, oy) || 1;
    ox /= on;
    oy /= on;
    let best = null;
    for (const l of [len, len * 1.6, len * 2.4, len * 3.4]) {
      for (let k = 0; k < 16; k++) {
        const a = (k / 16) * Math.PI * 2;
        const dx = Math.cos(a);
        const dy = Math.sin(a);
        const P = [A[0] + dx * l, A[1] + dy * l];
        const R = [P[0] - hw, P[1] - hh, P[0] + hw, P[1] + hh];
        let sc = l * 0.6 - (dx * ox + dy * oy) * len * 0.5;
        if (R[0] < 0 || R[1] < 0 || R[2] > L.W || R[3] > L.H) sc += 1e5;
        for (const q of placed) if (overlap(R, q) > 0) sc += 1e4;
        for (const o of obstacles) sc += (overlap(R, o) / (4 * hw * hh)) * len * 1.2;
        if (!best || sc < best.sc) best = { sc, P, R };
      }
    }
    placed.push(best.R);
    out[pid] = { A, P: best.P, tw };
  }
  return { size, fid, items: out };
}

// ---------------------------------------------------------------- logos
function cleanSvg(markup, tint) {
  let s = String(markup)
    .replace(/<\?xml[^>]*>/g, '')
    .replace(/<!DOCTYPE[^>]*>/gi, '')
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/\son\w+="[^"]*"/gi, '');
  if (tint) {
    const keep = (v) => /^(none|transparent)$/i.test(v.trim());
    s = s.replace(/\b(fill|stroke)=(["'])([^"']*)\2/gi, (m, k, q, v) => (keep(v) ? m : `${k}="${tint}"`));
    s = s.replace(/\b(fill|stroke)\s*:\s*([^;"}]+)/gi, (m, k, v) => (keep(v) ? m : `${k}:${tint}`));
  }
  return s;
}

// Logos in a row in their corner box.
function logoBoxes() {
  if (!L.logoBox || !S.images.length) return [];
  const [bx, by, , h] = L.logoBox;
  let x = bx;
  return S.images.map((im) => {
    const b = { im, x, y: by, w: h * im.aspect, h };
    x += b.w + h * 0.3;
    return b;
  });
}

function renderImage(parent, box, forExport) {
  const { im, h } = box;
  im.x = box.x;
  im.y = box.y;
  im.w = box.w;
  const g = el('g', { class: forExport ? null : 'img', 'data-img': forExport ? null : im.id, id: forExport ? `logo-${im.id}` : null }, parent);
  if (im.kind === 'svg') {
    const doc = new DOMParser().parseFromString(cleanSvg(im.data, im.tint ? S.mat.fg : null), 'image/svg+xml');
    const root = doc.documentElement;
    if (root?.nodeName.toLowerCase() === 'svg') {
      const node = document.importNode(root, true);
      if (!node.getAttribute('viewBox')) {
        const w = parseFloat(node.getAttribute('width')) || 100;
        const hh = parseFloat(node.getAttribute('height')) || 100;
        node.setAttribute('viewBox', `0 0 ${w} ${hh}`);
      }
      node.setAttribute('x', r2(im.x));
      node.setAttribute('y', r2(im.y));
      node.setAttribute('width', r2(im.w));
      node.setAttribute('height', r2(h));
      node.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      if (im.tint) node.setAttribute('fill', S.mat.fg);
      g.appendChild(node);
    }
  } else {
    el('image', { href: im.data, x: r2(im.x), y: r2(im.y), width: r2(im.w), height: r2(h), preserveAspectRatio: 'xMidYMid meet' }, g);
  }
  if (!forExport) {
    el('rect', { x: r2(im.x), y: r2(im.y), width: r2(im.w), height: r2(h), fill: 'transparent' }, g);
    if (selImage === im.id) el('rect', { class: 'sel', x: r2(im.x - 1), y: r2(im.y - 1), width: r2(im.w + 2), height: r2(h + 2), 'stroke-width': 0.6 }, g);
  }
}

function addImage(file) {
  const rd = new FileReader();
  const isSvg = /svg/i.test(file.type) || /\.svg$/i.test(file.name);
  rd.onload = () => {
    const finish = (aspect) => {
      S.images.push({ id: Math.random().toString(36).slice(2, 8), kind: isSvg ? 'svg' : 'img', data: rd.result, aspect: aspect || 1, tint: isSvg });
      save();
      render();
      redraw(); // the logo row may take space from the parts
    };
    if (isSvg) {
      const doc = new DOMParser().parseFromString(String(rd.result), 'image/svg+xml');
      const root = doc.documentElement;
      const vb = (root.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
      const w = vb.length === 4 ? vb[2] : parseFloat(root.getAttribute('width')) || 1;
      const h = vb.length === 4 ? vb[3] : parseFloat(root.getAttribute('height')) || 1;
      finish(w / h);
    } else {
      const img = new Image();
      img.onload = () => finish(img.naturalWidth / img.naturalHeight);
      img.src = rd.result;
    }
  };
  if (isSvg) rd.readAsText(file);
  else rd.readAsDataURL(file);
}

// ---------------------------------------------------------------- draw the sheet
function buildSheet(svg, forExport = false) {
  L = computeLayout();
  const { W, H } = L;
  const fc = faces();
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  svg.setAttribute('viewBox', `0 0 ${r2(W)} ${r2(H)}`);
  if (forExport) {
    const [w, h] = S.mat.size.split('x');
    svg.setAttribute('width', `${w}in`);
    svg.setAttribute('height', `${h}in`);
  }
  const fg = S.mat.fg;
  el('rect', { x: 0, y: 0, width: r2(W), height: r2(H), fill: S.mat.bg, id: forExport ? 'background' : null }, svg);
  if (!forExport) el('rect', { x: 0.5, y: 0.5, width: W - 1, height: H - 1, rx: 12, fill: 'none', stroke: '#444', 'stroke-width': 0.8 }, svg);
  if (!forExport && S.grid.on) {
    const gs = step();
    let d = '';
    for (let x = gs; x < W; x += gs) d += `M${r2(x)} 0V${r2(H)}`;
    for (let y = gs; y < H; y += gs) d += `M0 ${r2(y)}H${r2(W)}`;
    el('path', { d, stroke: '#888', 'stroke-opacity': 0.25, 'stroke-width': 0.25, fill: 'none', 'pointer-events': 'none' }, svg);
  }
  const boxes = S.grid.on ? [L.area, drawing?.asmBox, L.logoBox].filter(Boolean) : [];
  if (!forExport) {
    for (const [x, y, w, h] of boxes) el('rect', { x: r2(x), y: r2(y), width: r2(w), height: r2(h), fill: 'rgba(255,255,255,0.04)', stroke: '#bbb', 'stroke-width': 0.6, 'stroke-dasharray': '4 3', 'pointer-events': 'none' }, svg);
  }

  // text column
  const tg = el('g', { id: forExport ? 'text' : null }, svg);
  for (const b of L.blocks) {
    const g = el('g', { 'data-field': forExport ? null : b.field }, tg);
    if (!forExport) {
      const n = b.specs ? b.specs.length : b.lines.length;
      el('rect', { x: L.m, y: r2(b.y), width: r2(L.colW), height: r2(n * b.size * b.lh + b.size * 0.3), fill: 'transparent' }, g);
    }
    if (b.specs) {
      b.specs.forEach((row, i) => {
        const yy = b.y + b.size * 0.78 + i * b.size * b.lh;
        const at = row.indexOf(':');
        if (at > 0) {
          const key = row.slice(0, at + 1);
          text(g, key, L.m, yy, fc.bold, b.size, fg);
          text(g, row.slice(at + 1).trim(), L.m + measure(`${key} `, fc.bold, b.size), yy, fc.text, b.size, fg);
        } else text(g, row, L.m, yy, fc.text, b.size, fg);
      });
    } else {
      b.lines.forEach((ln, i) => text(g, ln, L.m, b.y + b.size * 0.78 + i * b.size * b.lh, b.fid, b.size, fg));
    }
  }

  if (drawing) {
    const f = fit();
    const num = numbering();
    const lw = 0.35;

    // parts list
    if (L.list && num.items.length) {
      const lay = fitList(num.items, L.colW, Math.max(L.list.bottom - L.list.y, 20), sz('list'), fc.bold);
      const g = el('g', { id: forExport ? 'parts-list' : null }, svg);
      for (const c of lay.cells) {
        const yy = L.list.y + lay.size * 0.78 + c.row * lay.lh;
        const row = forExport ? g : el('g', { 'data-item': c.label }, g);
        if (!forExport) el('rect', { x: r2(L.m + c.x), y: r2(yy - lay.size * 0.95), width: r2(lay.colW), height: r2(lay.lh), fill: 'transparent' }, row);
        text(row, c.prefix, L.m + c.x, yy, fc.bold, lay.size, fg);
        text(row, c.name, L.m + c.x + lay.indent, yy, fc.bold, lay.size, fg);
      }
    }

    // drawing
    const dg = el('g', { class: 'drawing', id: forExport ? 'drawing' : null }, svg);
    const inner = el('g', { transform: `translate(${r2(f.tx)} ${r2(f.ty)}) scale(${f.s.toFixed(5)})` }, dg);
    if (S.show.lines) {
      const lg = el('g', { id: forExport ? 'assembly-lines' : null, fill: 'none', stroke: fg, 'stroke-width': (lw * 0.6) / f.s, 'stroke-dasharray': `${(2.4 / f.s).toFixed(3)} ${(1.6 / f.s).toFixed(3)}` }, inner);
      for (const [pid, d] of Object.entries(drawing.lines || {})) if (!isHidden(pid)) el('path', { d, 'data-line': forExport ? null : pid }, lg);
    }
    const pg = el('g', { id: forExport ? 'parts' : null, fill: 'none', stroke: fg, 'stroke-width': lw / f.s, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, inner);
    const used = new Set();
    const order = parts().filter((p) => drawing.parts[p.id] && !isHidden(p.id));
    for (const p of order) {
      const d = drawing.parts[p.id];
      let id = null;
      if (forExport) {
        id = `${num.label(p.id) ? `${num.label(p.id)}_` : ''}${nameOf(p.id)}`.replace(/[^\w-]+/g, '_').replace(/^(\d)/, 'p$1').slice(0, 60);
        while (used.has(id)) id += '_';
        used.add(id);
      }
      const g = el('g', { id, 'data-part': forExport ? null : p.id }, pg);
      const dd = (d.paths.sharp || '') + (d.paths.outline || '');
      if (dd) el('path', { d: dd }, g);
    }
    if (!forExport) {
      const hits = el('g', {}, inner);
      const area = (poly) => Math.abs(poly.reduce((a, q, i) => a + q[0] * poly[(i + 1) % poly.length][1] - poly[(i + 1) % poly.length][0] * q[1], 0)) / 2;
      order
        .filter((p) => drawing.parts[p.id].outline?.length > 2)
        .sort((a, b) => area(drawing.parts[b.id].outline) - area(drawing.parts[a.id].outline))
        .forEach((p) => el('polygon', { class: 'hit', 'data-pid': p.id, points: drawing.parts[p.id].outline.map((q) => q.join(',')).join(' ') }, hits));
    }

    // the whole thing, put together: its own drawing in its own box
    const A = drawing.assembled;
    const ab = asmFit();
    if (A && ab) {
      const ag = el('g', { id: forExport ? 'assembled' : null, fill: 'none', stroke: fg, 'stroke-width': lw / ab.s, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, svg);
      el('path', { d: A.d, transform: `translate(${r2(ab.tx)} ${r2(ab.ty)}) scale(${ab.s.toFixed(5)})` }, ag);
    }

    // callouts
    if (S.show.numbers) {
      const co = placeCallouts(f, num);
      const cg = el('g', { id: forExport ? 'part-numbers' : null }, svg);
      for (const [pid, c] of Object.entries(co.items)) {
        const g = el('g', { class: forExport ? null : 'callout', 'data-co': forExport ? null : pid }, cg);
        const dx = c.A[0] - c.P[0];
        const dy = c.A[1] - c.P[1];
        const hw = c.tw / 2 + co.size * 0.3;
        const hh = co.size * 0.45;
        const t = Math.min(dx ? hw / Math.abs(dx) : Infinity, dy ? hh / Math.abs(dy) : Infinity);
        if (Math.hypot(dx, dy) > 0.5 && t < 1) {
          el('line', { x1: r2(c.P[0] + dx * t), y1: r2(c.P[1] + dy * t), x2: r2(c.A[0]), y2: r2(c.A[1]), stroke: fg, 'stroke-width': 0.3 }, g);
        }
        if (!forExport) el('rect', { x: r2(c.P[0] - hw), y: r2(c.P[1] - hh), width: r2(hw * 2), height: r2(hh * 2), fill: 'transparent' }, g);
        text(g, num.callout[pid], c.P[0], c.P[1] + co.size * 0.36, co.fid, co.size, fg, 'middle');
      }
    }
  }

  // logos on top
  const ig = el('g', { id: forExport ? 'logos' : null }, svg);
  for (const b of logoBoxes()) renderImage(ig, b, forExport);
}

function render() {
  if (!model) return;
  buildSheet($('#sheet'));
}

// ---------------------------------------------------------------- drawing requests
let reqId = 0;
let drawTimer;
let refreshRunning = false;
let refreshPending = false;
async function flushRefresh() {
  if (refreshRunning) {
    refreshPending = true;
    return;
  }
  refreshRunning = true;
  try {
    do {
      refreshPending = false;
      await requestDrawing('preview');
    } while (refreshPending);
  } finally {
    refreshRunning = false;
  }
}
function redraw(delay = 0) {
  clearTimeout(drawTimer);
  // Invalidate an old response immediately, before the debounce expires.
  ++reqId;
  drawTimer = setTimeout(flushRefresh, delay);
}

// Boxes the parts must stay out of (assembled view, logos), in sheet mm.
function frameFor(keep) {
  const pad = L.H * 0.02; // keep a little air around the boxes
  const obstacles = L.logoBox ? [[L.logoBox[0] - pad, L.logoBox[1] - pad, L.logoBox[2] + 2 * pad, L.logoBox[3] + 2 * pad]] : [];
  const seed = drawing || S.layoutSeed;
  return { regions: S.placementRegions, seed: seed?.offsets, seedPlace: seed?.place, seedView: seed?.view, area: L.area, obstacles, asm: L.asm, clearance: S.show.numbers ? sz('numbers') * 0.5 : 1.4, anchor: keep && drawing?.place ? drawing.place.c : null, k: keep && !refitNext && drawing?.place ? drawing.place.k : null };
}

// keep: leave every part where it is now and only apply the user's own moves
let nudgeNext = null; // parts just dropped: the server slides them off any overlap
let refitNext = false; // after a drop: scale up to fill the space again
async function requestDrawing(engine, keep = false, retry = true) {
  if (!model) return null;
  const nudge = nudgeNext || [];
  nudgeNext = null;
  L = computeLayout();
  const frame = frameFor(keep);
  refitNext = false;
  const id = ++reqId;
  const [, , aw, ah] = L.area;
  status(engine === 'exact' ? 'Making final lines…' : 'Drawing…');
  $('#sheet').classList.add('busy');
  try {
    const res = await api('POST', '/api/draw', {
      model: model.id,
      view: S.view,
      spread: S.spread,
      aspect: aw / ah,
      hidden: S.hidden,
      pinned: keep && drawing ? { ...drawing.offsets, ...S.pinned } : S.pinned,
      group_spread: S.groupSpread,
      nudge,
      engine,
      lines: true,
      assembled: S.asm.on ? {} : null,
      frame,
    });
    if (id !== reqId) return null;
    drawing = res;
    for (const m of nudge) if (res.offsets[m]) S.pinned[m] = res.offsets[m]; // keep where they ended up
    if (nudge.length) save();
    render();
    status('');
    return res;
  } catch (err) {
    if (retry && id === reqId && /not loaded/i.test(err.message)) {
      try {
        await api('POST', '/api/open', { path: model.source });
      } catch {}
      return requestDrawing(engine, keep, false);
    }
    if (id === reqId) status(`Error: ${err.message}`);
    return null;
  } finally {
    if (id === reqId) $('#sheet').classList.remove('busy');
  }
}

// ---------------------------------------------------------------- open
async function open({ path, file }) {
  busy(`Reading ${file ? file.name : 'STEP file'}… big files take a minute the first time`);
  try {
    if (!FONTS.length) await loadFontList().catch(() => {});
    let info;
    if (file) {
      const fd = new FormData();
      fd.append('file', file);
      info = await api('POST', '/api/upload', fd);
    } else info = await api('POST', '/api/open', { path });
    model = info;
    const d = DEFAULTS();
    const sv = info.saved || {};
    S = { ...d, ...sv, view: { ...d.view, ...sv.view }, text: { ...d.text, ...sv.text }, mat: { ...d.mat, ...sv.mat }, show: { ...d.show, ...sv.show }, fonts: { ...d.fonts, ...sv.fonts }, grid: { ...d.grid, ...sv.grid }, sizes: { ...sv.sizes }, groupSpread: { ...sv.groupSpread }, asm: { ...d.asm, ...sv.asm }, logo: { ...d.logo, ...sv.logo } };
    S.area = null; // the right column is always the parts area now
    if (!Number.isFinite(S.view.az)) {
      // settings saved by the older Side / ¾ buttons
      const old = sv.view || {};
      S.view = { az: (old.kind === 'side' ? 0 : 30) + (old.mirror ? 180 : 0), el: old.kind === 'side' ? 0 : 18, flip: !!old.flip };
    }
    const df = defaultFonts();
    if (!fontById.has(S.fonts.title)) S.fonts.title = df.title;
    if (!fontById.has(S.fonts.text)) S.fonts.text = df.text;
    if (!info.saved) S.text.title = info.name.replace(/\s*desk\s*mat\s*version\s*$/i, '');
    drawing = null;
    selImage = null;
    $('#empty').hidden = true;
    $('#name').textContent = info.name;
    document.title = info.name;
    loadFaces();
    syncPanel();
    render();
    busy('Laying out parts…');
    await requestDrawing('fast');
    save();
  } catch (err) {
    status(`Error: ${err.message}`);
  } finally {
    busy('');
  }
}

function loadFaces() {
  const f = faces();
  [f.title, f.text, f.bold].forEach(useFace);
}

$('#open').onclick = async () => {
  const dlg = $('#dlg');
  $('#dlg-file').value = '';
  $('#dlg-path').value = '';
  const list = $('#dlg-recent');
  list.innerHTML = '';
  try {
    for (const p of await api('GET', '/api/recent')) {
      const d = document.createElement('div');
      d.textContent = p.split('/').pop();
      d.title = p;
      d.onclick = () => {
        dlg.close();
        open({ path: p });
      };
      list.append(d);
    }
  } catch {}
  dlg.showModal();
};
$('#dlg-file').onchange = () => {
  const file = $('#dlg-file').files[0];
  if (file) {
    $('#dlg').close();
    open({ file });
  }
};
$('#dlg-go').onclick = (e) => {
  const path = $('#dlg-path').value.trim();
  if (!path) return e.preventDefault();
  open({ path });
};
const stage = $('#stage');
stage.addEventListener('dragover', (e) => {
  e.preventDefault();
  stage.classList.add('drop');
});
stage.addEventListener('dragleave', () => stage.classList.remove('drop'));
stage.addEventListener('drop', (e) => {
  e.preventDefault();
  stage.classList.remove('drop');
  const files = [...e.dataTransfer.files];
  const step = files.find((f) => /\.(step|stp)$/i.test(f.name));
  if (step) open({ file: step });
  else if (model) for (const f of files) if (/\.(svg|png|jpe?g)$/i.test(f.name)) addImage(f);
});

// ---------------------------------------------------------------- view cube
// Cube frame: F (front), R (right), T (top). az turns F toward R, el tilts toward T.
const FACES = [
  { name: 'FRONT', n: [1, 0, 0], u: [0, 1, 0], v: [0, 0, -1], tr: 'translateZ(39px)' },
  { name: 'BACK', n: [-1, 0, 0], u: [0, -1, 0], v: [0, 0, -1], tr: 'rotateY(180deg) translateZ(39px)' },
  { name: 'RIGHT', n: [0, 1, 0], u: [-1, 0, 0], v: [0, 0, -1], tr: 'rotateY(90deg) translateZ(39px)' },
  { name: 'LEFT', n: [0, -1, 0], u: [1, 0, 0], v: [0, 0, -1], tr: 'rotateY(-90deg) translateZ(39px)' },
  { name: 'TOP', n: [0, 0, 1], u: [0, 1, 0], v: [1, 0, 0], tr: 'rotateX(90deg) translateZ(39px)' },
  { name: 'BOTTOM', n: [0, 0, -1], u: [0, 1, 0], v: [-1, 0, 0], tr: 'rotateX(-90deg) translateZ(39px)' },
];

function buildCube() {
  const cube = $('#cube');
  for (const f of FACES) {
    const face = document.createElement('div');
    face.className = 'face';
    face.style.transform = f.tr;
    for (let j = 0; j < 3; j++) {
      for (let i = 0; i < 3; i++) {
        const c = document.createElement('div');
        c.className = 'cell';
        c.dataset.dir = [0, 1, 2].map((k) => f.n[k] + (i - 1) * f.u[k] + (j - 1) * f.v[k]).join(',');
        face.append(c);
      }
    }
    const lb = document.createElement('div');
    lb.className = 'label';
    lb.textContent = f.name;
    face.append(lb);
    cube.append(face);
  }
}

function cubeTransform(az, el) {
  const a = (az * Math.PI) / 180;
  const e = (el * Math.PI) / 180;
  const d = [Math.cos(e) * Math.cos(a), Math.cos(e) * Math.sin(a), Math.sin(e)]; // (F, R, T)
  const rt = [-Math.sin(a), Math.cos(a), 0];
  const up = [d[1] * rt[2] - d[2] * rt[1], d[2] * rt[0] - d[0] * rt[2], d[0] * rt[1] - d[1] * rt[0]];
  // CSS axes: x = R, y = -T, z = F. Screen: x right, y down, z toward viewer.
  const X = [rt[1], -up[1], d[1]];
  const Y = [-rt[2], up[2], -d[2]];
  const Z = [rt[0], -up[0], d[0]];
  $('#cube').style.transform = `matrix3d(${[...X, 0, ...Y, 0, ...Z, 0, 0, 0, 0, 1].map((v) => v.toFixed(5)).join(',')})`;
}

function setView(az, el, draw = true) {
  S.view.az = Math.round(az * 100) / 100;
  S.view.el = Math.max(-90, Math.min(90, Math.round(el * 100) / 100));
  cubeTransform(S.view.az, S.view.el);
  if (draw) {
    S.co = {};
    save();
    redraw();
  }
}

let cubeDrag = null;
const cw = $('#cube-wrap');
cw.addEventListener('pointerdown', (e) => {
  cubeDrag = { x: e.clientX, y: e.clientY, az: S.view.az, el: S.view.el, moved: false, cell: e.target.closest('.cell') };
  cw.setPointerCapture(e.pointerId);
});
cw.addEventListener('pointermove', (e) => {
  if (!cubeDrag) return;
  const dx = e.clientX - cubeDrag.x;
  const dy = e.clientY - cubeDrag.y;
  if (!cubeDrag.moved && Math.hypot(dx, dy) < 4) return;
  cubeDrag.moved = true;
  setView(cubeDrag.az - dx * 0.6, cubeDrag.el + dy * 0.6, false);
});
cw.addEventListener('pointerup', () => {
  const d = cubeDrag;
  cubeDrag = null;
  if (!d || !model) return;
  if (d.moved) return setView(S.view.az, S.view.el);
  if (!d.cell) return;
  const [F, R, T] = d.cell.dataset.dir.split(',').map(Number);
  const n = Math.hypot(F, R, T);
  const az = F === 0 && R === 0 ? S.view.az : (Math.atan2(R, F) * 180) / Math.PI;
  setView(az, (Math.asin(T / n) * 180) / Math.PI);
});
buildCube();
cubeTransform(HOME.az, HOME.el);

// ---------------------------------------------------------------- panel
// One name box per parts-list item. Renaming changes every part in that item.
function buildPartsNames() {
  const box = $('#parts-names');
  box.hidden = !S.show.list || !model;
  box.innerHTML = '';
  if (box.hidden) return;
  const num = numbering();
  for (const it of num.items) {
    const key = it.name.toLowerCase();
    const members = parts().filter((p) => (isHidden(p.id) ? nameOf(p.id).toLowerCase() === key : num.label(p.id) === it.label)).map((p) => p.id);
    const row = document.createElement('div');
    const n = document.createElement('span');
    n.textContent = it.label;
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.value = it.name;
    inp.spellcheck = false;
    inp.dataset.item = it.label;
    inp.oninput = () => {
      for (const m of members) S.names[m] = inp.value;
      render();
      save();
    };
    inp.onchange = () => buildPartsNames();
    row.append(n, inp);
    box.append(row);
  }
}

function syncPanel() {
  cubeTransform(S.view.az, S.view.el);
  $('#flip').classList.toggle('on', !!S.view.flip);
  $('#spread').value = S.spread;
  $('#show-asm').checked = S.asm.on;
  $('#asm-row').hidden = !S.asm.on;
  $('#asm-corner').value = S.asm.corner;
  $('#asm-w').value = S.asm.w;
  $('#logo-corner').value = S.logo.corner;
  $('#logo-h').value = S.logo.h;
  for (const k of Object.keys(AUTO)) $(`#sz-${k}`).value = ptOf(k);
  for (const k of ['title', 'subtitle', 'story', 'specs', 'tagline']) $(`#t-${k}`).value = S.text[k] || '';
  $('#font-title').value = fontById.get(S.fonts.title)?.full || '';
  $('#font-text').value = fontById.get(S.fonts.text)?.full || '';
  $('#size').value = S.mat.size;
  $('#grid-on').checked = S.grid.on;
  $('#grid-size').value = String(S.grid.size);
  $('#bg').value = S.mat.bg;
  $('#fg').value = S.mat.fg;
  $('#show-numbers').checked = S.show.numbers;
  $('#show-lines').checked = S.show.lines;
  $('#show-list').checked = S.show.list;
  buildPartsNames();
  const hl = $('#hidden-list');
  hl.innerHTML = '';
  $('#hidden-box').hidden = !S.hidden.length;
  for (const pid of S.hidden) {
    const row = document.createElement('div');
    const name = document.createElement('span');
    name.textContent = nameOf(pid);
    const b = document.createElement('button');
    b.textContent = 'Show';
    b.onclick = () => {
      S.hidden = S.hidden.filter((x) => x !== pid);
      syncPanel();
      save();
      redraw();
    };
    row.append(name, b);
    hl.append(row);
  }
}

$('#size').innerHTML = SIZES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
$('#home').onclick = () => setView(HOME.az, HOME.el);
$('#flip').onclick = () => {
  S.view.flip = !S.view.flip;
  S.co = {};
  syncPanel();
  save();
  redraw();
};
$('#spread').oninput = () => {
  S.spread = +$('#spread').value;
  save();
  redraw(80);
};
$('#show-asm').onchange = (e) => {
  S.asm.on = e.target.checked;
  syncPanel();
  save();
  redraw();
};
for (const [id, obj, key, min] of [['asm-corner', 'asm', 'corner'], ['asm-w', 'asm', 'w', 100], ['logo-corner', 'logo', 'corner'], ['logo-h', 'logo', 'h', 16]]) {
  $(`#${id}`).onchange = (e) => {
    let v = e.target.value;
    if (min) {
      v = Math.max(min, Math.round(+v || min));
      e.target.value = v;
    }
    S[obj][key] = v;
    S.co = {};
    save();
    render();
    redraw(200);
  };
}
$('#reset').onclick = () => {
  S.pinned = {};
  S.co = {};
  save();
  redraw();
};
for (const k of ['title', 'subtitle', 'story', 'specs', 'tagline']) {
  $(`#t-${k}`).oninput = (e) => {
    S.text[k] = e.target.value;
    render();
    save();
  };
}
for (const [k, key] of [['title', 'font-title'], ['text', 'font-text']]) {
  $(`#${key}`).onchange = (e) => {
    const f = findFont(e.target.value);
    if (!f) {
      status('Font not found. Pick one from the list.');
      return;
    }
    status('');
    S.fonts[k] = f.id;
    loadFaces();
    render();
    save();
  };
}
for (const k of Object.keys(AUTO)) {
  const inp = $(`#sz-${k}`);
  inp.onchange = () => {
    const v = Math.max(MIN_PT, Math.round(+inp.value || MIN_PT));
    S.sizes[k] = v;
    inp.value = v;
    render();
    save();
  };
  inp.oninput = () => {
    if (+inp.value >= MIN_PT) {
      S.sizes[k] = Math.round(+inp.value);
      render();
      save();
    }
  };
}
$('#add-logo').onclick = () => model && $('#logo-file').click();
$('#logo-file').onchange = (e) => {
  for (const file of e.target.files) addImage(file);
  e.target.value = '';
};
$('#size').onchange = (e) => {
  S.mat.size = e.target.value;
  save();
  render();
  redraw();
};
$('#grid-on').onchange = (e) => {
  S.grid.on = e.target.checked;
  render();
  save();
};
$('#grid-size').onchange = (e) => {
  S.grid.size = +e.target.value;
  render();
  save();
};
$('#bg').oninput = (e) => {
  S.mat.bg = e.target.value;
  render();
  save();
};
$('#fg').oninput = (e) => {
  S.mat.fg = e.target.value;
  render();
  save();
};
for (const k of ['numbers', 'lines', 'list']) {
  $(`#show-${k}`).onchange = (e) => {
    S.show[k] = e.target.checked;
    if (k === 'list') buildPartsNames();
    render();
    save();
  };
}

// ---------------------------------------------------------------- sheet interaction
const svg = $('#sheet');
let drag = null;

function svgPt(e) {
  const p = svg.createSVGPoint();
  p.x = e.clientX;
  p.y = e.clientY;
  const q = p.matrixTransform(svg.getScreenCTM().inverse());
  return [q.x, q.y];
}

svg.addEventListener('pointerdown', (e) => {
  closePops();
  const img = e.target.closest('[data-img]');
  const hit = e.target.closest('[data-pid]');
  const co = e.target.closest('[data-co]');
  const field = e.target.closest('[data-field]');
  const item = e.target.closest('[data-item]');
  const start = svgPt(e);
  if (img) {
    const im = S.images.find((x) => x.id === img.dataset.img);
    selImage = im.id;
    drag = { kind: 'img', im, start, x0: im.x, y0: im.y };
  } else if (co) {
    drag = { kind: 'co', pid: co.dataset.co, start, node: co };
  } else if (hit) {
    const pid = hit.dataset.pid;
    // any part moves its whole sub-assembly; hold Alt to move just that part
    const grp = drawing.groupOf?.[pid];
    const group = (!e.altKey && grp && drawing.groups?.[grp]) || drawing.bundles?.[pid] || [pid];
    const nodes = group.flatMap((m) => [svg.querySelector(`[data-pid="${m}"]`), svg.querySelector(`[data-part="${m}"]`)]);
    drag = { kind: 'part', pid, group, start, nodes, lines: group.map((m) => svg.querySelector(`[data-line="${m}"]`)) };
  } else if (item) {
    const inp = [...document.querySelectorAll('#parts-names input')].find((x) => x.dataset.item === item.dataset.item);
    if (inp) {
      inp.scrollIntoView({ block: 'center' });
      inp.focus();
      inp.select();
    }
  } else if (field) {
    const input = $(`#t-${field.dataset.field}`);
    input.focus();
    input.select?.();
  }
  if (!img && selImage) {
    selImage = null;
    render();
  }
  if (!drag) return;
  drag.x = e.clientX;
  drag.y = e.clientY;
  drag.moved = false;
  svg.setPointerCapture(e.pointerId);
  if (drag.kind === 'img') render();
});

svg.addEventListener('pointermove', (e) => {
  if (!drag) return;
  const p = svgPt(e);
  const dx = p[0] - drag.start[0];
  const dy = p[1] - drag.start[1];
  if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 3) return;
  drag.moved = true;
  drag.dx = dx;
  drag.dy = dy;
  if (drag.kind === 'img') {
    // logos stay in their corner
  } else if (drag.kind === 'co') drag.node.setAttribute('transform', `translate(${r2(dx)} ${r2(dy)})`);
  else {
    const f = fit();
    const tr = `translate(${(dx / f.s).toFixed(3)} ${(dy / f.s).toFixed(3)})`;
    for (const n of drag.nodes) n?.setAttribute('transform', tr);
    for (const n of drag.lines || []) if (n) n.style.opacity = 0;
  }
});

svg.addEventListener('pointerup', (e) => {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (!d.moved) {
    if (d.kind === 'part') openPop(d.pid, e.clientX, e.clientY);
    if (d.kind === 'img') openImgPop(d.im, e.clientX, e.clientY);
    return;
  }
  if (d.kind === 'img') return;
  const f = fit();
  if (d.kind === 'co') {
    const A = toSheet(drawing.parts[d.pid].anchor, f);
    const c = placeCallouts(f, numbering()).items[d.pid];
    S.co[d.pid] = [r2(c.P[0] - A[0] + d.dx), r2(c.P[1] - A[1] + d.dy)];
    save();
    render();
    return;
  }
  // part moved: turn the sheet move into a 3D move in the view plane
  const v = drawing.view;
  const dir = v.dir;
  const x = v.xdir;
  const yv = [dir[1] * x[2] - dir[2] * x[1], dir[2] * x[0] - dir[0] * x[2], dir[0] * x[1] - dir[1] * x[0]];
  const mx = d.dx / f.s;
  const my = d.dy / f.s;
  for (const m of d.group) {
    const base = S.pinned[m] || drawing.offsets[m] || [0, 0, 0];
    S.pinned[m] = base.map((b, i) => r2(b + mx * x[i] - my * yv[i]));
    delete S.co[m];
  }
  save();
  nudgeNext = d.group;
  refitNext = true;
  requestDrawing('fast', true);
});

// ---------------------------------------------------------------- popovers
let popPid = null;
let popImg = null;
function place(pop, x, y) {
  pop.hidden = false;
  pop.style.left = `${Math.min(x + 8, window.innerWidth - 240)}px`;
  pop.style.top = `${Math.min(y + 8, window.innerHeight - 140)}px`;
}
function openPop(pid, x, y) {
  popPid = pid;
  $('#pop-name').value = nameOf(pid);
  $('#pop-num').value = numbering().label(pid);
  $('#pop-reset').disabled = !(drawing?.groups?.[pid] || drawing?.bundles?.[pid] || [pid]).some((m) => S.pinned[m]);
  const g = drawing?.groupOf?.[pid];
  $('#pop-gs-row').hidden = !g;
  if (g) $('#pop-gs').value = S.groupSpread[g] ?? 1;
  place($('#pop'), x, y);
  $('#pop-name').focus();
}
// spread one sub-assembly on its own; its parts are laid out again (hand moves in it are dropped)
$('#pop-gs').oninput = (e) => {
  const g = popPid && drawing?.groupOf?.[popPid];
  if (!g) return;
  S.groupSpread[g] = +e.target.value;
  for (const [m, main] of Object.entries(drawing.groupOf)) if (main === g) delete S.pinned[m];
  save();
  redraw(80);
};
function openImgPop(im, x, y) {
  popImg = im;
  $('#img-tint').checked = !!im.tint;
  $('#img-tint').disabled = im.kind !== 'svg';
  place($('#img-pop'), x, y);
}
function closePops() {
  $('#pop').hidden = true;
  $('#img-pop').hidden = true;
  popPid = null;
  popImg = null;
}
$('#pop-name').oninput = (e) => {
  if (!popPid) return;
  const old = nameOf(popPid);
  for (const p of parts()) if (nameOf(p.id) === old) S.names[p.id] = e.target.value;
  render();
  buildPartsNames();
  save();
};
$('#pop-num').oninput = (e) => {
  if (!popPid) return;
  const name = nameOf(popPid);
  for (const p of parts()) if (nameOf(p.id) === name) S.labels[p.id] = e.target.value.trim();
  render();
  buildPartsNames();
  save();
};
$('#pop-hide').onclick = () => {
  if (!popPid) return;
  S.hidden.push(popPid);
  delete S.pinned[popPid];
  closePops();
  syncPanel();
  save();
  render();
  redraw();
};
$('#pop-reset').onclick = () => {
  if (!popPid) return;
  for (const m of drawing?.groups?.[popPid] || drawing?.bundles?.[popPid] || [popPid]) delete S.pinned[m];
  closePops();
  save();
  redraw();
};
$('#img-tint').onchange = (e) => {
  if (!popImg) return;
  popImg.tint = e.target.checked;
  render();
  save();
};
$('#img-remove').onclick = () => {
  if (!popImg) return;
  S.images = S.images.filter((x) => x !== popImg);
  selImage = null;
  closePops();
  render();
  save();
  redraw();
};
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closePops();
  if ((e.key === 'Delete' || e.key === 'Backspace') && selImage && !/INPUT|TEXTAREA/.test(document.activeElement?.tagName)) {
    S.images = S.images.filter((x) => x.id !== selImage);
    selImage = null;
    closePops();
    render();
    save();
    redraw();
  }
});
document.addEventListener('pointerdown', (e) => {
  if (!e.target.closest('#pop, #img-pop, #sheet')) closePops();
});

// ---------------------------------------------------------------- export
function download(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 1000);
}

async function finalSVG() {
  busy('Making final lines…');
  try {
    if (drawing?.engine !== 'exact') await requestDrawing('exact', true);
    const fc = faces();
    await loadOutlines([fc.title, fc.text, fc.bold]);
    const out = document.createElementNS(NS, 'svg');
    out.setAttribute('xmlns', NS);
    OUTLINE = true;
    try {
      buildSheet(out, true);
    } finally {
      OUTLINE = false;
    }
    return `<?xml version="1.0" encoding="UTF-8"?>\n${new XMLSerializer().serializeToString(out)}`;
  } finally {
    busy('');
  }
}
// Everything the Figma plugin (figma-plugin/) needs to rebuild the mat with
// live text and auto layout. Sizes are in mm; px = Figma pixels per mm.
async function figmaFile() {
  busy('Making the Figma file…');
  try {
    if (drawing?.engine !== 'exact') await requestDrawing('exact', true);
    const out = document.createElementNS(NS, 'svg');
    buildSheet(out, true); // live <text>; also refreshes L
    const { W, H } = L;
    const PX = 96 / MM;
    const fc = faces();
    const num = numbering();
    const font = (id) => {
      const f = fontById.get(id);
      return f ? { family: f.family, style: f.style } : { family: 'Inter', style: 'Regular' };
    };
    const ser = (n) => new XMLSerializer().serializeToString(n);
    const page = (inner) => `<svg xmlns="${NS}" width="${r2(W * PX)}" height="${r2(H * PX)}" viewBox="0 0 ${r2(W)} ${r2(H)}">${inner}</svg>`;
    const piece = (sel) => {
      const n = out.querySelector(sel);
      return n ? page(ser(n)) : null;
    };
    let leaders = null;
    const pn = out.querySelector('#part-numbers');
    if (pn) {
      const c = pn.cloneNode(true);
      c.querySelectorAll('text, path').forEach((t) => t.remove());
      leaders = page(ser(c));
    }
    const raw = { title: S.text.title, subtitle: S.text.subtitle, story: S.text.story, tagline: S.text.tagline };
    const blocks = L.blocks.map((b) => {
      const n = b.specs ? b.specs.length : b.lines.length;
      const base = { field: b.field, y: b.y, height: b.size * (0.78 + (n - 1) * b.lh) + b.size * 0.22, size: b.size, lineHeight: b.size * b.lh };
      if (b.specs) {
        return { ...base, kind: 'specs', font: font(fc.text), bold: font(fc.bold), rows: b.specs.map((r) => {
          const at = r.indexOf(':');
          return at > 0 ? { key: r.slice(0, at + 1), value: r.slice(at + 1).trim() } : { key: '', value: r };
        }) };
      }
      return { ...base, kind: 'text', text: b.field === 'specs' ? 'Technical details' : raw[b.field], upper: b.field !== 'story', font: font(b.fid) };
    });
    let list = null;
    if (L.list && num.items.length) {
      const lay = fitList(num.items, L.colW, Math.max(L.list.bottom - L.list.y, 20), sz('list'), fc.bold);
      const cols = [];
      for (const c of lay.cells) {
        let col = cols.find((k) => k.x === c.x);
        if (!col) cols.push((col = { x: c.x, rows: [] }));
        col.rows.push({ label: c.prefix, name: c.name });
      }
      list = { x: L.m, y: L.list.y, size: lay.size, lineHeight: lay.lh, indent: lay.indent, colW: lay.colW, gap: cols.length > 1 ? cols[1].x - cols[0].x - lay.colW : 0, columns: cols, font: font(fc.bold) };
    }
    let numbers = null;
    if (S.show.numbers) {
      const co = placeCallouts(fit(), num);
      numbers = { size: co.size, font: font(co.fid), items: Object.entries(co.items).map(([pid, c]) => ({ label: num.callout[pid], x: c.P[0], y: c.P[1] })) };
    }
    const lb = logoBoxes();
    const logos = lb.length
      ? { corner: S.logo.corner, gap: lb[0].h * 0.3, items: lb.map((b) => ({ x: b.x, y: b.y, w: b.w, h: b.h, svg: `<svg xmlns="${NS}" width="${r2(b.w * PX)}" height="${r2(b.h * PX)}" viewBox="${r2(b.x)} ${r2(b.y)} ${r2(b.w)} ${r2(b.h)}">${ser(out.querySelector(`#logo-${b.im.id}`))}</svg>` })) }
      : null;
    return JSON.stringify({ kind: 'mat', name: S.text.title || model.name, W, H, px: PX, bg: S.mat.bg, fg: S.mat.fg, drawing: piece('#drawing'), assembled: piece('#assembled'), leaders, textX: L.m, textW: L.colW, blocks, list, numbers, logos });
  } finally {
    busy('');
  }
}
// Figma: an SVG with live text (real font names) and named layers. Saved, and
// copied so it can be pasted into Figma with Cmd+V. Alt-click saves the file for
// the auto-layout plugin in figma-plugin/ instead.
$('#figma').onclick = async (e) => {
  if (!model) return;
  if (e.altKey) {
    download(new Blob([await figmaFile()], { type: 'application/json' }), `${fileName()}-figma.json`);
    status('Saved the plugin file. In Figma: Plugins → Development → Mat import.');
    return;
  }
  busy('Making the Figma file…');
  let str;
  try {
    if (drawing?.engine !== 'exact') await requestDrawing('exact', true);
    const out = document.createElementNS(NS, 'svg');
    out.setAttribute('xmlns', NS);
    LIVE = true;
    try {
      buildSheet(out, true);
    } finally {
      LIVE = false;
    }
    str = `<?xml version="1.0" encoding="UTF-8"?>\n${new XMLSerializer().serializeToString(out)}`;
  } finally {
    busy('');
  }
  download(new Blob([str], { type: 'image/svg+xml' }), `${fileName()} (Figma).svg`);
  let copied = false;
  try {
    await navigator.clipboard.writeText(str);
    copied = true;
  } catch {}
  status(copied ? 'Copied. Paste into Figma with Cmd+V, or drag the saved SVG in.' : 'Saved. Drag the SVG into Figma.');
};

const fileName = () => (S.text.title || model?.name || 'mat').replace(/[^\w\- ]+/g, '').trim() || 'mat';

$('#svg').onclick = async () => {
  if (!model) return;
  download(new Blob([await finalSVG()], { type: 'image/svg+xml' }), `${fileName()}.svg`);
};
$('#png').onclick = async () => {
  if (!model) return;
  const str = await finalSVG();
  busy('Making PNG…');
  try {
    const url = URL.createObjectURL(new Blob([str], { type: 'image/svg+xml' }));
    const img = new Image();
    await new Promise((res, rej) => {
      img.onload = res;
      img.onerror = rej;
      img.src = url;
    });
    const [w, h] = S.mat.size.split('x').map(Number);
    const c = document.createElement('canvas');
    c.width = Math.round(w * 150);
    c.height = Math.round(h * 150);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    URL.revokeObjectURL(url);
    download(await new Promise((res) => c.toBlob(res, 'image/png')), `${fileName()}.png`);
  } catch {
    status('Could not make the PNG');
  } finally {
    busy('');
  }
};

window.addEventListener('resize', () => render());
loadFontList().catch(() => {});
fetch('/api/version').then((r) => {
  if (!r.ok) status('This app was updated. Close its Terminal window, then double-click start.command.');
}).catch(() => {});
window.mat = { open, get S() { return S; }, get drawing() { return drawing; }, finalSVG, render, addImage };
