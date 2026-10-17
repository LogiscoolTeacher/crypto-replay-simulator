// Performance diagrams on plain canvases: the equity curve with the drawdown shaded, one bar per
// trade, the win/loss donut and the monthly returns grid. Each canvas gets a hover tooltip.
const UP = '#0ca30c';
const DOWN = '#d03b3b';
const INK = '#f0efec';
const INK_2 = '#c3c2b7';
const MUTED = '#95938c';
const GRID = 'rgba(255,255,255,0.08)';
const BLUE = '#5b9cf6';
const BLUE_FILL = 'rgba(91,156,246,0.18)';
const DD_FILL = 'rgba(208,59,59,0.28)';
const FONT = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
const FONT_BOLD = 'bold 11px system-ui, -apple-system, "Segoe UI", sans-serif';
const MONO = '11px ui-monospace, "Cascadia Mono", Consolas, monospace';

const money = (v) => (v === null || v === undefined ? '–' : (Math.abs(v) < 0.005 ? 0 : v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));  // no "-0.00"
const signed = (v) => (v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${money(v)}`);
const pct = (v) => (v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`);
const dateOf = (sec) => new Date(sec * 1000).toISOString().slice(0, 10);
const dateTime = (sec) => new Date(sec * 1000).toISOString().slice(0, 16).replace('T', ' ');

// Sizes a canvas to its CSS box at the device pixel ratio; returns the 2D context in CSS pixels.
function prepare(canvas) {
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(50, Math.floor(rect.width)), h = Math.max(50, Math.floor(rect.height));
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.font = FONT;
  return { ctx, w, h };
}

// "Nice" tick values between lo and hi.
function ticks(lo, hi, n = 4) {
  if (!(hi > lo)) return [lo];
  const span = hi - lo;
  const raw = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n + 1) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

// One shared tooltip element, positioned near the pointer inside `host`.
function tooltip(host) {
  let el = host.querySelector('.perf-tip');
  if (!el) {
    el = document.createElement('div');
    el.className = 'perf-tip hidden';
    host.appendChild(el);
  }
  return {
    show(x, y, lines) {
      el.innerHTML = '';
      for (const [k, v, cls] of lines) {
        const row = document.createElement('div');
        const a = document.createElement('span'); a.className = 'k'; a.textContent = k;
        const b = document.createElement('span'); b.className = `v ${cls || ''}`; b.textContent = v;
        row.append(a, b);
        el.appendChild(row);
      }
      el.classList.remove('hidden');
      const hw = host.clientWidth, tw = el.offsetWidth, th = el.offsetHeight;
      el.style.left = `${Math.min(x + 12, Math.max(0, hw - tw - 4))}px`;
      el.style.top = `${Math.max(0, Math.min(y - th - 10, host.clientHeight - th - 4))}px`;
    },
    hide() { el.classList.add('hidden'); },
  };
}

export class PerfCharts {
  constructor(hosts) {
    // hosts: { equity, bars, donut } -> container elements each holding one <canvas>
    this.hosts = hosts;
    this.report = null;
    this._hover = {};
    for (const key of Object.keys(hosts)) {
      const host = hosts[key];
      const canvas = host.querySelector('canvas');
      canvas.addEventListener('mousemove', (e) => this._onMove(key, e));
      canvas.addEventListener('mouseleave', () => this._onLeave(key));
    }
  }

  render(report) {
    this.report = report;
    this.drawEquity();
    this.drawBars();
    this.drawDonut();
  }

  _onMove(key, e) {
    const fn = { equity: this._hoverEquity, bars: this._hoverBars, donut: this._hoverDonut }[key];
    if (fn) fn.call(this, e);
  }
  _onLeave(key) {
    tooltip(this.hosts[key]).hide();
    if (key === 'equity' && this._hover.equity !== null) { this._hover.equity = null; this.drawEquity(); }
    if (key === 'bars' && this._hover.bars !== null) { this._hover.bars = null; this.drawBars(); }
  }

