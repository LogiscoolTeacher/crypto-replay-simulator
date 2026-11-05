// The Report tab: results across day sessions (backend replay/report.py, GET /api/report).
//
// The filters above pick the sessions (a checkbox list, or "the open session"), the end reason
// (all / timeout only / non-timeout only), the mode, the strategy, the coin and a date range of the
// entry day; the outcome and exit chips narrow the trade table, and the numbers follow it. The
// tiles hold the numbers of all selected trades in exit order; the session table holds each
// session's own numbers and how it ended. Export CSV downloads the trades as filtered, every column.
// The tab starts with the open session (when it is a day session) and follows the open session until
// another choice is made in the session list.

const TRADE_COLS = [
  // [key, header, title, numeric]
  ['session_name', 'Session', 'The session the trade belongs to', false],
  ['seq', '#', 'Trade number within its session', true],
  ['mode_label', 'Mode', 'Automatic, Manual or By hand (old day sessions without a strategy)', false],
  ['strategy', 'Strategy', 'Strategy name (the version is in the hover)', false],
  ['symbol', 'Coin', '', false],
  ['entry_ms', 'Entry', 'Entry time (UTC): the close of the entry candle', false],
  ['entry_price', 'Entry price', '', true],
  ['initial_sl', 'SL at entry', 'Stop loss at the entry (hover for the last one)', true],
  ['initial_tp', 'TP at entry', 'Take profit at the entry (hover for the last one)', true],
  ['exit_ms', 'Exit', 'Exit time (UTC)', false],
  ['exit_price', 'Exit price', '', true],
  ['exit_kind', 'Exit', 'SL, TP, manual close, strategy exit, session timeout or session stopped (hover for the reason)', false],
  ['duration_h', 'Hours', 'Hours from the entry to the exit', true],
  ['outcome', 'Outcome', 'After fees; -0.05 R to +0.05 R is BREAKEVEN', false],
  ['result_price', 'Result', 'Exit price minus entry price, per coin', true],
  ['result_pct', '%', 'P&L after fees in percent of the position value', true],
  ['result_r', 'R', 'P&L after fees divided by the initial risk', true],
  ['pnl_usdt', 'USDT', 'P&L after fees', true],
  ['high_price', 'High', 'Highest price between entry and exit (hover for the time)', true],
  ['low_price', 'Low', 'Lowest price between entry and exit (hover for the time)', true],
  ['mfe_r', 'MFE R', 'Maximum favourable excursion in R', true],
  ['mae_r', 'MAE R', 'Maximum adverse excursion in R', true],
  ['after_high_pct', '24 h high', 'Highest price in the 24 hours after the exit, percent from the exit price (hover for price and time)', true],
  ['after_low_pct', '24 h low', 'Lowest price in the 24 hours after the exit, percent from the exit price (hover for price and time)', true],
  ['sl_tp_changes', 'SL/TP moves', 'How often the stop or the target was moved (hover for each move)', true],
  ['confidence', 'Conf.', 'Confidence 0 to 10 at the entry', true],
  ['session_end', 'Session end', 'How the session ended', false],
];

export class ReportTab {
  constructor(ctx) {
    this.c = ctx;
    this.data = null;
    this.f = { end: 'all', mode: '', strategy: '', symbol: '', date_from: '', date_to: '', outcome: '', exit_kind: '' };
    this.picked = null;        // the chosen session ids; null = all sessions
    this.follow = true;        // the selection is "the open session" and moves with it
    this.sort = { key: 'exit_ms', dir: 1 };
    this.seq = 0;              // the answer of the latest request wins
  }
  get $() { return this.c.$; }
  visible() { return this.$('tab-report').getAttribute('aria-selected') === 'true'; }

  // A session was opened (or none): a selection that follows the open session moves with it.
  onSession(s) {
    if (!this.follow) return;
    this.picked = s && s.mode === 'day' ? [s.id] : null;
    if (this.visible()) this.load();
  }

  params() {
    const p = new URLSearchParams();
    if (this.picked) p.set('sessions', this.picked.join(','));
    for (const [k, v] of Object.entries(this.f)) if (v && !(k === 'end' && v === 'all')) p.set(k, v);
    return p.toString();
  }
  async load() {
    const my = ++this.seq;
    const $ = this.$;
    $('rp-state').textContent = 'loading…';
    $('btn-rp-csv').href = this.c.api.reportCsvUrl(this.params());
    try {
      const data = await this.c.api.report(this.params());
      if (my !== this.seq) return;
      this.data = data;
      $('rp-state').textContent = '';
      this.render();
    } catch (e) {
      if (my !== this.seq) return;
      $('rp-state').textContent = e.message;
    }
  }

