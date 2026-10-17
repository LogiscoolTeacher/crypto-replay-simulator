// The planned target and stop of a buy, shown while the mouse is over the Buy button: a green
// hatched area from the current price up to the target and a red one down to the stop, from the
// current candle to the right edge of the chart (Lightweight Charts primitive).
const TP_FILL = 'rgba(12,163,12,0.14)';
const TP_HATCH = 'rgba(12,163,12,0.35)';
const TP_TEXT = 'rgba(90,200,90,0.95)';
const SL_FILL = 'rgba(208,59,59,0.14)';
const SL_HATCH = 'rgba(208,59,59,0.35)';
const SL_TEXT = 'rgba(240,120,120,0.95)';
const LABEL_FONT = 'bold 11px system-ui, -apple-system, "Segoe UI", sans-serif';
const HATCH_STEP = 9;

export class PlanPrimitive {
  constructor() {
    this.plan = null;   // { price, tp, sl, tpPct, slPct, time } or null; tp/sl may be null
    this._views = [new PlanPaneView(this)];
    this._attached = null;
  }
  attached(params) { this._attached = params; }
  detached() { this._attached = null; }
  updateAllViews() {}
  paneViews() { return this._views; }
  requestUpdate() { if (this._attached) this._attached.requestUpdate(); }
}

class PlanPaneView {
  constructor(p) { this.p = p; this._renderer = new PlanRenderer(p); }
  renderer() { return this._renderer; }
  zOrder() { return 'bottom'; }
}

class PlanRenderer {
  constructor(p) { this.p = p; }
  draw(target) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const a = this.p._attached, plan = this.p.plan;
      if (!a || !plan) return;
      const yPrice = a.series.priceToCoordinate(plan.price);
      if (yPrice === null) return;
      let x0 = plan.time !== null && plan.time !== undefined ? a.chart.timeScale().timeToCoordinate(plan.time) : null;
      if (x0 === null || x0 === undefined || !Number.isFinite(x0)) x0 = 0;
      const w = mediaSize.width - x0;
      if (w <= 0) return;
      ctx.save();
      ctx.font = LABEL_FONT;
      ctx.textAlign = 'right';
      const fmt = (v) => v >= 1000 ? v.toFixed(2) : v >= 1 ? v.toFixed(4) : v.toFixed(6);
      const draw = (level, fill, hatch, text, label, above) => {
        if (level === null || level === undefined) return;
        let y = a.series.priceToCoordinate(level);
        if (y === null) y = above ? -1 : mediaSize.height + 1;   // off the pane: fill to the edge
        const top = Math.min(y, yPrice), h = Math.abs(y - yPrice);
        if (h < 1) return;
        ctx.save();
        ctx.beginPath(); ctx.rect(x0, top, w, h); ctx.clip();
        ctx.fillStyle = fill; ctx.fillRect(x0, top, w, h);
        ctx.strokeStyle = hatch; ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = x0 - h; x < x0 + w; x += HATCH_STEP) { ctx.moveTo(x, top + h); ctx.lineTo(x + h, top); }
        ctx.stroke();
        ctx.restore();
        ctx.strokeStyle = hatch;
        ctx.beginPath(); ctx.moveTo(x0, Math.round(y) + 0.5); ctx.lineTo(mediaSize.width, Math.round(y) + 0.5); ctx.stroke();
        ctx.fillStyle = text;
        ctx.textBaseline = above ? 'bottom' : 'top';
        const ly = above ? Math.max(12, y - 3) : Math.min(mediaSize.height - 12, y + 3);
        ctx.fillText(label, mediaSize.width - 8, ly);
      };
      draw(plan.tp, TP_FILL, TP_HATCH, TP_TEXT, `Target +${plan.tpPct}% → ${fmt(plan.tp || 0)}`, true);
      draw(plan.sl, SL_FILL, SL_HATCH, SL_TEXT, `Stop −${plan.slPct}% → ${fmt(plan.sl || 0)}`, false);
      ctx.restore();
    });
  }
}
