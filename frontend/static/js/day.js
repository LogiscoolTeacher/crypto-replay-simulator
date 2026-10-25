// Day-trading mode (sessions with mode "day"): the side panel with the trend check, the zones and the
// trade buttons, the New trade / Sell / Trend check dialogs, zone drawing by two clicks, the Trades
// tab with the picture per trade, and the handling of step events (exits, jumps).
//
// The module gets everything it needs from `ctx` (app.js): the shared state, the chart, the API,
// formatting helpers and the few app functions it has to call back (pause, applyState, refreshView).
// Nothing here runs for a swing session: every entry point checks `active()` first.

const ZONE_INTERVALS = ['1h', '4h'];
// The next animation frame, or a short timer when none comes (a hidden tab paints no frames).
const nextFrame = () => new Promise(r => { const id = setTimeout(r, 100); requestAnimationFrame(() => { clearTimeout(id); r(); }); });
const PLAN_DEBOUNCE_MS = 150;
const PLAN_MARGIN_BARS = 30;
// The entry conditions saved with every trade (same keys and order as CONDITIONS in day_engine.py).
const COND_LABELS = [
  ['ema_above', 'Daily close above EMA 200'],
  ['price_in_zone', 'Price inside the support zone'],
  ['rsi_dipped', 'RSI 14 (15m) dipped to 40 or below that day'],
  ['close_above_prev_high', 'Entry candle closed above the previous high'],
  ['rr_ok', 'Reward-to-risk at least 2'],
  ['stop_middle', 'Stop at the zone middle (the default)'],
  ['stop_atr', 'Stop at the ATR stop'],
  ['stop_own', 'Stop set by hand (neither middle nor ATR)'],
  ['zone_4h', 'Zone drawn on the 4h chart (else 1h)'],
  ['morning', 'Entry before 12:00'],
  ['afternoon', 'Entry 12:00 to 18:00'],
  ['evening', 'Entry after 18:00'],
  ['stop_moved', 'Stop moved during the trade'],
  ['tp_moved', 'Target moved during the trade'],
];   // empty bars kept right of the last candle while the New trade panel is open

// The Trades tab columns shown by default (data-col names of the table headers); the rest hide until chosen.
const DAY_COLS_DEFAULT = ['pic', 'seq', 'day', 'entry', 'exit', 'held', 'trend', 'cond', 'conf', 'pnl', 'pnl_pct', 'r'];

export class DayMode {
  constructor(ctx) {
    this.c = ctx;
    this.zones = [];
    this.trades = [];
    this.selectedZone = null;
    this.drawing = false;
    this.draft = null;         // first zone edge (price) after the first click
    this.planTimer = null;
    this.plan = null;          // last plan preview from the server
    this.planCursor = null;    // cursor the open plan was computed for; a new candle refreshes it
    this.planSeq = 0;
    this.busy = false;         // a buy, sell, skip or continue is in flight
    this.sort = { key: 'seq', dir: 1 };
    this.confidence = null;          // the score 0..10 picked in the New trade form (required for the buy)
    this.auto = { trades: [], summary: null };   // the automatic-exit twins (Auto exits tab)
    this.reviewTrade = null;      // the trade shown in the review dialog (editing from the trade dialog)
    this.inlineReviewId = null;   // the trade whose pending review is in the side panel (its marks keep the user's clicks)
    this.hiddenCols = this.loadCols();   // Trades tab: the hidden columns (null = the default set)
    this.conditions = null;        // the Conditions tab's last answer
    this.condScope = 'session';    // 'session' | 'all'
    this.condFilter = { key: 'all', val: 'all' };   // the Trades tab filter
    this.days = [];            // the daily journal rows
    this.calMonth = null;      // 'YYYY-MM' shown in the Calendar tab; null = the month of the current day
    this.notesDate = null;     // the day whose notes the card's box shows
    this.strategies = [];      // the jump strategies of the "Next signal" button (from the server)
    this.notesTimer = null;
    this.capturing = false;    // a day picture is being taken
  }

