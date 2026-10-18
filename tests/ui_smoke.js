// 无头冒烟测试：桩 DOM + 桩 Canvas 跑真实前端脚本，验证加载/建列表/渲染/闪光组
// 用法: node tests/ui_smoke.js <抽出的 app.js> [baseUrl]
const fs = require('fs');
const vm = require('vm');

const APP = process.argv[2];
const BASE = process.argv[3] || 'http://127.0.0.1:8790';
if (!APP || !fs.existsSync(APP)) {
  console.error('用法: node tests/ui_smoke.js <app.js> [baseUrl]');
  process.exit(2);
}

const ops = {};
const compOps = [];
const bump = (k) => { ops[k] = (ops[k] || 0) + 1; };

function mkCtx() {
  const st = {
    globalCompositeOperation: 'source-over', globalAlpha: 1,
    fillStyle: '#000', strokeStyle: '#000', lineWidth: 1,
    lineCap: 'butt', lineJoin: 'miter', font: '10px sans-serif',
    textAlign: 'left', textBaseline: 'alphabetic',
  };
  st.canvas = { width: 800, height: 600 };
  return new Proxy(st, {
    get(t, k) {
      if (k in t) return t[k];
      if (typeof k === 'symbol') return undefined;
      if (k === 'measureText') return () => ({ width: 8 });
      if (k === 'getImageData') return (x, y, w, h) => ({ data: new Uint8ClampedArray(4 * w * h) });
      if (k === 'createLinearGradient' || k === 'createRadialGradient')
        return () => ({ addColorStop() { } });
      return () => { bump(String(k)); };
    },
    set(t, k, v) {
      if (k === 'globalCompositeOperation') compOps.push(v);
      t[k] = v;
      return true;
    },
  });
}

function mkEl(tag) {
  const base = {
    tagName: String(tag || 'div').toUpperCase(),
    style: {}, dataset: {}, children: [], classList: {
      add() { }, remove() { }, toggle() { }, contains() { return false; },
    },
    innerHTML: '', textContent: '', value: '', checked: false, disabled: false,
    width: 800, height: 600, offsetWidth: 800, offsetHeight: 800,
    clientWidth: 800, clientHeight: 600, scrollTop: 0, scrollHeight: 600,
    appendChild(c) { this.children.push(c); return c; },
    removeChild() { }, remove() { }, insertBefore() { }, replaceChildren() { },
    addEventListener() { }, removeEventListener() { }, dispatchEvent() { },
    setAttribute() { }, getAttribute() { return null; }, removeAttribute() { },
    insertAdjacentHTML() { }, focus() { }, blur() { }, click() { },
    querySelector(sel) {
      // innerHTML 赋值后真实浏览器能查到子元素；桩里给个占位元素以便事件绑定代码继续执行
      this._qs = this._qs || {};
      if (!this._qs[sel]) this._qs[sel] = mkEl('input');
      return this._qs[sel];
    },
    querySelectorAll() { return []; },
    closest() { return null; }, contains() { return false; },    getBoundingClientRect() { return { left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600 }; },
    getContext() { return mkCtx(); },
    toBlob(cb) { cb && cb(null); }, toDataURL() { return 'data:image/png;base64,'; },
  };
  return new Proxy(base, {
    get(t, k) { if (k in t) return t[k]; if (typeof k === 'symbol') return undefined; return () => undefined; },
    set(t, k, v) { t[k] = v; return true; },
  });
}

