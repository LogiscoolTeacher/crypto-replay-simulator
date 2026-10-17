// Day-trading mode (sessions with mode "day"): the side panel with the trend check, the zones and the
// trade buttons, the New trade / Sell / Trend check dialogs, zone drawing by two clicks, the Trades
// tab with the picture per trade, and the handling of step events (exits, jumps).
//
// The module gets everything it needs from `ctx` (app.js): the shared state, the chart, the API,
// formatting helpers and the few app functions it has to call back (pause, applyState, refreshView).
// Nothing here runs for a swing session: every entry point checks `active()` first.

const ZONE_INTERVALS = ['1h', '4h'];
const PLAN_DEBOUNCE_MS = 150;
const PLAN_MARGIN_BARS = 30;
// The entry conditions saved with every trade (same keys and order as CONDITIONS in day_engine.py).
const COND_LABELS = [
  ['uptrend', 'Trend check: uptrend (EMA 200 and higher highs/lows)'],
  ['ema_above', 'Daily close above EMA 200'],
  ['hh_hl', 'Higher highs and higher lows (your answer)'],
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

export class DayMode {
  constructor(ctx) {
    this.c = ctx;
    this.zones = [];
    this.trades = [];
    this.selectedZone = null;
    this.drawing = false;
    this.draft = null;         // first zone edge (price) after the first click
    this.askedDay = null;      // "<session>:<date>" of the last automatic trend-check dialog
    this.planTimer = null;
    this.plan = null;          // last plan preview from the server
    this.planCursor = null;    // cursor the open plan was computed for; a new candle refreshes it
    this.planSeq = 0;
    this.busy = false;         // a buy, sell, skip or continue is in flight
    this.sort = { key: 'seq', dir: 1 };
    this.confidence = null;          // the score 0..10 picked in the New trade form (required for the buy)
    this.conditions = null;        // the Conditions tab's last answer
    this.condScope = 'session';    // 'session' | 'all'
    this.condFilter = { key: 'all', val: 'all' };   // the Trades tab filter
    this.days = [];            // the daily journal rows
    this.calMonth = null;      // 'YYYY-MM' shown in the Calendar tab; null = the month of the current day
    this.notesDate = null;     // the day whose notes the card's box shows
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
  dialogOpen() { return ['dlg-trend', 'dlg-exit'].some(id => this.$(id).open); }
  n(v, digits = 2) { return v === null || v === undefined || Number.isNaN(v) ? '–' : v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
  signed(v, digits = 2) { return v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${this.n(v, digits)}`; }
  pctText(v) { return v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`; }
  flag(v) { return v === null || v === undefined ? 'n/a' : v ? 'yes' : 'no'; }
  cls(v) { return v > 0 ? 'up' : v < 0 ? 'down' : ''; }

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
    this.plan = null;
    this.cancelDraw();
    this.c.chart.setUserZones([]);
    this.c.chart.setDayLines(null);
    this.c.chart.setTimeZone(on ? (s.day && s.day.timezone) || 'Europe/Malta' : null);
    for (const id of ['dlg-trend', 'dlg-exit']) if (this.$(id).open) this.$(id).close();
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
    const [zones, trades, days] = await Promise.all([this.c.api.dayZones(this.session.id), this.c.api.dayTrades(this.session.id), this.c.api.dayDays(this.session.id)]);
    if (!this.c.fresh(t)) return;
    this.zones = zones;
    if (!zones.some(z => z.id === this.selectedZone)) this.selectedZone = null;
    this.trades = trades.trades;
    this.days = days.days;
    this.renderZones();
    this.renderTrades();
    this.renderDays();
    this.renderCal();
    this.renderMarkers();
  }

  // ------------------------------------------------------------ calendar
  // One month, a week per row from Monday to Sunday: the P&L (after fees) and the trades of every
  // day the replay has seen, the week's and the month's total. A past day opens its journal; a
  // later day inside the replay jumps there (06:00).
  localDate(ms) { return ms === null || ms === undefined ? null : new Date(ms).toLocaleDateString('sv-SE', { timeZone: 'Europe/Malta' }); }
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
          cls.push('click'); td.title = `${ds}: jump there (06:00). No trade may be open.`;
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
    return d && d.local_time ? `${d.local_time} Malta` : null;
  }

  // The bar at the top right: how much of the trading day (06:00-24:00 Malta) has passed at the
  // cursor candle's close, and the hours still left; full once the day is over (the night).
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
    bar.title = `Trading day ${d.day_start_local.slice(11)}–24:00 Malta · now ${d.local_time.slice(11)} · ${left <= 0 ? 'over' : `${Number(left.toFixed(2))} hours left`}`;
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
    set('day-time', ok ? `${d.local_time.slice(11)} Malta${d.night ? ' · night' : ''}` : '');
    const phase = $('day-phase');
    phase.className = 'badge';
    if (!ok) phase.textContent = '';
    else if (s.finished) { phase.textContent = 'end of replay'; phase.classList.add('good'); }
    else if (d.phase === 'exited') { phase.textContent = 'trade closed'; phase.classList.add('warn'); }
    else if (d.trade) { phase.textContent = 'in a trade'; phase.classList.add('good'); }
    else phase.textContent = d.in_window ? 'trading hours' : 'night';
    this.renderProgress(ok ? s : null);

    const chk = ok ? d.check : null;
    if (chk) {
      const ema = chk.ema_above;
      const detail = chk.ema200 === null ? 'EMA 200 n/a' : `close ${this.c.fmtPrice(chk.daily_close)} vs EMA ${this.c.fmtPrice(chk.ema200)}`;
      set('day-ema', `${this.flag(ema)} · ${detail}`, `mono gauge ${ema === null ? '' : ema ? 'up' : 'down'}`);
      $('btn-hh-yes').classList.toggle('on', chk.hh_hl === true);
      $('btn-hh-no').classList.toggle('on', chk.hh_hl === false);
      set('day-uptrend', this.flag(chk.uptrend), `gauge ${chk.uptrend === null ? '' : chk.uptrend ? 'up' : 'down'}`);
    } else {
      set('day-ema', '–', 'mono gauge');
      set('day-uptrend', '–', 'gauge');
      $('btn-hh-yes').classList.remove('on');
      $('btn-hh-no').classList.remove('on');
    }
    set('day-risk', ok ? `${this.n(d.risk_amount)} USDT (${d.risk_pct} %)` : '–');
    const gate = $('day-gate');
    gate.textContent = ok ? (d.entries_allowed
      ? (d.check.hh_hl === null ? 'Entries allowed. The trend check is not answered: a trade would save the trend as unknown.'
        : d.uptrend === null ? 'Entries allowed. EMA 200 not available: a trade saves the trend as unknown.'
          : d.uptrend ? 'Entries allowed. Uptrend by the trend check.' : 'Entries allowed. No uptrend by the trend check (saved with the trade).')
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
    $('btn-hh-yes').disabled = $('btn-hh-no').disabled = !ok || busy || s.finished;
    $('btn-day-new').disabled = !ok || busy || !d.entries_allowed;
    $('btn-day-sell').disabled = !ok || busy || !d.can_sell;
    $('btn-day-skip').disabled = !ok || busy || !d.can_skip;
    $('btn-day-ema').disabled = !ok || busy || !(d.can_skip || d.can_continue);   // any state without an open trade
    $('btn-day-continue').disabled = !ok || busy || !d.can_continue;
    $('btn-day-continue').classList.toggle('hidden', !(ok && d.can_continue));
    $('btn-day-skip').classList.toggle('hidden', !!(ok && d.can_continue));   // both jump to the next 06:00: one button at a time
    $('btn-day-zone').disabled = !ok || busy;

    // the open trade
    const t = ok ? d.trade : null;
    $('day-trade').classList.toggle('hidden', !t);
    $('dt-move-row').classList.toggle('hidden', !t);
    if (!t) this.closeMove();
    if (t) {
      const fp = this.c.fmtPrice;
      const away = (v, word) => (v === null || v === undefined ? '' : ` · ${Math.abs(v).toFixed(2)}% ${word}`);
      const stopMoved = t.stop_initial !== undefined && t.stop_initial !== t.stop_used;
      const tpMoved = t.tp_initial !== undefined && t.tp_initial !== t.take_profit;
      const moves = (t.changes || []).length;
      const conf = t.confidence === null || t.confidence === undefined ? '' : ` · confidence ${t.confidence}/10`;
      set('dt-seq', `#${t.seq} · since ${t.entry_local.slice(11)}${conf}${t.overnight ? ' · overnight' : ''}${moves ? ` · moved ${moves}×` : ''}`);
      $('dt-seq').title = moves ? this.changesText(t) : '';
      set('dt-entry', fp(t.entry_price));
      const usdt = t.price_change_usdt === undefined ? t.coins * (this.session.price - t.entry_price) : t.price_change_usdt;
      set('dt-change', `${this.signed(usdt)} USDT · ${this.pctText(t.price_change_pct)}`, `mono gauge ${this.cls(t.price_change_pct)}`);
      set('dt-stop', `${fp(t.stop_used)} · ${this.pctText(t.stop_pct)}`);
      $('dt-stop').title = `${this.pctText(t.stop_pct)} from the entry${away(t.to_stop_pct, 'below the price now')}${stopMoved ? ` · at the buy ${fp(t.stop_initial)} (${this.pctText((t.stop_initial / t.entry_price - 1) * 100)}) · ${this.changesText(t)}` : ''}`;
      set('dt-tp', `${fp(t.take_profit)} · ${this.pctText(t.tp_pct)}`);
      $('dt-tp').title = `${this.pctText(t.tp_pct)} from the entry${away(t.to_tp_pct, 'above the price now')}${tpMoved ? ` · at the buy ${fp(t.tp_initial)} (${this.pctText((t.tp_initial / t.entry_price - 1) * 100)}) · ${this.changesText(t)}` : ''}`;
      $('btn-dt-move').disabled = busy || !d.can_sell;
      $('btn-mv-apply').disabled = busy || !d.can_sell;
      if (this.moveOpen()) this.renderMoveHint(t);
      set('dt-size', `${this.c.qtyFmt(t.coins)} ${this.c.baseAsset()} = ${this.n(t.position_value)} USDT${t.capped ? ' · capped' : ''}`);
      const rrNow = t.rr_now === undefined ? t.rr : t.rr_now;
      set('dt-risk', `${this.n(t.risk_usdt)} USDT · R/R ${this.n(rrNow)}`);
      $('dt-risk').title = rrNow !== t.rr ? `Reward-to-risk now; at the buy ${this.n(t.rr)}` : 'Reward-to-risk of the trade';
      set('dt-pnl', `${this.signed(t.unrealized_pnl)} (${this.pctText(t.unrealized_pnl_pct)}) · ${this.signed(t.r_now, 2)} R`, `mono gauge ${this.cls(t.unrealized_pnl)}`);
    }
    this.c.chart.setDayLines(t);
    this.maybeAskTrend();
  }

  // ------------------------------------------------- move the stop / target
  moveOpen() { return !this.$('dt-move').classList.contains('hidden'); }
  openMove() {
    const t = this.day && this.day.trade;
    if (!this.ready() || !t || !this.day.can_sell) return;
    const $ = this.$;
    $('mv-stop').value = this.roundPrice(t.stop_used);
    $('mv-tp').value = this.roundPrice(t.take_profit);
    $('mv-error').textContent = '';
    $('dt-move').classList.remove('hidden');
    $('btn-dt-move').setAttribute('aria-expanded', 'true');
    this.renderMoveHint(t);
    $('mv-stop').focus();
    $('mv-stop').select();
  }
  closeMove() {
    const $ = this.$;
    if (!this.moveOpen()) return;
    $('dt-move').classList.add('hidden');
    $('btn-dt-move').setAttribute('aria-expanded', 'false');
    $('mv-error').textContent = '';
  }
  renderMoveHint(t) {
    const price = this.session.price;
    const stop = parseFloat(this.$('mv-stop').value), tp = parseFloat(this.$('mv-tp').value);
    const parts = [`Price now ${this.c.fmtPrice(price)}.`];
    if (Number.isFinite(stop) && stop > 0) parts.push(`Stop ${this.pctText((stop / t.entry_price - 1) * 100)} from the entry${stop >= price ? ' (must be below the price now)' : ''}.`);
    if (Number.isFinite(tp) && tp > 0) parts.push(`Target ${this.pctText((tp / t.entry_price - 1) * 100)} from the entry${tp <= price ? ' (must be above the price now)' : ''}.`);
    if (Number.isFinite(stop) && stop > 0 && stop < t.entry_price) parts.push(`Risk from here ${this.n((t.entry_price - stop) * t.coins)} USDT (${this.n((t.entry_price - stop) * t.coins / t.risk_amount)} R of the risk at the buy).`);
    else if (Number.isFinite(stop) && stop >= t.entry_price && stop < price) parts.push('The stop is above the entry: the trade can no longer lose on the price.');
    this.$('mv-hint').textContent = parts.join(' ');
  }
  // One line per saved move, oldest first.
  changesText(t) {
    const fp = this.c.fmtPrice;
    const pe = (v) => this.pctText((v / t.entry_price - 1) * 100);
    return (t.changes || []).map(c => `${c.at_local.slice(11)} ${c.field === 'stop' ? 'stop' : 'target'} ${pe(c.from)} \u2192 ${pe(c.to)}`).join(' · ') || 'no moves';
  }
  async applyMove() {
    if (!this.ready() || this.busy) return;
    const t = this.day.trade;
    if (!t) return;
    const $ = this.$;
    const stop = parseFloat($('mv-stop').value), tp = parseFloat($('mv-tp').value);
    const body = {};
    // a field left as it was filled in (the rounded current level) is no move
    if (Number.isFinite(stop) && stop > 0 && $('mv-stop').value !== this.roundPrice(t.stop_used)) body.stop = stop;
    if (Number.isFinite(tp) && tp > 0 && $('mv-tp').value !== this.roundPrice(t.take_profit)) body.take_profit = tp;
    if (!Object.keys(body).length) { $('mv-error').textContent = 'Nothing changed.'; return; }
    const ok = await this.moveTrade(body, (msg) => { $('mv-error').textContent = msg; });
    if (ok) this.closeMove();
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

  // The trend check opens by itself at 06:00 of every new day until it is answered.
  maybeAskTrend() {
    const d = this.day;
    if (!this.ready() || this.session.finished || !d.check || d.check.hh_hl !== null || d.phase !== 'trading') return;
    const key = `${this.session.id}:${d.date}`;
    if (this.askedDay === key || this.dialogOpen() || this.$('dlg-session').open) return;
    this.askedDay = key;
    this.openTrend();
  }

  // ---------------------------------------------------------- trend check
  openTrend() {
    const d = this.day;
    if (!d || !d.check) return;
    const c = d.check;
    this.$('trend-date').textContent = d.date;
    const ema = this.$('trend-ema');
    if (c.ema200 === null) { ema.textContent = 'n/a (fewer than 200 daily candles)'; ema.className = 'mono'; }
    else {
      ema.textContent = `${c.ema_above ? 'YES' : 'NO'} · close ${this.c.fmtPrice(c.daily_close)} vs EMA 200 ${this.c.fmtPrice(c.ema200)}`;
      ema.className = `mono ${c.ema_above ? 'up' : 'down'}`;
    }
    this.$('trend-close-day').textContent = c.daily_close_iso ? `completed UTC day ${c.daily_close_iso.slice(0, 10)}` : '';
    this.$('dlg-trend').showModal();
    this.$('btn-trend-yes').focus();
  }
  async answerTrend(yes) {
    if (!this.ready() || this.busy) return;
    const t = this.c.token();
    this.busy = true;
    try {
      const st = await this.c.api.dayCheck(this.session.id, yes);
      if (!this.c.fresh(t)) return;
      if (this.$('dlg-trend').open) this.$('dlg-trend').close();
      this.c.applyState(st);
      const d = st.day;
      this.c.toast(d.uptrend ? 'Uptrend by the trend check.' : 'No uptrend by the trend check: it is saved with every trade of today.');
    } catch (e) { this.c.toast(e.message, true); }
    finally { this.busy = false; this.render(); }
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
    this.$('nt-title').textContent = `New trade · ${this.c.baseAsset()} @ ${this.c.fmtPrice(this.session.price)} · ${d.local_time.slice(11)} Malta`;
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
      ['1. Trend check says uptrend', sg.uptrend],
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
      const st = await this.c.api.daySell(this.session.id, { reason: picked.value, note });
      if (!this.c.fresh(t)) return;
      $('dlg-exit').close();
      const events = st.events || [];
      delete st.events;
      this.c.applyState(st);
      await this.handleEvents(events);
    } catch (e) { if (this.c.fresh(t)) $('exit-error').textContent = e.message; }
    finally { this.busy = false; this.render(); }
  }

  // --------------------------------------------------------- skip, continue
  async skipDay() {
    if (!this.ready() || this.busy || !this.day.can_skip) return;
    await this.jump(() => this.c.api.daySkip(this.session.id));
  }
  // Jump to the next trading day whose daily close is above the EMA 200 (the trend check's first line).
  async nextEmaDay() {
    if (!this.ready() || this.busy || !(this.day.can_skip || this.day.can_continue)) return;
    await this.jump(() => this.c.api.dayNextEma(this.session.id));
  }
  async continueDay() {
    if (!this.ready() || this.busy || !this.day.can_continue) return;
    await this.jump(() => this.c.api.dayContinue(this.session.id));
  }
  async jump(call) {
    this.busy = true;
    this.render();
    await this.captureDayPicture();   // the journal picture of the day being left (also switches the generation)
    const t = this.c.token();
    try {
      const st = await call();
      if (!this.c.fresh(t)) return;
      const events = st.events || [];
      delete st.events;
      this.c.applyState(st);
      await this.c.refreshView();
      for (const ev of events) if (ev.kind === 'jump') this.c.toast(`${ev.reason} Now ${ev.to_local} Malta.`);
      await this.refresh();   // the journal row of the day just left (how it ended) and the new day's row
    } catch (e) { if (this.c.fresh(t)) this.c.toast(e.message, true); }
    finally { this.busy = false; this.render(); }
  }

  // ----------------------------------------------------------------- events
  // Events of a step or a sell: an exit saves the picture of the chart at the exit and then jumps to
  // the next 06:00 by itself (spec: no confirmation); a jump is only announced.
  async handleEvents(events) {
    if (!this.active()) return;
    for (const ev of events || []) {
      if (ev.kind === 'jump') { this.c.toast(`${ev.reason} Now ${ev.to_local} Malta.`); continue; }
      if (ev.kind !== 'exit') continue;
      this.c.pause();
      const tr = ev.trade;
      this.c.toast(`Trade #${tr.seq} closed (${tr.exit_type}) @ ${this.c.fmtPrice(tr.exit_price)}: ${this.signed(tr.pnl)} USDT (${this.pctText(tr.pnl_pct)}, ${this.signed(tr.r_multiple)} R)`, tr.pnl < 0);
      await this.refresh();
      await this.capturePicture(tr);
      await this.captureDayPicture();   // the day's own journal picture, with the closed trade drawn
      // the replay stays on the exit candle: buy again, step on, or Continue -> 06:00
      this.c.toast('Trade closed. You can buy again, step on, or press Continue \u2192 06:00.');
    }
  }
  // The chart as it stands at the exit: zones, entry, stop, take-profit and the exit line.
  async capturePicture(trade) {
    if (!this.ready()) return;
    this.c.chart.setDayLines(trade);
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));  // let the lines paint
    let image = null;
    try { image = this.c.chart.chart.takeScreenshot().toDataURL('image/png'); } catch { image = null; }
    this.c.chart.setDayLines(this.day ? this.day.trade : null);
    if (!image) return;
    const t = this.c.token();
    try {
      const row = await this.c.api.dayTradeImage(this.session.id, trade.id, image);
      if (!this.c.fresh(t)) return;
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
      if (closed) this.c.chart.setDayLines(closed);
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));  // let the chart paint
      let image = null;
      try { image = this.c.chart.chart.takeScreenshot().toDataURL('image/png'); } catch { image = null; }
      if (closed) this.c.chart.setDayLines(d.trade);
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
      td(this.flag(r.hh_hl).replace('n/a', '–'), r.hh_hl === null ? '' : r.hh_hl ? 'up' : 'down');
      td(this.flag(r.uptrend).replace('n/a', '–'), r.uptrend === null ? '' : r.uptrend ? 'up' : 'down');
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
        ['Close above EMA 200', this.flag(r.ema_above)], ['Higher highs and higher lows', this.flag(r.hh_hl)], ['Uptrend', this.flag(r.uptrend)]]],
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
  renderTrades() {
    const $ = this.$;
    const tbody = $('day-trade-list');
    tbody.innerHTML = '';
    const f = this.condFilter;
    const shown = f.key === 'all' || f.val === 'all' ? this.trades
      : this.trades.filter(t => t.conditions && t.conditions[f.key] === (f.val === 'yes'));
    const rows = [...shown].sort((a, b) => {
      const va = a[this.sort.key], vb = b[this.sort.key];
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
        img.alt = `Chart at the exit of trade #${t.seq}`;
        pic.appendChild(img);
      } else pic.textContent = t.status === 'open' ? 'open' : '–';
      tr.appendChild(pic);
      td(`#${t.seq}`, 'seq');
      td(t.day_date, 'date');
      td(t.entry_local.slice(11), 'date', t.entry_iso);
      td(t.exit_local ? t.exit_local.slice(11) : '–', 'date', t.exit_iso || '');
      td(`${t.exit_type || 'open'}${(t.changes || []).length ? ' · moved' : ''}`, `exit ${t.exit_type === 'stop' ? 'stop' : t.exit_type === 'take-profit' ? 'target' : ''}`, (t.changes || []).length ? this.changesText(t) : '');
      td(t.exit_reason_label || '', 'reasons', t.exit_note || '');
      td(`${this.flag(t.ema_above)} / ${this.flag(t.hh_hl)}`, '', 'daily close above EMA 200 / higher highs and lows');
      td(this.condShort(t.conditions), 'reasons', this.condLong(t.conditions));
      const confCell = td('', 'num', 'confidence at the buy, 0 to 10');
      if (t.confidence === null || t.confidence === undefined) confCell.textContent = '–';
      else { const s = document.createElement('span'); s.className = 'conf'; s.textContent = String(t.confidence); confCell.appendChild(s); }
      td(fp(t.entry_price), 'num');
      td(t.entry_rsi === null ? '–' : t.entry_rsi.toFixed(1), 'num');
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
      const w = t.whatif;
      for (const k of ['above_exit_pct', 'below_exit_pct', 'high_vs_entry_pct', 'low_vs_entry_pct']) {
        td(w ? this.pctText(w[k]) : (t.status === 'closed' ? 'n/a' : ''), `num ${w ? this.cls(w[k]) : 'muted'}`,
          w ? `${w.n} candles after the exit until 24:00 · high ${fp(w.high)}, low ${fp(w.low)}` : 'no candles after the exit before 24:00');
      }
      td(t.notes || '', 'reasons', t.notes || '');
      tr.addEventListener('click', () => this.openTrade(t));
      tbody.appendChild(tr);
    }
    const empty = $('day-trade-empty');
    empty.classList.toggle('hidden', rows.length > 0);
    if (!rows.length && this.trades.length) empty.textContent = `No trade matches the filter (${this.trades.length} hidden).`;
    else if (!rows.length) empty.textContent = 'No trades yet. Answer the trend check at 06:00, draw a support zone on the 1h or 4h chart, then New trade.';
    $('day-trade-count').textContent = String(this.trades.length);
  }

  // ---------------------------------------------------------- conditions
  // Short "trend ✓ · zone ✓ · …" for the trades table, the full list for its hover text.
  condShort(c) {
    if (!c) return '';
    const yn = (v) => v === null || v === undefined ? '–' : v ? '✓' : '✗';
    const stop = c.stop_middle ? 'middle' : c.stop_atr ? 'ATR' : c.stop_own ? 'own' : '–';
    const when = c.morning ? 'morning' : c.afternoon ? 'afternoon' : c.evening ? 'evening' : '–';
    return `trend ${yn(c.uptrend)} · zone ${yn(c.price_in_zone)} · RSI dip ${yn(c.rsi_dipped)} · trigger ${yn(c.close_above_prev_high)} · R/R ${yn(c.rr_ok)} · stop ${stop} · ${c.zone_4h === null ? '–' : c.zone_4h ? '4h' : '1h'} · ${when}`;
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
    const groups = [
      ['Basics', [['Coin', this.session.symbol], ['Trading day', t.day_date], ['Confidence at the buy', t.confidence === null || t.confidence === undefined ? 'not scored' : `${t.confidence} / 10`],
        ['Checklist', `v1 · risk ${this.n(t.risk_amount)} USDT (1 % of the start)`]]],
      ['Trend check', [['Daily close above EMA 200', this.flag(t.ema_above)], ['Higher highs and higher lows', this.flag(t.hh_hl)]]],
      ['Conditions at the buy', COND_LABELS.map(([k, label]) => [label, this.flag(t.conditions ? t.conditions[k] : null)])],
      ['Entry', [['Time (Malta)', t.entry_local], ['Time (UTC)', t.entry_iso], ['Price', fp(t.entry_price)], ['RSI 14 (15m)', t.entry_rsi === null ? 'n/a' : t.entry_rsi.toFixed(2)],
        ['ATR 14 (15m)', t.entry_atr === null ? 'n/a' : fp(t.entry_atr)], ['Zone upper edge', fp(t.zone_upper)], ['Zone lower edge', fp(t.zone_lower)]]],
      ['Plan', [['Default stop (zone middle)', t.stop_suggested === null ? 'n/a' : fp(t.stop_suggested)], ['ATR stop (zone low − 0.5 × ATR)', t.stop_atr === null || t.stop_atr === undefined ? 'n/a' : fp(t.stop_atr)],
        ['Stop at the buy', fp(t.stop_initial === undefined ? t.stop_used : t.stop_initial)], ['Take-profit at the buy', fp(t.tp_initial === undefined ? t.take_profit : t.tp_initial)], ['Reward-to-risk at the buy', `${this.n(t.rr)} : 1`],
        ['Stop at the end', fp(t.stop_used)], ['Take-profit at the end', fp(t.take_profit)]]],
      ['Moves during the trade', (t.changes || []).length
        ? t.changes.map(c => [`${c.at_local.slice(0, 10) === t.day_date ? c.at_local.slice(11) : c.at_local} · ${c.field === 'stop' ? 'stop' : 'target'} (price ${fp(c.price)})`, `${this.pctText((c.from / t.entry_price - 1) * 100)} \u2192 ${this.pctText((c.to / t.entry_price - 1) * 100)} (${fp(c.to)})`])
        : [['Moves', 'none']]],
      ['Position', [['Coins', this.c.qtyFmt(t.coins)], ['Position value', `${this.n(t.position_value)} USDT`], ['Risk', `${this.n(t.risk_usdt)} USDT`], ['Capped', t.capped ? 'yes' : 'no'], ['Buy fee', `${this.n(t.buy_fee)} USDT`]]],
      ['Exit', [['Time (Malta)', t.exit_local || '–'], ['Price', t.exit_price === null || t.exit_price === undefined ? '–' : fp(t.exit_price)], ['Type', t.exit_type || 'open'],
        ['Reason', t.exit_reason_label || '–'], ['Note', t.exit_note || '–'], ['Overnight', t.overnight ? 'yes' : 'no']]],
      ['Result', [['P&L (after fees)', `${this.signed(t.pnl)} USDT`], ['P&L % of position', this.pctText(t.pnl_pct)], ['Fees', t.fees === null || t.fees === undefined ? '–' : `${this.n(t.fees)} USDT`], ['R', this.signed(t.r_multiple)]]],
      ['What if (until 24:00 of the exit day)', w ? [['Candles after the exit', String(w.n)], ['Highest high', fp(w.high)], ['Lowest low', fp(w.low)], ['Above exit', this.pctText(w.above_exit_pct)],
        ['Below exit', this.pctText(w.below_exit_pct)], ['High vs entry', this.pctText(w.high_vs_entry_pct)], ['Low vs entry', this.pctText(w.low_vs_entry_pct)]] : [['Window', t.status === 'closed' ? 'n/a (no candles before 24:00)' : 'trade still open']]],
      ['Notes', [['Notes', t.notes || '–']]],
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
    $('dlg-snapshot').showModal();
  }

  // ---------------------------------------------------------------- keys
  // Returns true when the key was handled. Day-mode shortcuts: N new trade, X sell, K skip, Z zone.
  onKey(e) {
    if (!this.active()) return false;
    if (this.dialogOpen()) return true;  // the dialogs handle their own keys
    const k = e.key.toLowerCase();
    if (this.panelOpen()) {   // New trade panel: Esc closes, the arrows move the stop; the replay keys still work
      if (k === 'escape') { this.closeNewTrade(); return true; }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { e.preventDefault(); this.moveStop(e.key === 'ArrowUp' ? 1 : -1, e.shiftKey); return true; }
      if (['n', 'b', 'x', 'k', 'z'].includes(k)) return true;
    }
    if (k === 'escape' && this.moveOpen()) { this.closeMove(); return true; }
    if (k === 'escape' && (this.drawing || this.draft !== null)) { this.cancelDraw(); return true; }
    if (k === 'm') { e.preventDefault(); this.moveOpen() ? this.closeMove() : this.openMove(); return true; }
    if (k === 'n' || k === 'b') { e.preventDefault(); this.openNewTrade(); return true; }
    if (k === 'x') { e.preventDefault(); this.openExit(); return true; }
    if (k === 'k') { e.preventDefault(); this.skipDay(); return true; }
    if (k === 'j') { e.preventDefault(); this.nextEmaDay(); return true; }
    if (k === 'z') { e.preventDefault(); this.drawing ? this.cancelDraw() : this.startDraw(); return true; }
    return false;
  }

  // ---------------------------------------------------------------- init
  init() {
    const $ = this.$;
    $('btn-hh-yes').addEventListener('click', () => this.answerTrend(true));
    $('btn-hh-no').addEventListener('click', () => this.answerTrend(false));
    $('btn-trend-yes').addEventListener('click', () => this.answerTrend(true));
    $('btn-trend-no').addEventListener('click', () => this.answerTrend(false));
    $('btn-trend-later').addEventListener('click', () => $('dlg-trend').close());
    $('dlg-trend').addEventListener('cancel', (e) => { e.preventDefault(); $('dlg-trend').close(); });
    $('btn-day-new').addEventListener('click', () => this.openNewTrade());
    $('btn-day-sell').addEventListener('click', () => this.openExit());
    $('btn-day-skip').addEventListener('click', () => this.skipDay());
    $('btn-day-ema').addEventListener('click', () => this.nextEmaDay());
    $('btn-day-continue').addEventListener('click', () => this.continueDay());
    $('btn-cal-prev').addEventListener('click', () => this.moveCal(-1));
    $('btn-cal-next').addEventListener('click', () => this.moveCal(1));
    $('btn-cal-now').addEventListener('click', () => { this.calMonth = null; this.renderCal(); });
    $('btn-day-zone').addEventListener('click', () => (this.drawing ? this.cancelDraw() : this.startDraw()));
    // move the stop / target of the open trade: the form and the dragged chart lines
    $('btn-dt-move').addEventListener('click', () => (this.moveOpen() ? this.closeMove() : this.openMove()));
    $('btn-mv-cancel').addEventListener('click', () => this.closeMove());
    $('dt-move').addEventListener('submit', (e) => { e.preventDefault(); this.applyMove(); });
    for (const id of ['mv-stop', 'mv-tp']) $(id).addEventListener('input', () => { if (this.day && this.day.trade) this.renderMoveHint(this.day.trade); });
    $('dt-move').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); this.closeMove(); } e.stopPropagation(); });
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
