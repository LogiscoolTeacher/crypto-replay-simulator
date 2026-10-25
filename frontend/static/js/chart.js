// Chart view on top of TradingView Lightweight Charts v5: candles + volume in pane 0,
// overlay indicators on the price pane, oscillators in their own panes, and a legend.

const LWC = window.LightweightCharts;

// Categorical series hues in fixed order (validated dark palette). Never cycled: the
// 9th indicator line reuses the last slot and is flagged in the legend.
import { FibPrimitive } from './fib.js';
import { FvgPrimitive } from './fvg.js';
import { ZonePrimitive } from './zones.js';
import { PlanPrimitive } from './plan.js';
import { RsiZonePrimitive } from './rsi.js';

// Short indicator names for the legend box (the full names stay in the Indicators panel).
const SHORT_NAMES = { ema: 'EMA', sma: 'SMA', bbands: 'BB', rsi: 'RSI', macd: 'MACD', atr: 'ATR', vdelta: 'Vol \u0394', vwap: 'VWAP', stoch: 'Stoch', obv: 'OBV' };

export const SERIES_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
export const UP = '#0ca30c';
export const DOWN = '#d03b3b';
const UP_SOFT = 'rgba(12,163,12,0.45)';
const DOWN_SOFT = 'rgba(208,59,59,0.45)';
const INK = '#c3c2b7';
const GRID = '#2c2c2a';
const AXIS = '#383835';
const SURFACE = '#1a1a19';
const PANE_HEIGHT = 140;
const RSI_PANE_HEIGHT = 130;   // the RSI pane of day-trading mode (15m and 1h lines)
const RSI_COLORS = ['#9085e9', '#c98500'];   // one per timeframe, in the order of the request
const RSI_LOWER = 30;
const RSI_UPPER = 70;
const INITIAL_BARS = 160;
const MIN_VISIBLE_BARS = 10;
const MAX_VISIBLE_BARS = 3000;
const ZOOM_STEP = 1.25;

// Stable identity of an indicator instance: key + sorted params. Series and payload
// entries are paired by identity, never by array position, so a response computed for an
// older indicator list can never feed values into the wrong series.
export function instanceIdent(inst) {
  const keys = Object.keys(inst.params || {}).sort();
  return JSON.stringify([inst.key, keys.map(k => [k, inst.params[k]])]);
}

function fmtNum(v, digits) {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  if (digits !== undefined) return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const a = Math.abs(v);
  const d = a >= 1000 ? 2 : a >= 1 ? 4 : 6;
  return v.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: d });
}

export function fmtPrice(v) {
  if (v === null || v === undefined) return '–';
  const a = Math.abs(v);
  const d = a >= 100 ? 2 : a >= 1 ? 4 : 6;
  return v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function fmtTime(unixSeconds, intraday = true, tz = null) {
  if (tz) {
    const parts = zoneParts(unixSeconds, tz);
    const date = `${parts.year}-${parts.month}-${parts.day}`;
    return intraday ? `${date} ${parts.hour}:${parts.minute}` : date;
  }
  const d = new Date(unixSeconds * 1000);
  const p = (n) => String(n).padStart(2, '0');
  const date = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
  return intraday ? `${date} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}` : date;
}

// Calendar parts of a unix time in an IANA zone, zero padded, plus the short month name.
const _zoneFmt = new Map();
function zoneParts(unixSeconds, tz) {
  if (!_zoneFmt.has(tz)) {
    _zoneFmt.set(tz, new Intl.DateTimeFormat('en-US', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    }));
  }
  const out = { month_short: '' };
  for (const part of _zoneFmt.get(tz).formatToParts(new Date(unixSeconds * 1000))) out[part.type] = part.value;
  if (out.hour === '24') out.hour = '00';
  out.month_short = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][parseInt(out.month, 10) - 1];
  return out;
}

// The library's own crosshair label, reproduced for UTC so the swing mode keeps its look.
function defaultCrosshairLabel(unixSeconds, intraday) {
  const d = new Date(unixSeconds * 1000);
  const p = (n) => String(n).padStart(2, '0');
  const mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getUTCMonth()];
  const date = `${d.getUTCDate()} ${mon} '${String(d.getUTCFullYear()).slice(2)}`;
  return intraday ? `${date}  ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}` : date;
}