  // ------------------------------------------------------------- filters
  renderFilters() {
    const $ = this.$;
    const o = this.data.options;
    const fill = (id, items, all) => {
      const sel = $(id);
      const cur = sel.value;
      sel.innerHTML = '';
      for (const [v, label] of [['', all], ...items]) { const x = document.createElement('option'); x.value = v; x.textContent = label; sel.appendChild(x); }
      sel.value = items.some(([v]) => v === cur) ? cur : '';
    };
    fill('rp-mode', o.modes.map(m => [m.key, m.label]), 'all modes');
    fill('rp-strategy', o.strategies.map(s => [s.key, s.label]), 'all strategies');
    fill('rp-symbol', o.symbols.map(s => [s, s]), 'all coins');
    fill('rp-exit_kind', o.exit_kinds.map(k => [k, k]), 'all exits');
    for (const k of ['mode', 'strategy', 'symbol', 'exit_kind']) $(`rp-${k}`).value = this.f[k] || '';
    for (const b of $('rp-end').querySelectorAll('.chip')) b.classList.toggle('on', b.dataset.v === this.f.end);
    for (const b of $('rp-outcome').querySelectorAll('.chip')) b.classList.toggle('on', b.dataset.v === this.f.outcome);
    // the session list: one checkbox per session
    const list = $('rp-sessions-list');
    list.innerHTML = '';
    for (const s of o.sessions) {
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.value = s.id; cb.checked = !this.picked || this.picked.includes(s.id);
      cb.addEventListener('change', () => this.pickFromList());
      label.append(cb, document.createTextNode(` ${s.name} · ${s.symbol} · ${s.mode_label}`));
      list.appendChild(label);
    }
    const foot = document.createElement('div');
    foot.className = 'cols-foot';
    const btn = (text, title, fn) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'btn small'; b.textContent = text; b.title = title; b.addEventListener('click', fn); foot.appendChild(b); };
    btn('All', 'Every day session', () => this.pick(null, false));
    btn('Open session', 'Only the session that is open, and follow it when another one is opened', () => {
      const s = this.c.state.session;
      this.pick(s && s.mode === 'day' ? [s.id] : null, true);
    });
    list.appendChild(foot);
    const n = this.picked ? this.picked.length : o.sessions.length;
    $('rp-sessions-btn').textContent = this.follow && this.picked ? 'Sessions: open session' : `Sessions: ${this.picked ? `${n} of ${o.sessions.length}` : 'all'}`;
  }
  pick(ids, follow) {
    this.picked = ids;
    this.follow = follow;
    this.$('rp-sessions').open = false;
    this.load();
  }
  pickFromList() {
    const boxes = Array.from(this.$('rp-sessions-list').querySelectorAll('input[type=checkbox]'));
    const on = boxes.filter(b => b.checked).map(b => b.value);
    this.picked = on.length === boxes.length ? null : on;
    this.follow = false;
    this.load();
  }
  setFilter(key, value) {
    this.f[key] = value;
    this.load();
  }

  // -------------------------------------------------------------- render
  render() {
    if (!this.data) return;
    this.renderFilters();
    this.renderTiles();
    this.renderSessions();
    this.renderTrades();
  }
  num(v, digits = 2) { return v === null || v === undefined ? '–' : v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }); }
  signed(v, digits = 2) { return v === null || v === undefined ? '–' : `${v > 0 ? '+' : ''}${this.num(v, digits)}`; }
  price(v) {
    if (v === null || v === undefined) return '–';
    const a = Math.abs(v);
    return this.num(v, a >= 1000 ? 2 : a >= 1 ? 4 : 6);
  }
  cls(v) { return v > 0 ? 'up' : v < 0 ? 'down' : ''; }
  renderTiles() {
    const s = this.data.summary;
    const box = this.$('rp-tiles');
    box.innerHTML = '';
    const tile = (k, v, sub = '', cls = '', title = '') => {
      const d = document.createElement('div');
      d.className = 'tile';
      if (title) d.title = title;
      d.innerHTML = '<div class="k"></div><div class="v"></div><div class="s"></div>';
      d.querySelector('.k').textContent = k;
      const val = d.querySelector('.v'); val.textContent = v; if (cls) val.classList.add(cls);
      d.querySelector('.s').textContent = sub;
      box.appendChild(d);
    };
    tile('Trades', String(s.trades), `${s.wins} win · ${s.losses} loss · ${s.breakevens} BE${s.open ? ` · ${s.open} open` : ''}`, '', 'Closed trades; open trades are listed but not counted');
    tile('Win rate', s.win_rate === null ? '–' : `${s.win_rate.toFixed(1)}%`, 'wins / closed trades', '', 'Breakevens count in the total');
    tile('Expectancy', s.expectancy_r === null ? '–' : `${this.signed(s.expectancy_r)} R`, 'average R per trade', this.cls(s.expectancy_r));
    tile('Average win', s.avg_win_r === null ? '–' : `${this.signed(s.avg_win_r)} R`, s.avg_win_usdt === null ? '' : `${this.signed(s.avg_win_usdt)} USDT`, 'up');
    tile('Average loss', s.avg_loss_r === null ? '–' : `${this.signed(s.avg_loss_r)} R`, s.avg_loss_usdt === null ? '' : `${this.signed(s.avg_loss_usdt)} USDT`, 'down');
    tile('Total', s.total_r === null ? '–' : `${this.signed(s.total_r)} R`,
      s.total_usdt === null ? '' : `${this.signed(s.total_usdt)} USDT${s.total_pct === null ? '' : ` · ${this.signed(s.total_pct)}%`}`, this.cls(s.total_r),
      'After fees. The percent is of the start balance (only when every selected session started with the same balance)');
    tile('Duration', s.avg_hours === null ? '–' : `${s.avg_hours.toFixed(1)} h`, s.median_hours === null ? '' : `median ${s.median_hours.toFixed(1)} h`, '', 'Average and median hours from entry to exit');
    tile('Max drawdown', s.max_drawdown_r === null ? '–' : `${this.num(s.max_drawdown_r)} R`, 'running R, peak to low', s.max_drawdown_r ? 'down' : '', 'The deepest fall of the running sum of R below its highest point (trades in exit order)');
    tile('Streaks', `${s.win_streak} / ${s.loss_streak}`, 'longest wins / losses', '', 'Longest run of wins and of losses in a row; a breakeven ends both');
  }
  renderSessions() {
    const body = this.$('rp-session-list');
    body.innerHTML = '';
    for (const r of this.data.sessions) {
      const s = r.stats;
      const tr = document.createElement('tr');
      const td = (text, cls = '', title = '') => { const x = document.createElement('td'); x.textContent = text; if (cls) x.className = cls; if (title) x.title = title; tr.appendChild(x); };
      td(r.name, '', r.id);
      td(r.symbol);
      td(r.mode_label);
      td(r.strategy_label || '–');
      td(r.end_flag || r.end_label || '–', '', r.end_detail || (r.end_flag ? '' : 'Old day sessions have no end flag'));
      td(String(s.trades), 'num');
      td(s.win_rate === null ? '–' : `${s.win_rate.toFixed(1)}%`, 'num');
      td(s.expectancy_r === null ? '–' : this.signed(s.expectancy_r), `num ${this.cls(s.expectancy_r)}`);
      td(s.total_r === null ? '–' : this.signed(s.total_r), `num ${this.cls(s.total_r)}`);
      td(s.total_usdt === null ? '–' : this.signed(s.total_usdt), `num ${this.cls(s.total_usdt)}`);
      td(s.max_drawdown_r === null ? '–' : this.num(s.max_drawdown_r), 'num');
      td(`${s.win_streak} / ${s.loss_streak}`, 'num');
      body.appendChild(tr);
    }
    this.$('rp-sessions-empty').classList.toggle('hidden', this.data.sessions.length > 0);
  }
  setSort(key) {
    this.sort = this.sort.key === key ? { key, dir: -this.sort.dir } : { key, dir: 1 };
    this.renderTrades();
  }
  renderTrades() {
    const $ = this.$;
    const head = $('rp-trade-head');
    if (!head.childElementCount) {
      for (const [key, label, title, numeric] of TRADE_COLS) {
        const th = document.createElement('th');
        th.className = `sortable${numeric ? ' num' : ''}`;
        th.dataset.key = key; th.textContent = label; if (title) th.title = title;
        th.addEventListener('click', () => this.setSort(key));
        head.appendChild(th);
      }
    }
    for (const th of head.children) {
      th.classList.toggle('asc', th.dataset.key === this.sort.key && this.sort.dir > 0);
      th.classList.toggle('desc', th.dataset.key === this.sort.key && this.sort.dir < 0);
    }
    const { key, dir } = this.sort;
    const rows = [...this.data.trades].sort((a, b) => {
      const x = a[key], y = b[key];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;    // empty cells last, either way
      if (y === null || y === undefined) return -1;
      return (x < y ? -1 : 1) * dir;
    });
    const body = $('rp-trade-list');
    body.innerHTML = '';
    for (const t of rows) {
      const tr = document.createElement('tr');
      const td = (text, cls = '', title = '') => { const x = document.createElement('td'); x.textContent = text; if (cls) x.className = cls; if (title) x.title = title; tr.appendChild(x); };
      const p = (v) => this.price(v);
      td(t.session_name, '', t.session_id);
      td(String(t.seq), 'num');
      td(t.mode_label);
      td(t.strategy || '–', '', t.strategy_version ? `version ${t.strategy_version}` : '');
      td(t.symbol);
      td(t.entry_time || '–', 'mono');
      td(p(t.entry_price), 'num');
      td(p(t.initial_sl), 'num', t.final_sl !== t.initial_sl ? `last ${p(t.final_sl)}` : '');
      td(p(t.initial_tp), 'num', t.final_tp !== t.initial_tp ? `last ${p(t.final_tp)}` : '');
      td(t.exit_time || (t.status === 'open' ? 'open' : '–'), 'mono');
      td(p(t.exit_price), 'num');
      td(t.exit_kind || '–', '', t.exit_reason || '');
      td(t.duration_h === null ? '–' : t.duration_h.toFixed(1), 'num');
      td(t.outcome || (t.status === 'open' ? 'open' : '–'), t.outcome === 'WIN' ? 'up' : t.outcome === 'LOSS' ? 'down' : '');
      td(t.result_price === null ? '–' : `${t.result_price > 0 ? '+' : ''}${p(t.result_price)}`, `num ${this.cls(t.result_price)}`);
      td(this.signed(t.result_pct), `num ${this.cls(t.result_pct)}`);
      td(this.signed(t.result_r), `num ${this.cls(t.result_r)}`);
      td(this.signed(t.pnl_usdt), `num ${this.cls(t.pnl_usdt)}`, t.fees_usdt === null ? '' : `fees ${this.num(t.fees_usdt)} USDT`);
      td(p(t.high_price), 'num', t.high_time ? `at ${t.high_time}` : '');
      td(p(t.low_price), 'num', t.low_time ? `at ${t.low_time}` : '');
      td(this.signed(t.mfe_r), 'num');
      td(this.signed(t.mae_r), 'num');
      td(t.after_high_pct === null ? '–' : `${this.signed(t.after_high_pct)}%`, 'num', t.after_high === null ? '' : `${p(t.after_high)} at ${t.after_high_time}${t.after_complete === false ? ' (the data ended within the 24 hours)' : ''}`);
      td(t.after_low_pct === null ? '–' : `${this.signed(t.after_low_pct)}%`, 'num', t.after_low === null ? '' : `${p(t.after_low)} at ${t.after_low_time}`);
      td(String(t.sl_tp_changes), 'num', (t.changes || '').split('; ').join('\n'));
      td(t.confidence === null ? '–' : String(t.confidence), 'num');
      td(t.session_end || '–');
      body.appendChild(tr);
    }
    $('rp-trade-empty').classList.toggle('hidden', rows.length > 0);
    $('rp-count').textContent = `${rows.length} trade${rows.length === 1 ? '' : 's'} in ${this.data.sessions.length} session${this.data.sessions.length === 1 ? '' : 's'}`;
  }

  // ---------------------------------------------------------------- init
  init() {
    const $ = this.$;
    for (const k of ['mode', 'strategy', 'symbol', 'exit_kind']) $(`rp-${k}`).addEventListener('change', () => this.setFilter(k, $(`rp-${k}`).value));
    for (const k of ['date_from', 'date_to']) $(`rp-${k}`).addEventListener('change', () => this.setFilter(k, $(`rp-${k}`).value));
    for (const b of $('rp-end').querySelectorAll('.chip')) b.addEventListener('click', () => this.setFilter('end', b.dataset.v));
    for (const b of $('rp-outcome').querySelectorAll('.chip')) b.addEventListener('click', () => this.setFilter('outcome', b.dataset.v));
    document.addEventListener('click', (e) => { const m = $('rp-sessions'); if (m.open && !m.contains(e.target)) m.open = false; });
  }
}
