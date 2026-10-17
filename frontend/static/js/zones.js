// Support / resistance zones ("tubes") as translucent bands across the whole price pane
// (Lightweight Charts primitive), labelled with the touch count and the distance to the band.
const RES_FILL = 'rgba(208,59,59,0.16)';
const RES_EDGE = 'rgba(208,59,59,0.6)';
const SUP_FILL = 'rgba(12,163,12,0.16)';
const SUP_EDGE = 'rgba(12,163,12,0.6)';
const RES_TEXT = 'rgba(240,120,120,0.95)';
const SUP_TEXT = 'rgba(90,200,90,0.95)';
const LABEL_FONT = 'bold 11px system-ui, -apple-system, "Segoe UI", sans-serif';

export class ZonePrimitive {
  constructor() {
    this.zones = [];   // [{ kind: 'resistance'|'support', low, high, label }]
    this._views = [new ZonePaneView(this)];
    this._attached = null;
  }
  attached(params) { this._attached = params; }
  detached() { this._attached = null; }
  updateAllViews() {}
  paneViews() { return this._views; }
  requestUpdate() { if (this._attached) this._attached.requestUpdate(); }
}

class ZonePaneView {
  constructor(p) { this.p = p; this._renderer = new ZoneRenderer(p); }
  renderer() { return this._renderer; }
  zOrder() { return 'bottom'; }   // behind candles and lines
}

class ZoneRenderer {
  constructor(p) { this.p = p; }
  draw(target) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const a = this.p._attached;
      if (!a) return;
      ctx.save();
      ctx.font = LABEL_FONT;
      ctx.textBaseline = 'middle';
      for (const z of this.p.zones) {
        const yTop = a.series.priceToCoordinate(z.high), yBot = a.series.priceToCoordinate(z.low);
        if (yTop === null || yBot === null) continue;
        const h = Math.max(2, yBot - yTop);
        const res = z.kind === 'resistance';
        ctx.fillStyle = res ? RES_FILL : SUP_FILL;
        ctx.strokeStyle = res ? RES_EDGE : SUP_EDGE;
        ctx.lineWidth = 1;
        ctx.fillRect(0, yTop, mediaSize.width, h);
        ctx.beginPath();
        ctx.moveTo(0, yTop + 0.5); ctx.lineTo(mediaSize.width, yTop + 0.5);
        ctx.moveTo(0, yBot - 0.5); ctx.lineTo(mediaSize.width, yBot - 0.5);
        ctx.stroke();
        // The label sits inside the band when it fits, else just above (resistance) / below (support) it.
        const y = h >= 14 ? yTop + h / 2 : res ? yTop - 8 : yBot + 8;
        ctx.fillStyle = res ? RES_TEXT : SUP_TEXT;
        ctx.fillText(z.label, 6, y);
      }
      ctx.restore();
    });
  }
}