export class ChartView {
  constructor(el, legendEl) {
    this.el = el;
    this.legendEl = legendEl;
    this.chart = LWC.createChart(el, {
      autoSize: true,
      layout: {
        background: { type: 'solid', color: SURFACE },
        textColor: INK,
        fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
        panes: { separatorColor: AXIS, separatorHoverColor: 'rgba(57,135,229,0.3)', enableResize: true },
      },
      grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      rightPriceScale: { borderColor: AXIS, scaleMargins: { top: 0.14, bottom: 0.22 } },
      timeScale: { borderColor: AXIS, timeVisible: true, secondsVisible: false, rightOffset: 6, barSpacing: 8 },
      localization: { locale: 'en-US', timeFormatter: (t) => this.crosshairLabel(t) },
    });
    this.tz = null;  // IANA zone for axis and legend labels; null = UTC (both modes run on UTC now)
    this.chart.timeScale().applyOptions({ tickMarkFormatter: (t, type) => this.tickLabel(t, type) });
    this.precision = 2;
    this.candles = this.chart.addSeries(LWC.CandlestickSeries, {
      upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN,
      priceLineVisible: true, lastValueVisible: true, priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    }, 0);
    this.volume = this.chart.addSeries(LWC.HistogramSeries, {
      priceScaleId: 'volume', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false,
    }, 0);
    this.chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    this.indicators = []; // [{ instance, spec, color, outputs: [{ name, series, kind }] }]
    this.indicatorArgs = { instances: [], catalog: {} };
    this.rsiIntervals = [];  // timeframes of the RSI lines in the pane under the price pane, in order
    this.rsiLines = [];      // [{ interval, color, series, data }], all in one pane
    this.rsiZones = null;    // the pane's shading primitive (on the first line)
    this.payload = null;
    this.intraday = true;
    this.intervalMs = 3600 * 1000;
    this.markers = LWC.createSeriesMarkers(this.candles, []);
    this.markerList = [];
    this.bucketLines = []; // [{ id, line }]
    this.quiet = false;    // true while a peek is held: price lines carry no title (see setQuiet)
    this._titles = new Map();  // price line -> its title, so setQuiet can hide and restore it
    this.planLines = [];   // dotted target and stop lines of the selected bucket
    this.levelLines = [];  // support / resistance price lines of the Bitcoin context peek
    this.levelHistory = []; // stepped stop / target lines of a day trade's moves (pictures only, see setLevelHistory)
    this.extremeLines = []; // dashed lines at the 6-month high and low of the coin on screen
    this.trades = [];
    this.targetRange = null;   // range requested but not yet applied by the library
    this.appliedRange = null;  // last range the library reported as applied
    this.chart.timeScale().subscribeVisibleLogicalRangeChange((r) => { this.appliedRange = r; this.targetRange = null; });
    this.chart.subscribeCrosshairMove((p) => { this.legendParam = p; this.renderLegend(p); });
    // The legend box folds to one short line with its arrow button; the app remembers the choice.
    // The button lives outside the rows that every crosshair move rewrites (a click on a button
    // replaced between mousedown and mouseup would be lost).
    this.legendMin = false;
    this.legendParam = null;
    this.onLegendFold = null;
    this.legendFold = document.createElement('button');
    this.legendFold.type = 'button';
    this.legendFold.className = 'legend-fold';
    this.legendBody = document.createElement('div');
    this.legendBody.className = 'legend-body';
    legendEl.replaceChildren(this.legendFold, this.legendBody);
    this.setLegendMin(false);
    this.legendFold.addEventListener('click', () => {
      this.setLegendMin(!this.legendMin);
      if (this.onLegendFold) this.onLegendFold(this.legendMin);
    });
    // Fibonacci retracements live in one primitive on the candle series; clicks go to the app.
    this.fib = new FibPrimitive(fmtPrice);
    this.candles.attachPrimitive(this.fib);
    this.fvg = new FvgPrimitive(fmtPrice);
    this.candles.attachPrimitive(this.fvg);
    this.zones = new ZonePrimitive();  // next support / resistance tubes
    this.candles.attachPrimitive(this.zones);
    this.plan = new PlanPrimitive();   // target / stop preview while hovering Buy
    this.candles.attachPrimitive(this.plan);
    this.userZones = new ZonePrimitive();  // day-trading mode: support / resistance zones drawn by the user
    this.candles.attachPrimitive(this.userZones);
    this.dayLines = [];    // day-trading mode: entry, stop and take-profit of the open trade
    this.dragLines = [];   // day-trading mode: the stop / target lines the user may drag ({ key, line, price })
    this.onDragLine = null; // (key, price) after a drag of one of them
    this._drag = null;
    this._wireLineDrag();
    this.zoneDraft = null; // day-trading mode: the first edge of a zone being drawn
    // Clicks are taken from the DOM: the library's click API drops a quick second click as a
    // double-click, which would lose the second fib anchor. A drag (pan) is not a click.
    this.onClick = null;
    this._down = null;
    this.el.addEventListener('pointerdown', (ev) => { this._down = { x: ev.clientX, y: ev.clientY }; });
    this.el.addEventListener('click', (ev) => {
      const d = this._down;
      this._down = null;
      if (!this.onClick || (d && Math.hypot(ev.clientX - d.x, ev.clientY - d.y) > 4)) return;
      const rect = this.el.getBoundingClientRect();
      this.onClick({ x: ev.clientX - rect.left, y: ev.clientY - rect.top });
    });
  }

  // ------------------------------------------------------------- time zone
  // Labels only: the data keeps its UTC timestamps, so every primitive and marker stays aligned.
  setTimeZone(tz) {
    this.tz = tz || null;
    this.chart.timeScale().applyOptions({ tickMarkFormatter: (t, type) => this.tickLabel(t, type) });
    this.renderLegend(null);
  }
  crosshairLabel(t) {
    const sec = typeof t === 'number' ? t : Math.floor(Date.UTC(t.year, t.month - 1, t.day) / 1000);
    return this.tz ? fmtTime(sec, this.intraday, this.tz) : defaultCrosshairLabel(sec, this.intraday);
  }
  tickLabel(t, type) {
    if (!this.tz) return null;  // the library's own UTC labels
    const sec = typeof t === 'number' ? t : Math.floor(Date.UTC(t.year, t.month - 1, t.day) / 1000);
    const z = zoneParts(sec, this.tz);
    switch (type) {
      case 0: return z.year;                        // TickMarkType.Year
      case 1: return z.month_short;                 // Month
      case 2: return String(parseInt(z.day, 10));   // DayOfMonth
      default: return `${z.hour}:${z.minute}`;      // Time / TimeWithSeconds
    }
  }

