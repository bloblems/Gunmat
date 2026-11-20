// Builds a mat from the app's .figma.json file: live text, auto layout
// frames for the text column, parts list and logos, line art as vectors.

figma.showUI(__html__, { width: 300, height: 140 });

figma.ui.onmessage = async (msg) => {
  if (msg.type !== 'build') return;
  try {
    const mat = await build(msg.data);
    figma.currentPage.selection = [mat];
    figma.viewport.scrollAndZoomIntoView([mat]);
    figma.closePlugin('Mat added');
  } catch (e) {
    figma.ui.postMessage({ error: String((e && e.message) || e) });
  }
};

function rgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255 };
}

async function build(d) {
  const K = d.px; // Figma px per mm
  const ink = [{ type: 'SOLID', color: rgb(d.fg) }];
  const fonts = {};

  async function useFont(f) {
    const key = f.family + '/' + f.style;
    if (!fonts[key]) {
      try {
        await figma.loadFontAsync(f);
        fonts[key] = f;
      } catch (e) {
        const alt = { family: 'Inter', style: /bold/i.test(f.style) ? 'Bold' : 'Regular' };
        await figma.loadFontAsync(alt);
        fonts[key] = alt;
      }
    }
    return fonts[key];
  }

  async function text(str, f, size, o) {
    o = o || {};
    const t = figma.createText();
    t.fontName = await useFont(f);
    t.fontSize = size * K;
    t.characters = str || ' ';
    t.fills = ink;
    if (o.lineHeight) t.lineHeight = { value: o.lineHeight * K, unit: 'PIXELS' };
    if (o.upper) t.textCase = 'UPPER';
    if (o.align) t.textAlignHorizontal = o.align;
    if (o.width) {
      t.textAutoResize = 'HEIGHT';
      t.resize(o.width * K, Math.max(1, t.height));
    } else t.textAutoResize = 'WIDTH_AND_HEIGHT';
    t.name = o.name || str.slice(0, 40);
    return t;
  }

  function vector(svg, name) {
    const n = figma.createNodeFromSvg(svg);
    n.name = name;
    n.fills = [];
    n.clipsContent = false;
    return n;
  }

  function stack(name, dir, spacing) {
    const f = figma.createFrame();
    f.name = name;
    f.layoutMode = dir;
    f.itemSpacing = (spacing || 0) * K;
    f.primaryAxisSizingMode = 'AUTO';
    f.counterAxisSizingMode = 'AUTO';
    f.fills = [];
    f.clipsContent = false;
    return f;
  }

  const mat = figma.createFrame();
  mat.name = d.name || 'Mat';
  mat.resize(d.W * K, d.H * K);
  mat.fills = [{ type: 'SOLID', color: rgb(d.bg) }];
  mat.clipsContent = true;
  const c = figma.viewport.center;
  mat.x = Math.round(c.x - (d.W * K) / 2);
  mat.y = Math.round(c.y - (d.H * K) / 2);

  const place = (node, x, y) => {
    mat.appendChild(node);
    node.x = x;
    node.y = y;
    return node;
  };

  // line art
  if (d.drawing) place(vector(d.drawing, 'Exploded view'), 0, 0);
  if (d.assembled) place(vector(d.assembled, 'Assembled view'), 0, 0);

  // part numbers: leader lines + live numbers
  if (d.numbers) {
    const nodes = [];
    if (d.leaders) nodes.push(place(vector(d.leaders, 'Leaders'), 0, 0));
    for (const it of d.numbers.items) {
      const t = await text(it.label, d.numbers.font, d.numbers.size, { align: 'CENTER', name: it.label });
      place(t, it.x * K - t.width / 2, it.y * K - t.height / 2);
      nodes.push(t);
    }
    if (nodes.length) figma.group(nodes, mat).name = 'Part numbers';
  }

  // text column: one vertical auto layout frame; spacing kept as padding under each block
  const col = stack('Text', 'VERTICAL', 0);
  col.counterAxisSizingMode = 'FIXED';
  col.resize(d.textW * K, 10);
  const blocks = d.blocks.filter((b) => b.field !== 'tagline');
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    const next = blocks[i + 1];
    const wrap = stack(b.kind === 'specs' ? 'Specs' : b.field[0].toUpperCase() + b.field.slice(1), 'VERTICAL', 0);
    wrap.layoutAlign = 'STRETCH';
    wrap.counterAxisSizingMode = 'FIXED';
    wrap.paddingBottom = next ? Math.max(0, (next.y - b.y - b.height) * K) : 0;
    let t;
    if (b.kind === 'specs') {
      const lines = b.rows.map((r) => (r.key ? r.key + ' ' + r.value : r.value));
      t = await text(lines.join('\n'), b.font, b.size, { lineHeight: b.lineHeight, width: d.textW, name: 'Specs' });
      const bold = await useFont(b.bold);
      let at = 0;
      for (let k = 0; k < b.rows.length; k++) {
        if (b.rows[k].key) t.setRangeFontName(at, at + b.rows[k].key.length, bold);
        at += lines[k].length + 1;
      }
    } else {
      t = await text(b.text, b.font, b.size, { lineHeight: b.lineHeight, upper: b.upper, width: d.textW });
    }
    t.layoutAlign = 'STRETCH';
    wrap.appendChild(t);
    col.appendChild(wrap);
  }
  if (blocks.length) place(col, d.textX * K, blocks[0].y * K);
  else col.remove();

  const tag = d.blocks.find((b) => b.field === 'tagline');
  if (tag) place(await text(tag.text, tag.font, tag.size, { lineHeight: tag.lineHeight, upper: true, width: d.textW, name: 'Tagline' }), d.textX * K, tag.y * K);

  // parts list: columns of rows, all auto layout
  if (d.list) {
    const L = d.list;
    const list = stack('Parts list', 'HORIZONTAL', L.gap);
    for (const colData of L.columns) {
      const column = stack('Column', 'VERTICAL', 0);
      for (const r of colData.rows) {
        const row = stack(r.label + ' ' + r.name, 'HORIZONTAL', 0);
        const n = await text(r.label, L.font, L.size, { lineHeight: L.lineHeight, width: L.indent, name: 'No.' });
        const nm = await text(r.name, L.font, L.size, { lineHeight: L.lineHeight, name: 'Name' });
        row.appendChild(n);
        row.appendChild(nm);
        column.appendChild(row);
      }
      list.appendChild(column);
    }
    place(list, L.x * K, L.y * K);
  }

  // logos: a row in their corner
  if (d.logos) {
    const row = stack('Logos', 'HORIZONTAL', d.logos.gap);
    row.counterAxisAlignItems = d.logos.corner[0] === 't' ? 'MIN' : 'MAX';
    d.logos.items.forEach((it, i) => row.appendChild(vector(it.svg, 'Logo ' + (i + 1))));
    const x0 = Math.min.apply(null, d.logos.items.map((it) => it.x));
    place(row, x0 * K, d.logos.items[0].y * K);
  }
  return mat;
}