  // ------------------------------------------------------------- equity curve
  _equityLayout() {
    const canvas = this.hosts.equity.querySelector('canvas');
    const { ctx, w, h } = prepare(canvas);
    const pad = { l: 64, r: 12, t: 14, b: 22 };
    return { canvas, ctx, w, h, pad, pw: w - pad.l - pad.r, ph: h - pad.t - pad.b };
  }
  drawEquity() {
    const r = this.report;
    const L = this._equityLayout();
    const { ctx, w, h, pad, pw, ph } = L;
    if (!r || !r.curve || r.curve.value.length < 1) { this._empty(ctx, w, h, 'No equity yet'); return; }
    const cv = r.curve;
    const xs = cv.time, ys = cv.value;
    const cumulative = cv.kind === 'cumulative';
    const base = cumulative ? 0 : r.start_balance;
    let lo = Math.min(base, ...ys), hi = Math.max(base, ...ys);
    if (hi === lo) { hi += 1; lo -= 1; }
    const span = hi - lo;
    lo -= span * 0.08; hi += span * 0.08;
    const x0 = xs[0], x1 = xs[xs.length - 1];
    const X = (t) => pad.l + (x1 === x0 ? pw / 2 : (t - x0) / (x1 - x0) * pw);
    const Y = (v) => pad.t + (hi - v) / (hi - lo) * ph;
    L.X = X; L.Y = Y; L.lo = lo; L.hi = hi;
    // grid + y ticks
    ctx.strokeStyle = GRID; ctx.fillStyle = MUTED; ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.font = MONO;
    for (const v of ticks(lo, hi, 4)) {
      const y = Y(v);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      ctx.fillText(cumulative ? signed(v) : money(v), pad.l - 6, y);
    }
    // x ticks
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const nx = Math.max(2, Math.min(6, Math.floor(pw / 110)));
    for (let i = 0; i < nx; i++) {
      const t = x0 + (x1 - x0) * i / (nx - 1);
      ctx.fillText(cumulative ? `trade ${Math.round(t)}` : dateOf(t), X(t), h - pad.b + 6);
    }
    // drawdown shading: between the running peak and the curve
    let peak = ys[0];
    ctx.beginPath();
    ctx.moveTo(X(xs[0]), Y(ys[0]));
    const peaks = [];
    for (let i = 0; i < ys.length; i++) { peak = Math.max(peak, ys[i]); peaks.push(peak); ctx.lineTo(X(xs[i]), Y(peak)); }
    for (let i = ys.length - 1; i >= 0; i--) ctx.lineTo(X(xs[i]), Y(ys[i]));
    ctx.closePath();
    ctx.fillStyle = DD_FILL; ctx.fill();
    // area under the curve + the line
    ctx.beginPath();
    ctx.moveTo(X(xs[0]), Y(base));
    for (let i = 0; i < ys.length; i++) ctx.lineTo(X(xs[i]), Y(ys[i]));
    ctx.lineTo(X(xs[xs.length - 1]), Y(base));
    ctx.closePath();
    ctx.fillStyle = BLUE_FILL; ctx.fill();
    ctx.beginPath();
    for (let i = 0; i < ys.length; i++) i ? ctx.lineTo(X(xs[i]), Y(ys[i])) : ctx.moveTo(X(xs[i]), Y(ys[i]));
    ctx.strokeStyle = BLUE; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
    // baseline (start balance / zero)
    ctx.setLineDash([4, 4]); ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.l, Y(base)); ctx.lineTo(w - pad.r, Y(base)); ctx.stroke();
    ctx.setLineDash([]);
    // peak and trough of the deepest drawdown
    const marks = [[cv.peak_index, UP, 'peak'], [cv.trough_index, DOWN, 'low']];
    for (const [i, color, name] of marks) {
      if (i === null || i === undefined || !(i in ys)) continue;
      const x = X(xs[i]), y = Y(ys[i]);
      ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = '#0d0d0d'; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.font = FONT_BOLD; ctx.fillStyle = color; ctx.textBaseline = name === 'peak' ? 'bottom' : 'top'; ctx.textAlign = x > w / 2 ? 'right' : 'left';
      ctx.fillText(`${name} ${cumulative ? signed(ys[i]) : money(ys[i])}`, x + (x > w / 2 ? -7 : 7), y + (name === 'peak' ? -6 : 6));
    }
    ctx.font = FONT;
    // hover crosshair
    const hv = this._hover.equity;
    if (hv !== null && hv !== undefined && hv in ys) {
      const x = X(xs[hv]);
      ctx.strokeStyle = 'rgba(255,255,255,0.4)'; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = INK; ctx.beginPath(); ctx.arc(x, Y(ys[hv]), 3.5, 0, Math.PI * 2); ctx.fill();
    }
    this._equity = { ...L, xs, ys, peaks, cumulative, base };
  }
  _hoverEquity(e) {
    const E = this._equity;
    if (!E || !E.xs.length) return;
    const rect = E.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    // nearest point by x
    let best = 0, bd = Infinity;
    for (let i = 0; i < E.xs.length; i++) { const d = Math.abs(E.X(E.xs[i]) - mx); if (d < bd) { bd = d; best = i; } }
    if (this._hover.equity !== best) { this._hover.equity = best; this.drawEquity(); }
    const r = this.report, v = E.ys[best];
    const lines = E.cumulative
      ? [['Trade', `${E.xs[best]} of ${r.summary.trades}`], ['Realized P&L', signed(v), v >= 0 ? 'up' : 'down'], ['Drawdown', pct(r.curve.drawdown_pct[best]), 'down']]
      : [['Time', dateTime(E.xs[best])], ['Equity', money(v)], ['vs start', pct((v / r.start_balance - 1) * 100), v >= r.start_balance ? 'up' : 'down'],
         ['Below peak', pct(r.curve.drawdown_pct[best]), r.curve.drawdown_pct[best] < 0 ? 'down' : '']];
    tooltip(this.hosts.equity).show(mx, my, lines);
  }

  // ------------------------------------------------------------- trade bars
  drawBars() {
    const r = this.report;
    const canvas = this.hosts.bars.querySelector('canvas');
    const { ctx, w, h } = prepare(canvas);
    const pad = { l: 48, r: 10, t: 12, b: 20 };
    const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
    const trades = r ? r.trades : [];
    if (!trades.length) { this._empty(ctx, w, h, 'No closed trades yet'); this._bars = null; return; }
    const vals = trades.map(t => t.pct);
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    if (hi === lo) { hi = 1; lo = -1; }
    const span = hi - lo; lo -= span * 0.1; hi += span * 0.1;
    const Y = (v) => pad.t + (hi - v) / (hi - lo) * ph;
    ctx.strokeStyle = GRID; ctx.fillStyle = MUTED; ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.font = MONO;
    for (const v of ticks(lo, hi, 4)) {
      const y = Y(v);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      ctx.fillText(`${v >= 0 ? '+' : ''}${v.toFixed(Math.abs(hi - lo) < 2 ? 2 : 1)}%`, pad.l - 6, y);
    }
    const n = trades.length;
    const slot = pw / n, bw = Math.max(2, Math.min(28, slot * 0.7));
    const rects = [];
    for (let i = 0; i < n; i++) {
      const v = vals[i];
      const x = pad.l + slot * i + (slot - bw) / 2;
      const y0 = Y(0), y1 = Y(v);
      const top = Math.min(y0, y1), hh = Math.max(1, Math.abs(y1 - y0));
      ctx.fillStyle = v >= 0 ? UP : DOWN;
      ctx.globalAlpha = this._hover.bars === null || this._hover.bars === undefined || this._hover.bars === i ? 1 : 0.55;
      ctx.beginPath(); ctx.roundRect(x, top, bw, hh, 2); ctx.fill();
      ctx.globalAlpha = 1;
      rects.push({ x, w: bw, i });
    }
    // zero line and the average
    ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.l, Y(0)); ctx.lineTo(w - pad.r, Y(0)); ctx.stroke();
    const avg = r.summary.avg_pct;
    if (avg !== null && avg !== undefined) {
      ctx.setLineDash([4, 3]); ctx.strokeStyle = BLUE;
      ctx.beginPath(); ctx.moveTo(pad.l, Y(avg)); ctx.lineTo(w - pad.r, Y(avg)); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = BLUE; ctx.textAlign = 'right'; ctx.textBaseline = avg >= 0 ? 'bottom' : 'top'; ctx.font = FONT_BOLD;
      ctx.fillText(`avg ${pct(avg)}`, w - pad.r - 2, Y(avg) + (avg >= 0 ? -2 : 2));
    }
    ctx.font = FONT; ctx.fillStyle = MUTED; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / 60))));
    for (let i = 0; i < n; i += step) ctx.fillText(trades[i].label, pad.l + slot * i + slot / 2, h - pad.b + 5);
    this._bars = { canvas, rects, slot, pad };
  }
  _hoverBars(e) {
    const B = this._bars;
    if (!B) return;
    const rect = B.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const i = Math.floor((mx - B.pad.l) / B.slot);
    const t = this.report.trades[i];
    if (!t) { this._onLeave('bars'); return; }
    if (this._hover.bars !== i) { this._hover.bars = i; this.drawBars(); }
    const held = t.held_ms / 3600000;
    tooltip(this.hosts.bars).show(mx, my, [
      ['Trade', `${t.label}${t.symbol && this.report.scope === 'all' ? ` ${t.symbol.replace(/USDT$/, '')}` : ''}`],
      ['Sold', dateTime(t.closed_ms / 1000)],
      ['P&L', `${signed(t.pnl)} (${pct(t.pct)})`, t.pnl >= 0 ? 'up' : 'down'],
      ['Held', held >= 48 ? `${(held / 24).toFixed(1)} days` : `${held.toFixed(0)} h`],
      ['Exit', t.trigger ? `automatic at the ${t.trigger}` : 'by hand'],
    ]);
  }

  // ------------------------------------------------------------- donut
  drawDonut() {
    const r = this.report;
    const canvas = this.hosts.donut.querySelector('canvas');
    const { ctx, w, h } = prepare(canvas);
    const sm = r ? r.summary : null;
    if (!sm || !sm.trades) { this._empty(ctx, w, h, 'No closed trades yet'); this._donut = null; return; }
    const cx = Math.min(w / 2, 70 + 10), cy = h / 2, R = Math.min(cy - 8, 62), rr = R * 0.62;
    const parts = [['Winners', sm.winners, UP], ['Losers', sm.losers, DOWN], ['Flat', sm.flat, MUTED]].filter(p => p[1] > 0);
    let a = -Math.PI / 2;
    const arcs = [];
    for (const [name, n, color] of parts) {
      const b = a + (n / sm.trades) * Math.PI * 2;
      ctx.beginPath(); ctx.arc(cx, cy, R, a, b); ctx.arc(cx, cy, rr, b, a, true); ctx.closePath();
      ctx.fillStyle = color; ctx.fill();
      ctx.strokeStyle = '#1a1a19'; ctx.lineWidth = 2; ctx.stroke();
      arcs.push({ name, n, color, a, b });
      a = b;
    }
    ctx.fillStyle = INK; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.font = 'bold 15px system-ui, sans-serif';
    ctx.fillText(sm.win_rate_pct === null ? '–' : `${sm.win_rate_pct.toFixed(0)}%`, cx, cy - 7);
    ctx.font = FONT; ctx.fillStyle = MUTED; ctx.fillText('won', cx, cy + 9);
    // legend
    const lx = cx + R + 16;
    let ly = cy - 30;
    ctx.textAlign = 'left';
    for (const [name, n, color, extra] of [
      ['Winners', sm.winners, UP, sm.avg_win_pct === null ? '' : `avg ${pct(sm.avg_win_pct)}`],
      ['Losers', sm.losers, DOWN, sm.avg_loss_pct === null ? '' : `avg ${pct(sm.avg_loss_pct)}`],
      ['Flat', sm.flat, MUTED, ''],
    ]) {
      ctx.fillStyle = color; ctx.fillRect(lx, ly - 5, 10, 10);
      ctx.fillStyle = INK_2; ctx.font = FONT;
      ctx.fillText(`${name} ${n}`, lx + 16, ly);
      ctx.fillStyle = MUTED; ctx.font = MONO;
      ctx.fillText(extra, lx + 86, ly);
      ly += 20;
    }
    this._donut = { canvas, cx, cy, R, rr, arcs };
  }
  _hoverDonut(e) {
    const D = this._donut;
    if (!D) return;
    const rect = D.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const dx = mx - D.cx, dy = my - D.cy, d = Math.hypot(dx, dy);
    if (d < D.rr || d > D.R) { tooltip(this.hosts.donut).hide(); return; }
    let ang = Math.atan2(dy, dx);
    if (ang < -Math.PI / 2) ang += Math.PI * 2;
    const arc = D.arcs.find(x => ang >= x.a && ang < x.b);
    if (!arc) { tooltip(this.hosts.donut).hide(); return; }
    const sm = this.report.summary;
    tooltip(this.hosts.donut).show(mx, my, [[arc.name, `${arc.n} of ${sm.trades} (${(arc.n / sm.trades * 100).toFixed(0)}%)`, arc.name === 'Winners' ? 'up' : arc.name === 'Losers' ? 'down' : '']]);
  }

  _empty(ctx, w, h, text) {
    ctx.fillStyle = MUTED; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.font = FONT;
    ctx.fillText(text, w / 2, h / 2);
  }
}

