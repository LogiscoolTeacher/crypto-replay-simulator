// Fair value gaps as translucent boxes on the price pane (Lightweight Charts primitive).
// A gap runs from its middle candle to the candle that filled it, or to the right edge while open.
import { xForTime } from './fib.js';

const BULL = 'rgba(12,163,12,0.20)';
const BEAR = 'rgba(208,59,59,0.20)';
const BULL_EDGE = 'rgba(12,163,12,0.55)';
const BEAR_EDGE = 'rgba(208,59,59,0.55)';
const FILLED = 'rgba(149,147,140,0.10)';
const FILLED_EDGE = 'rgba(149,147,140,0.35)';
const LABEL_FONT = '10px system-ui, -apple-system, "Segoe UI", sans-serif';

export class FvgPrimitive {
  constructor(fmtPrice) {
    this.fmtPrice = fmtPrice;
    this.gaps = [];          // [{ dir, t_start_ms, t_end_ms|null, top, bottom, filled }]
    this.hideFilled = false;
    this._views = [new FvgPaneView(this)];
    this._attached = null;
  }
  attached(params) { this._attached = params; }
  detached() { this._attached = null; }
  updateAllViews() {}
  paneViews() { return this._views; }
  requestUpdate() { if (this._attached) this._attached.requestUpdate(); }
}

class FvgPaneView {
  constructor(p) { this.p = p; this._renderer = new FvgRenderer(p); }
  renderer() { return this._renderer; }
  zOrder() { return 'bottom'; }   // behind candles and lines
}

class FvgRenderer {
  constructor(p) { this.p = p; }
  draw(target) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const a = this.p._attached;
      if (!a) return;
      ctx.save();
      ctx.font = LABEL_FONT;
      ctx.textBaseline = 'middle';
      for (const g of this.p.gaps) {
        if (g.filled && this.p.hideFilled) continue;
        const x1 = xForTime(a.chart, g.t_start_ms, mediaSize.width);
        if (x1 === null) continue;
        const x2 = g.t_end_ms ? xForTime(a.chart, g.t_end_ms, mediaSize.width) : mediaSize.width;
        if (x2 === null || x2 <= x1) continue;
        const yTop = a.series.priceToCoordinate(g.top), yBot = a.series.priceToCoordinate(g.bottom);
        if (yTop === null || yBot === null) continue;
        const h = Math.max(1, yBot - yTop);
        ctx.fillStyle = g.filled ? FILLED : g.dir === 'bull' ? BULL : BEAR;
        ctx.strokeStyle = g.filled ? FILLED_EDGE : g.dir === 'bull' ? BULL_EDGE : BEAR_EDGE;
        ctx.lineWidth = 1;
        ctx.fillRect(x1, yTop, x2 - x1, h);
        ctx.strokeRect(x1 + 0.5, yTop + 0.5, x2 - x1 - 1, h - 1);
        if (h >= 11 && x2 - x1 > 40) {
          ctx.fillStyle = g.filled ? 'rgba(149,147,140,0.9)' : g.dir === 'bull' ? 'rgba(90,200,90,0.95)' : 'rgba(240,120,120,0.95)';
          ctx.fillText(`FVG ${g.dir === 'bull' ? '▲' : '▼'} ${this.p.fmtPrice(g.bottom)}–${this.p.fmtPrice(g.top)}`, x1 + 4, yTop + h / 2);
        }
      }
      ctx.restore();
    });
  }
}
