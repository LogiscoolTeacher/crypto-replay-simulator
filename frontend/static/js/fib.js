// Fibonacci retracements drawn on the price pane through the Lightweight Charts primitives API.
// One primitive draws every stored fib, the optional automatic fib and the in-progress draft.
export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const MANUAL_COLOR = '#c98500';   // amber, categorical slot 4
const AUTO_COLOR = '#9085e9';     // violet, categorical slot 7
const LABEL_FONT = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
const HIT_PX = 6;

// Pixel x of a time (ms) on the chart, clamped to the edges when the bar is off screen; null when unknown.
export function xForTime(chart, ms, mediaWidth) {
  const ts = chart.timeScale();
  const range = ts.getVisibleRange();
  if (!range) return null;
  const t = Math.floor(ms / 1000);
  const x = ts.timeToCoordinate(t);
  if (x !== null) return x;
  if (t < range.from) return 0;
  if (t > range.to) return mediaWidth;
  const lo = ts.timeToCoordinate(range.from), hi = ts.timeToCoordinate(range.to);
  if (lo === null || hi === null) return null;
  return lo + (hi - lo) * ((t - range.from) / (range.to - range.from || 1));
}

export function levelsOf(p1, p2) {
  return FIB_LEVELS.map(r => ({ ratio: r, price: p2 + (p1 - p2) * r }));
}

export class FibPrimitive {
  constructor(fmtPrice) {
    this.fmtPrice = fmtPrice;
    this.fibs = [];         // stored fibs: { id, t1_ms, p1, t2_ms, p2, levels }
    this.auto = null;       // automatic fib payload or null
    this.draft = null;      // { t_ms, price } after the first click
    this.selectedId = null;
    this._views = [new FibPaneView(this)];
    this._attached = null;
  }

  attached(params) { this._attached = params; }
  detached() { this._attached = null; }
  updateAllViews() {}
  paneViews() { return this._views; }
  requestUpdate() { if (this._attached) this._attached.requestUpdate(); }

  // Pixel geometry of one fib on the current view, or null when nothing of it is on screen.
  geometry(fib, mediaWidth) {
    const a = this._attached;
    if (!a) return null;
    const xOf = (ms) => xForTime(a.chart, ms, mediaWidth);
    const x1 = xOf(fib.t1_ms), x2 = xOf(fib.t2_ms);
    if (x1 === null || x2 === null) return null;
    const y1 = a.series.priceToCoordinate(fib.p1), y2 = a.series.priceToCoordinate(fib.p2);
    if (y1 === null || y2 === null) return null;
    const levels = (fib.levels || levelsOf(fib.p1, fib.p2)).map(l => ({ ...l, y: a.series.priceToCoordinate(l.price) }));
    return { x1, y1, x2, y2, xStart: Math.min(x1, x2), levels };
  }

  // The stored fib whose level line passes within a few pixels of (x, y), or null.
  hitTest(x, y, mediaWidth) {
    for (let i = this.fibs.length - 1; i >= 0; i--) {
      const g = this.geometry(this.fibs[i], mediaWidth);
      if (!g || x < g.xStart - HIT_PX) continue;
      if (g.levels.some(l => l.y !== null && Math.abs(l.y - y) <= HIT_PX)) return this.fibs[i];
    }
    return null;
  }
}

class FibPaneView {
  constructor(p) { this.p = p; this._renderer = new FibRenderer(p); }
  renderer() { return this._renderer; }
  zOrder() { return 'normal'; }
}

class FibRenderer {
  constructor(p) { this.p = p; }
  fmt(v) { return this.p.fmtPrice(v); }

  draw(target) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const p = this.p;
      ctx.save();
      ctx.font = LABEL_FONT;
      ctx.textBaseline = 'bottom';
      for (const fib of p.fibs) this.drawFib(ctx, mediaSize, fib, MANUAL_COLOR, fib.id === p.selectedId, false);
      if (p.auto) this.drawFib(ctx, mediaSize, p.auto, AUTO_COLOR, false, true);
      if (p.draft) this.drawDraft(ctx, mediaSize, p.draft);
      ctx.restore();
    });
  }

  drawFib(ctx, size, fib, color, selected, dashed) {
    const g = this.p.geometry(fib, size.width);
    if (!g) return;
    const right = size.width;
    ctx.lineWidth = selected ? 2 : 1;
    // Trend line between the anchors.
    ctx.strokeStyle = color;
    ctx.globalAlpha = 0.55;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(g.x1, g.y1); ctx.lineTo(g.x2, g.y2); ctx.stroke();
    // Level lines from the swing start to the right edge, each with "ratio price" at the right.
    ctx.setLineDash(dashed ? [6, 4] : []);
    for (const l of g.levels) {
      if (l.y === null) continue;
      const key = l.ratio === 0 || l.ratio === 1;
      ctx.globalAlpha = key ? 0.9 : 0.65;
      ctx.beginPath(); ctx.moveTo(g.xStart, l.y); ctx.lineTo(right, l.y); ctx.stroke();
      const text = `${(l.ratio * 100).toFixed(1)}%  ${this.fmt(l.price)}`;
      const w = ctx.measureText(text).width;
      ctx.globalAlpha = 0.85;
      ctx.fillStyle = 'rgba(20,20,20,0.75)';
      ctx.fillRect(right - w - 8, l.y - 14, w + 6, 13);
      ctx.fillStyle = color;
      ctx.fillText(text, right - w - 5, l.y - 2);
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    // Anchor dots.
    ctx.fillStyle = color;
    for (const [x, y] of [[g.x1, g.y1], [g.x2, g.y2]]) { ctx.beginPath(); ctx.arc(x, y, selected ? 4 : 3, 0, Math.PI * 2); ctx.fill(); }
  }

  drawDraft(ctx, size, d) {
    const a = this.p._attached;
    if (!a) return;
    const y = a.series.priceToCoordinate(d.price);
    const x = a.chart.timeScale().timeToCoordinate(Math.floor(d.t_ms / 1000));
    if (y === null) return;
    ctx.strokeStyle = MANUAL_COLOR;
    ctx.fillStyle = MANUAL_COLOR;
    ctx.globalAlpha = 0.9;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(size.width, y); ctx.stroke();
    ctx.setLineDash([]);
    if (x !== null) { ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill(); }
    const text = `100%  ${this.fmt(d.price)} — click the second anchor`;
    ctx.fillText(text, 8, y - 3);
    ctx.globalAlpha = 1;
  }
}
