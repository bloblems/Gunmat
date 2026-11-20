// Reopen vector artwork without asking the CAD renderer to rebuild it.
const NS = 'http://www.w3.org/2000/svg';
const EDITABLE = 'g,path,text,image,rect,circle,ellipse,line,polyline,polygon,use';
const xml = (node) => new XMLSerializer().serializeToString(node);
const externalCSS = (value) => /@import/i.test(value) || [...value.matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/gi)].some((match) => !match[2].trim().startsWith('#'));
export function svgPixels(value, fallback) {
  const match = /^(\d*\.?\d+)\s*(px|in|mm|cm|pt)?$/.exec(value || '');
  return match ? +match[1] * ({ in: 96, mm: 96 / 25.4, cm: 96 / 2.54, pt: 96 / 72 }[match[2]] || 1) : fallback;
}

export function parseArtwork(source, parser = new DOMParser()) {
  const doc = parser.parseFromString(source, 'image/svg+xml');
  const root = doc.documentElement;
  if (doc.querySelector('parsererror') || root.localName !== 'svg' || root.namespaceURI !== NS) {
    throw new Error('Choose an SVG drawing exported by Gunmat or Figma.');
  }
  root.querySelectorAll('script,foreignObject,iframe,object,embed,animate,animateTransform,animateMotion,set').forEach((n) => n.remove());
  for (const node of [root, ...root.querySelectorAll('*')]) {
    for (const attr of [...node.attributes]) {
      const value = attr.value.trim();
      const href = attr.localName === 'href';
      const embeddedImage = node.localName === 'image' && /^data:image\/(png|jpeg|webp|gif);base64,/i.test(value);
      if (/^on/i.test(attr.name) || (href && !value.startsWith('#') && !embeddedImage) || externalCSS(value)) {
        node.removeAttributeNode(attr);
      }
    }
    if (node.localName === 'style' && externalCSS(node.textContent)) node.remove();
  }
  if (!root.hasAttribute('viewBox')) {
    const dimension = (attr) => {
      const match = /^(\d*\.?\d+)\s*(px|in|mm|cm|pt)?$/.exec(root.getAttribute(attr) || '');
      if (!match) throw new Error('The SVG needs a viewBox or fixed width and height.');
      return +match[1] * ({ in: 96, mm: 96 / 25.4, cm: 96 / 2.54, pt: 96 / 72 }[match[2]] || 1);
    };
    root.setAttribute('viewBox', `0 0 ${dimension('width')} ${dimension('height')}`);
  }
  const box = root.getAttribute('viewBox').trim().split(/[\s,]+/).map(Number);
  if (box.length !== 4 || !box.every(Number.isFinite) || box[2] <= 0 || box[3] <= 0) throw new Error('The SVG has invalid dimensions.');
  root.setAttribute('viewBox', box.join(' '));
  return root;
}

// Also accepts the original plugin JSON files, before they included a full SVG.
export function pluginArtwork(data, document, parser = new DOMParser()) {
  if (data.kind !== 'mat' || !Number.isFinite(data.W) || !Number.isFinite(data.H) || data.W <= 0 || data.H <= 0) throw new Error('Choose a Gunmat Figma JSON file.');
  if (typeof data.svg === 'string') return data.svg;
  const root = document.createElementNS(NS, 'svg');
  root.setAttributeNS('http://www.w3.org/2000/xmlns/', 'xmlns', NS);
  root.setAttribute('viewBox', `0 0 ${data.W} ${data.H}`);
  root.setAttribute('width', `${data.W}mm`);
  root.setAttribute('height', `${data.H}mm`);
  const add = (tag, attrs, text) => {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) if (value != null) node.setAttribute(key, value);
    if (text != null) node.textContent = text;
    root.appendChild(node);
    return node;
  };
  add('rect', { width: data.W, height: data.H, fill: data.bg, id: 'background' });
  const append = (source, name) => {
    if (!source) return;
    const part = parseArtwork(source, parser);
    // Plugin pieces are full-sheet SVGs with their own coordinate systems.
    const group = add('g', { 'data-name': name });
    part.removeAttribute('width');
    part.removeAttribute('height');
    part.setAttribute('width', data.W);
    part.setAttribute('height', data.H);
    group.appendChild(document.importNode(part, true));
  };
  append(data.drawing, 'Exploded view');
  append(data.assembled, 'Assembled view');
  append(data.leaders, 'Leaders');
  const text = (value, x, y, size, font, upper, anchor) => add('text', {
    x, y: y + size * 0.78, fill: data.fg, 'font-size': size,
    'font-family': font?.family || 'sans-serif', 'font-weight': /bold/i.test(font?.style) ? 'bold' : 'normal',
    'font-style': /italic/i.test(font?.style) ? 'italic' : 'normal', 'text-anchor': anchor,
  }, upper ? value.toUpperCase() : value);
  for (const block of data.blocks || []) {
    const lines = block.kind === 'specs' ? block.rows.map((r) => `${r.key} ${r.value}`.trim()) : String(block.text || '').split('\n');
    lines.forEach((line, i) => text(line, data.textX, block.y + i * block.lineHeight, block.size, block.font, block.upper));
  }
  if (data.list) {
    const list = data.list;
    for (const column of list.columns) column.rows.forEach((row, i) => {
      text(row.label, list.x + column.x, list.y + i * list.lineHeight, list.size, list.font);
      text(row.name, list.x + column.x + list.indent, list.y + i * list.lineHeight, list.size, list.font);
    });
  }
  if (data.numbers) for (const item of data.numbers.items) text(item.label, item.x, item.y - data.numbers.size * 0.5, data.numbers.size, data.numbers.font, false, 'middle');
  for (const item of data.logos?.items || []) {
    const logo = parseArtwork(item.svg, parser);
    logo.setAttribute('x', item.x);
    logo.setAttribute('y', item.y);
    logo.setAttribute('width', item.w);
    logo.setAttribute('height', item.h);
    root.appendChild(document.importNode(logo, true));
  }
  return xml(root);
}