  // ------------------------------------------------------------- helpers
  get $() { return this.c.$; }
  get state() { return this.c.state; }
  get session() { return this.state.session; }
  get day() { const s = this.session; return s && s.mode === 'day' ? s.day : null; }
  active() { const s = this.session; return !!s && s.mode === 'day'; }
  ready() { return this.active() && this.session.status === 'ready' && !!this.day && !!this.day.date; }
  dialogOpen() { return ['dlg-exit', 'dlg-review'].some(id => this.$(id).open); }
  n(v, digits = 2) { return v === null || v === undefined || Number.isNaN(v) ? '–' : v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
  signed(v, digits = 2) { return v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${this.n(v, digits)}`; }
  pctText(v) { return v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`; }
  flag(v) { return v === null || v === undefined ? 'n/a' : v ? 'yes' : 'no'; }
  cls(v) { return v > 0 ? 'up' : v < 0 ? 'down' : ''; }
  // Holding time: hours up to a day, then days and hours ("2 d 4 h"); an open trade counts from the fill to the cursor.
  holdHours(entryMs, exitMs) { return ((exitMs === null || exitMs === undefined ? this.session.cursor_ms : exitMs) - entryMs) / 3600000; }
  holdText(h) {
    if (h === null || h === undefined || !isFinite(h)) return '–';
    if (h < 24) return `${h.toFixed(1)} h`;
    const d = Math.floor(h / 24), r = h - d * 24;
    return `${d} d ${r.toFixed(0)} h`;
  }

  // ------------------------------------------------------- session switch
  // Called whenever a session is loaded (or none): switches the page into or out of the day mode.
  onSession(s) {
    const on = !!s && s.mode === 'day';
    document.body.classList.toggle('day-mode', on);
    this.zones = [];
    this.trades = [];
    this.days = [];
    this.calMonth = null;
    this.notesDate = null;
    clearTimeout(this.notesTimer);
    this.selectedZone = null;
    this.$('day-signal-last').textContent = '';
    this.plan = null;
    this.cancelDraw();
    this.c.chart.setUserZones([]);
    this.c.chart.setDayLines(null);
    this.c.chart.setTimeZone(null);   // the day clock is UTC, like the exchange (was Europe/Malta)
    for (const id of ['dlg-exit', 'dlg-review']) if (this.$(id).open) this.$(id).close();
    this.inlineReviewId = null;
    if (this.panelOpen()) this.closeNewTrade();
    const dayTab = ['tab-day', 'tab-days', 'tab-cal', 'tab-cond'].some(id => this.$(id).getAttribute('aria-selected') === 'true');
    this.conditions = null;
    if (on && !dayTab) this.c.selectTab('tab-day');
    else if (!on && dayTab) this.c.selectTab('tab-open');
    this.renderZones();
    this.renderTrades();
    this.renderDays();
  }

  // Zones, trades and the daily journal from the server (after loading, a trade, a reset).
  async refresh() {
    if (!this.ready()) return;
    const t = this.c.token();
    const [zones, trades, days, auto] = await Promise.all([this.c.api.dayZones(this.session.id), this.c.api.dayTrades(this.session.id), this.c.api.dayDays(this.session.id), this.c.api.dayAutoTrades(this.session.id)]);
    if (!this.c.fresh(t)) return;
    this.zones = zones;
    if (!zones.some(z => z.id === this.selectedZone)) this.selectedZone = null;
    this.trades = trades.trades;
    this.days = days.days;
    this.auto = auto;
    this.renderZones();
    this.renderTrades();
    this.renderAuto();
    this.renderDays();
    this.renderCal();
    this.renderMarkers();
  }

  // ------------------------------------------------------------ calendar
  // One month, a week per row from Monday to Sunday: the P&L (after fees) and the trades of every
  // day the replay has seen, the week's and the month's total. A past day opens its journal; a
  // later day inside the replay jumps there (00:00).
  localDate(ms) { return ms === null || ms === undefined ? null : new Date(ms).toLocaleDateString('sv-SE', { timeZone: 'UTC' }); }
  calShown() {
    if (this.calMonth) return this.calMonth;
    return this.day && this.day.date ? this.day.date.slice(0, 7) : null;
  }
  moveCal(delta) {
    const cur = this.calShown();
    if (!cur) return;
    const [y, m] = cur.split('-').map(Number);
    const d = new Date(Date.UTC(y, m - 1 + delta, 1));
    this.calMonth = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
    this.renderCal();
  }
  renderCal() {
    const $ = this.$;
    const body = $('cal-body');
    if (!body) return;
    body.innerHTML = '';
    const month = this.calShown();
    if (!this.ready() || !month) { $('cal-title').textContent = '–'; $('cal-summary').textContent = ''; return; }
    const [y, m] = month.split('-').map(Number);
    const first = new Date(Date.UTC(y, m - 1, 1));
    $('cal-title').textContent = first.toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' });
    const byDate = new Map(this.days.map(r => [r.date, r]));
    const today = this.day.date;
    const s = this.session;
    const startDate = this.localDate(s.data_start_ms !== undefined ? s.data_start_ms : s.start_ms);
    const endDate = this.localDate(s.data_end_ms !== undefined ? s.data_end_ms : s.end_ms);
    const iso = (d) => d.toISOString().slice(0, 10);
    const start = new Date(first); start.setUTCDate(1 - ((first.getUTCDay() + 6) % 7));   // back to Monday
    const daysInMonth = new Date(Date.UTC(y, m, 0)).getUTCDate();
    let monthPnl = 0, monthTrades = 0, monthDays = 0;
    for (let w = 0; w < 6; w++) {
      const rowStart = new Date(start); rowStart.setUTCDate(start.getUTCDate() + w * 7);
      if (rowStart.getUTCMonth() !== m - 1 && w > 0 && iso(rowStart) > iso(new Date(Date.UTC(y, m - 1, daysInMonth)))) break;
      const tr = document.createElement('tr');
      let weekPnl = 0, weekHas = false;
      for (let i = 0; i < 7; i++) {
        const d = new Date(rowStart); d.setUTCDate(rowStart.getUTCDate() + i);
        const ds = iso(d);
        const r = byDate.get(ds);
        const td = document.createElement('td');
        const cls = [];
        if (d.getUTCMonth() !== m - 1) cls.push('out');
        const inRange = (!startDate || ds >= startDate) && (!endDate || ds <= endDate);
        if (!inRange) cls.push('off');
        if (ds === today) cls.push('today');
        const num = document.createElement('span'); num.className = 'd'; num.textContent = String(d.getUTCDate()); td.appendChild(num);
        if (r) {
          const n = r.trade_count || 0;
          if (n) {
            const pnl = document.createElement('span'); pnl.className = `pnl ${this.cls(r.day_pnl)}`;
            pnl.textContent = `${this.signed(r.day_pnl)}`; td.appendChild(pnl);
            for (const x of r.exit_types || []) {   // one tag per closed trade: PFT take-profit, SL stop, MAN manual exit
              const tag = document.createElement('span');
              const kind = x === 'take-profit' ? 'pft' : x === 'stop' ? 'sl' : 'man';
              tag.className = `tag ${kind}`; tag.textContent = kind.toUpperCase(); tag.title = x;
              pnl.appendChild(tag);
            }
            const meta = document.createElement('span'); meta.className = 'meta';
            meta.textContent = `${n} trade${n === 1 ? '' : 's'}${r.outcome === 'open' ? ' · open' : ''}`; td.appendChild(meta);
            weekPnl += r.day_pnl || 0; weekHas = true;
            if (d.getUTCMonth() === m - 1) { monthPnl += r.day_pnl || 0; monthTrades += n; monthDays += 1; }
          } else {
            const meta = document.createElement('span'); meta.className = 'meta';
            meta.textContent = r.outcome === 'current' ? 'today' : r.outcome === 'skipped' ? 'skipped' : 'no trade'; td.appendChild(meta);
          }
          if (ds !== today) { cls.push('click'); td.title = `${ds}: open the day's journal`; td.addEventListener('click', () => this.openDay(r)); }
          else td.title = `${ds}: the current day (${this.day.local_time.slice(11)})`;
        } else if (inRange && ds > today && !this.session.finished) {
          cls.push('click'); td.title = `${ds}: jump there (00:00). No trade may be open.`;
          td.addEventListener('click', () => this.gotoDay(ds));
        } else if (inRange && ds < today) {
          td.title = `${ds}: not visited (jumped over)`;
        }
        td.className = cls.join(' ');
        tr.appendChild(td);
      }
      const wk = document.createElement('td'); wk.className = `week ${weekHas ? this.cls(weekPnl) : ''}`;
      wk.textContent = weekHas ? this.signed(weekPnl) : ''; tr.appendChild(wk);
      body.appendChild(tr);
    }
    $('cal-summary').textContent = monthTrades
      ? `Month: ${this.signed(monthPnl)} USDT · ${monthTrades} trade${monthTrades === 1 ? '' : 's'} on ${monthDays} day${monthDays === 1 ? '' : 's'}`
      : 'No trades this month.';
  }
  async gotoDay(dateStr) {
    if (!this.ready() || this.busy) return;
    if (this.day.trade) { this.c.toast('A trade is open: close it before jumping to another day.', true); return; }
    await this.jump(() => this.c.api.dayGoto(this.session.id, dateStr));
  }

  renderMarkers() {
    const list = [];
    for (const t of this.trades) {
      list.push({ time_ms: t.entry_ms, side: 'buy', seq: t.seq });
      if (t.exit_ms !== null && t.exit_ms !== undefined) list.push({ time_ms: t.exit_ms, side: 'sell', seq: t.seq });
    }
    this.c.chart.setTrades(list);
  }

  // Top-bar time of the cursor candle's close in the local zone.
  cursorText() {
    const d = this.day;
    return d && d.local_time ? `${d.local_time} UTC` : null;
  }

  // The bar at the top right: how much of the trading day (00:00-24:00 UTC) has passed at the
  // cursor candle's close, and the hours still left.
  renderProgress(s) {
    const bar = this.$('day-progress'), fill = this.$('day-progress-fill'), text = this.$('day-progress-text');
    const d = s && s.day;
    if (!d || !d.day_start_ms || !d.day_end_ms) { fill.style.width = '0'; text.textContent = '–'; bar.classList.remove('over'); return; }
    const close = s.cursor_ms + this.c.intervalMs(s.base_interval);
    const total = (d.day_end_ms - d.day_start_ms) / 3.6e6;
    const left = Math.min(total, Math.max(0, (d.day_end_ms - close) / 3.6e6));
    fill.style.width = `${((total - left) / total * 100).toFixed(1)}%`;
    bar.classList.toggle('over', left <= 0);
    text.textContent = left <= 0 ? 'day over' : `${Number(left.toFixed(2))} h left`;
    bar.title = `Trading day ${d.day_start_local.slice(11)}–24:00 UTC · now ${d.local_time.slice(11)} · ${left <= 0 ? 'over' : `${Number(left.toFixed(2))} hours left`}`;
  }

  // ---------------------------------------------------------------- panel
  // Called from renderState() on every state change.
  render() {
    if (!this.active()) return;
    const $ = this.$;
    const s = this.session;
    const d = this.day;
    const ok = this.ready();
    const set = (id, text, cls) => { const el = $(id); el.textContent = text; if (cls !== undefined) el.className = cls; };
    set('day-date', ok ? d.date : '–');
    set('day-time', ok ? d.local_time.slice(11) : '');
    const phase = $('day-phase');
    phase.className = 'badge';
    if (!ok) phase.textContent = '';
    else if (s.finished) { phase.textContent = 'end of replay'; phase.classList.add('good'); }
    else if (d.phase === 'exited') { phase.textContent = 'trade closed'; phase.classList.add('warn'); }
    else if (d.trade) { phase.textContent = 'in a trade'; phase.classList.add('good'); }
    else phase.textContent = 'no trade';
    this.renderProgress(ok ? s : null);

    const chk = ok ? d.check : null;
    if (chk) {
      const ema = chk.ema_above;
      const detail = chk.ema200 === null ? 'EMA 200 n/a' : `close ${this.c.fmtPrice(chk.daily_close)} vs EMA ${this.c.fmtPrice(chk.ema200)}`;
      set('day-ema', this.flag(ema), `mono gauge ${ema === null ? '' : ema ? 'up' : 'down'}`);
      $('day-ema').title = detail;
    } else {
      set('day-ema', '–', 'mono gauge');
      $('day-ema').title = '';
    }
    const gate = $('day-gate');
    gate.textContent = ok ? (d.entries_allowed
      ? (d.check.ema_above === null ? 'Entries allowed. EMA 200 not available: a trade saves the trend as unknown.'
        : d.check.ema_above ? 'Entries allowed. Daily close above the EMA 200.' : 'Entries allowed. Daily close below the EMA 200 (saved with the trade).')
      : d.block_reason) : '';
    gate.className = `small ${ok && d.entries_allowed ? 'up' : 'muted'}`;

    if (this.panelOpen()) {
      if (!ok || s.finished) this.closeNewTrade();
      else if (s.cursor_ms !== this.planCursor) { this.planCursor = s.cursor_ms; this.setPanelTitle(); this.requestPlan(); }
    }
    const ta = $('day-notes');
    ta.disabled = !ok;
    if (ok && d.date !== this.notesDate) { clearTimeout(this.notesTimer); this.notesDate = d.date; ta.value = d.check.notes || ''; $('day-notes-state').textContent = ''; }
    else if (!ok && this.notesDate !== null) { this.notesDate = null; ta.value = ''; }
    const busy = this.busy || this.state.busy;
    $('btn-day-new').disabled = !ok || busy || !d.entries_allowed;
    $('btn-day-sell').disabled = !ok || busy || !d.can_sell;
    $('btn-day-skip').disabled = !ok || busy || !d.can_skip;
    $('btn-day-ema').disabled = !ok || busy || !d.can_skip;   // any state without an open trade or an exit choice
    $('btn-day-signal').disabled = !ok || busy || !d.can_skip || !$('day-strategy').value;
    $('btn-day-zone').disabled = !ok || busy;
    this.renderInlineReview(ok ? d.review_pending : null);
    this.renderExitChoice(ok && d.exit_choice ? d.last_exit : null);

    // the open trade
    const t = ok ? d.trade : null;
    $('day-trade').classList.toggle('hidden', !t);
    if (t) {
      const fp = this.c.fmtPrice;
      const away = (v, word) => (v === null || v === undefined ? '' : ` · ${Math.abs(v).toFixed(2)}% ${word}`);
      const stopMoved = t.stop_initial !== undefined && t.stop_initial !== t.stop_used;
      const tpMoved = t.tp_initial !== undefined && t.tp_initial !== t.take_profit;
      const moves = (t.changes || []).length;
      const conf = t.confidence === null || t.confidence === undefined ? '' : ` · confidence ${t.confidence}/10`;
      set('dt-seq', `#${t.seq} · since ${t.entry_local.slice(11)} (${this.holdText(this.holdHours(t.entry_ms, null))})${conf}${t.overnight ? ' · overnight' : ''}${moves ? ` · moved ${moves}×` : ''}`);
      $('dt-seq').title = moves ? this.changesText(t) : '';
      set('dt-entry', fp(t.entry_price));
      const usdt = t.price_change_usdt === undefined ? t.coins * (this.session.price - t.entry_price) : t.price_change_usdt;
      set('dt-change', `${this.signed(usdt)} USDT · ${this.pctText(t.price_change_pct)}`, `mono gauge ${this.cls(t.price_change_pct)}`);
      set('dt-auto', this.autoText(d.auto));
      set('dt-ac', this.acText(t.exits_ac));
      set('dt-stop', `${fp(t.stop_used)} · ${this.pctText(t.stop_pct)}`);
      $('dt-stop').title = `${this.pctText(t.stop_pct)} from the entry${away(t.to_stop_pct, 'below the price now')}${stopMoved ? ` · at the buy ${fp(t.stop_initial)} (${this.pctText((t.stop_initial / t.entry_price - 1) * 100)}) · ${this.changesText(t)}` : ''}`;
      set('dt-tp', `${fp(t.take_profit)} · ${this.pctText(t.tp_pct)}`);
      $('dt-tp').title = `${this.pctText(t.tp_pct)} from the entry${away(t.to_tp_pct, 'above the price now')}${tpMoved ? ` · at the buy ${fp(t.tp_initial)} (${this.pctText((t.tp_initial / t.entry_price - 1) * 100)}) · ${this.changesText(t)}` : ''}`;
      set('dt-size', `${this.c.qtyFmt(t.coins)} ${this.c.baseAsset()} = ${this.n(t.position_value)} USDT${t.capped ? ' · capped' : ''}`);
      const rrNow = t.rr_now === undefined ? t.rr : t.rr_now;
      set('dt-risk', `${this.n(t.risk_usdt)} USDT · R/R ${this.n(rrNow)}`);
      $('dt-risk').title = rrNow !== t.rr ? `Reward-to-risk now; at the buy ${this.n(t.rr)}` : 'Reward-to-risk of the trade';
      set('dt-pnl', `${this.signed(t.unrealized_pnl)} (${this.pctText(t.unrealized_pnl_pct)}) · ${this.signed(t.r_now, 2)} R`, `mono gauge ${this.cls(t.unrealized_pnl)}`);
    }
    this.c.chart.setDayLines(t);
    this.c.renderCardSums();   // the summary of the minimized card
  }

  // ------------------------------------------------- move the stop / target
  // (by dragging the dotted lines on the chart; see onDragLine)
  // One line per saved move, oldest first.
  changesText(t) {
    const fp = this.c.fmtPrice;
    const pe = (v) => this.pctText((v / t.entry_price - 1) * 100);
    return (t.changes || []).map(c => `${c.at_local.slice(11)} ${c.field === 'stop' ? 'stop' : 'target'} ${pe(c.from)} \u2192 ${pe(c.to)}`).join(' · ') || 'no moves';
  }
  // Send a move to the server; `onError` shows the message (default: a toast). Returns true when saved.
  async moveTrade(body, onError = null) {
    if (!this.ready() || this.busy || !this.day.trade) return false;
    const tok = this.c.token();
    this.busy = true;
    this.render();
    try {
      const res = await this.c.api.dayMoveTrade(this.session.id, body);
      if (!this.c.fresh(tok)) return false;
      this.c.applyState(res.state);
      const t = res.trade;
      const fp = this.c.fmtPrice;
      const what = [];
      if ('stop' in body) what.push(`stop ${fp(t.stop_used)}`);
      if ('take_profit' in body) what.push(`target ${fp(t.take_profit)}`);
      this.c.toast(`Moved: ${what.join(', ')} (saved with trade #${t.seq}).`);
      await this.refresh();
      return true;
    } catch (e) {
      if (this.c.fresh(tok)) { if (onError) onError(e.message); else this.c.toast(e.message, true); }
      return false;
    } finally { this.busy = false; this.render(); }
  }

  // ---------------------------------------------------------------- zones
  startDraw() {
    if (!this.ready() || this.drawing) return;
    if (!ZONE_INTERVALS.includes(this.state.interval) || this.state.peek) {
      this.c.toast('Switch to the 1h or 4h chart to draw a zone.', true);
      return;
    }
    this.c.cancelFibDraw();
    this.drawing = true;
    this.draft = null;
    this.$('chart').classList.add('drawing');
    this.c.showHint('Zone 1 of 2: click one edge of the zone. Clicks snap to the candle high or low. Esc cancels.');
  }
  cancelDraw() {
    if (!this.drawing && this.draft === null) return;
    this.drawing = false;
    this.draft = null;
    this.c.chart.setZoneDraft(null);
    this.$('chart').classList.remove('drawing');
    this.c.showHint('');
  }
  // Returns true when the click was consumed (zone drawing).
  async onChartClick(param) {
    if (!this.active()) return false;
    if (!this.drawing) {
      if (this.selectedZone !== null) { this.selectedZone = null; this.renderZones(); }
      return false;
    }
    const snap = this.c.chart.snapClick(param);
    if (!snap) return true;
    if (this.draft === null) {
      this.draft = snap.price;
      this.c.chart.setZoneDraft(snap.price);
      this.c.showHint(`Zone 2 of 2: ${snap.kind} ${this.c.fmtPrice(snap.price)} taken. Click the other edge.`);
      return true;
    }
    if (snap.price === this.draft) { this.c.toast('The second edge needs a different price', true); return true; }
    const t = this.c.token();
    try {
      const z = await this.c.api.dayAddZone(this.session.id, { interval: this.state.interval, p1: this.draft, p2: snap.price });
      if (!this.c.fresh(t)) return true;
      this.cancelDraw();
      this.zones.push(z);
      this.selectedZone = z.id;
      this.renderZones();
      this.c.toast(`Zone drawn: ${this.c.fmtPrice(z.lower)} – ${this.c.fmtPrice(z.upper)} (${z.interval})`);
    } catch (e) { if (this.c.fresh(t)) { this.c.toast(e.message, true); this.cancelDraw(); } }
    return true;
  }
  renderZones() {
    const $ = this.$;
    this.c.chart.setUserZones(this.active() ? this.zones : [], this.selectedZone);
    const ul = $('day-zones');
    ul.innerHTML = '';
    for (const z of this.zones) {
      const li = document.createElement('li');
      li.className = `zone${z.id === this.selectedZone ? ' selected' : ''}`;
      const sw = document.createElement('span'); sw.className = 'sw';
      const name = document.createElement('span');
      name.className = 'name small';
      name.textContent = `${z.interval} `;
      const lo = document.createElement('input'); lo.type = 'number'; lo.step = 'any'; lo.className = 'short'; lo.value = String(z.lower); lo.setAttribute('aria-label', 'Lower edge');
      const hi = document.createElement('input'); hi.type = 'number'; hi.step = 'any'; hi.className = 'short'; hi.value = String(z.upper); hi.setAttribute('aria-label', 'Upper edge');
      const save = () => this.updateZone(z.id, parseFloat(lo.value), parseFloat(hi.value));
      lo.addEventListener('change', save);
      hi.addEventListener('change', save);
      const dash = document.createElement('span'); dash.textContent = '–'; dash.className = 'muted';
      const del = document.createElement('button');
      del.type = 'button'; del.textContent = '×'; del.setAttribute('aria-label', 'Delete zone');
      del.addEventListener('click', (e) => { e.stopPropagation(); this.deleteZone(z.id); });
      li.append(sw, name, lo, dash, hi, del);
      li.addEventListener('click', (e) => { if (e.target.tagName !== 'INPUT') { this.selectedZone = this.selectedZone === z.id ? null : z.id; this.renderZones(); } });
      ul.appendChild(li);
    }
    $('day-zones-empty').classList.toggle('hidden', this.zones.length > 0);
    $('day-zone-count').textContent = String(this.zones.length);
  }
  async updateZone(id, p1, p2) {
    if (!this.ready()) return;
    if (!Number.isFinite(p1) || !Number.isFinite(p2) || p1 <= 0 || p2 <= 0 || p1 === p2) { this.c.toast('A zone needs two different positive prices', true); this.renderZones(); return; }
    const t = this.c.token();
    try {
      const z = await this.c.api.dayUpdateZone(this.session.id, id, { p1, p2 });
      if (!this.c.fresh(t)) return;
      this.zones = this.zones.map(x => (x.id === id ? z : x));
      this.renderZones();
    } catch (e) { if (this.c.fresh(t)) { this.c.toast(e.message, true); this.renderZones(); } }
  }
  async deleteZone(id) {
    if (!this.ready()) return;
    const t = this.c.token();
    try {
      await this.c.api.dayDeleteZone(this.session.id, id);
      if (!this.c.fresh(t)) return;
      this.zones = this.zones.filter(z => z.id !== id);
      if (this.selectedZone === id) this.selectedZone = null;
      this.renderZones();
    } catch (e) { if (this.c.fresh(t)) this.c.toast(e.message, true); }
  }

  // ------------------------------------------------------------ new trade
  // A floating panel over the chart (not a modal dialog), so the replay can go on while the plan is
  // open: the entry follows the current candle, the stop defaults to the middle of the chosen zone
  // and moves with +/- or the arrow keys, the target comes from the reward-to-risk ratio, and the
  // green / red hatched areas on the chart show the plan. Buy only by clicking the button.
  panelOpen() { return !this.$('nt-panel').classList.contains('hidden'); }
  openNewTrade() {
    const d = this.day;
    if (!this.ready() || this.busy || !d.entries_allowed) { if (d && d.block_reason) this.c.toast(d.block_reason, true); return; }
    if (!this.zones.length) { this.c.toast('Draw a support zone first (Z on the 1h or 4h chart).', true); return; }
    const $ = this.$;
    const price = this.session.price;
    const sel = $('nt-zone');
    sel.innerHTML = '';
    // default: the zone the price is inside, else the nearest one below the price, else the first
    const inside = this.zones.find(z => z.lower <= price && price <= z.upper);
    const below = this.zones.filter(z => z.upper < price).sort((a, b) => b.upper - a.upper)[0];
    const pick = this.selectedZone !== null ? this.zones.find(z => z.id === this.selectedZone) : null;
    const def = pick || inside || below || this.zones[0];
    for (const z of this.zones) {
      const o = document.createElement('option');
      o.value = String(z.id);
      o.textContent = `${z.interval} · ${this.c.fmtPrice(z.lower)} – ${this.c.fmtPrice(z.upper)}${z.lower <= price && price <= z.upper ? ' (price inside)' : ''}`;
      o.selected = z.id === def.id;
      sel.appendChild(o);
    }
    $('nt-stop').value = '';
    if (!(parseFloat($('nt-ratio').value) > 0)) $('nt-ratio').value = '2';   // the last ratio stays, 1:2 at first
    $('nt-notes').value = '';
    this.confidence = null;          // the score is picked anew for every trade
    this.renderConfidence();
    $('nt-error').textContent = '';
    $('nt-warn').textContent = '';
    this.plan = null;
    this.planCursor = this.session.cursor_ms;
    this.renderPlan(null);
    this.setPanelTitle();
    $('nt-panel').classList.remove('hidden');
    $('nt-panel').closest('.chart-area').classList.add('planning');
    $('nt-panel').focus();   // the arrow keys move the stop; no input is focused
    // room to the right of the last candle, so the green / red plan areas are easy to see
    const r = this.c.chart.currentRange();
    this.planShift = null;
    if (r) { this.planShift = { from: r.from + PLAN_MARGIN_BARS, to: r.to + PLAN_MARGIN_BARS }; this.c.chart.setRange(this.planShift); }
    this.requestPlan();
  }
  closeNewTrade() {
    const $ = this.$;
    clearTimeout(this.planTimer);
    this.planSeq++;
    this.plan = null;
    $('nt-panel').classList.add('hidden');
    $('nt-panel').closest('.chart-area').classList.remove('planning');
    this.c.chart.setPlan(null);
    const r = this.c.chart.currentRange();   // undo the shift unless the user scrolled or zoomed meanwhile
    if (r && this.planShift && Math.abs(r.to - this.planShift.to) < 1.5 && Math.abs(r.from - this.planShift.from) < 1.5) {
      this.c.chart.setRange({ from: r.from - PLAN_MARGIN_BARS, to: r.to - PLAN_MARGIN_BARS });
    }
    this.planShift = null;
  }
  setPanelTitle() {
    const d = this.day;
    this.$('nt-title').textContent = `New trade · ${this.c.baseAsset()} @ ${this.c.fmtPrice(this.session.price)} · ${d.local_time.slice(11)}`;
  }
  // The stop moves in steps of 0.05 % of the entry price (Shift: five steps).
  stopStep(big) {
    const entry = this.plan ? this.plan.entry_price : this.session.price;
    return entry * 0.0005 * (big ? 5 : 1);
  }
  currentStop() {
    const v = parseFloat(this.$('nt-stop').value);
    if (Number.isFinite(v) && v > 0) return v;
    return this.plan && this.plan.stop_used !== null ? this.plan.stop_used : null;
  }
  roundPrice(v) {
    const d = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
    return String(Math.round(v * 10 ** d) / 10 ** d);
  }
  moveStop(dir, big) {
    if (!this.panelOpen()) return;
    const cur = this.currentStop();
    if (cur === null) return;
    const v = cur + dir * this.stopStep(big);
    if (v <= 0) return;
    this.$('nt-stop').value = this.roundPrice(v);
    this.requestPlan();
  }
  setStop(value) {   // null = back to the default (the zone middle)
    this.$('nt-stop').value = value === null || value === undefined ? '' : this.roundPrice(value);
    this.requestPlan();
  }
  setRatio(r) {
    this.$('nt-ratio').value = String(r);
    this.requestPlan();
  }
  requestPlan() {
    clearTimeout(this.planTimer);
    this.planTimer = setTimeout(() => this.fetchPlan(), PLAN_DEBOUNCE_MS);
  }
  planBody() {
    const $ = this.$;
    const num = (id) => { const v = parseFloat($(id).value); return Number.isFinite(v) && v > 0 ? v : null; };
    return { zone_id: parseInt($('nt-zone').value, 10) || null, stop: num('nt-stop'), ratio: num('nt-ratio') };
  }
  async fetchPlan() {
    if (!this.ready() || !this.panelOpen()) return;
    const seq = ++this.planSeq;
    const t = this.c.token();
    try {
      const p = await this.c.api.dayPlan(this.session.id, this.planBody());
      if (!this.c.fresh(t) || seq !== this.planSeq || !this.panelOpen()) return;
      this.plan = p;
      this.renderPlan(p);
    } catch (e) {
      if (!this.c.fresh(t) || seq !== this.planSeq) return;
      this.plan = null;
      this.renderPlan(null);
      this.$('nt-error').textContent = e.message;
    }
  }
  renderPlan(p) {
    const $ = this.$;
    const fp = this.c.fmtPrice;
    const set = (id, text, cls) => { const el = $(id); el.textContent = text; if (cls !== undefined) el.className = `mono ${cls}`.trim(); };
    const ratio = parseFloat($('nt-ratio').value);
    for (const b of $('nt-panel').querySelectorAll('.nt-ratio-chip')) b.classList.toggle('on', parseFloat(b.dataset.ratio) === ratio);
    if (!p) {
      for (const id of ['np-entry', 'np-atr', 'np-mid', 'np-atrstop', 'np-stop', 'np-tp', 'np-coins', 'np-value', 'np-fee', 'np-risk', 'np-rr']) set(id, '–', '');
      $('nt-rules').innerHTML = '';
      $('btn-nt-buy').disabled = true;
      $('btn-stop-atr').disabled = true;
      this.c.chart.setPlan(null);
      return;
    }
    const pct = (v) => this.pctText((v / p.entry_price - 1) * 100);
    set('np-entry', fp(p.entry_price));
    set('np-atr', p.atr === null ? 'n/a' : fp(p.atr));
    set('np-mid', fp(p.stop_suggested));
    set('np-atrstop', p.stop_atr === null ? 'n/a' : fp(p.stop_atr));
    $('btn-stop-atr').disabled = p.stop_atr === null;
    $('nt-stop').placeholder = this.roundPrice(p.stop_suggested);
    const stopOk = p.stop_used !== null && p.stop_used < p.entry_price;
    set('np-stop', p.stop_used === null ? '–' : `${fp(p.stop_used)}${stopOk ? ` (${pct(p.stop_used)})` : ''}`, stopOk ? '' : 'down');
    set('np-tp', p.take_profit === null ? '–' : `${fp(p.take_profit)} (${pct(p.take_profit)})`);
    set('np-coins', p.coins === null ? '–' : `${this.c.qtyFmt(p.coins)} ${this.c.baseAsset()}`);
    set('np-value', p.position_value === null ? '–' : `${this.n(p.position_value)} USDT${p.capped ? ' · capped by the cash' : ''}`, p.capped ? 'down' : '');
    set('np-fee', p.buy_fee === null ? '–' : `${this.n(p.buy_fee)} USDT`);
    set('np-risk', p.risk_usdt === null ? '–' : `${this.n(p.risk_usdt)} USDT of ${this.n(p.risk_amount)} (${this.day.risk_pct} %)`, p.capped ? 'down' : '');
    const rrCls = p.rr === null ? '' : p.rr >= p.rr_warn_below ? 'up' : 'down';
    set('np-rr', p.rr === null ? '–' : `${this.n(p.rr)} : 1`, rrCls);
    const sg = p.signals;
    const rules = [
      ['1. Daily close above the EMA 200', sg.ema_above],
      ['2. Price inside the support zone', sg.price_in_zone],
      [`3. RSI 14 (15m) dipped to ${this.day.rsi_dip_max} or below today · now ${sg.rsi === null ? '–' : sg.rsi.toFixed(1)}, day low ${sg.rsi_day_min === null ? '–' : sg.rsi_day_min.toFixed(1)}`, sg.rsi_dipped],
      [`4. Trigger: this candle closed above the previous high${sg.prev_high === null ? '' : ` (${fp(sg.prev_high)})`}`, sg.close_above_prev_high],
    ];
    const ul = $('nt-rules');
    ul.innerHTML = '';
    for (const [text, v] of rules) {
      const li = document.createElement('li');
      li.className = v === null ? 'muted' : v ? 'up' : 'down';
      li.textContent = `${v === null ? '–' : v ? '✓' : '✗'} ${text}`;
      ul.appendChild(li);
    }
    $('nt-warn').textContent = p.warnings.join(' ');
    $('nt-error').textContent = [p.blocked, ...p.errors].filter(Boolean).join(' ');
    $('btn-nt-buy').disabled = !p.can_buy || this.busy || this.confidence === null;
    $('btn-nt-buy').title = p.can_buy && this.confidence === null ? 'Pick your confidence (0 to 10) first.' : '';
    // the plan on the chart: green hatched up to the target, red down to the stop
    this.c.chart.setPlan(stopOk || p.take_profit !== null ? {
      price: p.entry_price,
      tp: p.take_profit, tpPct: p.take_profit === null ? null : ((p.take_profit / p.entry_price - 1) * 100).toFixed(2),
      sl: stopOk ? p.stop_used : null, slPct: stopOk ? ((1 - p.stop_used / p.entry_price) * 100).toFixed(2) : null,
    } : null);
  }
  // The confidence score, 0..10, picked with the chips in the New trade form (required for the buy).
  renderConfidence() {
    const box = this.$('nt-conf');
    if (!box.childElementCount) {
      for (let i = 0; i <= 10; i++) {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'btn chip nt-conf-chip'; b.textContent = String(i); b.dataset.conf = String(i);
        b.addEventListener('click', () => this.setConfidence(i));
        box.appendChild(b);
      }
    }
    for (const b of box.children) {
      const on = parseInt(b.dataset.conf, 10) === this.confidence;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }
  setConfidence(n) {
    this.confidence = n;
    this.renderConfidence();
    this.renderPlan(this.plan);   // the Buy button follows
  }
  async confirmBuy() {
    if (!this.ready() || this.busy || !this.plan || !this.plan.can_buy || !this.panelOpen()) return;
    if (this.confidence === null) { this.$('nt-error').textContent = 'Pick your confidence (0 to 10) first.'; return; }
    const body = { ...this.planBody(), notes: this.$('nt-notes').value.trim() || null, confidence: this.confidence };
    const t = this.c.token();
    this.busy = true;
    this.render();
    try {
      const res = await this.c.api.dayBuy(this.session.id, body);
      if (!this.c.fresh(t)) return;
      this.closeNewTrade();
      this.c.applyState(res.state);
      const tr = res.trade;
      this.c.toast(`Trade #${tr.seq}: bought ${this.c.qtyFmt(tr.coins)} ${this.c.baseAsset()} @ ${this.c.fmtPrice(tr.entry_price)} · stop ${this.c.fmtPrice(tr.stop_used)} · target ${this.c.fmtPrice(tr.take_profit)} (1 : ${this.n(tr.rr)})`);
      await this.refresh();
    } catch (e) { if (this.c.fresh(t)) this.$('nt-error').textContent = e.message; }
    finally { this.busy = false; this.render(); }
  }

  // ------------------------------------------------------------ early exit
  openExit() {
    const d = this.day;
    if (!this.ready() || this.busy || !d.can_sell) return;
    const $ = this.$;
    const list = $('exit-reasons');
    list.innerHTML = '';
    for (const r of d.exit_reasons) {
      const label = document.createElement('label');
      const rb = document.createElement('input');
      rb.type = 'radio'; rb.name = 'exit-reason'; rb.value = r.key;
      label.append(rb, document.createTextNode(r.label));
      list.appendChild(label);
    }
    $('exit-note').value = '';
    $('exit-error').textContent = '';
    // exits A / C: an exit already closed can't be chosen; Both while both are open, else the one left
    const open = d.open_exits || ['A', 'C'];
    for (const rb of $('exit-legs').querySelectorAll('input')) {
      rb.disabled = rb.value !== 'both' && !open.includes(rb.value);
      rb.checked = open.length === 2 ? rb.value === 'both' : rb.value === open[0];
    }
    $('exit-legs').querySelector('input[value="both"]').disabled = open.length < 2;
    const t = d.trade;
    $('exit-title').textContent = `Sell trade #${t.seq} @ ${this.c.fmtPrice(this.session.price)} · ${this.signed(t.unrealized_pnl)} USDT (${this.signed(t.r_now)} R)`;
    $('dlg-exit').showModal();
    const first = list.querySelector('input');
    if (first) first.focus();
  }
  async confirmExit() {
    if (!this.ready() || this.busy) return;
    const $ = this.$;
    const picked = $('exit-reasons').querySelector('input:checked');
    if (!picked) { $('exit-error').textContent = 'Choose a reason.'; return; }
    const note = $('exit-note').value.trim() || null;
    if (picked.value === 'other' && !note) { $('exit-error').textContent = 'Say what the reason is under Other.'; $('exit-note').focus(); return; }
    const t = this.c.token();
    this.busy = true;
    this.render();
    try {
      const leg = $('exit-legs').querySelector('input:checked');
      const st = await this.c.api.daySell(this.session.id, { reason: picked.value, note, exits: leg ? leg.value : 'both' });
      if (!this.c.fresh(t)) return;
      $('dlg-exit').close();
      const events = st.events || [];
      delete st.events;
      this.c.applyState(st);
      await this.handleEvents(events);
    } catch (e) { if (this.c.fresh(t)) $('exit-error').textContent = e.message; }
    finally { this.busy = false; this.render(); }
  }

  // ------------------------------------------------------------------ skip
  async skipDay() {
    if (!this.ready() || this.busy || !this.day.can_skip) return;
    await this.jump(() => this.c.api.daySkip(this.session.id));
  }
  // Jump to the next trading day whose daily close is above the EMA 200 (the trend check's first line).
  async nextEmaDay() {
    if (!this.ready() || this.busy || !this.day.can_skip) return;
    await this.jump(() => this.c.api.dayNextEma(this.session.id));
  }
  // Jump to the selected strategy's next entry signal; the zone it came from is added and selected.
  async nextSignal() {
    if (!this.ready() || this.busy || !this.day.can_skip) return;
    const key = this.$('day-strategy').value;
    if (!key) return;
    await this.jump(() => this.c.api.dayNextSignal(this.session.id, { strategy: key }), (events) => {
      const ev = events.find(x => x.kind === 'jump' && x.signal);
      if (!ev) return;
      if (this.zones.some(z => z.id === ev.zone_id)) { this.selectedZone = ev.zone_id; this.renderZones(); }
      this.$('day-signal-last').textContent = `${ev.to_local} · ${ev.reason}`;
    });
  }
  async loadStrategies() {
    try { this.strategies = await this.c.api.dayStrategies(); } catch { this.strategies = []; }
    const sel = this.$('day-strategy');
    sel.innerHTML = '';
    for (const s of this.strategies) {
      const o = document.createElement('option');
      o.value = s.key; o.textContent = `${s.name} (${s.interval})`;
      sel.appendChild(o);
    }
    this.renderStrategyNote();
    this.render();
  }
  renderStrategyNote() {
    const s = this.strategies.find(x => x.key === this.$('day-strategy').value);
    this.$('day-strategy-note').textContent = s ? s.description : 'No strategies available.';
  }
  // After an exit: "End the day" jumps to 24:00 of the exit's day, which is 00:00 of the next: the day
  // is left (its picture first) and the review is asked there; "Keep trading" goes on from the exit candle.
  async endDay() {
    if (!this.ready() || this.busy || !this.day.exit_choice) return;
    await this.jump(() => this.c.api.dayEndDay(this.session.id));
    this.reviewToast();
  }
  async keepTrading() {
    if (!this.ready() || this.busy || !this.day.exit_choice) return;
    this.busy = true;
    this.render();
    const t = this.c.token();
    try {
      const st = await this.c.api.dayKeepTrading(this.session.id);
      if (!this.c.fresh(t)) return;
      this.c.applyState(st);
      this.c.toast('Keep trading: buy again or step on. The review comes at 24:00.');
    } catch (e) { if (this.c.fresh(t)) this.c.toast(e.message, true); }
    finally { this.busy = false; this.render(); }
  }
  renderExitChoice(t) {
    const $ = this.$;
    $('day-exit-choice').classList.toggle('hidden', !t);
    if (!t) return;
    $('dx-title').textContent = `Trade #${t.seq} closed (${t.exit_type}) \u00b7 ${this.signed(t.pnl)} USDT (${this.signed(t.r_multiple)} R)`;
    $('dx-sub').textContent = 'End the day here, or keep trading as if no trade had happened today.';
    const busy = this.busy || this.state.busy;
    $('btn-dx-end').disabled = $('btn-dx-keep').disabled = busy;
  }
  exitChoiceOpen() { return !this.$('day-exit-choice').classList.contains('hidden'); }
  // Every jump leaves the trading day (since days start at 00:00 also "End the day"), so its journal picture is taken first.
  async jump(call, after = null) {
    this.busy = true;
    this.render();
    try {
      await this.captureDayPicture();   // the journal picture of the day being left (also switches the generation)
    } catch (e) { this.c.toast(`Day picture not saved: ${e.message}`, true); }
    const t = this.c.token();
    try {
      const st = await call();
      if (!this.c.fresh(t)) return;
      const events = st.events || [];
      delete st.events;
      this.c.applyState(st);
      await this.c.refreshView();
      for (const ev of events) if (ev.kind === 'jump') this.c.toast(`${ev.reason} Now ${ev.to_local}.`);
      await this.refresh();   // the journal row of the day just left (how it ended) and the new day's row
      await this.reviewPicture();   // a jump past an exit's 24:00 makes its review due
      if (after && this.c.fresh(t)) after(events);
    } catch (e) { if (this.c.fresh(t)) this.c.toast(e.message, true); }
    finally { this.busy = false; this.render(); }
  }

  // ----------------------------------------------------------------- events
  // Events of a step or a sell. After an exit by day the replay stays on the exit candle and the
  // side panel asks: end the day (jump to 24:00) or keep trading. The review and the trade's picture
  // (15m, from the entry's 00:00 to that 24:00) come once the replay reaches that 24:00 (a 'review'
  // event). A jump is only announced.
  async handleEvents(events) {
    if (!this.active()) return;
    for (const ev of events || []) {
      if (ev.kind === 'jump') { this.c.toast(`${ev.reason} Now ${ev.to_local}.`); continue; }
      if (ev.kind === 'review') { this.c.pause(); await this.refresh(); continue; }
      if (ev.kind === 'exit_leg') {   // exit A or C closed, the other one keeps the trade open
        const legs = ev.trade.exits_ac;
        const done = ev.closed.map(k => `Exit ${k} closed @ ${this.c.fmtPrice(legs[k].exit_price)} · ${this.signed(legs[k].r_multiple)} R`).join(', ');
        this.c.toast(`${done}. ${ev.open.join(' and ')} still open: the trade stays open.`);
        await this.refresh();
        continue;
      }
      if (ev.kind !== 'exit') continue;
      this.c.pause();
      const tr = ev.trade;
      this.c.toast(`Trade #${tr.seq} closed (${tr.exit_type}) @ ${this.c.fmtPrice(tr.exit_price)}: ${this.signed(tr.pnl)} USDT (${this.pctText(tr.pnl_pct)}, ${this.signed(tr.r_multiple)} R)`, tr.pnl < 0);
      await this.refresh();
    }
    await this.reviewPicture();
    if (events.some(ev => ev.kind === 'exit' || ev.kind === 'review')) this.reviewToast();
  }
  reviewToast() {
    const d = this.day;
    if (!this.ready()) return;
    if (d.review_pending) this.c.toast(`Review trade #${d.review_pending.seq} in the side panel: keys 1 to 4 switch the marks, Enter saves. The replay waits.`);
    else if (d.exit_choice) this.c.toast('Trade closed. End the day (Enter) or keep trading (K) in the side panel.');
  }
  // The picture of the trade whose review just became due, taken once (at its 24:00 or later).
  async reviewPicture() {
    const d = this.day;
    if (!this.ready() || !d.review_pending || d.review_pending.has_image) return;
    await this.capturePicture(d.review_pending);
  }

  // ---------------------------------------------------------------- columns
  // The Trades table shows a short set of columns by default; the "Hide cols" menu adds or removes
  // any of them, and the choice is kept in this browser.
  // "A 10:00 @ 101.00 · +0.45 R · C open": exits A and C of a trade in one line.
  acText(ac) {
    if (!ac) return '–';
    return ['A', 'C'].map(k => {
      const x = ac[k];
      return !x || x.status === 'open' ? `${k} open` : `${k} ${x.exit_local.slice(11)} @ ${this.c.fmtPrice(x.exit_price)} \u00b7 ${this.signed(x.r_multiple)} R`;
    }).join(' \u00b7 ');
  }
  legText(x) {
    if (!x || x.status === 'open') return 'open';
    const fp = this.c.fmtPrice;
    return `${x.exit_type === 'take-profit' ? 'target' : x.exit_type} at ${x.exit_local} @ ${fp(x.exit_price)}${x.reason_label ? ` (${x.reason_label}${x.note ? `: ${x.note}` : ''})` : ''} \u00b7 ${this.signed(x.pnl)} USDT \u00b7 ${this.signed(x.r_multiple)} R${x.diff === null ? '' : ` \u00b7 ${this.signed(x.diff)} USDT vs the real exit`}`;
  }
  altText(x) {
    const fp = this.c.fmtPrice;
    return `${x.exit_type === 'take-profit' ? 'target' : x.exit_type} at ${x.exit_local} @ ${fp(x.exit_price)} \u00b7 ${this.signed(x.pnl)} USDT \u00b7 ${this.signed(x.r_multiple)} R \u00b7 stop ${fp(x.stop)}, target ${fp(x.take_profit)}${x.diff === null ? '' : ` \u00b7 ${this.signed(x.diff)} USDT vs your exit`}`;
  }
  colNames() { return Array.from(this.$('day-trade-table').querySelectorAll('thead th')).map(th => th.dataset.col); }
  loadCols() {
    try { const v = JSON.parse(localStorage.getItem('day.cols.hidden') || 'null'); if (Array.isArray(v)) return new Set(v); } catch { /* no storage */ }
    return null;
  }
  hiddenSet() {
    if (this.hiddenCols === null) this.hiddenCols = new Set(this.colNames().filter(c => !DAY_COLS_DEFAULT.includes(c)));
    return this.hiddenCols;
  }
  saveCols() { try { localStorage.setItem('day.cols.hidden', JSON.stringify([...this.hiddenSet()])); } catch { /* ignore */ } }
  applyCols() {
    const hidden = this.hiddenSet();
    const names = this.colNames();
    const table = this.$('day-trade-table');
    table.querySelectorAll('thead th').forEach((th, i) => th.classList.toggle('col-hidden', hidden.has(names[i])));
    for (const tr of table.querySelectorAll('tbody tr')) tr.querySelectorAll('td').forEach((td, i) => td.classList.toggle('col-hidden', hidden.has(names[i])));
    for (const cb of this.$('day-cols-list').querySelectorAll('input')) cb.checked = !hidden.has(cb.value);
  }
  buildColsMenu() {
    const list = this.$('day-cols-list');
    list.innerHTML = '';
    for (const th of this.$('day-trade-table').querySelectorAll('thead th')) {
      const lab = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.value = th.dataset.col;
      cb.addEventListener('change', () => { const h = this.hiddenSet(); if (cb.checked) h.delete(cb.value); else h.add(cb.value); this.saveCols(); this.applyCols(); });
      lab.appendChild(cb); lab.appendChild(document.createTextNode(th.textContent.trim()));
      if (th.title) lab.title = th.title;
      list.appendChild(lab);
    }
    const foot = document.createElement('div'); foot.className = 'cols-foot';
    const reset = document.createElement('button'); reset.type = 'button'; reset.className = 'btn small'; reset.textContent = 'Default columns';
    reset.addEventListener('click', () => { this.hiddenCols = null; try { localStorage.removeItem('day.cols.hidden'); } catch { /* ignore */ } this.applyCols(); });
    const all = document.createElement('button'); all.type = 'button'; all.className = 'btn small'; all.textContent = 'Show all';
    all.addEventListener('click', () => { this.hiddenCols = new Set(); this.saveCols(); this.applyCols(); });
    foot.appendChild(reset); foot.appendChild(all);
    list.appendChild(foot);
    this.applyCols();
  }

  // ----------------------------------------------------------------- review
  // Four marks per closed trade, asked at 24:00 of the exit day (the replay waits for them) and
  // editable later from the trade dialog. 'over the target' is pre-ticked from the candles after the exit.
  reviewTags(t) {
    const r = t.review;
    if (!r || !r.reviewed) return null;
    return [['stop_tight', 'tight stop'], ['over_target', 'over target'], ['stop_saved', 'stop saved'], ['stop_moved_up', 'stop moved too far up']].filter(([k]) => r[k]).map(([, label]) => label);
  }
  reviewCount(t) { const tags = this.reviewTags(t); return tags === null ? -1 : tags.length; }
  reviewTitle(t) { const fp = this.c.fmtPrice; return `Review trade #${t.seq} \u00b7 ${t.exit_type} @ ${fp(t.exit_price)} \u00b7 ${this.signed(t.pnl)} USDT (${this.signed(t.r_multiple)} R)`; }
  reviewSub(t) { const fp = this.c.fmtPrice; return `${t.day_date} \u00b7 entry ${t.entry_local.slice(11)} @ ${fp(t.entry_price)} \u00b7 exit ${(t.exit_local || '').slice(11)} \u00b7 held ${this.holdText(t.hold_hours)} \u00b7 original stop ${fp(t.stop_initial)} \u00b7 original target ${fp(t.tp_initial)}`; }
  reviewWindowText(t) {
    const fp = this.c.fmtPrice; const w = t.whatif; const r = t.review || {};
    return w
      ? `After the exit until 24:00: high ${fp(w.high)} (${this.pctText(w.above_exit_pct)} vs the exit), low ${fp(w.low)}${r.over_target_suggested ? ' \u00b7 the high reached the original target' : ' \u00b7 the high stayed below the original target'}.`
      : 'No candles between the exit and 24:00.';
  }
  // The saved answers, or the defaults (all false, 'over the target' from the data).
  reviewValues(t) {
    const r = t.review || {};
    return r.reviewed ? { stop_tight: !!r.stop_tight, over_target: !!r.over_target, stop_saved: !!r.stop_saved, stop_moved_up: !!r.stop_moved_up }
      : { stop_tight: false, over_target: !!r.over_target_suggested, stop_saved: false, stop_moved_up: false };
  }
  // The pending review sits in the side panel, so the chart stays visible while answering. Its marks
  // are filled once per trade: later renders keep the user's clicks.
  renderInlineReview(t) {
    const $ = this.$;
    $('day-review').classList.toggle('hidden', !t);
    if (!t) { this.inlineReviewId = null; return; }
    if (this.inlineReviewId === t.id) return;
    this.inlineReviewId = t.id;
    $('dr-title').textContent = this.reviewTitle(t);
    $('dr-sub').textContent = this.reviewSub(t);
    $('dr-window').textContent = this.reviewWindowText(t);
    const values = this.reviewValues(t);
    for (const b of $('dr-list').querySelectorAll('.rv-toggle')) this.setToggle(b, values[b.dataset.key]);
    $('dr-error').textContent = '';
  }
  inlineReviewOpen() { return !this.$('day-review').classList.contains('hidden'); }
  // The dialog: editing the answers of any closed trade (from the trade dialog).
  openReview(t) {
    const $ = this.$;
    if ($('dlg-exit').open) $('dlg-exit').close();
    if ($('dlg-snapshot').open) $('dlg-snapshot').close();
    this.reviewTrade = t;
    $('review-title').textContent = this.reviewTitle(t);
    $('review-sub').textContent = this.reviewSub(t);
    $('review-window').textContent = this.reviewWindowText(t);
    const values = this.reviewValues(t);
    for (const b of $('review-list').querySelectorAll('.rv-toggle')) this.setToggle(b, values[b.dataset.key]);
    $('review-error').textContent = '';
    if (!$('dlg-review').open) $('dlg-review').showModal();
  }
  setToggle(b, on) { b.classList.toggle('on', !!on); b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.textContent = on ? 'true' : 'false'; }
  // Keys 1 / 2 / 3: the dialog when it is open, else the side panel.
  toggleReview(key) {
    const list = this.$('dlg-review').open ? 'review-list' : 'dr-list';
    const b = this.$(list).querySelector(`.rv-toggle[data-key='${key}']`);
    if (b) this.setToggle(b, !b.classList.contains('on'));
  }
  reviewAnswers(listId) {
    const out = {};
    for (const b of this.$(listId).querySelectorAll('.rv-toggle')) out[b.dataset.key] = b.classList.contains('on');
    return out;
  }
  closeReview() {
    this.$('dlg-review').close();
    this.reviewTrade = null;
  }
  // Saves the answers; returns the server's answer or null (the error is shown in errId).
  async submitReview(t, answers, errId, btnId) {
    const $ = this.$;
    if (!t || !this.ready()) return null;
    const tok = this.c.token();
    $(btnId).disabled = true;
    try {
      const res = await this.c.api.dayReview(this.session.id, t.id, answers);
      if (!this.c.fresh(tok)) return null;
      this.trades = this.trades.map(x => (x.id === res.trade.id ? res.trade : x));
      this.c.applyState(res.state);
      this.renderTrades();
      const tags = this.reviewTags(res.trade);
      this.c.toast(`Review of trade #${res.trade.seq} saved: ${tags.length ? tags.join(', ') : 'no marks'}. You can buy again, step on, or skip to the next day.`);
      return res;
    } catch (e) { if (this.c.fresh(tok)) $(errId).textContent = e.message; return null; }
    finally { $(btnId).disabled = false; }
  }
  async saveReview() {
    const res = await this.submitReview(this.reviewTrade, this.reviewAnswers('review-list'), 'review-error', 'btn-review-save');
    if (res) { this.reviewTrade = null; this.$('dlg-review').close(); }
  }
  async saveInlineReview() {
    const d = this.day;
    if (!d || !d.review_pending) return;
    await this.submitReview(d.review_pending, this.reviewAnswers('dr-list'), 'dr-error', 'btn-dr-save');
  }
  // The trade's picture, taken when its review becomes due (24:00 of the exit day): always the 15-minute chart from 00:00 of the
  // entry's trading day to 24:00 of the exit's (trade.frame), with
  // zones, entry, stop, take-profit, every earlier stop / target level (chart.setLevelHistory) and the exit line, whatever the zoom on screen. A hold longer than
  // the loaded bars (~6 days) starts at the oldest one. The view and zoom come back afterwards.
  async capturePicture(trade) {
    if (!this.ready() || this.capturing) return;
    this.capturing = true;
    const chart = this.c.chart;
    const prev = this.state.interval;
    const range = chart.currentRange();
    let image = null;
    try {
      if (prev !== '15m') await this.c.setInterval('15m');
      const frame = trade.frame && chart.rangeForTimes(trade.frame.from_s, trade.frame.to_s);
      if (frame) chart.setRange(frame);
      chart.setDayLines(trade);
      chart.setLevelHistory(trade);   // every stop / target move, not only the last levels
      // the range applies on a later frame: wait until the chart shows it, then one more for the paint
      // (a frame falls back to a timer, so a hidden page cannot hang here)
      for (let k = 0; k < 10; k++) {
        await nextFrame();
        const v = chart.chart.timeScale().getVisibleLogicalRange();
        if (!frame || (v && Math.abs(v.from - frame.from) < 0.5 && Math.abs(v.to - frame.to) < 0.5)) break;
      }
      await nextFrame();
      try { image = chart.chart.takeScreenshot().toDataURL('image/png'); } catch { image = null; }
      chart.setLevelHistory(null);
      chart.setDayLines(this.day ? this.day.trade : null);
      if (prev !== '15m' && this.state.interval === '15m') await this.c.setInterval(prev);
      if (range && this.state.interval === prev) chart.setRange(range);
    } finally { chart.setLevelHistory(null); this.capturing = false; }
    if (!image) return;
    const t = this.c.token();
    try {
      const row = await this.c.api.dayTradeImage(this.session.id, trade.id, image);
      if (!this.c.fresh(t)) return;
      if (this.day && this.day.review_pending && this.day.review_pending.id === row.id) this.day.review_pending.has_image = true;
      this.trades = this.trades.map(x => (x.id === row.id ? row : x));
      this.renderTrades();
    } catch (e) { if (this.c.fresh(t)) this.c.toast(`Picture not saved: ${e.message}`, true); }
  }

  // ------------------------------------------------------------ daily journal
  // Called by app.js before every step: the step that leaves the day gets the day's picture first.
  async beforeStep() {
    const d = this.day;
    if (this.ready() && d.leaving_next && !d.check.has_image) await this.captureDayPicture();
  }
  // The end-of-day picture: always the 15-minute chart with the zones (and the lines of the day's
  // closed trade); the view switches back afterwards. Taken once per day (the server keeps one).
  async captureDayPicture() {
    const d = this.day;
    if (!this.ready() || !d || !d.date || d.check.has_image || this.capturing) return;
    this.capturing = true;
    const prev = this.state.interval;
    try {
      if (prev !== '15m') await this.c.setInterval('15m');
      const closed = d.last_exit && d.last_exit.day_date === d.date ? d.last_exit : null;
      if (closed) { this.c.chart.setDayLines(closed); this.c.chart.setLevelHistory(closed); }
      await nextFrame(); await nextFrame();   // let the chart paint
      let image = null;
      try { image = this.c.chart.chart.takeScreenshot().toDataURL('image/png'); } catch { image = null; }
      if (closed) { this.c.chart.setLevelHistory(null); this.c.chart.setDayLines(d.trade); }
      if (image) {
        const t = this.c.token();
        try {
          const row = await this.c.api.dayImage(this.session.id, d.date, image);
          if (!this.c.fresh(t)) return;
          if (this.day && this.day.date === row.date) this.day.check.has_image = true;
          this.mergeDay(row);
        } catch (e) { if (this.c.fresh(t)) this.c.toast(`Day picture not saved: ${e.message}`, true); }
      }
      if (prev !== '15m' && this.state.interval === '15m') await this.c.setInterval(prev);
    } finally { this.capturing = false; }
  }
  mergeDay(row) {
    this.days = this.days.some(x => x.date === row.date) ? this.days.map(x => (x.date === row.date ? row : x)) : [...this.days, row];
    this.renderDays();
  }
  // Notes of the card's box: saved by themselves shortly after typing stops, and on leaving the box.
  queueNotes() {
    clearTimeout(this.notesTimer);
    this.$('day-notes-state').textContent = '…';
    this.notesTimer = setTimeout(() => this.saveNotes(this.notesDate, this.$('day-notes').value, 'day-notes-state'), 800);
  }
  async saveNotes(date, text, stateId) {
    if (!this.ready() || !date) return;
    const t = this.c.token();
    try {
      const row = await this.c.api.dayNotes(this.session.id, date, text);
      if (!this.c.fresh(t)) return;
      if (this.day && this.day.date === row.date) this.day.check.notes = row.notes;
      if (this.notesDate === row.date && document.activeElement !== this.$('day-notes')) this.$('day-notes').value = row.notes || '';
      this.mergeDay(row);
      this.$(stateId).textContent = `saved ${new Date().toTimeString().slice(0, 5)}`;
    } catch (e) { if (this.c.fresh(t)) { this.$(stateId).textContent = 'not saved'; this.c.toast(`Notes not saved: ${e.message}`, true); } }
  }
  outcomeText(r) {
    const many = (r.trade_seqs || []).length > 1;
    const who = () => (many ? `Trades ${r.trade_seqs.map(n => `#${n}`).join(', ')} · last` : `Trade #${r.trade.seq} ·`);
    if (r.outcome === 'trade') return `${who()} ${r.trade.exit_type}`;
    if (r.outcome === 'open') return `${who()} open`;
    if (r.outcome === 'skipped') return 'Skipped';
    if (r.outcome === 'no trade') return 'No trade';
    return 'In progress';
  }
  renderDays() {
    const $ = this.$;
    const tbody = $('day-days-list');
    tbody.innerHTML = '';
    const rows = [...this.days].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));   // newest first
    const fp = this.c.fmtPrice;
    for (const r of rows) {
      const tr = document.createElement('tr');
      tr.className = 'bucket';
      const td = (text, cls, title) => { const c = document.createElement('td'); c.textContent = text; if (cls) c.className = cls; if (title) c.title = title; tr.appendChild(c); return c; };
      const pic = document.createElement('td');
      if (r.has_image) {
        const img = document.createElement('img');
        img.className = 'thumb';
        img.src = `${this.c.api.dayImageUrl(this.session.id, r.date)}?v=${encodeURIComponent(r.updated_at)}`;
        img.alt = `Chart at the end of ${r.date}`;
        pic.appendChild(img);
      } else pic.textContent = r.outcome === 'current' ? 'today' : '–';
      tr.appendChild(pic);
      td(r.date, 'date');
      td(r.ema200 === null ? 'n/a' : this.flag(r.ema_above), r.ema_above === null ? '' : r.ema_above ? 'up' : 'down',
        r.ema200 === null ? 'fewer than 200 daily candles' : `close ${fp(r.daily_close)} vs EMA ${fp(r.ema200)}`);
      td(this.outcomeText(r), `outcome ${r.outcome.replace(' ', '-')}`, (r.trade_seqs || []).length > 1 ? `${r.trade_count} trades this day · day P&L ${this.signed(r.day_pnl)} USDT after fees · the row shows the last one` : '');
      td(r.left_local ? r.left_local.slice(11) : '–', 'date', r.left_label || '');
      const t = r.trade;
      td(t ? `${t.entry_local.slice(11)} → ${t.exit_local ? t.exit_local.slice(11) : 'open'}` : '–', 'date', t && t.exit_reason_label ? t.exit_reason_label : '');
      td(t && t.pnl !== null ? this.signed(t.pnl) : '–', `num ${t && t.pnl !== null ? this.cls(t.pnl) : ''}`);
      td(t && t.pnl_pct !== null ? this.pctText(t.pnl_pct) : '–', `num ${t && t.pnl_pct !== null ? this.cls(t.pnl_pct) : ''}`);
      td(t && t.r_multiple !== null ? this.signed(t.r_multiple, 3) : '–', `num ${t && t.r_multiple !== null ? this.cls(t.r_multiple) : ''}`);
      const notes = r.notes || '';
      td(notes.length > 90 ? `${notes.slice(0, 90)}…` : notes, 'notes', notes);
      tr.addEventListener('click', () => this.openDay(r));
      tbody.appendChild(tr);
    }
    $('day-days-count').textContent = String(this.days.length);
    $('day-days-empty').classList.toggle('hidden', rows.length > 0);
    $('btn-days-csv').href = this.session ? this.c.api.dayDaysCsvUrl(this.session.id) : '#';
  }
  openDay(r) {
    const $ = this.$;
    $('snap-title').textContent = `Day ${r.date} · ${this.outcomeText(r)}`;
    const img = $('snap-img');
    if (r.has_image) { img.src = `${this.c.api.dayImageUrl(this.session.id, r.date)}?v=${encodeURIComponent(r.updated_at)}`; img.classList.remove('hidden'); }
    else { img.removeAttribute('src'); img.classList.add('hidden'); }
    $('snap-nopic').classList.toggle('hidden', r.has_image);
    $('snap-nopic').textContent = r.outcome === 'current' ? 'The picture is taken when the replay leaves this day.' : 'No picture was saved for this day.';
    const fp = this.c.fmtPrice;
    const t = r.trade;
    const groups = [
      ['Trend check', [['Daily close', r.daily_close === null ? 'n/a' : fp(r.daily_close)], ['EMA 200 (1D)', r.ema200 === null ? 'n/a' : fp(r.ema200)],
        ['Close above EMA 200', this.flag(r.ema_above)]]],
      ['Day', [['Outcome', this.outcomeText(r)], ['Left', r.left_label ? `${r.left_label} · ${r.left_local}` : 'still in progress'], ['Picture taken', r.image_local || '–']]],
      ['Trade', t ? [['Number', `#${t.seq}`], ['Entry', `${t.entry_local} @ ${fp(t.entry_price)}`], ['Exit', t.exit_local ? `${t.exit_local} @ ${fp(t.exit_price)}` : 'open'],
        ['Type', t.exit_type || 'open'], ['Reason', t.exit_reason_label || '–'], ['Overnight', t.overnight ? 'yes' : 'no'],
        ['P&L (after fees)', t.pnl === null ? '–' : `${this.signed(t.pnl)} USDT`], ['P&L % of position', this.pctText(t.pnl_pct)], ['R', this.signed(t.r_multiple, 3)]] : [['Trade', 'none']]],
    ];
    const tb = $('snap-readings');
    tb.innerHTML = '';
    for (const [name, rows] of groups) {
      const h = document.createElement('tr'); h.className = 'section';
      const hc = document.createElement('td'); hc.colSpan = 2; hc.textContent = name; h.appendChild(hc); tb.appendChild(h);
      for (const [k, v] of rows) {
        const tr = document.createElement('tr');
        const a = document.createElement('td'); a.textContent = k;
        const b = document.createElement('td'); b.className = 'v'; b.textContent = v;
        tr.append(a, b); tb.appendChild(tr);
      }
    }
    $('snap-notes').value = r.notes || '';
    $('snap-notes-state').textContent = '';
    $('snap-notes-wrap').classList.remove('hidden');
    $('snap-notes-wrap').dataset.date = r.date;
    $('dlg-snapshot').showModal();
  }

  // ----------------------------------------------------------------- trades
  setSort(key) {
    if (this.sort.key === key) this.sort.dir = -this.sort.dir; else this.sort = { key, dir: 1 };
    this.renderTrades();
  }
  // ------------------------------------------------------- automatic twins
  autoText(a) {
    if (!a) return 'none (trade from before the feature)';
    const fp = this.c.fmtPrice;
    if (a.status === 'open') return `open · stop ${fp(a.stop)} · target ${fp(a.take_profit)}`;
    return `closed · ${a.exit_type} @ ${fp(a.exit_price)} · ${this.signed(a.pnl)} USDT (${this.signed(a.r_multiple)} R) · held ${this.holdText(a.hold_hours)}`;
  }
  renderAuto() {
    const $ = this.$;
    const tbody = $('day-auto-list');
    tbody.innerHTML = '';
    const rows = this.auto.trades || [];
    $('day-auto-count').textContent = String(rows.length);
    $('day-auto-empty').classList.toggle('hidden', rows.length > 0);
    $('day-auto-count').title = rows.some(a => a.status === 'none') ? 'rows without a twin show only the alternatives' : '';
    const sm = this.auto.summary;
    $('auto-summary').textContent = !sm || !sm.count ? '' : [
      `${sm.closed} closed (${sm.by_type['take-profit']} target · ${sm.by_type.stop} stop)${sm.open ? ` · ${sm.open} open` : ''}`,
      sm.closed ? `auto ${this.signed(sm.pnl)} USDT${sm.avg_r === null ? '' : ` · avg ${this.signed(sm.avg_r)} R`} · held ${this.holdText(sm.avg_hold_hours)} on average` : '',
      sm.compared ? `vs manual ${this.signed(sm.manual_pnl)} USDT on the same ${sm.compared} trade${sm.compared === 1 ? '' : 's'} (held ${this.holdText(sm.manual_avg_hold_hours)}) · difference ${this.signed(sm.diff)} USDT` : '',
      ...['alt1', 'alt2'].map(k => {
        const a = sm[k];
        if (!a || !a.count) return '';
        const types = Object.entries(a.by_type).filter(([, n]) => n).map(([typ, n]) => `${n} ${typ === 'take-profit' ? 'target' : typ}`).join(' · ');
        return `${k === 'alt1' ? 'Alt 1' : 'Alt 2'} ${this.signed(a.pnl)} USDT on ${a.count} (${types})${a.avg_r === null ? '' : ` · avg ${this.signed(a.avg_r)} R`}${a.compared ? ` · vs manual ${this.signed(a.diff)}` : ''}`;
      }),
      ...['A', 'C'].map(k => {
        const a = sm[k];
        if (!a || !a.count) return '';
        const types = Object.entries(a.by_type).filter(([, n]) => n).map(([typ, n]) => `${n} ${typ === 'take-profit' ? 'target' : typ}`).join(' · ');
        return `Exit ${k} ${this.signed(a.pnl)} USDT on ${a.count} (${types})${a.avg_r === null ? '' : ` · avg ${this.signed(a.avg_r)} R`}${a.compared ? ` · vs real ${this.signed(a.diff)}` : ''}`;
      }),
    ].filter(Boolean).join('   |   ');
    const fp = this.c.fmtPrice;
    for (const a of rows) {
      const tr = document.createElement('tr');
      tr.className = 'bucket';
      const td = (text, cls, title) => { const c = document.createElement('td'); c.textContent = text; if (cls) c.className = cls; if (title) c.title = title; tr.appendChild(c); return c; };
      td(`#${a.seq}`, 'seq');
      td(a.day_date, 'date');
      td(a.entry_local.slice(11), 'date');
      td(fp(a.entry_price), 'num');
      const none = a.status === 'none';   // a trade from before the twins existed: only the alternatives
      td(none ? '–' : fp(a.stop), 'num', none ? '' : `${this.pctText(a.stop_pct)} from the entry`);
      td(none ? '–' : fp(a.take_profit), 'num', none ? '' : `${this.pctText(a.tp_pct)} from the entry`);
      td(a.exit_local ? a.exit_local.slice(11) : '–', 'date');
      td(none ? 'no twin' : a.exit_type || 'open', none ? 'muted' : `exit ${a.exit_type === 'stop' ? 'stop' : a.exit_type === 'take-profit' ? 'target' : ''}`, none ? 'the trade was made before the twins existed' : '');
      td(a.exit_price === null || a.exit_price === undefined ? '–' : fp(a.exit_price), 'num');
      td(none ? '–' : this.holdText(this.holdHours(a.entry_ms, a.exit_ms)), `num${a.exit_ms === null ? ' muted' : ''}`, none ? '' : a.exit_ms === null ? 'open: held until now' : 'hours from the buy to the exit');
      td(this.signed(a.pnl), `num pnl ${this.cls(a.pnl)}`);
      td(this.pctText(a.pnl_pct), `num ${this.cls(a.pnl_pct)}`);
      td(this.signed(a.r_multiple), `num ${this.cls(a.r_multiple)}`);
      const m = a.manual;
      td(!m ? '–' : m.status === 'open' ? 'open' : `${m.exit_type} @ ${fp(m.exit_price)}${m.moved ? ' · moved' : ''}`, 'reasons', m && m.exit_local ? m.exit_local : '');
      td(m ? this.holdText(m.hold_hours === null ? this.holdHours(a.entry_ms, null) : m.hold_hours) : '–', `num${m && m.hold_hours === null ? ' muted' : ''}`);
      td(m ? this.signed(m.pnl) : '–', `num ${m ? this.cls(m.pnl) : ''}`);
      td(this.signed(a.diff), `num ${this.cls(a.diff)}`, 'twin minus manual');
      const al = a.alts;
      const altCells = (x, withTarget) => {
        if (withTarget) td(x ? fp(x.take_profit) : '–', 'num', x ? `${this.pctText((x.take_profit / a.entry_price - 1) * 100)} from the entry` : '');
        if (!x) { td('not yet', 'muted', 'available once the replay reaches the closing 24:00'); td('', 'num'); td('', 'num'); return; }
        td(`${x.exit_type === 'take-profit' ? 'target' : x.exit_type} ${x.exit_local.slice(11)}`, `exit ${x.exit_type === 'stop' ? 'stop' : x.exit_type === 'take-profit' ? 'target' : ''}`, `@ ${fp(x.exit_price)} · held ${this.holdText(x.hold_hours)}${x.diff === null ? '' : ` · ${this.signed(x.diff)} USDT vs manual`}`);
        td(this.signed(x.pnl), `num pnl ${this.cls(x.pnl)}`);
        td(this.signed(x.r_multiple), `num ${this.cls(x.r_multiple)}`);
      };
      altCells(al && al.alt1, false);
      altCells(al && al.alt2, true);
      for (const k of ['A', 'C']) {   // exits A and C of the manual trade
        const x = a.exits_ac && a.exits_ac[k];
        if (!x || x.status === 'open') { td(a.exits_ac ? 'open' : '–', 'muted'); td('', 'num'); td('', 'num'); continue; }
        td(`${x.exit_type === 'take-profit' ? 'target' : x.exit_type} ${x.exit_local.slice(11)}`, `exit ${x.exit_type === 'stop' ? 'stop' : x.exit_type === 'take-profit' ? 'target' : ''}`,
          `@ ${fp(x.exit_price)}${x.reason_label ? ` · ${x.reason_label}` : ''}${x.note ? ` · ${x.note}` : ''} · held ${this.holdText(x.hold_hours)}${x.diff === null ? '' : ` · ${this.signed(x.diff)} USDT vs the real exit`}`);
        td(this.signed(x.pnl), `num pnl ${this.cls(x.pnl)}`);
        td(this.signed(x.r_multiple), `num ${this.cls(x.r_multiple)}`);
      }
      const manualTrade = this.trades.find(t => t.id === a.trade_id);
      if (manualTrade) tr.addEventListener('click', () => this.openTrade(manualTrade));
      tbody.appendChild(tr);
    }
  }
  renderTrades() {
    const $ = this.$;
    const tbody = $('day-trade-list');
    tbody.innerHTML = '';
    const f = this.condFilter;
    const shown = f.key === 'all' || f.val === 'all' ? this.trades
      : this.trades.filter(t => t.conditions && t.conditions[f.key] === (f.val === 'yes'));
    const rows = [...shown].sort((a, b) => {
      const val = t => (this.sort.key === 'hold_hours' ? this.holdHours(t.entry_ms, t.exit_ms) : this.sort.key === 'review_n' ? this.reviewCount(t)
        : ['mae_pct', 'mfe_pct', 'mae_r', 'mfe_r'].includes(this.sort.key) ? (t.excursion ? t.excursion[this.sort.key] : null) : t[this.sort.key]);
      const va = val(a), vb = val(b);
      const x = va === null || va === undefined ? -Infinity : va, y = vb === null || vb === undefined ? -Infinity : vb;
      return (x < y ? -1 : x > y ? 1 : 0) * this.sort.dir;
    });
    for (const th of $('day-trade-table').querySelectorAll('th.sortable')) {
      th.classList.toggle('asc', th.dataset.key === this.sort.key && this.sort.dir === 1);
      th.classList.toggle('desc', th.dataset.key === this.sort.key && this.sort.dir === -1);
    }
    const fp = this.c.fmtPrice;
    for (const t of rows) {
      const tr = document.createElement('tr');
      tr.className = 'bucket';
      const td = (text, cls, title) => { const c = document.createElement('td'); c.textContent = text; if (cls) c.className = cls; if (title) c.title = title; tr.appendChild(c); return c; };
      const pic = document.createElement('td');
      if (t.has_image) {
        const img = document.createElement('img');
        img.className = 'thumb';
        img.src = `${this.c.api.dayTradeImageUrl(this.session.id, t.id)}?v=${encodeURIComponent(t.updated_at)}`;
        img.alt = `Chart at 24:00 of the exit day of trade #${t.seq}`;
        pic.appendChild(img);
      } else pic.textContent = t.status === 'open' ? 'open' : '–';
      tr.appendChild(pic);
      td(`#${t.seq}`, 'seq');
      td(t.day_date, 'date');
      td(t.entry_local.slice(11), 'date', t.entry_iso);
      td(t.exit_local ? t.exit_local.slice(11) : '–', 'date', t.exit_iso || '');
      td(`${t.exit_type || 'open'}${(t.changes || []).length ? ' · moved' : ''}`, `exit ${t.exit_type === 'stop' ? 'stop' : t.exit_type === 'take-profit' ? 'target' : ''}`, (t.changes || []).length ? this.changesText(t) : '');
      td(this.holdText(this.holdHours(t.entry_ms, t.exit_ms)), `num${t.exit_ms === null ? ' muted' : ''}`, t.exit_ms === null ? 'open: held until now' : 'hours from the buy to the exit');
      td(t.exit_reason_label || '', 'reasons', t.exit_note || '');
      td(this.flag(t.ema_above), t.ema_above === null || t.ema_above === undefined ? '' : t.ema_above ? 'up' : 'down', 'daily close above EMA 200');
      td(this.condShort(t.conditions), 'reasons', this.condLong(t.conditions));
      const confCell = td('', 'num', 'confidence at the buy, 0 to 10');
      if (t.confidence === null || t.confidence === undefined) confCell.textContent = '–';
      else { const s = document.createElement('span'); s.className = 'conf'; s.textContent = String(t.confidence); confCell.appendChild(s); }
      const rvCell = td('', 'reasons', t.status === 'open' ? 'reviewed at 24:00 of the exit day' : (t.review && t.review.reviewed ? `reviewed ${t.review.reviewed_at.slice(0, 16).replace('T', ' ')} \u00b7 click the row to change` : 'not reviewed yet'));
      const tags = this.reviewTags(t);
      if (t.status === 'open') rvCell.textContent = '';
      else if (tags === null) { rvCell.textContent = '\u2013'; rvCell.classList.add('muted'); }
      else if (!tags.length) { const s = document.createElement('span'); s.className = 'rtag none'; s.textContent = 'no marks'; rvCell.appendChild(s); }
      else for (const label of tags) { const s = document.createElement('span'); s.className = 'rtag'; s.textContent = label; rvCell.appendChild(s); }
      td(fp(t.entry_price), 'num');
      td(t.entry_rsi === null ? '–' : t.entry_rsi.toFixed(1), 'num');
      td(t.entry_rsi_1h === null || t.entry_rsi_1h === undefined ? '–' : t.entry_rsi_1h.toFixed(1), 'num');
      td(t.entry_atr === null ? '–' : fp(t.entry_atr), 'num');
      td(`${fp(t.zone_lower)} – ${fp(t.zone_upper)}`, 'num');
      td(fp(t.stop_used), 'num', `default (zone middle) ${t.stop_suggested === null ? 'n/a' : fp(t.stop_suggested)} · ATR stop ${t.stop_atr === null || t.stop_atr === undefined ? 'n/a' : fp(t.stop_atr)}`);
      td(fp(t.take_profit), 'num');
      td(this.n(t.rr), 'num', 'reward-to-risk at the entry');
      td(this.c.qtyFmt(t.coins), 'num');
      td(this.n(t.position_value), 'num');
      td(this.n(t.risk_usdt), 'num');
      td(t.capped ? 'yes' : 'no', t.capped ? 'down' : '');
      td(t.overnight ? 'yes' : 'no', t.overnight ? 'down' : '');
      td(t.exit_price === null || t.exit_price === undefined ? '–' : fp(t.exit_price), 'num');
      td(t.fees === null || t.fees === undefined ? this.n(t.buy_fee) : this.n(t.fees), 'num');
      td(this.signed(t.pnl), `num pnl ${this.cls(t.pnl)}`);
      td(this.pctText(t.pnl_pct), `num ${this.cls(t.pnl_pct)}`);
      td(this.signed(t.r_multiple), `num ${this.cls(t.r_multiple)}`);
      const x = t.excursion;
      const xt = x ? `lowest low ${this.pctText(x.mae_pct)} / highest high ${this.pctText(x.mfe_pct)} vs the entry, between the entry and the exit` : (t.status === 'closed' ? 'not available' : 'after the exit');
      td(x ? this.pctText(x.mae_pct) : (t.status === 'closed' ? 'n/a' : ''), `num ${x ? this.cls(x.mae_pct) : 'muted'}`, xt);
      td(x ? this.pctText(x.mfe_pct) : (t.status === 'closed' ? 'n/a' : ''), `num ${x ? this.cls(x.mfe_pct) : 'muted'}`, xt);
      td(x && x.mae_r !== null ? this.signed(x.mae_r) : (t.status === 'closed' ? 'n/a' : ''), `num ${x ? this.cls(x.mae_r) : 'muted'}`, xt);
      td(x && x.mfe_r !== null ? this.signed(x.mfe_r) : (t.status === 'closed' ? 'n/a' : ''), `num ${x ? this.cls(x.mfe_r) : 'muted'}`, xt);
      const w = t.whatif;
      const wt = w ? `${w.n} candles after the exit until 24:00 · high ${fp(w.high)}, low ${fp(w.low)}` : (t.status === 'closed' ? 'no candles after the exit before 24:00' : 'after the exit');
      for (const [k, r] of [['above_exit_pct', false], ['below_exit_pct', false], ['above_exit_r', true], ['below_exit_r', true], ['high_vs_entry_pct', false], ['low_vs_entry_pct', false]]) {
        const has = w && w[k] !== null && w[k] !== undefined;
        td(has ? (r ? this.signed(w[k]) : this.pctText(w[k])) : (t.status === 'closed' ? 'n/a' : ''), `num ${has ? this.cls(w[k]) : 'muted'}`, wt);
      }
      td(t.notes || '', 'reasons', t.notes || '');
      tr.addEventListener('click', () => this.openTrade(t));
      tbody.appendChild(tr);
    }
    this.applyCols();
    const empty = $('day-trade-empty');
    empty.classList.toggle('hidden', rows.length > 0);
    if (!rows.length && this.trades.length) empty.textContent = `No trade matches the filter (${this.trades.length} hidden).`;
    else if (!rows.length) empty.textContent = 'No trades yet. Draw a support zone on the 1h or 4h chart, then New trade.';
    $('day-trade-count').textContent = String(this.trades.length);
  }

  // ---------------------------------------------------------- conditions
  // Short "EMA ✓ · zone ✓ · …" for the trades table, the full list for its hover text.
  condShort(c) {
    if (!c) return '';
    const yn = (v) => v === null || v === undefined ? '–' : v ? '✓' : '✗';
    const stop = c.stop_middle ? 'middle' : c.stop_atr ? 'ATR' : c.stop_own ? 'own' : '–';
    const when = c.morning ? 'morning' : c.afternoon ? 'afternoon' : c.evening ? 'evening' : '–';
    return `EMA ${yn(c.ema_above)} · zone ${yn(c.price_in_zone)} · RSI dip ${yn(c.rsi_dipped)} · trigger ${yn(c.close_above_prev_high)} · R/R ${yn(c.rr_ok)} · stop ${stop} · ${c.zone_4h === null ? '–' : c.zone_4h ? '4h' : '1h'} · ${when}`;
  }
  condLong(c) {
    if (!c) return '';
    return COND_LABELS.map(([k, label]) => `${this.flag(c[k])} — ${label}`).join('\n');
  }
  setCondFilter(key, val) {
    if (key !== undefined) this.condFilter.key = key;
    if (val !== undefined) this.condFilter.val = val;
    for (const b of this.$('panel-day').querySelectorAll('.chip[data-v]')) b.classList.toggle('on', b.dataset.v === this.condFilter.val);
    this.$('day-cond-key').value = this.condFilter.key;
    this.renderTrades();
  }

  // The Conditions tab: closed trades with and without each condition, this session or all day sessions.
  async loadConditions() {
    const $ = this.$;
    if (!this.ready()) { this.conditions = null; this.renderConditions(); return; }
    $('btn-cond-csv').href = this.c.api.dayConditionsCsvUrl(this.session.id, this.condScope);
    const t = this.c.token();
    try {
      const ev = await this.c.api.dayConditions(this.session.id, this.condScope);
      if (!this.c.fresh(t)) return;
      this.conditions = ev;
    } catch (e) { this.c.toast(`Conditions not loaded: ${e.message}`, true); return; }
    this.renderConditions();
  }
  setCondScope(scope) {
    this.condScope = scope;
    for (const b of this.$('panel-cond').querySelectorAll('.chip[data-scope]')) b.classList.toggle('on', b.dataset.scope === scope);
    this.loadConditions();
  }
  renderConditions() {
    const $ = this.$;
    const tbody = $('cond-list');
    tbody.innerHTML = '';
    const ev = this.conditions;
    const n = ev ? ev.trades : 0;
    $('cond-summary').textContent = !ev ? '' : `${n} closed trade${n === 1 ? '' : 's'}${ev.scope === 'all' ? ` in ${ev.sessions} session${ev.sessions === 1 ? '' : 's'}` : ''}`;
    $('cond-empty').classList.toggle('hidden', n > 0);
    if (!ev || !n) return;
    const pct = (v) => v === null || v === undefined ? '–' : `${v.toFixed(0)} %`;
    const cell = (g, first) => [[String(g.trades), `num${first ? ' first' : ''}`], [pct(g.win_rate), `num ${g.win_rate === null ? '' : g.win_rate >= 50 ? 'up' : 'down'}`],
      [this.signed(g.avg_r), `num ${this.cls(g.avg_r)}`], [this.signed(g.pnl), `num pnl ${this.cls(g.pnl)}`]];
    const row = (label, cells, cls, title) => {
      const tr = document.createElement('tr');
      if (cls) tr.className = cls;
      const first = document.createElement('td'); first.textContent = label; if (title) first.title = title; tr.appendChild(first);
      for (const [text, c] of cells) { const td = document.createElement('td'); td.textContent = text; td.className = c; tr.appendChild(td); }
      tbody.appendChild(tr);
    };
    row('All closed trades', [...cell(ev.all, true), ...[['', 'num first'], ['', 'num'], ['', 'num'], ['', 'num']], ['', 'num first']], 'all');
    for (const c of ev.conditions) {
      row(c.label, [...cell(c.with, true), ...cell(c.without, true), [String(c.unknown), 'num first muted']], '', c.key);
    }
  }

  // The picture and every saved field of a trade, in the shared snapshot dialog.
  openTrade(t) {
    const $ = this.$;
    $('snap-title').textContent = `Trade #${t.seq} · ${t.day_date} · ${t.exit_type || 'open'}`;
    const img = $('snap-img');
    if (t.has_image) { img.src = `${this.c.api.dayTradeImageUrl(this.session.id, t.id)}?v=${encodeURIComponent(t.updated_at)}`; img.classList.remove('hidden'); }
    else { img.removeAttribute('src'); img.classList.add('hidden'); }
    $('snap-nopic').classList.toggle('hidden', t.has_image);
    $('snap-nopic').textContent = 'No picture was saved for this trade.';
    $('snap-notes-wrap').classList.add('hidden');
    const fp = this.c.fmtPrice;
    const w = t.whatif;
    const x = t.excursion;
    const groups = [
      ['Basics', [['Coin', this.session.symbol], ['Trading day', t.day_date], ['Confidence at the buy', t.confidence === null || t.confidence === undefined ? 'not scored' : `${t.confidence} / 10`],
        ['Checklist', `v1 · risk ${this.n(t.risk_amount)} USDT`]]],
      ['Trend check', [['Daily close above EMA 200', this.flag(t.ema_above)]]],
      ['Conditions at the buy', [...COND_LABELS.map(([k, label]) => [label, this.flag(t.conditions ? t.conditions[k] : null)]),
        ['RSI 14 (15m) at the buy', t.entry_rsi === null || t.entry_rsi === undefined ? 'n/a' : t.entry_rsi.toFixed(2)],
        ['RSI 14 (1h) at the buy', t.entry_rsi_1h === null || t.entry_rsi_1h === undefined ? 'n/a' : t.entry_rsi_1h.toFixed(2)]]],
      ['Entry', [['Time (UTC)', t.entry_local], ['Price', fp(t.entry_price)], ['RSI 14 (15m)', t.entry_rsi === null ? 'n/a' : t.entry_rsi.toFixed(2)],
        ['ATR 14 (15m)', t.entry_atr === null ? 'n/a' : fp(t.entry_atr)], ['Zone upper edge', fp(t.zone_upper)], ['Zone lower edge', fp(t.zone_lower)]]],
      ['Plan', [['Default stop (zone middle)', t.stop_suggested === null ? 'n/a' : fp(t.stop_suggested)], ['ATR stop (zone low − 0.5 × ATR)', t.stop_atr === null || t.stop_atr === undefined ? 'n/a' : fp(t.stop_atr)],
        ['Stop at the buy', fp(t.stop_initial === undefined ? t.stop_used : t.stop_initial)], ['Take-profit at the buy', fp(t.tp_initial === undefined ? t.take_profit : t.tp_initial)], ['Reward-to-risk at the buy', `${this.n(t.rr)} : 1`],
        ['Stop at the end', fp(t.stop_used)], ['Take-profit at the end', fp(t.take_profit)]]],
      ['Moves during the trade', (t.changes || []).length
        ? t.changes.map(c => [`${c.at_local.slice(0, 10) === t.day_date ? c.at_local.slice(11) : c.at_local} · ${c.field === 'stop' ? 'stop' : 'target'} (price ${fp(c.price)})`, `${this.pctText((c.from / t.entry_price - 1) * 100)} \u2192 ${this.pctText((c.to / t.entry_price - 1) * 100)} (${fp(c.to)})`])
        : [['Moves', 'none']]],
      ['Position', [['Coins', this.c.qtyFmt(t.coins)], ['Position value', `${this.n(t.position_value)} USDT`], ['Risk', `${this.n(t.risk_usdt)} USDT`], ['Capped', t.capped ? 'yes' : 'no'], ['Buy fee', `${this.n(t.buy_fee)} USDT`]]],
      ['Exit', [['Time (UTC)', t.exit_local || '–'], ['Held', this.holdText(this.holdHours(t.entry_ms, t.exit_ms))], ['Price', t.exit_price === null || t.exit_price === undefined ? '–' : fp(t.exit_price)], ['Type', t.exit_type || 'open'],
        ['Reason', t.exit_reason_label || '–'], ['Note', t.exit_note || '–'], ['Overnight', t.overnight ? 'yes' : 'no']]],
      ['Result', [['P&L (after fees)', `${this.signed(t.pnl)} USDT`], ['P&L % of position', this.pctText(t.pnl_pct)], ['Fees', t.fees === null || t.fees === undefined ? '–' : `${this.n(t.fees)} USDT`], ['R', this.signed(t.r_multiple)]]],
      ['MAE / MFE (between the entry and the exit)', x
        ? [['Worst move against (MAE)', `${this.pctText(x.mae_pct)} of the entry${x.mae_r === null ? '' : ` \u00b7 ${this.signed(x.mae_r)} R`}`],
          ['Best move for (MFE)', `${this.pctText(x.mfe_pct)} of the entry${x.mfe_r === null ? '' : ` \u00b7 ${this.signed(x.mfe_r)} R`}`]]
        : [['MAE / MFE', t.status === 'closed' ? 'n/a' : 'after the exit']]],
      ['After the exit (until 24:00 of the exit day)', w
        ? [['Candles after the exit', String(w.n)],
          ['Best move above the exit (MFE after)', `${this.pctText(w.above_exit_pct)} of the exit${w.above_exit_r === null ? '' : ` \u00b7 ${this.signed(w.above_exit_r)} R`} (high ${fp(w.high)})`],
          ['Worst move below the exit (MAE after)', `${this.pctText(w.below_exit_pct)} of the exit${w.below_exit_r === null ? '' : ` \u00b7 ${this.signed(w.below_exit_r)} R`} (low ${fp(w.low)})`],
          ['High vs entry', this.pctText(w.high_vs_entry_pct)], ['Low vs entry', this.pctText(w.low_vs_entry_pct)]]
        : [['Window', t.status === 'closed' ? 'n/a (no candles before 24:00)' : 'trade still open']]],
      ['Exits A and C (each for the whole position; the later one is the real exit)', t.exits_ac
        ? [['Exit A', this.legText(t.exits_ac.A)], ['Exit C', this.legText(t.exits_ac.C)]]
        : [['Exits', 'n/a']]],
      ['Alternatives (hands-off, closed at 24:00 at the latest)', t.alts
        ? [['Alt 1: stop and target of the buy', this.altText(t.alts.alt1)], ['Alt 2: same stop, target 1:2', this.altText(t.alts.alt2)], ['Closing candle', t.alts.end_local]]
        : [['Alternatives', 'available once the replay reaches 24:00 of the entry day']]],
      ['Review (at 24:00 of the exit day)', t.review && t.review.reviewed
        ? [['Stop too tight', t.review.stop_tight ? 'true' : 'false'], ['Price later went over the target', t.review.over_target ? 'true' : 'false'], ['The stop saved me', t.review.stop_saved ? 'true' : 'false'],
          ['Moved the stop too far up', t.review.stop_moved_up === null ? '\u2013 (reviewed before this mark existed)' : t.review.stop_moved_up ? 'true' : 'false'],
          ['Answered', t.review.reviewed_at.slice(0, 16).replace('T', ' ')]]
        : [['Review', t.status === 'closed' ? 'not answered yet' : 'after the exit']]],
      ['Notes', [['Notes', t.notes || '–']]],
    ];
    const rb = $('btn-snap-review');
    rb.classList.toggle('hidden', t.status !== 'closed');
    rb.textContent = t.review && t.review.reviewed ? 'Edit review' : 'Answer review';
    rb.onclick = () => this.openReview(t);
    const tb = $('snap-readings');
    tb.innerHTML = '';
    for (const [name, rows] of groups) {
      const h = document.createElement('tr'); h.className = 'section';
      const hc = document.createElement('td'); hc.colSpan = 2; hc.textContent = name; h.appendChild(hc); tb.appendChild(h);
      for (const [k, v] of rows) {
        const tr = document.createElement('tr');
        const a = document.createElement('td'); a.textContent = k;
        const b = document.createElement('td'); b.className = 'v'; b.textContent = v;
        tr.append(a, b); tb.appendChild(tr);
      }
    }
    $('dlg-snapshot').showModal();
  }

  // ---------------------------------------------------------------- keys
  // Returns true when the key was handled. Day-mode shortcuts: N new trade, X sell, K skip, Z zone.
  onKey(e) {
    if (!this.active()) return false;
    if (this.dialogOpen()) return true;  // the dialogs handle their own keys
    const k = e.key.toLowerCase();
    if (this.exitChoiceOpen()) {   // just closed: Enter ends the day, K keeps trading
      if (e.key === 'Enter' && !(e.target && e.target.closest && e.target.closest('button'))) { e.preventDefault(); this.endDay(); return true; }
      if (k === 'k') { e.preventDefault(); this.keepTrading(); return true; }
    }
    if (this.inlineReviewOpen()) {   // the pending review in the side panel
      const keys = { 1: 'stop_tight', 2: 'over_target', 3: 'stop_saved', 4: 'stop_moved_up' };
      if (keys[e.key]) { e.preventDefault(); this.toggleReview(keys[e.key]); return true; }
      if (e.key === 'Enter' && !(e.target && e.target.closest && e.target.closest('button'))) { e.preventDefault(); this.saveInlineReview(); return true; }
    }
    if (this.panelOpen()) {   // New trade panel: Esc closes, the arrows move the stop; the replay keys still work
      if (k === 'escape') { this.closeNewTrade(); return true; }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); this.moveStop(e.key === 'ArrowUp' ? 1 : -1, e.shiftKey); return true; }
      if (['n', 'b', 'x', 'k', 'z'].includes(k)) return true;
    }
    if (k === 'escape' && (this.drawing || this.draft !== null)) { this.cancelDraw(); return true; }
    if (k === 'n' || k === 'b') { e.preventDefault(); this.openNewTrade(); return true; }
    if (k === 'x') { e.preventDefault(); this.openExit(); return true; }
    if (k === 'k') { e.preventDefault(); this.skipDay(); return true; }
    if (k === 'j') { e.preventDefault(); this.nextEmaDay(); return true; }
    if (k === 'e') { e.preventDefault(); this.nextSignal(); return true; }
    if (k === 'z') { e.preventDefault(); this.drawing ? this.cancelDraw() : this.startDraw(); return true; }
    return false;
  }

  // ---------------------------------------------------------------- init
  init() {
    const $ = this.$;
    $('form-review').addEventListener('submit', (e) => { e.preventDefault(); this.saveReview(); });
    $('btn-review-cancel').addEventListener('click', () => this.closeReview());
    $('dlg-review').addEventListener('cancel', (e) => { e.preventDefault(); this.closeReview(); });
    for (const b of $('review-list').querySelectorAll('.rv-toggle')) b.addEventListener('click', () => this.setToggle(b, !b.classList.contains('on')));
    $('day-review').addEventListener('submit', (e) => { e.preventDefault(); this.saveInlineReview(); });
    for (const b of $('dr-list').querySelectorAll('.rv-toggle')) b.addEventListener('click', () => this.setToggle(b, !b.classList.contains('on')));
    $('dlg-review').addEventListener('keydown', (e) => {
      const keys = { 1: 'stop_tight', 2: 'over_target', 3: 'stop_saved', 4: 'stop_moved_up' };
      if (keys[e.key]) { e.preventDefault(); this.toggleReview(keys[e.key]); }
    });
    $('btn-day-new').addEventListener('click', () => this.openNewTrade());
    $('btn-day-sell').addEventListener('click', () => this.openExit());
    $('btn-day-skip').addEventListener('click', () => this.skipDay());
    $('btn-day-ema').addEventListener('click', () => this.nextEmaDay());
    $('btn-day-signal').addEventListener('click', () => this.nextSignal());
    $('day-strategy').addEventListener('change', () => { this.renderStrategyNote(); this.render(); });
    this.loadStrategies();
    this.buildColsMenu();
    document.addEventListener('click', (e) => { const m = $('day-cols'); if (m.open && !m.contains(e.target)) m.open = false; });
    $('btn-dx-end').addEventListener('click', () => this.endDay());
    $('btn-dx-keep').addEventListener('click', () => this.keepTrading());
    $('btn-cal-prev').addEventListener('click', () => this.moveCal(-1));
    $('btn-cal-next').addEventListener('click', () => this.moveCal(1));
    $('btn-cal-now').addEventListener('click', () => { this.calMonth = null; this.renderCal(); });
    $('btn-day-zone').addEventListener('click', () => (this.drawing ? this.cancelDraw() : this.startDraw()));
    // move the stop / target of the open trade by dragging its dotted chart lines
    this.c.chart.onDragLine = (key, price) => {
      const t = this.day && this.day.trade;
      if (!t) return;
      const p = parseFloat(this.roundPrice(price));
      this.moveTrade(key === 'stop' ? { stop: p } : { take_profit: p }).then((ok) => { if (!ok) this.c.chart.setDayLines(this.day && this.day.trade); });
    };
    for (const id of ['nt-stop', 'nt-ratio']) {
      $(id).addEventListener('input', () => this.requestPlan());
      $(id).addEventListener('change', () => this.requestPlan());
    }
    $('nt-zone').addEventListener('change', () => this.setStop(null));   // a new zone: back to its middle
    for (const b of $('nt-panel').querySelectorAll('.nt-ratio-chip')) b.addEventListener('click', () => this.setRatio(parseFloat(b.dataset.ratio)));
    $('btn-stop-down').addEventListener('click', (e) => this.moveStop(-1, e.shiftKey));
    $('btn-stop-up').addEventListener('click', (e) => this.moveStop(1, e.shiftKey));
    $('btn-stop-mid').addEventListener('click', () => this.setStop(null));
    $('btn-stop-atr').addEventListener('click', () => { if (this.plan && this.plan.stop_atr !== null) this.setStop(this.plan.stop_atr); });
    $('btn-nt-buy').addEventListener('click', () => this.confirmBuy());
    $('btn-nt-cancel').addEventListener('click', () => this.closeNewTrade());
    // inside the panel's fields: the arrows still move the stop (the ratio field keeps its own stepping)
    $('nt-panel').addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); this.closeNewTrade(); return; }
      if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && e.target.id !== 'nt-ratio' && e.target.closest('input, select')) {
        e.preventDefault(); this.moveStop(e.key === 'ArrowUp' ? 1 : -1, e.shiftKey);
      }
    });
    $('form-exit').addEventListener('submit', (e) => { e.preventDefault(); this.confirmExit(); });
    $('btn-exit-cancel').addEventListener('click', () => $('dlg-exit').close());
    $('dlg-exit').addEventListener('cancel', (e) => { e.preventDefault(); $('dlg-exit').close(); });
    for (const th of $('day-trade-table').querySelectorAll('th.sortable')) th.addEventListener('click', () => this.setSort(th.dataset.key));
    const condSel = $('day-cond-key');
    for (const [k, label] of COND_LABELS) { const o = document.createElement('option'); o.value = k; o.textContent = label; condSel.appendChild(o); }
    condSel.addEventListener('change', () => this.setCondFilter(condSel.value, undefined));
    for (const b of $('panel-day').querySelectorAll('.chip[data-v]')) b.addEventListener('click', () => this.setCondFilter(undefined, b.dataset.v));
    for (const b of $('panel-cond').querySelectorAll('.chip[data-scope]')) b.addEventListener('click', () => this.setCondScope(b.dataset.scope));
    $('day-notes').addEventListener('input', () => this.queueNotes());
    $('day-notes').addEventListener('change', () => { clearTimeout(this.notesTimer); this.saveNotes(this.notesDate, $('day-notes').value, 'day-notes-state'); });
    $('btn-snap-notes-save').addEventListener('click', () => this.saveNotes($('snap-notes-wrap').dataset.date, $('snap-notes').value, 'snap-notes-state'));
    $('dlg-snapshot').addEventListener('close', () => $('snap-notes-wrap').classList.add('hidden'));   // the swing journal shares the dialog
  }
}
