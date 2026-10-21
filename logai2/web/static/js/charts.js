// ECharts helpers (plain globals). The library is vendored: vendor/echarts.min.js.
const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const _chartObservers = new Map(); // element -> ResizeObserver

function chartTokens() {
  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(`--${name}`).trim();
  return {
    text: v('text'), dim: v('text-dim'), muted: v('text-muted'),
    grid: v('chart-grid'), border: v('border'), card: v('bg-card'), accent: v('accent'),
    band: v('chart-band'), line: v('chart-line'), lastweek: v('chart-lastweek'),
    cls: Object.fromEntries(['in', 'up1', 'up2', 'up3', 'down1', 'down2', 'down3'].map(k => [k, v(`chart-${k}`)])
      .concat([['none', v('bg-table')]])),
    seq: [1, 2, 3, 4].map(i => v(`chart-seq${i}`)),
    status: { ok: v('green'), warn: v('orange'), bad: v('red') },
  };
}

function baseOption() {
  const t = chartTokens();
  return {
    animation: !REDUCED_MOTION,
    aria: { enabled: true },
    textStyle: { color: t.dim, fontFamily: getComputedStyle(document.body).fontFamily },
    tooltip: { backgroundColor: t.card, borderColor: t.border, textStyle: { color: t.text } },
  };
}

// Same element -> same instance, so a poll can call setOption without re-creating.
function chartFor(el, renderer = 'canvas') {
  const existing = echarts.getInstanceByDom(el);
  if (existing) return existing;
  const chart = echarts.init(el, null, { renderer });
  const ro = new ResizeObserver(() => chart.resize());
  ro.observe(el);
  _chartObservers.set(el, ro);
  return chart;
}

function disposeCharts() {
  _chartObservers.forEach((ro, el) => { ro.disconnect(); echarts.getInstanceByDom(el)?.dispose(); });
  _chartObservers.clear();
}