const cache = new Map();
globalThis.document = {
  getElementById(id) {
    if (!cache.has(id)) cache.set(id, mkEl(id === 'cv' ? 'canvas' : 'div'));
    return cache.get(id);
  },
  createElement: (t) => mkEl(t),
  createElementNS: (n, t) => mkEl(t),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() { },
  body: mkEl('body'), documentElement: mkEl('html'), head: mkEl('head'),
};
globalThis.window = {
  addEventListener() { }, removeEventListener() { },
  devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800,
  requestAnimationFrame: (f) => setTimeout(f, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
  getComputedStyle: () => ({ getPropertyValue: () => '#8899aa' }),
  matchMedia: () => ({ matches: false, addEventListener() { } }),
};
globalThis.ResizeObserver = class { observe() { } unobserve() { } disconnect() { } };
globalThis.requestAnimationFrame = globalThis.window.requestAnimationFrame;
globalThis.getComputedStyle = globalThis.window.getComputedStyle;
globalThis.Blob = class { constructor(p, o) { this.parts = p; this.type = (o || {}).type || ''; } };
globalThis.location = { href: '' };

const _fetch = globalThis.fetch;
globalThis.fetch = (u, o) => _fetch(String(u).startsWith('http') ? u : BASE + u, o);

const errors = [];
process.on('unhandledRejection', (e) => errors.push('rejection: ' + ((e && e.message) || e)));

let code = fs.readFileSync(APP, 'utf8');
if (/\.html?$/i.test(APP)) {
  // 直接喂 index.html：抽出最后一个 <script> 块
  const m = code.match(/<script[^>]*>([\s\S]*?)<\/script>/g);
  if (!m || !m.length) {
    console.error('未在 HTML 中找到 <script> 块');
    process.exit(2);
  }
  code = m[m.length - 1].replace(/^<script[^>]*>/, '').replace(/<\/script>$/, '');
}
code += '\nglobalThis.__S = S;';
code += '\nglobalThis.__f = {buildLayerList, buildDrillBox, buildAnalysis, buildNetBox,'
  + ' fitView, render, drawObj, api, loading, status, loadSample,'
  + ' pickerOpen, pickerClose, pickGo, renderPickList, selectRow, pickLoad,'
  + ' gotoXY, doGoto, setGotoInputs, updateGotoInfo, centerOn,'
  + ' setBoardComps, buildCompBox, applyXY, refreshXY, compRecords, currentComps,'
  + ' drawComps, drawTarget, exportCompsCsv, pickLoadXY,'
  + ' alignRow, bindAlignRow, reapplyXY,'
  + ' uval, ulen, udual, udualShort, uhead, setUnit, coordText, UDEF};\n';
code += '\nglobalThis.__P = P;\n';

try {
  vm.runInThisContext(code, { filename: APP });
} catch (e) {
  console.log('BOOT ERROR: ' + (e && e.stack || e));
  process.exit(2);
}

const results = { errors, steps: [] };
const step = async (name, fn) => {
  try { await fn(); results.steps.push([name, 'ok']); return true; }
  catch (e) { results.steps.push([name, 'FAIL', String((e && e.message) || e)]); return false; }
};

setTimeout(async () => {
  const S = globalThis.__S || {};
  const f = globalThis.__f || {};

  const want = process.env.PCB_SAMPLE;
  if (want && f.loadSample) {
    try { await f.loadSample(want); results.steps.push(['loadSample:' + want, 'ok']); }
    catch (e) { results.steps.push(['loadSample:' + want, 'FAIL', String((e && e.message) || e)]); }
  }

  results.loaded = !!S.summary;
  if (!S.summary) {
    results.steps.push(['load', 'FAIL', 'S.summary 为空，示例未加载']);
    console.log(JSON.stringify(results, null, 2));
    process.exit(1);
  }
  const s = S.summary;
  results.board = { name: s.name, format: s.format, layers: s.layers.length, bbox: s.bbox };
  results.drills = s.drills ? { holes: s.drills.holes.length, tools: s.drills.tools.length } : null;
  results.netlists = (s.netlists || []).length;
  results.analysis = !!(s.analysis && 'min_trace_width' in s.analysis);
  results.opacity = s.layers.map((l) => l.name + '=' + l.opacity);

  for (const n of ['buildLayerList', 'buildDrillBox', 'buildAnalysis', 'buildNetBox', 'fitView', 'render']) {
    await step(n, () => { if (!f[n]) throw new Error('missing'); f[n](); });
  }

  // 复刻 applySummary 的逐层拉取，确认真实渲染路径
  for (let i = 0; i < s.layers.length; i++) {
    try {
      const d = await f.api('/api/layer?i=' + i);
      S.objs[i] = d.objects || [];
    } catch (e) {
      S.objs[i] = [];
      results.steps.push(['layer' + i, 'FAIL', String((e && e.message) || e)]);
    }
  }
  results.objSizes = S.objs.map((o) => (o ? o.length : null));
  results.objTotal = S.objs.reduce((a, o) => a + ((o && o.length) || 0), 0);

  const before = { fill: ops.fill || 0, stroke: ops.stroke || 0, arc: ops.arc || 0 };
  await step('render-with-geometry', () => f.render());
  results.drawOps = {
    fill: (ops.fill || 0) - before.fill,
    stroke: (ops.stroke || 0) - before.stroke,
    arc: (ops.arc || 0) - before.arc,
    drawImage: ops.drawImage || 0,
  };
  results.compositeModes = compOps.reduce((a, v) => { a[v] = (a[v] || 0) + 1; return a; }, {});

  // 闪光组（光圈宏负曝光）渲染分支
  const g0 = ops.drawImage || 0;
  await step('drawObj-group', () => f.drawObj(mkCtx(), {
    t: 'group', b: [0, 0, 4, 4],
    items: [
      { t: 'circle', c: [2, 2], r: 1.6 },
      { t: 'poly', p: [[2.2, 0], [2.8, 0], [2.8, 4], [2.2, 4]], neg: 1 },
    ],
  }, 1, S.view, 1));
  results.groupDrawImage = (ops.drawImage || 0) - g0;

  // 缩放 / 平移后重绘
  const v0 = { s: S.view.s, ox: S.view.ox, oy: S.view.oy };
  S.view.s *= 2; S.view.ox -= 40; S.view.oy += 25;
  await step('render-after-zoom', () => f.render());
  S.view.s = v0.s; S.view.ox = v0.ox; S.view.oy = v0.oy;

  // 本机浏览对话框：真实走 /api/browse
  const nodePath = require('path');
  const samp = nodePath.join(process.cwd(), 'tests', 'samples');
  const P = globalThis.__P || {rows: [], data: null};
  results.picker = {};
  let pickOk = false;
  try {
    await f.pickGo(samp);
    results.picker.root = {
      path: P.path,
      dirs: P.rows.filter((r) => r.type === 'dir').length,
      files: P.rows.filter((r) => r.type === 'file').length,
      crumbs: (P.data && P.data.crumbs || []).length,
      quick: (P.data && P.data.quick || []).length,
      drives: (P.data && P.data.drives || []).length,
    };
    pickOk = results.picker.root.dirs >= 2 && results.picker.root.files >= 2
      && results.picker.root.crumbs >= 3 && results.picker.root.quick >= 3;
    const odb = P.rows.find((r) => r.type === 'dir' && r.o && r.o.hint === 'odb');
    if (odb) {
      await f.pickGo(odb.path);
      results.picker.enter = { path: P.path, hint: P.data.hint, rows: P.rows.length };
      pickOk = pickOk && P.data.hint === 'odb';
    } else {
      results.picker.enter = 'no odb dir found';
    }
    // 选中态与页脚
    if (P.rows.length) {
      f.selectRow(P.rows[0]);
      results.picker.selected = !!(P.selItem && P.selItem.path);
      pickOk = pickOk && results.picker.selected;
    }
  } catch (e) {
    results.picker.error = String((e && e.message) || e);
  }
  results.steps.push(['picker-browse', pickOk ? 'ok' : 'FAIL']);

  // ---- 坐标定位 / 元件标记 / XY 导入 ----
  const nodePath2 = require('path');
  const xyFile = nodePath2.join(process.cwd(), 'tests', 'samples', 'demo-xy.csv');

  // 1) 手动坐标跳转：输入框 -> doGoto -> 视图居中 + 目标十字线
  results.goto = {};
  await step('goto-input', () => {
    $('gunit').value = '1';
    $('gx').value = '20.000';
    $('gy').value = '10.000';
    f.doGoto();
    if (!S.target) throw new Error('no target');
    const wx = (S.W / 2 - S.view.ox) / S.view.s;
    const wy = (S.view.oy - S.H / 2) / S.view.s;
    results.goto.center = { wx: +wx.toFixed(3), wy: +wy.toFixed(3) };
    if (Math.abs(wx - 20) > 0.05 || Math.abs(wy - 10) > 0.05) throw new Error('not centered');
  });

  // 2) 单位换算（mil）：输入 1000 mil -> 25.4mm
  await step('goto-unit-mil', () => {
    $('gunit').value = '0.0254';
    $('gx').value = '1000';
    $('gy').value = '1000';
    f.doGoto();
    results.goto.mil = { x: +S.target.x.toFixed(4), y: +S.target.y.toFixed(4) };
    if (Math.abs(S.target.x - 25.4) > 1e-3) throw new Error('mil conversion wrong');
    $('gunit').value = '1';
  });

  // 3) 屏幕取点回填 + 目标十字线绘制
  await step('goto-target-draw', () => {
    f.setGotoInputs(5, 5);
    if (Math.abs(parseFloat($('gx').value) - 5) > 1e-6) throw new Error('inputs not filled');
    f.gotoXY(5, 5, { label: 'R1', zoom: 8 });
    const st = ops.stroke || 0;
    f.render();
    if ((ops.stroke || 0) <= st) throw new Error('no draw after goto');
    if (!S.target.label) throw new Error('label lost');
  });

  // 4) 板上器件：注入 -> 面板 -> 十字标记绘制
  await step('board-components', () => {
    f.setBoardComps(S.summary.components || []);
    results.goto.boardComps = (S.boardComps || []).length;
    S.showComp = true;
    const st = ops.stroke || 0;
    f.render();
    const added = (ops.stroke || 0) - st;
    results.goto.compStrokes = added;
    if ((S.boardComps || []).length && added <= 0) throw new Error('comp markers not drawn');
  });

  // 5) 真实导入 XY 并跑一次校验（打 /api/xy）
  await step('xy-import', () => {
    if (!S.summary || S.summary.format !== 'odbpp') {
      results.goto.xy = 'skipped (need odb sample)';
      return;
    }
    return f.pickLoadXY(xyFile).then(() => {
      const c = S.xy && S.xy.check;
      if (!S.xy || !c) throw new Error('xy not applied');
      results.goto.xy = {
        count: S.xy.count, units: S.xy.units, yflip: S.xy.yflip,
        stats: c.stats, maxOffset: c.max_offset,
      };
      const f2 = ops.stroke || 0;
      f.render();
      results.goto.xyStrokes = (ops.stroke || 0) - f2;
      if (!c.total) throw new Error('empty check result');
      if (S.xyComps.length !== S.xy.count) throw new Error('xy comps mismatch');
    });
  });

  // 6) 对齐参数覆盖：强制 Y 镜像后，坐标应翻到板子另一侧
  await step('xy-override-yflip', async () => {
    const before = (S.xyComps && S.xyComps[0]) || null;
    if (!before){ results.goto.override = 'skipped (no xy)'; return; }
    const y0 = before.y;
    await f.reapplyXY({yflip: true, units: 'mm', tol: 0.2});
    const after = (S.xyComps && S.xyComps[0]) || null;
    results.goto.override = {
      yflip: S.xy.yflip,
      y0: +y0.toFixed(3),
      y1: after ? +after.y.toFixed(3) : null,
    };
    if (!after) throw new Error('xy lost after override');
    if (!S.xy.yflip) throw new Error('yflip override not applied');
    if (Math.abs(after.y - y0) < 1e-6) throw new Error('y did not change');
  });

  // 7) 单位切换：公制 mm / 英制 mil、inch / 双单位
  results.units = {};
  await step('units-metric-imperial', () => {
    f.setUnit('mm', true);
    const mm = f.udual(25.4);
    results.units.mm = mm;
    if (!/25\.400 mm/.test(mm)) throw new Error('mm fmt: ' + mm);
    f.setUnit('mil', true);
    const mil = f.udual(25.4);
    results.units.mil = mil;
    if (!/1000\.0 mil/.test(mil)) throw new Error('mil fmt: ' + mil);
    f.setUnit('inch', true);
    const inch = f.udual(25.4);
    results.units.inch = inch;
    if (!/1\.0000 in/.test(inch)) throw new Error('inch fmt: ' + inch);
    f.setUnit('both', true);
    const both = f.udual(25.4);
    results.units.both = both;
    if (!/25\.400 mm \/ 1000\.0 mil/.test(both)) throw new Error('both fmt: ' + both);
  });

  // 8) 单位切换后：底部坐标、钻孔表头、元件坐标列表同步
  await step('units-ui-follow', () => {
    f.setUnit('mil', true);
    const ct = f.coordText(25.4, 0);
    results.units.coords = ct;
    if (!/1000\.0/.test(ct)) throw new Error('coords not in mil: ' + ct);
    f.buildDrillBox();
    const html = $('drillBox').innerHTML;
    if (S.summary.drills && !/孔径\(mil\)/.test(html)) {
      throw new Error('drill header not mil: ' + String(html).slice(0, 140));
    }
    if ((S.boardComps || []).length){
      f.buildCompBox();
      const ch = $('compBox').innerHTML;
      results.units.compBox = String(ch).length;
    }
    f.setUnit('mm', true);
  });

  results.ok = results.steps.every((x) => x[1] === 'ok') && results.objTotal > 0 && errors.length === 0;
  console.log(JSON.stringify(results, null, 2));
  process.exit(results.ok ? 0 : 1);
}, 5000);
