// Shading of the RSI pane (Lightweight Charts series primitive on the first RSI line): a light
// green band under the lower level and a light red band over the upper one, always visible, and
// for every line a stronger fill in the line's own color between the line and the level wherever
// that line is inside one of the zones.
const LO_BAND = 'rgba(12,163,12,0.09)';
const HI_BAND = 'rgba(208,59,59,0.09)';
const FILL_ALPHA = 0.4;
const LABEL_FONT = 'bold 11px system-ui, -apple-system, "Segoe UI", sans-serif';

// '#rrggbb' -> 'rgba(r,g,b,a)'
function withAlpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

export class RsiZonePrimitive {
  constructor(lower = 30, upper = 70) {
    this.lower = lower;
    this.upper = upper;
    this.lines = [];   // [{ label, color, values }]: one entry per bar of the chart, null where there is no RSI yet
    this._views = [new RsiPaneView(this)];
    this._attached = null;
    this.hidden = false;   // the pane is minimized: draw nothing (chart.js _renderPaneFolds)
  }
  attached(params) { this._attached = params; }
  detached() { this._attached = null; }
  updateAllViews() {}
  paneViews() { return this._views; }
  setLines(lines) { this.lines = lines || []; if (this._attached) this._attached.requestUpdate(); }
  setHidden(on) { this.hidden = !!on; if (this._attached) this._attached.requestUpdate(); }
}

class RsiPaneView {
  constructor(p) { this.p = p; this._renderer = new RsiRenderer(p); }
  renderer() { return this._renderer; }
  zOrder() { return 'bottom'; }
}

class RsiRenderer {
  constructor(p) { this.p = p; }
  draw(target) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const a = this.p._attached;
      if (!a || this.p.hidden) return;
      const yLo = a.series.priceToCoordinate(this.p.lower);
      const yHi = a.series.priceToCoordinate(this.p.upper);
      if (yLo === null || yHi === null) return;
      const W = mediaSize.width, H = mediaSize.height;
      ctx.save();
      ctx.fillStyle = HI_BAND; ctx.fillRect(0, 0, W, Math.max(0, yHi));
      ctx.fillStyle = LO_BAND; ctx.fillRect(0, yLo, W, Math.max(0, H - yLo));
      const ts = a.chart.timeScale();
      const range = ts.getVisibleLogicalRange();
      for (const line of this.p.lines) {
        const vals = line.values;
        if (!range || !vals.length) continue;
        const from = Math.max(0, Math.floor(range.from) - 1), to = Math.min(vals.length - 1, Math.ceil(range.to) + 1);
        const color = withAlpha(line.color, FILL_ALPHA);
        // Runs of consecutive bars with a value, as polygons between the line and the level;
        // clipping to the zone leaves exactly the parts where the RSI is inside it.
        const fill = (clipTop, clipH, baseY, color) => {
          if (clipH <= 0) return;
          ctx.save();
          ctx.beginPath(); ctx.rect(0, clipTop, W, clipH); ctx.clip();
          ctx.fillStyle = color;
          let run = [];
          const flush = () => {
            if (run.length) {
              ctx.beginPath();
              ctx.moveTo(run[0][0], baseY);
              for (const [x, y] of run) ctx.lineTo(x, y);
              ctx.lineTo(run[run.length - 1][0], baseY);
              ctx.closePath(); ctx.fill();
            }
            run = [];
          };
          for (let i = from; i <= to; i++) {
            const v = vals[i];
            if (v === null || v === undefined) { flush(); continue; }
            const x = ts.logicalToCoordinate(i), y = a.series.priceToCoordinate(v);
            if (x === null || y === null) { flush(); continue; }
            run.push([x, y]);
          }
          flush();
          ctx.restore();
        };
        fill(yLo, H - yLo, yLo, color);   // below the lower level
        fill(0, yHi, yHi, color);         // above the upper level
      }
      // the lines' names in their colors at the top-left of the pane
      ctx.font = LABEL_FONT; ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      let x = 8;
      for (const line of this.p.lines) {
        ctx.fillStyle = line.color;
        ctx.fillText(line.label, x, 5);
        x += ctx.measureText(line.label).width + 14;
      }
      ctx.restore();
    });
  }
}