// A flattened line drawing can hold many independent contours in one path.
// Give each contour an absolute start so relative path commands keep their shape.
export function splitPathData(data) {
  const pattern = /[a-zA-Z]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?/g;
  const tokens = data.match(pattern) || [];
  if (data.replace(pattern, '').replace(/[\s,]/g, '')) throw new Error('This path contains unsupported data.');
  const counts = { M: 2, L: 2, H: 1, V: 1, C: 6, S: 4, Q: 4, T: 2, A: 7 };
  const paths = [];
  let command = null;
  let current = [];
  let x = 0, y = 0, startX = 0, startY = 0;
  let at = 0;
  while (at < tokens.length) {
    if (/^[a-z]$/i.test(tokens[at])) command = tokens[at++];
    if (!command) throw new Error('This path has no starting point.');
    const upper = command.toUpperCase();
    const relative = upper !== command;
    if (upper === 'Z') {
      current.push(command);
      x = startX;
      y = startY;
      command = null;
      continue;
    }
    const count = counts[upper];
    const values = tokens.slice(at, at + count).map(Number);
    if (!count || values.length !== count || !values.every(Number.isFinite)) throw new Error('This path contains unsupported data.');
    at += count;
    if (upper === 'M') {
      if (current.length) paths.push(current.join(' '));
      x = values[0] + (relative ? x : 0);
      y = values[1] + (relative ? y : 0);
      startX = x;
      startY = y;
      current = ['M', x, y];
      command = relative ? 'l' : 'L';
      continue;
    }
    if (!current.length) throw new Error('This path has no starting point.');
    current.push(command, ...values);
    if (upper === 'H') x = values[0] + (relative ? x : 0);
    else if (upper === 'V') y = values[0] + (relative ? y : 0);
    else {
      x = values[count - 2] + (relative ? x : 0);
      y = values[count - 1] + (relative ? y : 0);
    }
  }
  if (current.length) paths.push(current.join(' '));
  return paths;
}