// Monthly returns as a year × month grid (HTML, not canvas): one cell per month with its percent.
export function renderMonths(host, months, cumulative) {
  host.innerHTML = '';
  if (!months || !months.length) {
    const p = document.createElement('p'); p.className = 'muted small'; p.textContent = 'No months yet.'; host.appendChild(p); return;
  }
  const byYear = new Map();
  for (const m of months) {
    const [y, mo] = m.month.split('-');
    if (!byYear.has(y)) byYear.set(y, new Map());
    byYear.get(y).set(parseInt(mo, 10), m);
  }
  const table = document.createElement('table');
  table.className = 'months';
  const head = document.createElement('tr');
  for (const t of ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', 'Year']) {
    const th = document.createElement('th'); th.textContent = t; head.appendChild(th);
  }
  table.appendChild(head);
  const maxAbs = Math.max(1e-9, ...months.map(m => Math.abs(m.pct || 0)));
  for (const [y, map] of [...byYear.entries()].sort()) {
    const tr = document.createElement('tr');
    const th = document.createElement('th'); th.textContent = y; tr.appendChild(th);
    let yearPnl = 0, yearPct = 0, any = false;
    for (let mo = 1; mo <= 12; mo++) {
      const td = document.createElement('td');
      const m = map.get(mo);
      if (m && m.pct !== null && m.pct !== undefined) {
        any = true;
        yearPnl += m.pnl;
        yearPct = cumulative ? yearPct + m.pct : (1 + yearPct / 100) * (1 + m.pct / 100) * 100 - 100;
        const alpha = 0.15 + 0.6 * Math.min(1, Math.abs(m.pct) / maxAbs);
        td.style.background = m.pct >= 0 ? `rgba(12,163,12,${alpha.toFixed(2)})` : `rgba(208,59,59,${alpha.toFixed(2)})`;
        td.textContent = `${m.pct >= 0 ? '+' : ''}${m.pct.toFixed(1)}%`;
        td.title = `${m.month}: ${signed(m.pnl)} USDT (${pct(m.pct)})`;
        td.className = m.pct >= 0 ? 'up' : 'down';
      } else {
        td.textContent = '';
        td.className = 'empty';
      }
      tr.appendChild(td);
    }
    const tot = document.createElement('td');
    tot.className = `total ${yearPct >= 0 ? 'up' : 'down'}`;
    tot.textContent = any ? `${yearPct >= 0 ? '+' : ''}${yearPct.toFixed(1)}%` : '';
    tot.title = any ? `${y}: ${signed(yearPnl)} USDT` : '';
    tr.appendChild(tot);
    table.appendChild(tr);
  }
  host.appendChild(table);
}