  // ------------------------------------------------------------- day trade
  // Entry (solid), stop (red dotted) and take-profit (green dotted) of the day-trading mode's open
  // trade; a closed trade also marks its exit. null removes the lines.
  setDayLines(trade) {
    if (this._drag) return;   // a drag is in progress: its line is being moved by hand
    for (const l of this.dayLines) this.drop(l);
    this.dayLines = [];
    this.dragLines = [];
    if (!trade) return;
    const tag = `#${trade.seq}`;
    const pct = (v) => (v === null || v === undefined ? '' : ` ${v >= 0 ? '+' : '\u2212'}${Math.abs(v).toFixed(2)}%`);
    const open = trade.status === 'open';
    const lines = [
      [null, trade.entry_price, '#3987e5', LWC.LineStyle.Solid, 2, `${tag} entry${pct(trade.price_change_pct)}`],
      ['stop', trade.stop_used, DOWN, LWC.LineStyle.Dotted, 1, `${tag} stop${pct(trade.stop_pct)}`],
      ['take_profit', trade.take_profit, UP, LWC.LineStyle.Dotted, 1, `${tag} target${pct(trade.tp_pct)}`],
    ];
    if (trade.status === 'closed' && trade.exit_price !== null && trade.exit_price !== undefined) {
      lines.push([null, trade.exit_price, trade.pnl >= 0 ? UP : DOWN, LWC.LineStyle.Dashed, 2, `${tag} exit (${trade.exit_type})`]);
    }
    for (const [key, price, color, lineStyle, lineWidth, title] of lines) {
      if (price === null || price === undefined) continue;
      const line = this.line({ price, color, lineWidth, lineStyle, axisLabelVisible: true, title });
      this.dayLines.push(line);
      if (key && open) this.dragLines.push({ key, line, price, title, entry: trade.entry_price });
    }
  }
  // The history of a day trade's stop and target (the pictures): each level as a stepped line from the
  // entry to the exit (or the latest candle), changing at the candle after the one it was moved on
  // (a move takes effect at that candle's close), with a small "SL ↑" / "TP ↓" mark at each move.
  // Nothing is drawn for a trade whose levels never moved: the dotted price lines already tell it all.
  // `null` removes it again.
  setLevelHistory(trade) {
    for (const s of this.levelHistory || []) this.chart.removeSeries(s);
    this.levelHistory = [];
    const changes = trade ? trade.changes || [] : [];
    if (!changes.length || !this.payload || !this.payload.time || !this.payload.time.length) return;
    const step = this.intervalMs / 1000;
    const times = this.payload.time;
    const startS = Math.floor(trade.entry_ms / 1000);
    const endS = Math.min(trade.exit_ms !== null && trade.exit_ms !== undefined ? Math.floor(trade.exit_ms / 1000) : Infinity, times[times.length - 1]);
    const fields = [
      ['stop', trade.stop_initial, DOWN, 'SL', 'aboveBar'],
      ['take_profit', trade.tp_initial, UP, 'TP', 'belowBar'],
    ];
    for (const [field, initial, color, short, position] of fields) {
      const moves = changes.filter(c => c.field === field);
      if (!moves.length || initial === null || initial === undefined || endS < startS) continue;
      // one value per bar time (two moves on one candle: the later one counts), oldest first
      const at = new Map([[startS, initial]]);
      const marks = [];
      for (const c of moves) {
        const t = Math.min(Math.floor(c.at_ms / 1000) + step, endS);
        at.set(t, c.to);
        marks.push({ time: t, position, color, shape: c.to > c.from ? 'arrowUp' : 'arrowDown', text: `${short} ${c.to > c.from ? '↑' : '↓'}`, size: 0.8 });
      }
      const pts = [...at.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
      if (pts[pts.length - 1].time < endS) pts.push({ time: endS, value: pts[pts.length - 1].value });   // run on to the exit
      const series = this.chart.addSeries(LWC.LineSeries, {
        color, lineWidth: 2, lineType: LWC.LineType.WithSteps, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, priceFormat: this.priceFormat(),   // the old levels count for the price scale too
      }, 0);
      series.setData(pts);
      LWC.createSeriesMarkers(series, marks.filter((m, i, all) => all.findIndex(x => x.time === m.time) === i));
      this.levelHistory.push(series);
    }
  }

  // Dragging the stop / target line of the open trade (day-trading mode). The pointerdown near a
  // line is taken in the capture phase and default-prevented, so the library never sees the mouse
  // gesture (no pan, no click); the new price goes to onDragLine on release.
  _wireLineDrag() {
    const GRAB = 6;   // px
    const hit = (ev) => {
      if (!this.onDragLine || this.quiet || !this.dragLines.length) return null;
      const rect = this.el.getBoundingClientRect();
      const y = ev.clientY - rect.top;
      if (y < 0 || y > this.chart.panes()[0].getHeight()) return null;
      let best = null;
      for (const d of this.dragLines) {
        const ly = this.candles.priceToCoordinate(d.price);
        if (ly === null) continue;
        const dist = Math.abs(ly - y);
        if (dist <= GRAB && (!best || dist < best.dist)) best = { d, dist };
      }
      return best ? best.d : null;
    };
    this.el.addEventListener('pointermove', (ev) => {
      if (this._drag) {
        const rect = this.el.getBoundingClientRect();
        const price = this.candles.coordinateToPrice(ev.clientY - rect.top);
        if (price === null || price <= 0) return;
        this._drag.price = price;
        this._drag.moved = this._drag.moved || Math.abs(ev.clientY - this._drag.startY) > 2;
        const rel = this._drag.entry ? ` ${price >= this._drag.entry ? '+' : '\u2212'}${Math.abs((price / this._drag.entry - 1) * 100).toFixed(2)}%` : '';
        this._drag.d.line.applyOptions({ price, title: `${this._drag.d.title.split(' ').slice(0, 2).join(' ')} \u2192 ${fmtPrice(price)}${rel}` });
        return;
      }
      this.el.classList.toggle('drag-line', !!hit(ev));
    });
    this.el.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0) return;
      const d = hit(ev);
      if (!d) return;
      ev.preventDefault();      // no mousedown / click for the library
      ev.stopPropagation();
      this._drag = { d, startY: ev.clientY, price: d.price, moved: false, entry: d.entry };
      try { this.el.setPointerCapture(ev.pointerId); } catch (_) { /* not needed */ }
    }, true);
    const release = (ev) => {
      const drag = this._drag;
      if (!drag) return;
      this._drag = null;
      this.el.classList.remove('drag-line');
      try { this.el.releasePointerCapture(ev.pointerId); } catch (_) { /* already released */ }
      drag.d.line.applyOptions({ price: drag.d.price, title: drag.d.title });   // the server's answer redraws the lines
      if (drag.moved && ev.type === 'pointerup' && this.onDragLine) this.onDragLine(drag.d.key, drag.price);
    };
    this.el.addEventListener('pointerup', release, true);
    this.el.addEventListener('pointercancel', release, true);
  }
  // Zones drawn by the user (day-trading mode): translucent bands labelled with their edges.
  setUserZones(zones, selectedId = null) {
    this.userZones.zones = (zones || []).map(z => ({
      kind: 'support', low: z.lower, high: z.upper,
      label: `${z.id === selectedId ? '\u25B6 ' : ''}Zone ${z.interval} ${fmtPrice(z.lower)} \u2013 ${fmtPrice(z.upper)}`,
    }));
    this.userZones.requestUpdate();
  }
  // The first edge while a zone is being drawn (null removes it).
  setZoneDraft(price) {
    if (this.zoneDraft) { this.drop(this.zoneDraft); this.zoneDraft = null; }
    if (price === null || price === undefined) return;
    this.zoneDraft = this.line({ price, color: '#c98500', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true, title: 'zone edge 1 of 2' });
  }

  // ------------------------------------------------------------------ fibs
  get fibs() { return this.fib.fibs; }
  setFibs(list) { this.fib.fibs = list || []; this.fib.requestUpdate(); }
  setAutoFib(auto) { this.fib.auto = auto || null; this.fib.requestUpdate(); }
  selectFib(id) { this.fib.selectedId = id; this.fib.requestUpdate(); }
  setFibDraft(draft) { this.fib.draft = draft; this.fib.requestUpdate(); }
  setFvg(gaps, hideFilled) { this.fvg.gaps = gaps || []; this.fvg.hideFilled = !!hideFilled; this.fvg.requestUpdate(); }
  // Daily EMA 200 overlay (multi-timeframe line), independent of the indicator list.
  setDailyEma(overlay) {
    const p = this.payload;
    if (!overlay || !p || !p.count) {
      if (this.dailyEma) { this.chart.removeSeries(this.dailyEma); this.dailyEma = null; }
      return;
    }
    if (!this.dailyEma) {
      this.dailyEma = this.chart.addSeries(LWC.LineSeries, {
        color: '#d55181', lineWidth: 2, lineStyle: LWC.LineStyle.LargeDashed, title: 'EMA200 1D',
        priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, priceFormat: this.priceFormat(),
      }, 0);
    }
    const data = [];
    for (let i = 0; i < p.count; i++) if (overlay.values[i] !== null && overlay.values[i] !== undefined) data.push({ time: p.time[i], value: overlay.values[i] });
    this.dailyEma.setData(data);
  }
  // Snap a click (pixels relative to the chart element) to the high or low of the candle under it.
  snapClick(pt) {
    const p = this.payload;
    if (!pt || !p || !p.count) return null;
    if (pt.y < 0 || pt.y > this.chart.panes()[0].getHeight()) return null;  // not on the price pane
    const logical = this.chart.timeScale().coordinateToLogical(pt.x);
    if (logical === null) return null;
    const i = Math.round(logical);
    if (i < 0 || i >= p.count) return null;
    const y = this.candles.coordinateToPrice(pt.y);
    if (y === null) return null;
    const hi = p.high[i], lo = p.low[i];
    const useHigh = Math.abs(hi - y) <= Math.abs(y - lo);
    return { t_ms: p.time[i] * 1000, price: useHigh ? hi : lo, kind: useHigh ? 'high' : 'low' };
  }
  fibAt(pt) { return pt ? this.fib.hitTest(pt.x, pt.y, this.el.clientWidth) : null; }

  // --------------------------------------------------------------- markers
  // Buy/sell markers from the trade journal plus strategy enter/exit events of the
  // current payload. Trade times (base candles) are floored to the view interval.
  setTrades(trades) {
    this.trades = trades || [];
    this.renderMarkers();
  }

  renderMarkers() {
    const p = this.payload;
    const list = [];
    if (p && p.count) {
      const step = this.intervalMs / 1000;
      const first = p.time[0];
      for (const t of this.trades) {
        const time = Math.floor(t.time_ms / 1000 / step) * step;
        if (time < first) continue;
        const buy = t.side === 'buy';
        list.push({
          time, position: buy ? 'belowBar' : 'aboveBar', shape: buy ? 'arrowUp' : 'arrowDown',
          color: buy ? UP : DOWN, text: `${buy ? 'B' : 'S'}${t.seq || ''}`, size: 1.4,
        });
      }
      const s = p.strategy;
      if (s) {
        const last = p.partial_last ? p.count - 2 : p.count - 1; // events on the forming bar are not final
        for (let i = 0; i <= last; i++) {
          if (s.enter[i]) list.push({ time: p.time[i], position: 'belowBar', shape: 'circle', color: UP, size: 0.6 });
          if (s.exit[i]) list.push({ time: p.time[i], position: 'aboveBar', shape: 'circle', color: DOWN, size: 0.6 });
        }
      }
      list.sort((a, b) => a.time - b.time);
    }
    this.markerList = list;
    this.markers.setMarkers(list);
  }

  // Price lines of the candle pane. Their titles are drawn over the latest candles, so a peek
  // (D / Y) hides them with setQuiet(true) and restores them on release.
  line(opts) {
    const l = this.candles.createPriceLine(this.quiet ? { ...opts, title: '' } : opts);
    this._titles.set(l, opts.title || '');
    return l;
  }
  drop(l) { this._titles.delete(l); this.candles.removePriceLine(l); }
  setQuiet(on) {
    this.quiet = !!on;
    for (const [l, title] of this._titles) l.applyOptions({ title: on ? '' : title });
    if (this.dailyEma) this.dailyEma.applyOptions({ title: on ? '' : 'EMA200 1D' });
  }

  // ---------------------------------------------------------- bucket lines
  // One horizontal line per open bucket at its entry price, green in profit, red in loss.
  setBucketLines(buckets, selectedId) {
    for (const b of this.bucketLines) this.drop(b.line);
    this.bucketLines = [];
    for (const b of buckets || []) {
      const pnl = b.unrealized_pnl ?? 0;
      const selected = b.id === selectedId;
      const pct = b.unrealized_pnl_pct;
      const line = this.line({
        price: b.entry_price,
        color: pnl >= 0 ? UP : DOWN,
        lineWidth: selected ? 3 : 1,
        lineStyle: selected ? LWC.LineStyle.Solid : LWC.LineStyle.Dashed,
        axisLabelVisible: true,
        title: `#${b.seq} ${pct === null || pct === undefined ? '' : (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%'}`,
      });
      this.bucketLines.push({ id: b.id, line });
    }
    // The selected bucket also shows its target and stop as dotted lines.
    for (const l of this.planLines) this.drop(l);
    this.planLines = [];
    const sel = (buckets || []).find(b => b.id === selectedId);
    if (sel) {
      for (const [price, color, title] of [
        [sel.tp_price, UP, `#${sel.seq} target +${sel.tp_pct}%`],
        [sel.sl_price, DOWN, `#${sel.seq} stop −${sel.sl_pct}%`],
      ]) {
        if (price === null || price === undefined) continue;
        this.planLines.push(this.line({
          price, color, lineWidth: 1, lineStyle: LWC.LineStyle.Dotted, axisLabelVisible: true, title,
        }));
      }
    }
  }

  // The planned target and stop of a buy (hatched areas from the current candle on); null hides them.
  setPlan(plan) {
    const n = this.payload ? this.payload.count : 0;
    this.plan.plan = plan ? { ...plan, time: n ? this.payload.time[n - 1] : null } : null;
    this.plan.requestUpdate();
  }

  // Horizontal support (green) and resistance (red) levels; null removes them.
  setLevels(levels) {
    for (const l of this.levelLines) this.drop(l);
    this.levelLines = [];
    if (!levels) return;
    for (const [key, color] of [['resistance', DOWN], ['support', UP]]) {
      for (const l of levels[key] || []) {
        this.levelLines.push(this.line({
          price: l.price, color, lineWidth: 2, lineStyle: LWC.LineStyle.Solid, axisLabelVisible: true,
          title: `${l.label} ${l.touches}×`,
        }));
      }
    }
  }

  // Next resistance (red) and next support (green) as translucent tubes across the pane, labelled
  // with touches, breaks and the distance to the band; plus dashed lines at the 6-month high and
  // low with the distance in percent. `g` is the view's gauges or null to clear everything.
  setNearLevels(g) {
    for (const l of this.extremeLines) this.drop(l);
    this.extremeLines = [];
    this.zones.zones = [];
    const lv = g && g.levels, w = g && g.extremes && g.extremes['6m'];
    if (lv) {
      for (const [key, tag] of [['resistance', 'R'], ['support', 'S']]) {
        const z = lv[key];
        if (!z) continue;
        const dist = z.inside ? 'price inside' : `${z.pct >= 0 ? '+' : ''}${z.pct.toFixed(2)}% to the edge`;
        const brk = z.breaks ? ` · ${z.breaks} break${z.breaks === 1 ? '' : 's'}` : '';
        this.zones.zones.push({ kind: key, low: z.low, high: z.high, label: `${tag} ${z.touches} touch${z.touches === 1 ? '' : 'es'}${brk} · ${dist}` });
      }
    }
    this.zones.requestUpdate();
    if (w && w.high !== null && w.high !== undefined) {
      for (const [price, p, name] of [[w.high, w.vs_high_pct, '6M high'], [w.low, w.vs_low_pct, '6M low']]) {
        this.extremeLines.push(this.line({
          price, color: '#c9a227', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true,
          title: `${name} ${p >= 0 ? '+' : ''}${p.toFixed(2)}%`,
        }));
      }
    }
  }

  // ------------------------------------------------------------ indicators
  setIndicators(instances, catalog) {
    this.indicatorArgs = { instances, catalog };
    this._rebuildPanes();
  }

  // The RSI pane under the price pane (day-trading mode: one line per timeframe, 15m and 1h).
  // It follows the view payload, so a payload without panes (swing mode, the Bitcoin context) removes it.
  setRsiPanes(intervals, { withData = true } = {}) {
    const want = intervals || [];
    if (want.length === this.rsiIntervals.length && want.every((iv, i) => iv === this.rsiIntervals[i])) return;
    this.rsiIntervals = [...want];
    this._rebuildPanes({ withData });
  }

  // Panes are rebuilt as a whole: the RSI pane first (right under the candles), then the oscillators
  // of the indicator list. Pane 0 always holds the candles.
  _rebuildPanes({ withData = true } = {}) {
    for (const ind of this.indicators) {
      for (const o of ind.outputs) this.chart.removeSeries(o.series);
    }
    this.indicators = [];
    for (const r of this.rsiLines) this.chart.removeSeries(r.series);
    this.rsiLines = [];
    this.rsiZones = null;
    if (typeof this.chart.removePane === 'function') {
      while (this.chart.panes().length > 1) this.chart.removePane(this.chart.panes().length - 1);
    }
    if (this.rsiIntervals.length) {
      const paneIndex = this.chart.panes().length;
      this.rsiIntervals.forEach((interval, i) => {
        const color = RSI_COLORS[Math.min(i, RSI_COLORS.length - 1)];
        const series = this.chart.addSeries(LWC.LineSeries, {
          color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
          priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
          // the axis is always exactly 0 to 100
          autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 }, margins: { above: 6, below: 6 } }),   // a few pixels so the 0 and 100 labels stay readable
        }, paneIndex);
        if (i === 0) {
          for (const level of [RSI_LOWER, RSI_UPPER]) {
            series.createPriceLine({ price: level, color: AXIS, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });
          }
          this.rsiZones = new RsiZonePrimitive(RSI_LOWER, RSI_UPPER);
          series.attachPrimitive(this.rsiZones);
        }
        this.rsiLines.push({ interval, color, series, data: null });
      });
      this.rsiLines[0].series.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });
    }
    const { instances, catalog } = this.indicatorArgs;
    instances.forEach((instance, i) => {
      const spec = catalog[instance.key];
      if (!spec) return;
      const color = SERIES_COLORS[Math.min(i, SERIES_COLORS.length - 1)];
      const paneIndex = spec.overlay ? 0 : this.chart.panes().length;
      const outputs = spec.outputs.map((out, j) => {
        let series;
        if (out.kind === 'histogram') {
          series = this.chart.addSeries(LWC.HistogramSeries, {
            color, priceLineVisible: false, lastValueVisible: false, priceFormat: this.paneFormat(spec),
          }, paneIndex);
        } else {
          const secondary = spec.outputs.length > 1 && j > 0;
          series = this.chart.addSeries(LWC.LineSeries, {
            color: !spec.overlay && j === 1 ? INK : color,  // oscillator signal lines in neutral ink
            lineWidth: secondary ? 1 : 2,
            lineStyle: (spec.overlay && j === 1 && spec.outputs.length === 3) ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid,
            priceLineVisible: false, lastValueVisible: !spec.overlay && j === 0, crosshairMarkerVisible: false,
            priceFormat: spec.overlay ? this.priceFormat() : this.paneFormat(spec),
          }, paneIndex);
        }
        return { name: out.name, label: out.label, kind: out.kind, series, color: series.options().color };
      });
      if (!spec.overlay) {
        for (const level of spec.levels || []) {
          outputs[0].series.createPriceLine({
            price: level, color: AXIS, lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false,
          });
        }
        if (spec.range) {
          outputs[0].series.applyOptions({
            autoscaleInfoProvider: () => ({ priceRange: { minValue: spec.range[0], maxValue: spec.range[1] }, margins: { above: 4, below: 4 } }),
          });
        }
      }
      this.indicators.push({ instance, ident: instanceIdent(instance), spec, color, outputs, data: null });
    });
    this._layoutPanes();
    if (withData && this.payload) this.setData(this.payload);
  }

  // Pane heights as stretch factors: a freshly added pane has no height yet, so the library's
  // setHeight (which divides by the current heights) would give it a fraction of what was asked.
  // Every extra pane gets its fixed height, the candles take what is left.
  _layoutPanes() {
    const panes = this.chart.panes();
    if (panes.length < 2 || typeof panes[0].setStretchFactor !== 'function') return;
    const total = Math.max(300, (this.el.clientHeight || 0) - 30);   // minus the time axis
    const rsiPanes = this.rsiLines.length ? 1 : 0;
    const wanted = panes.map((_, i) => i === 0 ? 0 : i <= rsiPanes ? RSI_PANE_HEIGHT : PANE_HEIGHT);
    const rest = Math.max(150, total - wanted.reduce((a, b) => a + b, 0));
    panes.forEach((pane, i) => pane.setStretchFactor(i === 0 ? rest : wanted[i]));
  }

  // ------------------------------------------------------------------ data
  priceFormat() {
    return { type: 'price', precision: this.precision, minMove: Math.pow(10, -this.precision) };
  }

  // Bounded oscillators (RSI) read fine with 2 decimals; price-derived ones (MACD) need the
  // price precision plus two, otherwise a 0.63 USDT coin shows "0.00" everywhere.
  paneDecimals(spec) { return spec.range || spec.key === 'vdelta' ? 2 : this.precision + 2; }

  paneFormat(spec) {
    if (spec.key === 'vdelta') return { type: 'volume', precision: 1 };   // base-coin volume: 1.2K, 3.4M on the axis
    const d = this.paneDecimals(spec);
    return { type: 'price', precision: d, minMove: Math.pow(10, -d) };
  }

  // Price precision follows the price level: 2 decimals above 100, 4 above 1, else 6.
  updatePrecision(lastClose) {
    if (lastClose === null || lastClose === undefined) return;
    const a = Math.abs(lastClose);
    const p = a >= 100 ? 2 : a >= 1 ? 4 : 6;
    if (p === this.precision) return;
    this.precision = p;
    this.candles.applyOptions({ priceFormat: this.priceFormat() });
    for (const ind of this.indicators) {
      const fmt = ind.spec.overlay ? this.priceFormat() : this.paneFormat(ind.spec);
      for (const o of ind.outputs) o.series.applyOptions({ priceFormat: fmt });
    }
  }

  setData(payload) {
    this.payload = payload;
    const n = payload.count;
    if (n) this.updatePrecision(payload.close[n - 1]);
    const candles = new Array(n);
    const volume = new Array(n);
    for (let i = 0; i < n; i++) {
      const up = payload.close[i] >= payload.open[i];
      const partial = payload.partial_last && i === n - 1;
      const c = { time: payload.time[i], open: payload.open[i], high: payload.high[i], low: payload.low[i], close: payload.close[i] };
      if (partial) { c.color = up ? UP_SOFT : DOWN_SOFT; c.wickColor = up ? UP_SOFT : DOWN_SOFT; }
      candles[i] = c;
      volume[i] = { time: payload.time[i], value: payload.volume[i], color: up ? 'rgba(12,163,12,0.35)' : 'rgba(208,59,59,0.35)' };
    }
    this.candles.setData(candles);
    this.volume.setData(volume);

    const rsiPanes = payload.rsi_panes || [];
    this.setRsiPanes(rsiPanes.map(r => r.interval), { withData: false });
    const shading = [];
    for (const r of this.rsiLines) {
      const entry = rsiPanes.find(x => x.interval === r.interval) || null;
      r.data = entry;
      const values = entry ? entry.values.slice(0, n) : [];
      const pts = [];
      for (let k = 0; k < n; k++) {
        const v = values[k];
        if (v !== null && v !== undefined) pts.push({ time: payload.time[k], value: v });
      }
      r.series.setData(pts);
      shading.push({ label: `RSI ${entry ? entry.period : 14} · ${r.interval}`, color: r.color, values });
    }
    if (this.rsiZones) this.rsiZones.setLines(shading);

    const pending = new Map();
    for (const v of this.indicators) {
      v.data = null;
      if (!pending.has(v.ident)) pending.set(v.ident, []);
      pending.get(v.ident).push(v);
    }
    for (const ind of payload.indicators || []) {
      const queue = pending.get(instanceIdent(ind));
      const view = queue && queue.shift();
      if (!view) continue;
      view.data = ind;
      for (const out of view.outputs) {
        const values = ind.outputs[out.name] || [];
        const pts = [];
        for (let k = 0; k < n; k++) {
          const v = values[k];
          if (v === null || v === undefined) continue;
          pts.push(out.kind === 'histogram'
            ? { time: payload.time[k], value: v, color: v >= 0 ? 'rgba(12,163,12,0.7)' : 'rgba(208,59,59,0.7)' }
            : { time: payload.time[k], value: v });
        }
        out.series.setData(pts);
      }
    }
    for (const v of this.indicators) {
      if (!v.data) for (const out of v.outputs) out.series.setData([]);
    }
    this.renderMarkers();
    this.renderLegend(null);
  }

  // Show the most recent bars at a readable density instead of squeezing the whole window in.
  fit(bars = INITIAL_BARS) {
    const n = this.payload ? this.payload.count : 0;
    if (n <= bars) { this.targetRange = null; this.chart.timeScale().fitContent(); }
    else this._setRange({ from: n - bars, to: n + 6 });
  }

  // The library applies a requested range on the next animation frame, so a read right after a
  // request is stale. Remember the request so rapid key presses chain; the change event clears it.
  _setRange(range) {
    const cur = this.currentRange();
    if (cur && Math.abs(cur.from - range.from) < 1e-9 && Math.abs(cur.to - range.to) < 1e-9) return; // no event would follow
    this.targetRange = range;
    this.chart.timeScale().setVisibleLogicalRange(range);
  }
  setRange(range) { if (range && Number.isFinite(range.from) && Number.isFinite(range.to) && range.to > range.from) this._setRange(range); }
  // The logical range showing the bars opening from `fromS` to `toS` (unix seconds), clamped to the
  // loaded bars, with a little room on both sides; null without bars.
  rangeForTimes(fromS, toS) {
    const t = this.payload ? this.payload.time : [];
    if (!t.length) return null;
    const firstAtOrAfter = (x) => { let lo = 0, hi = t.length; while (lo < hi) { const m = (lo + hi) >> 1; if (t[m] < x) lo = m + 1; else hi = m; } return lo; };
    const i0 = Math.min(firstAtOrAfter(fromS), t.length - 1);
    const i1 = Math.max(i0, Math.min(firstAtOrAfter(toS + 1) - 1, t.length - 1));
    return { from: i0 - 1, to: i1 + 2 };
  }
  // Pending request first, then the range reported by the change event (the getter lags a frame).
  currentRange() { return this.targetRange || this.appliedRange || this.chart.timeScale().getVisibleLogicalRange(); }

  scrollToEnd() { this.chart.timeScale().scrollToRealTime(); }

  // Zoom by a factor (>1 zooms in) keeping the right edge, i.e. the replay cursor, in place.
  zoom(factor) {
    const range = this.currentRange();
    if (!range || !Number.isFinite(factor) || factor <= 0) return;
    const width = range.to - range.from;
    const next = Math.min(MAX_VISIBLE_BARS, Math.max(MIN_VISIBLE_BARS, width / factor));
    if (next === width) return;
    this._setRange({ from: range.to - next, to: range.to });
  }
  zoomIn() { this.zoom(ZOOM_STEP); }
  zoomOut() { this.zoom(1 / ZOOM_STEP); }
  visibleBars() { const r = this.appliedRange || this.chart.timeScale().getVisibleLogicalRange(); return r ? r.to - r.from : null; }

  // ---------------------------------------------------------------- legend
  setLegendMin(min) {
    this.legendMin = !!min;
    const label = this.legendMin ? 'Show the full chart box' : 'Make the chart box small';
    this.legendFold.textContent = this.legendMin ? '\u25B8' : '\u25BE';
    this.legendFold.setAttribute('aria-expanded', String(!this.legendMin));
    this.legendFold.setAttribute('aria-label', label);
    this.legendFold.title = label;
    this.renderLegend(this.legendParam);
  }
  renderLegend(param) {
    const p = this.payload;
    this.legendEl.classList.toggle('blank', !p || !p.count);
    if (!p || !p.count) { this.legendBody.innerHTML = ''; return; }
    let i = p.count - 1;
    if (param && param.time !== undefined) {
      const idx = p.time.indexOf(param.time);
      if (idx >= 0) i = idx;
    }
    const o = p.open[i], h = p.high[i], l = p.low[i], c = p.close[i];
    const chg = o ? ((c / o - 1) * 100) : 0;
    const cls = c >= o ? 'up' : 'down';
    const partial = p.partial_last && i === p.count - 1;
    const rows = [];
    const when = `<span class="mono">${fmtTime(p.time[i], this.intraday, this.tz)} ${this.tz || 'UTC'}</span>`;
    if (this.legendMin) {   // one line: what, when, the close and its change
      this.legendBody.innerHTML = `<div class="row"><b>${p.symbol}</b> · ${p.interval} · ${when} · <span class="mono">C <b class="${cls}">${fmtPrice(c)}</b> ` +
        `<span class="${cls}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span></span>${partial ? ' <span class="partial">forming</span>' : ''}</div>`;
      return;
    }
    rows.push(`<div class="row"><b>${p.symbol}</b> · ${p.interval} · ${when}` +
      (partial ? ' <span class="partial">forming</span>' : '') + '</div>');
    rows.push(`<div class="row mono">O <b>${fmtPrice(o)}</b> H <b>${fmtPrice(h)}</b> L <b>${fmtPrice(l)}</b> C <b class="${cls}">${fmtPrice(c)}</b> ` +
      `<span class="${cls}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span> Vol <b>${fmtNum(p.volume[i], 2)}</b></div>`);
    for (const view of this.indicators) {
      const ind = view.data;
      if (!ind) continue;
      const key = view.instance ? view.instance.key : '';
      const args = Object.values(ind.params);
      const label = `${SHORT_NAMES[key] || view.spec.name}${args.length ? ` (${args.join(', ')})` : ''}`;
      const vals = view.outputs.map(out => {
        const v = (ind.outputs[out.name] || [])[i];
        return `<span class="sw" style="background:${out.color}"></span>${out.label} <b>${v === null || v === undefined ? '–' : fmtNum(v, view.spec.overlay ? undefined : this.paneDecimals(view.spec))}</b>`;
      }).join(' ');
      rows.push(`<div class="row mono"><span>${label}</span> ${vals}</div>`);
    }
    if (this.rsiLines.length) {
      const vals = this.rsiLines.map(r => {
        const v = r.data ? r.data.values[i] : null;
        const has = v !== null && v !== undefined;
        const cls = has && v <= RSI_LOWER ? 'up' : has && v >= RSI_UPPER ? 'down' : '';
        return `<span class="sw" style="background:${r.color}"></span>${r.interval} <b class="${cls}">${has ? v.toFixed(1) : '–'}</b>`;
      }).join(' ');
      rows.push(`<div class="row mono"><span>RSI 14</span> ${vals}</div>`);
    }
    this.legendBody.innerHTML = rows.join('');
  }
}