export function createDrawingEditor({ sheet, panel, status, onOpen }) {
  let root = null;
  let name = 'drawing';
  let width = null;
  let height = null;
  let selected = null;
  let overlay = null;
  let drag = null;
  const history = [];
  const $ = (id) => panel.querySelector(`#art-${id}`);
  const layers = () => [...root.querySelectorAll(EDITABLE)].filter((n) => !n.closest('defs,clipPath,mask,pattern,marker,symbol'));
  const snapshot = () => {
    history.push(xml(root));
    if (history.length > 30) history.shift();
    $('undo').disabled = false;
  };
  const paintSelection = () => {
    overlay?.remove();
    overlay = null;
    if (!selected?.isConnected) return;
    try {
      const b = selected.getBBox();
      overlay = document.createElementNS(NS, 'rect');
      const matrix = sheet.getScreenCTM().inverse().multiply(selected.getScreenCTM());
      for (const [key, value] of Object.entries({ x: b.x, y: b.y, width: b.width, height: b.height,
        transform: `matrix(${[matrix.a,matrix.b,matrix.c,matrix.d,matrix.e,matrix.f].join(' ')})`,
        fill: 'none', stroke: '#80bfff', 'stroke-width': 1.5, 'vector-effect': 'non-scaling-stroke', 'pointer-events': 'none' })) overlay.setAttribute(key, value);
      sheet.appendChild(overlay);
    } catch {}
  };
  const select = (node) => {
    selected = node;
    const all = layers();
    $('layers').value = node ? String(all.indexOf(node)) : '';
    $('selection').disabled = !node;
    $('text').disabled = node?.localName !== 'text';
    $('inside').disabled = !node?.querySelector(EDITABLE);
    $('parent').disabled = !node || node.parentElement === root;
    $('separate').disabled = node?.localName !== 'path';

    const spans = node?.localName === 'text' ? [...node.querySelectorAll('tspan')] : [];
    const multiline = spans.length > 1 && spans.slice(1).every((span) => span.hasAttribute('dy') || (span.hasAttribute('y') && span.getAttribute('y') !== spans[0].getAttribute('y')));
    $('text').value = node?.localName === 'text' ? (multiline ? spans.map((span) => span.textContent).join('\n') : node.textContent) : '';
    $('fill').value = node ? getComputedStyle(node).fill : '';
    $('stroke').value = node ? getComputedStyle(node).stroke : '';
    $('width').value = node ? parseFloat(getComputedStyle(node).strokeWidth) || 0 : 0;
    paintSelection();
  };
  const refreshLayers = () => {
    $('layers').replaceChildren(new Option('Select a layer…', ''));
    for (const [i, node] of layers().entries()) {
      let depth = 0;
      for (let p = node.parentElement; p && p !== root; p = p.parentElement) depth++;
      const label = node.getAttribute('data-name') || node.getAttribute('id') || (node.localName === 'text' ? node.textContent.slice(0, 45) : node.localName);
      $('layers').add(new Option(`${'  '.repeat(depth)}${label}`, String(i)));
    }
    select(null);
  };
  const mount = (node) => {
    root = document.importNode(node, true);
    sheet.replaceChildren(root);
    sheet.setAttribute('viewBox', root.getAttribute('viewBox'));
    // A nested SVG keeps its original root styles and physical export dimensions.
    const [x, y, w, h] = root.getAttribute('viewBox').split(/[\s,]+/).map(Number);
    root.setAttribute('x', x);
    root.setAttribute('y', y);
    root.setAttribute('width', w);
    root.setAttribute('height', h);
    refreshLayers();
  };
  const move = (dx, dy, original = selected?.getAttribute('transform') || '') => {
    selected.setAttribute('transform', `translate(${dx} ${dy}) ${original}`.trim());
    paintSelection();
  };
  $('layers').onchange = () => select($('layers').value === '' ? null : layers()[+$('layers').value]);
  $('inside').onclick = () => {
    const child = selected?.querySelector(EDITABLE);
    if (child) select(child);
  };
  $('parent').onclick = () => {
    const parent = selected?.parentElement;
    if (parent && parent !== root) select(parent);
  };
  $('separate').onclick = () => {
    if (selected?.localName !== 'path') return;
    const style = getComputedStyle(selected);
    if (style.fill !== 'none' && style.fill !== 'transparent' && style.fillOpacity !== '0') {
      return status('This is a filled shape. Separating its contours could change holes and lettering. Choose an existing child layer, or export separate parts from Figma.');
    }
    let contours;
    try { contours = splitPathData(selected.getAttribute('d') || ''); }
    catch (err) { return status(err.message); }
    if (contours.length < 2) return status('This path has one connected contour. Select another layer to find a separate part.');
    snapshot();
    const group = document.createElementNS(NS, 'g');
    const label = selected.id || 'Separated path';
    // Keep transforms, clips, inherited styles, and the original layer identity.
    for (const attr of selected.attributes) if (attr.name !== 'd') group.setAttributeNS(attr.namespaceURI, attr.name, attr.value);
    contours.forEach((data, i) => {
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', data);
      path.setAttribute('data-name', label + ' / contour ' + (i + 1));
      group.appendChild(path);
    });
    selected.replaceWith(group);
    refreshLayers();
    select(group.firstElementChild);
    status('Separated ' + contours.length + ' contours. Each can now move independently. Undo restores the combined path.');
  };
  $('text').onchange = () => {
    if (selected?.localName !== 'text') return;
    snapshot();
    // Keep tspan layout when changing a single line. Multiline text gets explicit lines.
    const spans = [...selected.querySelectorAll('tspan')];
    const lines = $('text').value.split('\n');
    if (spans.length === lines.length && spans.length) spans.forEach((span, i) => { span.textContent = lines[i]; });
    else if (spans.length && lines.length === 1) {
      spans[0].textContent = lines[0];
      spans.slice(1).forEach((span) => span.remove());
    } else {
      const x = selected.getAttribute('x') || spans[0]?.getAttribute('x') || '0';
      const y = selected.getAttribute('y') || spans[0]?.getAttribute('y') || '0';
      const size = parseFloat(getComputedStyle(selected).fontSize) || 12;
      selected.replaceChildren();
      lines.forEach((line, i) => {
        const span = document.createElementNS(NS, 'tspan');
        span.setAttribute('x', x);
        if (i === 0) span.setAttribute('y', y);
        else span.setAttribute('dy', size * 1.2);
        span.textContent = line;
        selected.appendChild(span);
      });
    }
    paintSelection();
  };
  for (const prop of ['fill', 'stroke', 'width']) $(prop).onchange = () => {
    if (!selected) return;
    const key = prop === 'width' ? 'stroke-width' : prop;
    const value = $(prop).value.trim();
    if (prop !== 'width' && !CSS.supports(key, value)) return status('Enter a color such as #ffffff or none.');
    snapshot();
    selected.style.setProperty(key, value);
    paintSelection();
  };
  $('delete').onclick = () => {
    if (!selected) return;
    snapshot();
    selected.remove();
    refreshLayers();
  };
  $('undo').onclick = () => {
    if (!history.length) return;
    mount(parseArtwork(history.pop()));
    $('undo').disabled = !history.length;
  };
  sheet.addEventListener('pointerdown', (e) => {
    if (!root) return;
    e.stopImmediatePropagation();
    const target = e.target.closest(EDITABLE);
    if (!target || !root.contains(target)) return select(null);
    select(target);
    const matrix = target.parentNode.getScreenCTM().inverse();
    const point = new DOMPoint(e.clientX, e.clientY).matrixTransform(matrix);
    drag = { point, matrix, original: target.getAttribute('transform') || '', changed: false };
    sheet.setPointerCapture(e.pointerId);
    sheet.focus();
  }, true);
  sheet.addEventListener('pointermove', (e) => {
    if (!root) return;
    e.stopImmediatePropagation();
    if (!drag) return;
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(drag.matrix);
    if (!drag.changed && Math.hypot(p.x - drag.point.x, p.y - drag.point.y) < 0.2) return;
    if (!drag.changed) snapshot();
    drag.changed = true;
    move(p.x - drag.point.x, p.y - drag.point.y, drag.original);
  }, true);
  for (const event of ['pointerup', 'pointercancel']) sheet.addEventListener(event, (e) => {
    if (!root) return;
    e.stopImmediatePropagation();
    drag = null;
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!root || /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) return;
    e.stopImmediatePropagation();
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); $('undo').click(); return; }
    if (!selected) return;
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); $('delete').click(); return; }
    const delta = { ArrowLeft: [-1,0], ArrowRight: [1,0], ArrowUp: [0,-1], ArrowDown: [0,1] }[e.key];
    if (!delta) return;
    e.preventDefault();
    snapshot();
    move(delta[0] * (e.shiftKey ? 10 : 1), delta[1] * (e.shiftKey ? 10 : 1));
  }, true);
  return {
    get active() { return !!root; },
    get name() { return name; },
    async open(file) {
      let source = await file.text();
      if (/\.json$/i.test(file.name)) source = pluginArtwork(JSON.parse(source), document);
      const node = parseArtwork(source);
      // Validate everything before replacing the current document.
      onOpen();
      width = node.getAttribute('width');
      height = node.getAttribute('height');
      name = file.name.replace(/\.(svg|json)$/i, '');
      history.length = 0;
      mount(node);
      $('undo').disabled = true;
      status(node.querySelector('#drawing, #parts') ? 'Opened Gunmat drawing. Select a layer or click the artwork to edit.' : 'Opened SVG artwork. Select a layer or click the artwork to edit.');
    },
    clear() { root = null; selected = null; overlay = null; drag = null; history.length = 0; },
    serialize() {
      const out = root.cloneNode(true);
      out.removeAttribute('x');
      out.removeAttribute('y');
      const [, , w, h] = out.getAttribute('viewBox').split(/[\s,]+/).map(Number);
      out.setAttribute('width', width || w);
      out.setAttribute('height', height || h);
      return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml(out);
    },
    dimensions() {
      const [, , w, h] = root.getAttribute('viewBox').split(/[\s,]+/).map(Number);
      return [svgPixels(width, w), svgPixels(height, h)];
    },
  };
}
