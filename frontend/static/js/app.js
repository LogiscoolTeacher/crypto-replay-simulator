// Application controller: sessions, replay controls, timeframe, indicators, strategy lights,
// buying and selling buckets, account panel and history.
//
// Concurrency rule: every response from the server is applied only if it still belongs to the
// current "generation" (session, timeframe, indicator set, strategy). `state.gen` is bumped
// whenever one of those changes; handlers capture it before awaiting and drop stale results.

import { api } from './api.js';
import { ChartView, fmtPrice } from './chart.js';
import { IndicatorPanel } from './indicators.js';
import { buildParamInputs, readParamInputs } from './params.js';
import { PerfCharts, renderMonths } from './perf.js';
import { DayMode } from './day.js';

const $ = (id) => document.getElementById(id);
const VIEW_LIMIT = 600;
const PEEK_INTERVAL = '1D';  // hold D / the 1Y button: daily candles of the last year
const PEEK_BARS = 365;
const CONTEXT_YEARS = 2;     // hold Y / the BTC 2Y button: Bitcoin daily candles with major levels
const SR_TOL_DEFAULT = 1.5;
const SWING_START_BALANCE = '10000';   // the new-session form's default balance per mode (day: 0.1 % risk = 100 USDT)
const DAY_START_BALANCE = '100000';
const FIB_HINT = 'Fib 1 of 2: click the swing start. Clicks snap to the candle high or low. Esc cancels.';
const DEFAULT_INDICATORS = [{ key: 'ema', params: { period: 20 } }, { key: 'ema', params: { period: 50 } }];

const state = {
  coins: [], intervals: [], catalog: {}, strategies: {}, sessions: [],
  session: null,        // full state object from GET /api/sessions/{id}
  interval: null,       // current view/step timeframe
  strategy: null,       // { key, params } or null
  lastGoodStrategy: null,     // last strategy the server accepted (rollback target)
  lastGoodIndicators: [],     // last indicator list the server accepted (rollback target)
  strategyQueue: Promise.resolve(),
  trades: [],           // journal (with bucket seq attached)
  closedBuckets: [],
  fibs: [], selectedFib: null, fibDraft: null, drawing: false,
  autoFib: { on: false, lookback: 100 },
  fvg: { on: false, minPct: 0.1, hideFilled: false },
  dailyEma: false,
  viewSeq: 0,  // only the latest view request may paint the chart
  // Table sorting and RSI filter, per table ('open' | 'history').
  sort: { open: { key: 'seq', dir: 1 }, history: { key: 'seq', dir: 1 }, journal: { key: 'time_ms', dir: 1 } },
  journal: [], journalFilter: 'all',
  reasons: null,         // the buy and sell checklists from the server (GET /api/reasons)
  pending: null,         // the trade waiting in the reason dialog: {side, run(reasons, note)}
  stats: null, statsSymbol: '',  // the Reasons statistics and its coin filter
  perf: null, perfScope: 'session', perfTimer: null,  // the Performance report, its scope and a refresh debounce
  rsiFilter: { open: 'all', history: 'all' },
  bottomOpen: true,    // the bottom tab area shows its content (false: only the tab bar)
  bottomMode: null,    // mode of the last loaded session; a mode change resets bottomOpen
  selectedBucket: null, // bucket id highlighted on the chart
  lastSignalTime: null,
  peek: null,         // 'year' (D / 1Y: this coin, daily, last year) or 'btc' (Y / BTC 2Y) while held
  srTol: SR_TOL_DEFAULT,  // cluster tolerance of the Bitcoin levels, percent
  nearLevels: true,       // tubes at the next support / resistance and the 6-month high / low on the chart
  wholeCoins: false,  // buys round the quantity to whole coins (the Whole coins toggle)
  peekSaved: null,    // timeframe display and zoom to restore when the peek ends
  peekPending: null,  // a restore that was requested but has not painted yet
  speed: 2,
  playing: false,
  busy: false,          // a step is in flight
  trading: false,       // a buy or sell is in flight
  gen: 0,
  timer: null,
  pollTimer: null,
};

const chart = new ChartView($('chart'), $('legend'));
let indicators;
let day;  // the day-trading mode (day.js); every entry point checks the session's mode itself
function isDay() { return !!state.session && state.session.mode === 'day'; }
function fetchSession(id, mode) { return mode === 'day' ? api.daySession(id) : api.session(id); }
function sessionMode(id) { const m = state.sessions.find(x => x.id === id); return m ? m.mode : (state.session && state.session.id === id ? state.session.mode : 'swing'); }

// ------------------------------------------------------------------ helpers
let toastTimer = null;
function toast(msg, isError = false) {
  const el = $('toast');
  el.classList.toggle('error', isError);
  el.classList.remove('hidden');           // unhide first so the live region announces the change
  requestAnimationFrame(() => { el.textContent = msg; });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), isError ? 6000 : 3000);
}

function money(v, signed = false) {
  if (v === null || v === undefined) return '–';
  const s = v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return signed && v > 0 ? `+${s}` : s;
}
function pct(v) { return v === null || v === undefined ? '' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`; }
function qtyFmt(q) { return q.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 6 }); }
function pnlClass(v) { return v > 0 ? 'up' : v < 0 ? 'down' : ''; }
function pctOrDash(v) { return v === null || v === undefined ? '–' : pct(v); }
function isoShort(iso) { return iso ? iso.replace('T', ' ').replace('+00:00', '') : ''; }
// Age of a bucket on the replay clock: days first, hours while the bucket is young.
function ageText(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const hours = Math.floor(ms / 3_600_000);
  const days = Math.floor(hours / 24);
  if (hours < 1) return '<1h';
  if (days < 1) return `${hours}h`;
  const rest = hours - days * 24;
  return days < 10 && rest > 0 ? `${days}d ${rest}h` : `${days}d`;
}
const RSI_THRESHOLD = 30;
function rsiText(v) { return v === null || v === undefined ? '–' : v.toFixed(1); }
function rsiClass(v) { return v === null || v === undefined ? '' : v < RSI_THRESHOLD ? 'low' : v > RSI_THRESHOLD ? 'high' : ''; }
function rsiTitle(v, iv) { return v === null || v === undefined ? 'RSI not available (too little history)' : `RSI(14) on ${iv} at the trade`; }
function rsiMatches(v, filter) {
  if (filter === 'all') return true;
  if (v === null || v === undefined) return false;
  return filter === 'below' ? v < RSI_THRESHOLD : v > RSI_THRESHOLD;
}
// Sort keys per column, in header order (null = not sortable).
const OPEN_KEYS = ['seq', 'opened_ms', 'age', 'qty', 'entry_price', 'buy_rsi', 'buy_fee', 'cost', 'market_value', 'unrealized_pnl', 'unrealized_pnl_pct', 'held_high_pct', 'held_low_pct', 'tp_pct', 'sl_pct', 'armed_ms', null];
const JOURNAL_KEYS = [null, 'side', 'bucket_seq', 'time_ms', 'interval', 'price', 'quote', 'rsi', 'bb_pctb', 'ema200_pct', 'ath_pct', 'atl_pct', 'vs_last_buy_pct', 'vs_last_sell_pct', 'strategy', null, null, 'realized_pnl', 'realized_pnl_pct', 'held_ms'];
const HISTORY_KEYS = ['seq', 'opened_ms', 'closed_ms', 'held', 'qty', 'entry_price', 'exit_price', 'buy_rsi', 'sell_rsi', 'ema200_change_pct', 'ema200_dist_change', 'fees', 'cost', 'proceeds', 'realized_pnl', 'realized_pnl_pct', 'tp_pct', 'sl_pct', 'trigger', 'held_high', 'held_high_pct', 'held_low', 'held_low_pct'];
function sortValue(b, key) {
  switch (key) {
    case 'age': return (state.session ? state.session.cursor_ms : 0) - b.opened_ms;
    case 'held': return b.closed_ms - b.opened_ms;
    case 'fees': return b.buy_fee + (b.sell_fee || 0);
    default: return b[key];
  }
}
function sortedRows(rows, table) {
  const { key, dir } = state.sort[table];
  return rows.slice().sort((a, b) => {
    const va = sortValue(a, key), vb = sortValue(b, key);
    const na = va === null || va === undefined, nb = vb === null || vb === undefined;
    const tie = (a.seq ?? a.id ?? 0) - (b.seq ?? b.id ?? 0) || (a.part ?? 0) - (b.part ?? 0);  // parts of one bucket in order
    if (na || nb) return na === nb ? tie : na ? 1 : -1;  // missing values last
    return (va < vb ? -1 : va > vb ? 1 : tie) * dir;
  });
}
function setSort(table, key) {
  const cur = state.sort[table];
  state.sort[table] = cur.key === key ? { key, dir: -cur.dir } : { key, dir: 1 };
  renderSortHeaders(table);
  if (table === 'journal') renderJournal();
  else table === 'open' ? renderBuckets() : renderHistory();
}
function setRsiFilter(table, filter) {
  state.rsiFilter[table] = filter;
  for (const chip of $(`${table}-tools`).querySelectorAll('.chip')) {
    chip.classList.toggle('on', chip.dataset.f === filter);
    chip.setAttribute('aria-pressed', String(chip.dataset.f === filter));
  }
  table === 'open' ? renderBuckets() : renderHistory();
}
function renderSortHeaders(table) {
  const { key, dir } = state.sort[table];
  const tableId = { open: 'bucket-table', history: 'history-table', journal: 'journal-table' }[table];
  for (const th of $(tableId).querySelectorAll('thead th')) {
    const active = th.dataset.key && th.dataset.key === key;
    th.classList.toggle('asc', active && dir === 1);
    th.classList.toggle('desc', active && dir === -1);
    if (th.dataset.key) th.setAttribute('aria-sort', active ? (dir === 1 ? 'ascending' : 'descending') : 'none');
  }
}
function initTableTools() {
  for (const [table, id, keys] of [['open', 'bucket-table', OPEN_KEYS], ['history', 'history-table', HISTORY_KEYS], ['journal', 'journal-table', JOURNAL_KEYS]]) {
    $(id).querySelectorAll('thead th').forEach((th, i) => {
      const key = keys[i];
      if (!key) return;
      th.dataset.key = key;
      th.classList.add('sortable');
      th.tabIndex = 0;
      th.setAttribute('role', 'columnheader');
      th.addEventListener('click', () => setSort(table, key));
      th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSort(table, key); } });
    });
    renderSortHeaders(table);
    if (table === 'journal') continue;  // its chips filter by side, wired in initJournal
    for (const chip of $(`${table}-tools`).querySelectorAll('.chip')) {
      chip.setAttribute('aria-pressed', String(chip.classList.contains('on')));
      chip.addEventListener('click', () => setRsiFilter(table, chip.dataset.f));
    }
  }
}
function ageTitle(ms) { return Number.isFinite(ms) && ms >= 0 ? `${(ms / 86_400_000).toFixed(2)} days` : ''; }
function intervalMs(name) { return (state.intervals.find(i => i.name === name) || {}).ms || 0; }
function isIntraday(name) { return intervalMs(name) < 24 * 3600 * 1000; }
function baseAsset() { return state.session ? state.session.symbol.replace(/USDT$/, '') : ''; }
function ready() { return !!state.session && state.session.status === 'ready'; }

function prefKey(k) { return `replay.pref.${state.session ? state.session.id : 'none'}.${k}`; }
function getPref(k, fallback) { try { return localStorage.getItem(prefKey(k)) ?? fallback; } catch { return fallback; } }
function setPref(k, v) { try { localStorage.setItem(prefKey(k), v); } catch { /* ignore */ } }

// Capture the current generation; `fresh(token)` tells whether a response may still be applied.
function token() { return { gen: state.gen, sid: state.session ? state.session.id : null }; }
function fresh(t) { return t.gen === state.gen && !!state.session && state.session.id === t.sid; }
function bump() { state.gen += 1; }

function strategyInstances() {
  if (!state.strategy) return [];
  const spec = state.strategies[state.strategy.key];
  if (!spec) return [];
  return spec.indicator_templates.map(t => ({
    key: t.key,
    params: Object.fromEntries(Object.entries(t.params).map(([k, v]) => [k, state.strategy.params[v]])),
  }));
}

// Day-trading mode: the daily peek (D / 1Y) carries the EMA 200 of the trend check.
const PEEK_EMA = { key: 'ema', params: { period: 200 } };
function peekInstances() { return state.peek === 'year' && isDay() ? [PEEK_EMA] : []; }

// User instances first, then the strategy's and the peek's own, skipping duplicates of a user instance.
function allIndicatorInstances() {
  const user = indicators.requestList();
  const seen = new Set(user.map(i => JSON.stringify([i.key, i.params])));
  const extra = [...strategyInstances(), ...peekInstances()].filter(i => !seen.has(JSON.stringify([i.key, i.params])));
  return [...user, ...extra];
}
function applyIndicators() { chart.setIndicators(allIndicatorInstances(), state.catalog); }

// The timeframe the chart shows: the chosen one, or the daily peek while it is held.
function viewInterval() { return state.peek ? PEEK_INTERVAL : state.interval; }
function contextRequest() { return { years: CONTEXT_YEARS, tol_pct: state.srTol, indicators: allIndicatorInstances() }; }

// Day-trading mode: RSI panes under the chart, always these two timeframes.
const DAY_RSI_PANES = ['15m', '1h'];

function viewRequest() {
  return {
    interval: viewInterval(), limit: VIEW_LIMIT, indicators: allIndicatorInstances(), strategy: state.strategy,
    rsi_panes: isDay() && !state.peek ? DAY_RSI_PANES : [],   // not on the daily peek: intraday RSI at a day's close says little
    auto_fib: state.autoFib.on ? state.autoFib.lookback : null,
    fvg_min_pct: state.fvg.on ? state.fvg.minPct : null,
    daily_ema: state.dailyEma,
    sr_tol: state.srTol,
  };
}

// ---------------------------------------------------------------- rendering
function renderTimeframes() {
  const group = $('tf-group');
  group.innerHTML = '';
  const base = state.session ? intervalMs(state.session.base_interval) : 0;
  for (const iv of state.intervals) {
    const b = document.createElement('button');
    b.className = 'tf';
    b.type = 'button';
    b.setAttribute('role', 'radio');
    b.textContent = iv.name;
    b.dataset.interval = iv.name;
    b.disabled = !state.session || iv.ms < base;
    b.setAttribute('aria-checked', String(iv.name === viewInterval()));
    b.addEventListener('click', () => setInterval_(iv.name));
    group.appendChild(b);
  }
}

function renderSessions() {
  const sel = $('session-select');
  sel.innerHTML = '';
  if (!state.sessions.length) {
    const o = document.createElement('option');
    o.textContent = 'No sessions yet';
    o.value = '';
    sel.appendChild(o);
  }
  for (const s of state.sessions) {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = `${s.name} · ${s.mode === 'day' ? 'Day' : s.base_interval}${s.status !== 'ready' ? ` (${s.status})` : ''}`;
    o.selected = state.session && s.id === state.session.id;
    sel.appendChild(o);
  }
  $('btn-delete-session').disabled = !state.session;
}

function renderOverlay() {
  const s = state.session;
  const overlay = $('chart-overlay');
  const card = $('overlay-card');
  card.innerHTML = '';
  const p = (text, cls) => { const el = document.createElement('p'); el.textContent = text; if (cls) el.className = cls; return el; };
  const heading = (text) => { const el = document.createElement('p'); const b = document.createElement('b'); b.textContent = text; el.appendChild(b); return el; };
  if (!s) {
    card.append(p('No session selected.'), p('Create a new session to start replaying.', 'muted'));
  } else if (s.status === 'preparing') {
    const prog = s.prepare_progress;
    const bar = document.createElement('div'); bar.className = 'bar'; bar.appendChild(document.createElement('div'));
    card.append(heading('Downloading candles from Binance…'), p(prog ? `${prog.candles.toLocaleString()} candles, ${prog.requests} requests` : 'starting', 'muted'), bar);
  } else if (s.status === 'error') {
    const btn = document.createElement('button');
    btn.className = 'btn primary'; btn.id = 'btn-retry'; btn.type = 'button'; btn.textContent = 'Retry download';
    btn.addEventListener('click', retryPrepare);
    card.append(heading('Could not prepare this session'), p(s.error || 'unknown error', 'error'), btn); // textContent: may echo upstream HTML
  } else {
    overlay.classList.add('hidden');
    return;
  }
  overlay.classList.remove('hidden');
}

function renderState() {
  const s = state.session;
  const ok = ready();
  $('cursor-time').textContent = ok ? ((isDay() && day.cursorText()) || `${isoShort(s.cursor_iso)} UTC`) : '–';
  $('cursor-price').textContent = ok && s.price !== null ? fmtPrice(s.price) : '–';
  $('step-size').textContent = state.interval || '–';
  $('btn-reset').disabled = !ok;
  $('btn-step').disabled = !ok || s.finished;
  $('btn-step-back').classList.toggle('hidden', !isDay());   // day trading only
  $('btn-step-back').disabled = !ok || !isDay() || state.busy || !(s.day && s.day.can_step_back);
  $('btn-play').disabled = !ok || s.finished;
  $('btn-play').innerHTML = state.playing ? '&#x23F8;' : '&#x25B6;';
  $('btn-play').setAttribute('aria-label', state.playing ? 'Pause' : 'Play');
  $('btn-indicators').disabled = !ok;
  $('btn-fib').disabled = !ok;
  $('btn-peek').disabled = !ok;
  $('btn-peek-btc').disabled = !ok;
  $('btn-fib-draw').disabled = !ok;
  $('btn-fvg').disabled = !ok;
  $('strategy-key').disabled = !ok;
  $('btn-buy').disabled = !ok || state.trading;
  $('buy-amount').disabled = !ok;
  $('buy-coins').disabled = !ok;
  $('btn-whole').disabled = !ok;
  for (const b of $('buy-presets').querySelectorAll('button')) b.disabled = !ok;

  const badge = $('status-badge');
  if (!s) { badge.classList.add('hidden'); }
  else {
    badge.classList.remove('hidden', 'good', 'warn', 'bad');
    if (s.status === 'preparing') { badge.textContent = 'downloading data…'; badge.classList.add('warn'); }
    else if (s.status === 'error') { badge.textContent = 'error'; badge.classList.add('bad'); }
    else if (s.finished) { badge.textContent = 'end of replay'; badge.classList.add('good'); }
    else { badge.textContent = state.playing ? 'playing' : 'paused'; }
  }
  // Why the replay starts later than asked (200 daily candles of warm-up), or that a young coin lacks them.
  const note = $('session-note');
  note.textContent = ok && s.start_note ? s.start_note : '';
  note.classList.toggle('hidden', !(ok && s.start_note));

  if (ok) {
    const span = s.data_end_ms - s.data_start_ms;
    const p = span > 0 ? ((s.cursor_ms - s.data_start_ms) / span) * 100 : 100;
    $('progress-bar').style.width = `${Math.max(0, Math.min(100, p))}%`;
    $('progress-text').textContent = `${s.data_start_iso.slice(0, 10)} → ${s.data_end_iso.slice(0, 10)}`;
  } else {
    $('progress-bar').style.width = '0%';
    $('progress-text').textContent = '';
  }

  const a = ok ? s.account : null;
  const set = (id, v, cls) => { const el = $(id); el.textContent = v; el.className = `mono ${cls || ''}`.trim(); };
  set('acc-cash', a ? money(a.cash) : '–');
  set('acc-invested', a ? money(a.invested) : '–');
  set('acc-mv', a ? money(a.market_value) : '–');
  set('acc-unreal', a ? money(a.unrealized_pnl, true) : '–', a && pnlClass(a.unrealized_pnl));
  set('acc-real', a ? money(a.realized_pnl, true) : '–', a && pnlClass(a.realized_pnl));
  set('acc-fees', a ? money(a.fees_paid) : '–');
  set('acc-equity', a ? money(a.equity) : '–', 'total');
  set('acc-return', a ? `${money(a.total_return, true)} (${pct(a.total_return_pct)})` : '–', a && pnlClass(a.total_return));

  renderOverlay();
  renderBuckets();
  renderReferenceGauges();
  updateEstimate();
  day.render();
  renderCardSums();
}

// "#5" for a bucket, "#5.2" for the second part sold of bucket 5.
const bucketLabel = (seq, part) => `#${seq}${part ? `.${part}` : ''}`;
const SELL_PARTS = [[0.25, '25 %'], [0.5, '50 %'], [1, 'All']];

function renderBuckets() {
  const buckets = ready() ? state.session.buckets : [];
  const list = $('bucket-list');
  // Remember keyboard focus inside the table so a re-render during playback does not lose it.
  const active = document.activeElement;
  const focusRow = active && list.contains(active) ? active.closest('.bucket') : null;
  const focusId = focusRow ? focusRow.dataset.id : null;
  const focusSell = !!(focusRow && active.classList.contains('sell'));

  list.innerHTML = '';
  if (state.selectedBucket !== null && !buckets.some(b => b.id === state.selectedBucket)) state.selectedBucket = null;
  const asset = baseAsset();
  const cursor = ready() ? state.session.cursor_ms : 0;
  const shown = sortedRows(buckets.filter(b => rsiMatches(b.buy_rsi, state.rsiFilter.open)), 'open');
  for (const b of shown) {
    const age = cursor - b.opened_ms;
    const tr = document.createElement('tr');
    tr.className = `bucket${b.id === state.selectedBucket ? ' selected' : ''}`;
    tr.dataset.id = b.id;
    tr.tabIndex = 0;
    tr.setAttribute('aria-selected', String(b.id === state.selectedBucket));
    const cls = pnlClass(b.unrealized_pnl);
    const cells = [
      ['seq', b.parts_sold ? `#${b.seq} ·${b.parts_sold}` : `#${b.seq}`, b.parts_sold ? `${b.parts_sold} part${b.parts_sold === 1 ? '' : 's'} of this bucket already sold (see History)` : ''],
      ['date', isoShort(b.opened_iso)],
      ['num age', ageText(age), ageTitle(age)],
      ['num', `${qtyFmt(b.qty)} ${asset}`],
      ['num', fmtPrice(b.entry_price)],
      [`num rsi ${rsiClass(b.buy_rsi)}`, rsiText(b.buy_rsi), rsiTitle(b.buy_rsi, b.buy_rsi_interval)],
      ['num', money(b.buy_fee)],
      ['num', money(b.cost)],
      ['num', money(b.market_value)],
      [`num pnl ${cls}`, money(b.unrealized_pnl, true)],
      [`num pnl ${cls}`, pct(b.unrealized_pnl_pct)],
      ...excursionCells(b, false),
      ...planCells(b, true),
    ];
    for (const [c, text, title] of cells) {
      const td = document.createElement('td');
      td.className = c;
      td.textContent = text;
      if (title) td.title = title;
      tr.appendChild(td);
    }
    const tdArm = document.createElement('td');
    tdArm.className = 'auto';
    const armBtn = document.createElement('button');
    armBtn.type = 'button';
    const hasPlan = b.tp_price !== null && b.tp_price !== undefined || b.sl_price !== null && b.sl_price !== undefined;
    armBtn.className = `btn arm${b.armed_ms ? ' on' : ''}`;
    armBtn.textContent = b.armed_ms ? 'On' : 'Off';
    armBtn.setAttribute('aria-pressed', String(!!b.armed_ms));
    armBtn.setAttribute('aria-label', `Auto sell of bucket ${b.seq}`);
    armBtn.disabled = state.trading || !hasPlan;
    armBtn.title = !hasPlan ? 'No target or stop was set at the buy'
      : b.armed_ms ? `Auto on since ${isoShort(b.armed_iso)}: sells everything left at the target or the stop. Click to switch off.`
      : 'Click to sell automatically when a candle reaches the target or the stop';
    armBtn.addEventListener('click', (e) => { e.stopPropagation(); arm(b.id, !b.armed_ms); });
    tdArm.appendChild(armBtn);
    tr.appendChild(tdArm);
    const td = document.createElement('td');
    td.className = 'action';
    const group = document.createElement('span');
    group.className = 'sells';
    for (const [part, label] of SELL_PARTS) {
      const sellBtn = document.createElement('button');
      sellBtn.type = 'button';
      sellBtn.className = 'btn sell';
      sellBtn.dataset.id = b.id;
      sellBtn.dataset.part = String(part);
      sellBtn.textContent = label;
      // Whole coins: a part is rounded down to whole coins and refused when that leaves none.
      const coins = part < 1 && state.wholeCoins ? Math.floor(b.qty * part + 1e-9) : null;
      const qty = coins !== null ? coins : b.qty * part;
      sellBtn.disabled = state.trading || (coins !== null && coins < 1);
      sellBtn.title = coins !== null && coins < 1
        ? `${label} of ${qtyFmt(b.qty)} ${asset} is less than one whole coin`
        : `Sell ${part < 1 ? `${label} of what is left: ` : 'everything left: '}${qtyFmt(qty)} ${asset}${ready() && state.session.price !== null ? ` ≈ ${money(qty * state.session.price)}` : ''}`;
      sellBtn.setAttribute('aria-label', `Sell ${label} of bucket ${b.seq}`);
      sellBtn.addEventListener('click', (e) => { e.stopPropagation(); sell(b.id, part); });
      group.appendChild(sellBtn);
    }
    td.appendChild(group);
    tr.appendChild(td);
    const select = () => selectBucket(b.id === state.selectedBucket ? null : b.id);
    tr.addEventListener('click', select);
    tr.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); select(); }
    });
    list.appendChild(tr);
  }
  const ref = ready() ? state.session.reference : null;
  // With nothing open, keep the last sold bucket visible as a reference row.
  if (buckets.length === 0 && ref && ref.last_closed) list.appendChild(closedRefRow(ref.last_closed, asset));
  renderNowRow(ref);
  const anyRow = buckets.length > 0 || !!(ref && (ref.last_buy || ref.last_sell));
  if (buckets.length > 0 && shown.length === 0) {
    const tr = document.createElement('tr');
    tr.className = 'filtered';
    const td = document.createElement('td');
    td.colSpan = OPEN_KEYS.length;
    td.className = 'muted';
    td.textContent = `No open bucket matches the RSI filter (${buckets.length} hidden).`;
    tr.appendChild(td);
    list.appendChild(tr);
  }
  $('bucket-count').textContent = String(buckets.length);
  $('bucket-empty').classList.toggle('hidden', anyRow);
  $('bucket-table').classList.toggle('hidden', !anyRow);
  chart.setBucketLines(buckets, state.selectedBucket);

  if (focusId !== null) {
    const row = list.querySelector(`.bucket[data-id="${focusId}"]`);
    if (row) (focusSell ? row.querySelector('.sell') : row).focus();
  }
}

// The most recently sold bucket, shown greyed when no bucket is open.
function closedRefRow(b, asset) {
  const tr = document.createElement('tr');
  tr.className = 'ref';
  tr.dataset.id = b.id;
  const cls = pnlClass(b.realized_pnl);
  const held = b.closed_ms - b.opened_ms;
  const cells = [
    ['seq', bucketLabel(b.seq, b.part), partTitle(b)],
    ['date', isoShort(b.opened_iso)],
    ['num age', ageText(held), ageTitle(held)],
    ['num', `${qtyFmt(b.qty)} ${asset}`],
    ['num', fmtPrice(b.entry_price)],
    [`num rsi ${rsiClass(b.buy_rsi)}`, rsiText(b.buy_rsi), rsiTitle(b.buy_rsi, b.buy_rsi_interval)],
    ['num', money(b.buy_fee + b.sell_fee)],
    ['num', money(b.cost)],
    ['num', fmtPrice(b.exit_price), 'sell price'],
    [`num pnl ${cls}`, money(b.realized_pnl, true)],
    [`num pnl ${cls}`, pct(b.realized_pnl_pct)],
    ['action', `sold ${isoShort(b.closed_iso)}`],
  ];
  for (const [c, text, title] of cells) {
    const td = document.createElement('td');
    td.className = c;
    td.textContent = text;
    if (title) td.title = title;
    tr.appendChild(td);
  }
  return tr;
}

// Hover text of a part: which part it was and how much of what was left it took.
function partTitle(b) {
  if (!b.part) return '';
  return b.part_pct >= 100 ? `Part ${b.part} of bucket #${b.seq}: the rest of the bucket` : `Part ${b.part} of bucket #${b.seq}: ${b.part_pct.toFixed(0)} % of what was left`;
}

// Template row: what a buy right now would look like against the last buy and the last sell.
function renderNowRow(ref) {
  const tr = $('bucket-now');
  tr.innerHTML = '';
  const s = state.session;
  const show = !!(ref && (ref.last_buy || ref.last_sell) && s && s.price !== null && s.price !== undefined);
  tr.classList.toggle('hidden', !show);
  if (!show) return;
  const td = (c, text) => { const el = document.createElement('td'); el.className = c; el.textContent = text; tr.appendChild(el); return el; };
  td('seq', 'Now');
  td('date', isoShort(s.cursor_iso));
  td('num', '');
  td('num', '');
  td('num', fmtPrice(s.price));
  const vs = td('vs', '');
  vs.colSpan = 9;
  const parts = [['buy', ref.last_buy], ['sell', ref.last_sell]].filter(([, p]) => p);
  parts.forEach(([what, p], i) => {
    if (i > 0) { const sep = document.createElement('span'); sep.className = 'sep'; sep.textContent = '·'; vs.appendChild(sep); }
    const label = document.createElement('span');
    label.className = 'muted';
    const when = `${what === 'buy' ? 'Bought' : 'Sold'} ${isoShort(p.iso)} @ ${fmtPrice(p.price)}`;
    if (p.basis === 'peak') {
      // The market closed above the trade since then: measure from that height instead.
      label.textContent = `vs high since ${what} ${bucketLabel(p.seq, p.part)} @ ${fmtPrice(p.peak)} `;
      label.title = `${when}. Highest close since: ${fmtPrice(p.peak)} on ${isoShort(p.peak_iso)}`;
    } else {
      label.textContent = `vs last ${what} ${bucketLabel(p.seq, p.part)} @ ${fmtPrice(p.price)} `;
      label.title = when;
    }
    const val = document.createElement('span');
    val.className = `pnl ${pnlClass(p.pct)}`;
    val.textContent = pct(p.pct);
    vs.appendChild(label);
    vs.appendChild(val);
  });
  const act = td('action', '');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn';
  btn.textContent = 'Buy…';
  btn.title = 'Go to the buy amount field (B)';
  btn.addEventListener('click', () => $('buy-amount').focus());
  act.appendChild(btn);
}

function selectBucket(id) {
  state.selectedBucket = id;
  renderBuckets();
}

// Target and stop of a bucket as table cells (percent of the buy price; price and, for an open
// bucket, the distance from the current price in the hover text).
function planCells(b, open) {
  const cells = [];
  for (const [k, sign, cls] of [['tp', '+', 'up'], ['sl', '−', 'down']]) {
    const p = b[`${k}_pct`], price = b[`${k}_price`], dist = b[`${k}_dist_pct`];
    if (p === null || p === undefined) { cells.push([`num ${k} muted`, '–', k === 'tp' ? 'No target set at the buy' : 'No stop set at the buy']); continue; }
    const title = `${k === 'tp' ? 'Target' : 'Stop'} ${fmtPrice(price)}${open && dist !== null && dist !== undefined ? ` · ${pct(dist)} from here` : ''}`;
    cells.push([`num ${k} ${cls}`, `${sign}${Number(p).toFixed(1)}%`, title]);
  }
  return cells;
}
// How a closed bucket was sold: by hand or automatically at the target or the stop.
function exitCell(b) {
  const t = b.trigger;
  const armed = b.armed_iso ? `Auto was on since ${isoShort(b.armed_iso)}` : 'Auto was off';
  return [`exit ${t || 'manual'}`, t === 'target' ? 'target' : t === 'stop' ? 'stop' : 'by hand', t ? `Sold automatically at the ${t} price. ${armed}` : armed];
}
async function arm(bucketId, armed) {
  const s = state.session;
  if (!ready() || state.trading) return;
  const t = token();
  try {
    const res = await api.arm(s.id, bucketId, armed);
    if (!fresh(t)) return;
    applyState(res.state);
    const b = res.bucket;
    toast(armed ? `Auto on for bucket #${b.seq}: sells at ${b.tp_price ? `the target ${fmtPrice(b.tp_price)}` : ''}${b.tp_price && b.sl_price ? ' or ' : ''}${b.sl_price ? `the stop ${fmtPrice(b.sl_price)}` : ''}` : `Auto off for bucket #${b.seq}`);
  } catch (e) { toast(e.message, true); }
}
// Buckets the armed targets and stops sold during a step: tell the user and take their journal pictures.
async function handleAutoSells(sold) {
  for (const b of sold) {
    const what = b.trigger === 'target' ? 'Target reached' : 'Stop reached';
    toast(`Auto: ${what}, sold bucket ${bucketLabel(b.seq, b.part)} @ ${fmtPrice(b.exit_price)}: ${money(b.realized_pnl, true)} (${pct(b.realized_pnl_pct)})`, b.trigger === 'stop');
  }
  if (state.selectedBucket !== null && sold.some(b => b.id === state.selectedBucket)) state.selectedBucket = null;
  await refreshTrades();
  for (const b of sold) await captureSnapshot(b.id, 'sell');
}
// The target / stop inputs of the buy card: numbers in percent, or null when empty or invalid.
function planValue(id) {
  const v = parseFloat($(id).value);
  return Number.isFinite(v) && v > 0 ? v : null;
}
function showPlan(on) {
  if (!on || !ready() || state.session.price === null || state.session.price === undefined) { chart.setPlan(null); return; }
  const price = state.session.price, tp = planValue('buy-tp'), sl = planValue('buy-sl');
  if (tp === null && sl === null) { chart.setPlan(null); return; }
  chart.setPlan({ price, tp: tp === null ? null : price * (1 + tp / 100), sl: sl === null ? null : price * (1 - sl / 100), tpPct: tp, slPct: sl });
}
function initPlan() {
  for (const [id, key] of [['buy-tp', 'replay.plan.tp'], ['buy-sl', 'replay.plan.sl']]) {
    try { const v = localStorage.getItem(key); if (v !== null) $(id).value = v; } catch { /* no storage */ }
    $(id).addEventListener('change', () => { try { localStorage.setItem(key, $(id).value); } catch { /* no storage */ } showPlan(chart.plan.plan !== null); });
  }
  const btn = $('btn-buy');
  btn.addEventListener('mouseenter', () => showPlan(true));
  btn.addEventListener('focus', () => showPlan(true));
  btn.addEventListener('mouseleave', () => showPlan(false));
  btn.addEventListener('blur', () => showPlan(false));
}

// Highest high and lowest low while a bucket is (or was) held, in percent of the buy price; with
// `withPrices` the prices get their own cells, else they sit in the hover text.
function excursionCells(b, withPrices) {
  const cells = [];
  for (const k of ['high', 'low']) {
    const v = b[`held_${k}`], p = b[`held_${k}_pct`];
    const title = v === null || v === undefined ? '' : `${k === 'high' ? 'Highest high' : 'Lowest low'} ${fmtPrice(v)} at ${isoShort(b[`held_${k}_iso`])} UTC`;
    if (withPrices) cells.push(['num', v === null || v === undefined ? '–' : fmtPrice(v), title]);
    cells.push([`num ${pnlClass(p)}`, pctOrDash(p), title]);
  }
  return cells;
}

// Percentage points, signed: the change of a "price vs line" distance between buy and sell.
function pp(v) { return v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${v.toFixed(2)} pp`; }
// History cells for the daily EMA 200: how the line moved and how the price's distance to it changed.
function longAvgCells(b) {
  const cells = [];
  for (const [name, label] of [['ema200', 'EMA 200']]) {
    const ch = b[`${name}_change_pct`], dist = b[`${name}_dist_change`];
    const at = (side) => (b[`${side}_${name}`] === null || b[`${side}_${name}`] === undefined ? '–' : fmtPrice(b[`${side}_${name}`]));
    const atPct = (side) => pctOrDash(b[`${side}_${name}_pct`]);
    cells.push([`num ${pnlClass(ch)}`, pctOrDash(ch), `${label} at buy ${at('buy')} → at sell ${at('sell')}`]);
    cells.push([`num ${pnlClass(dist)}`, pp(dist), `Price vs ${label}: at buy ${atPct('buy')} → at sell ${atPct('sell')}`]);
  }
  return cells;
}

function renderHistory() {
  const list = $('history-list');
  list.innerHTML = '';
  const asset = baseAsset();
  const shown = sortedRows(state.closedBuckets.filter(b => rsiMatches(b.buy_rsi, state.rsiFilter.history)), 'history');
  for (const b of shown) {
    const tr = document.createElement('tr');
    tr.className = 'bucket closed';
    tr.dataset.id = b.id;
    const cls = pnlClass(b.realized_pnl);
    const cells = [
      ['seq', bucketLabel(b.seq, b.part), partTitle(b)],
      ['date', isoShort(b.opened_iso)],
      ['date', isoShort(b.closed_iso)],
      ['num held', ageText(b.closed_ms - b.opened_ms), ageTitle(b.closed_ms - b.opened_ms)],
      ['num', `${qtyFmt(b.qty)} ${asset}`],
      ['num', fmtPrice(b.entry_price)],
      ['num', fmtPrice(b.exit_price)],
      [`num rsi ${rsiClass(b.buy_rsi)}`, rsiText(b.buy_rsi), rsiTitle(b.buy_rsi, b.buy_rsi_interval)],
      [`num rsi ${rsiClass(b.sell_rsi)}`, rsiText(b.sell_rsi), rsiTitle(b.sell_rsi, b.sell_rsi_interval)],
      ...longAvgCells(b),
      ['num', money(b.buy_fee + b.sell_fee)],
      ['num', money(b.cost)],
      ['num', money(b.proceeds)],
      [`num pnl ${cls}`, money(b.realized_pnl, true)],
      [`num pnl ${cls}`, pct(b.realized_pnl_pct)],
      ...planCells(b, false),
      exitCell(b),
      ...excursionCells(b, true),
    ];
    for (const [c, text, title] of cells) {
      const td = document.createElement('td');
      td.className = c;
      td.textContent = text;
      if (title) td.title = title;
      tr.appendChild(td);
    }
    list.appendChild(tr);
  }
  if (state.closedBuckets.length > 0 && shown.length === 0) {
    const tr = document.createElement('tr');
    tr.className = 'filtered';
    const td = document.createElement('td');
    td.colSpan = HISTORY_KEYS.length;
    td.className = 'muted';
    td.textContent = `No closed bucket matches the RSI filter (${state.closedBuckets.length} hidden).`;
    tr.appendChild(td);
    list.appendChild(tr);
  }
  $('history-count').textContent = String(state.closedBuckets.length);
  $('history-empty').classList.toggle('hidden', state.closedBuckets.length > 0);
  $('history-table').classList.toggle('hidden', state.closedBuckets.length === 0);
}

function updateEstimate() {
  const s = state.session;
  const el = $('buy-estimate');
  const amount = parseFloat($('buy-amount').value);
  if (!ready() || !Number.isFinite(amount) || amount <= 0 || !s.price) { el.textContent = ''; return; }
  if (state.wholeCoins) {
    const typed = parseInt($('buy-coins').value, 10);
    const { cost, coins } = Number.isFinite(typed) && typed >= 1
      ? { coins: typed, cost: typed * s.price / (1 - s.fee_rate) }
      : wholeCoinOrder(amount, s.price, s.fee_rate, s.cash);
    if (coins < 1) {
      el.textContent = `One whole ${baseAsset()} costs ${money(s.price / (1 - s.fee_rate))} with the fee, more than your cash.`;
      el.classList.add('error');
      return;
    }
    const fee = cost * s.fee_rate;
    el.textContent = `${coins} whole ${baseAsset()} = ${money(cost)} @ ${fmtPrice(s.price)} · fee ${money(fee)}`;
    el.classList.toggle('error', cost > s.cash + 1e-9);
    return;
  }
  const fee = amount * s.fee_rate;
  const qty = (amount - fee) / s.price;
  el.textContent = `≈ ${qtyFmt(qty)} ${baseAsset()} @ ${fmtPrice(s.price)} · fee ${money(fee)}`;
  el.classList.toggle('error', amount > s.cash + 1e-9);
}

// Same rounding as the server: down to the whole coins the amount buys, so the cost never exceeds it.
function wholeCoinOrder(amount, price, feeRate, cash) {
  const target = Math.min(amount, cash) * (1 - feeRate) / price;
  let coins = Math.floor(target + 1e-9);
  let cost = coins * price / (1 - feeRate);
  if (cost > cash + 1e-9) { coins -= 1; cost = coins * price / (1 - feeRate); }
  return { cost, coins: Math.max(coins, 0) };
}

function loadWholeCoinsPref() {
  state.wholeCoins = getPref('wholecoins', '0') === '1';
  renderWholeCoins();
}
function renderWholeCoins() {
  $('btn-whole').setAttribute('aria-pressed', String(state.wholeCoins));
  $('buy-coins-wrap').classList.toggle('hidden', !state.wholeCoins);
}
function setWholeCoins(on) {
  state.wholeCoins = !!on;
  renderWholeCoins();
  renderBuckets();  // the 25 % / 50 % buttons round to whole coins in this mode
  setPref('wholecoins', state.wholeCoins ? '1' : '0');
  if (state.wholeCoins) roundAmountToCoins(); else { $('buy-coins').value = ''; updateEstimate(); }
}
// Whole-coins mode: the USDT field is rewritten to the cost of the whole coins the typed amount
// buys (rounded down), fee included.
function roundAmountToCoins() {
  const s = state.session;
  const amount = parseFloat($('buy-amount').value);
  if (!ready() || !s.price) return;
  if (!Number.isFinite(amount) || amount <= 0) { $('buy-coins').value = ''; updateEstimate(); return; }
  const { cost, coins } = wholeCoinOrder(amount, s.price, s.fee_rate, s.cash);
  $('buy-coins').value = coins >= 1 ? String(coins) : '';
  if (coins >= 1) $('buy-amount').value = cost.toFixed(2);
  updateEstimate();
}
// The coins field was edited: the USDT field follows.
function amountFromCoins() {
  const s = state.session;
  const coins = parseInt($('buy-coins').value, 10);
  if (!ready() || !s.price) return;
  if (Number.isFinite(coins) && coins >= 1) $('buy-amount').value = (coins * s.price / (1 - s.fee_rate)).toFixed(2);
  updateEstimate();
}

function flash(lamp) {
  lamp.classList.remove('flash');
  void lamp.offsetWidth; // restart the animation
  lamp.classList.add('flash');
  setTimeout(() => lamp.classList.remove('flash'), 1900);
}

// The lights follow the last CLOSED candle of the viewed timeframe.
function renderLights(payload) {
  const lampE = $('lamp-enter');
  const lampX = $('lamp-exit');
  const note = $('strategy-note');
  const s = payload && payload.strategy;
  const spec = state.strategy && state.strategies[state.strategy.key];
  if (!s || !spec || !payload.count) {
    lampE.classList.remove('on');
    lampX.classList.remove('on');
    note.textContent = state.strategy ? 'Waiting for data…' : 'No strategy selected.';
    return;
  }
  const last = payload.partial_last ? payload.count - 2 : payload.count - 1;
  if (last < 0) return;
  const st = s.state[last];
  lampE.classList.toggle('on', st === 1);
  lampX.classList.toggle('on', st === -1);
  const t = payload.time[last];
  if (t !== state.lastSignalTime) {
    if (s.enter[last]) flash(lampE);
    if (s.exit[last]) flash(lampX);
    state.lastSignalTime = t;
  }
  const word = st === 1 ? 'entry bias' : st === -1 ? 'exit bias' : 'neutral';
  const ev = s.enter[last] ? ' · ENTER signal on this candle' : s.exit[last] ? ' · EXIT signal on this candle' : '';
  note.textContent = `${spec.name}: ${word} on the last closed ${payload.interval} candle${ev}`;
}

function applyState(s) {
  state.session = s;
  if (s && s.finished && state.playing) pause();
  renderState();
}

function applyContext(ctx) {
  chart.setData(ctx);
  chart.setDailyEma(null);
  chart.setAutoFib(null);
  chart.setFvg([], state.fvg.hideFilled);
  chart.setLevels(ctx.levels);
  chart.setNearLevels(null);  // the peek shows Bitcoin; the coin's own tubes and lines come back on release
  const own = state.session && state.session.symbol === ctx.symbol;
  chart.setFibs(own ? state.fibs : []);
  if (!own) chart.setBucketLines([], null);
  const r = ctx.levels.resistance.length, s = ctx.levels.support.length;
  showHint(`Bitcoin daily candles, last ${ctx.years} years: ${r} resistance, ${s} support level${s === 1 ? '' : 's'}. Release to return.`);
}

function applyView(view) {
  chart.setLevels(null); chart.setNearLevels(null);
  chart.setData(view);
  renderLights(view);
  renderGauges(view.gauges);
  chart.setNearLevels(state.nearLevels ? view.gauges : null);
  chart.setDailyEma(state.dailyEma ? view.daily_ema : null);
  chart.setAutoFib(view.auto_fib || null);
  renderFvg(view.fvg);
}

// Side-panel rows for the price against the last buy and the last sell (same basis as the Now row:
// the trade price, or the highest close since it once the market went above).
function renderReferenceGauges() {
  const ref = ready() ? state.session.reference : null;
  for (const [what, key] of [['buy', 'last_buy'], ['sell', 'last_sell']]) {
    const p = ref ? ref[key] : null;
    const label = $(`gauge-${what}-label`), val = $(`gauge-${what}`);
    const show = !!(p && p.pct !== null && p.pct !== undefined);
    label.classList.toggle('hidden', !show);
    val.classList.toggle('hidden', !show);
    if (!show) continue;
    const peak = p.basis === 'peak';
    label.textContent = peak ? `vs high since ${what} ${bucketLabel(p.seq, p.part)}` : `vs last ${what} ${bucketLabel(p.seq, p.part)}`;
    label.title = peak
      ? `${what === 'buy' ? 'Bought' : 'Sold'} ${isoShort(p.iso)} @ ${fmtPrice(p.price)}; highest close since: ${fmtPrice(p.peak)} on ${isoShort(p.peak_iso)}`
      : `${what === 'buy' ? 'Bought' : 'Sold'} ${isoShort(p.iso)} @ ${fmtPrice(p.price)}`;
    val.textContent = pct(p.pct);
    val.classList.remove('up', 'down');
    if (pnlClass(p.pct)) val.classList.add(pnlClass(p.pct));
  }
}

// Highs & lows card: for each window the distance above the low (left), a range bar with the
// price marker, and the distance below the high (right). Daily wicks known at the cursor.
const dayOf = (iso) => (iso ? iso.slice(0, 10) : '');
function renderExtremes(ex) {
  for (const row of $('extremes').querySelectorAll('.extreme')) {
    const w = ex ? ex[row.dataset.key] : null;
    const lowEl = row.querySelector('.ex-low'), highEl = row.querySelector('.ex-high');
    const fill = row.querySelector('.ex-fill'), marker = row.querySelector('.ex-marker');
    lowEl.classList.remove('up', 'down'); highEl.classList.remove('up', 'down');
    let title = '';
    if (!w || w.high === null || w.high === undefined) {
      lowEl.textContent = '–'; highEl.textContent = '–';
      fill.style.width = '0%'; marker.style.left = '0%';
    } else {
      lowEl.textContent = pct(w.vs_low_pct); highEl.textContent = pct(w.vs_high_pct);
      if (pnlClass(w.vs_low_pct)) lowEl.classList.add(pnlClass(w.vs_low_pct));
      if (pnlClass(w.vs_high_pct)) highEl.classList.add(pnlClass(w.vs_high_pct));
      const pos = Math.max(0, Math.min(1, w.position)) * 100;
      fill.style.width = `${pos}%`; marker.style.left = `${pos}%`;
      const span = w.days === null
        ? `${w.bars} days since the listing on ${dayOf(w.from_iso)}${ex.complete ? '' : ' (history incomplete, download failed)'}`
        : `${w.bars} days from ${dayOf(w.from_iso)}`;
      title = `Low ${fmtPrice(w.low)} on ${dayOf(w.low_iso)} · High ${fmtPrice(w.high)} on ${dayOf(w.high_iso)} · ${span}`;
    }
    for (const el of row.children) el.title = title;  // the row itself has no box (display: contents)
  }
}

// Next resistance and support: distance in percent, touch count and days since the last touch (on the
// replay clock); all touch dates on hover.
function daysAgo(ms) {
  if (!state.session || ms === null || ms === undefined) return '';
  const days = Math.floor((state.session.cursor_ms - ms) / 86400000);
  return days <= 0 ? 'today' : `${days}d ago`;
}
function renderLevelGauges(lv) {
  for (const [key, id, what] of [['resistance', 'gauge-res', 'Resistance'], ['support', 'gauge-sup', 'Support']]) {
    const el = $(id), label = $(`${id}-label`);
    const z = lv ? lv[key] : null;
    const rule = lv ? `zones of ±${lv.tol_pct} % around the swing levels of the closed daily candles of the last ${lv.days} days; a visit counts as a touch when the price leaves on the side it came from within ${lv.max_visit_bars} days, at least ${lv.min_touches} touches` : '';
    if (!z) {
      el.textContent = '–';
      el.title = lv ? `No ${what.toLowerCase()} zone with ${lv.min_touches} touches ${key === 'resistance' ? 'above' : 'below'} the price (${rule})` : '';
      label.title = el.title;
      continue;
    }
    el.textContent = `${z.inside ? 'inside' : pct(z.pct)} · ${z.touches}× · ${daysAgo(z.last_ms)}`;
    const breaks = z.visits.filter(v => v.result === 'break').map(v => v.date);
    const stays = z.visits.filter(v => v.result === 'stay').map(v => v.date);
    el.title = `${what} zone ${fmtPrice(z.low)} – ${fmtPrice(z.high)} (centre ${fmtPrice(z.price)}), `
      + (z.inside ? 'the price is inside it' : `${pct(z.pct)} to its ${key === 'resistance' ? 'lower' : 'upper'} edge`)
      + `; touched ${z.touches} times: ${z.touch_dates.join(', ')}; last touch ${dayOf(z.last_iso)}`
      + (breaks.length ? `; broken ${breaks.length} times: ${breaks.join(', ')}` : '; never broken')
      + (stays.length ? `; ${stays.length} longer stay${stays.length === 1 ? '' : 's'} inside: ${stays.join(', ')}` : '')
      + ` (${rule})`;
    label.title = el.title;
  }
}

// Side-panel readings: RSI of the timeframe on screen (green at or below 30, red above),
// daily EMA 200 and where the price sits relative to it.
function renderGauges(g) {
  queueMicrotask(renderCardSums);   // the Readings summary, once the values below are in
  renderExtremes(g ? g.extremes : null);
  const rsi = $('gauge-rsi'), ema = $('gauge-ema'), pctEl = $('gauge-ema-pct'), bbEl = $('gauge-bb'), bbw = $('gauge-bbw');
  const paint = (el, text, cls) => { el.textContent = text; el.classList.remove('up', 'down'); if (cls) el.classList.add(cls); };
  if (!g) {
    $('gauge-rsi-label').textContent = 'RSI 14';
    paint(rsi, '–'); paint(ema, '–'); paint(pctEl, '–'); paint(bbEl, '–'); paint(bbw, '–');
    renderLevelGauges(null);
    $('gauge-bb-label').textContent = '%B (BB 20/2)';
    return;
  }
  renderLevelGauges(g.levels);
  const bb = g.bb || {};
  $('gauge-bb-label').textContent = `%B (BB ${bb.period || 20}/${bb.mult || 2}, ${g.rsi.interval})`;
  const pb = bb.pctb;
  paint(bbEl, pb === null || pb === undefined ? '–' : pb.toFixed(2), pb === null || pb === undefined ? '' : pb <= 0 ? 'up' : pb >= 1 ? 'down' : '');
  bbEl.title = pb === null || pb === undefined ? 'Needs 20 candles of history' : `Bands ${fmtPrice(bb.lower)} – ${fmtPrice(bb.upper)}, middle ${fmtPrice(bb.middle)}`;
  paint(bbw, bb.width_pct === null || bb.width_pct === undefined ? '–' : `${bb.width_pct.toFixed(2)}%`);
  $('gauge-rsi-label').textContent = `RSI ${g.rsi.period} (${g.rsi.interval})`;
  const r = g.rsi.value;
  paint(rsi, r === null ? '–' : r.toFixed(1), r === null ? '' : r <= RSI_THRESHOLD ? 'up' : 'down');
  const e = g.ema_long.value;
  paint(ema, e === null ? '–' : fmtPrice(e));
  const p = g.ema_long.price_pct;
  paint(pctEl, p === null ? '–' : pct(p), p === null ? '' : pnlClass(p));
  ema.title = e === null ? 'Needs 200 daily candles of history' : `EMA ${g.ema_long.period} on ${g.ema_long.interval}`;
}

// ----------------------------------------------------------------- sessions
async function refreshSessions() {
  state.sessions = await api.sessions();
  renderSessions();
}

async function refreshTrades() {
  if (!ready()) return;
  if (isDay()) { await day.refresh(); return; }
  const t = token();
  const j = await api.trades(state.session.id);
  if (!fresh(t)) return;
  const seqOf = Object.fromEntries(j.buckets.map(b => [b.id, `${b.seq}${b.part ? `.${b.part}` : ''}`]));  // chart markers: S1.2 for a part
  state.trades = j.trades.map(x => ({ ...x, seq: seqOf[x.bucket_id] }));
  state.closedBuckets = j.buckets.filter(b => b.status === 'closed').reverse();
  chart.setTrades(state.trades);
  renderHistory();
  schedulePerf();
}

async function loadSession(id, { fit = true } = {}) {
  pause();
  bump();
  clearTimeout(state.pollTimer);
  state.selectedBucket = null;
  state.lastSignalTime = null;
  state.trades = [];
  state.closedBuckets = [];
  chart.setTrades([]);
  renderHistory();
  renderGauges(null);
  cancelFibDraw();
  state.fibs = [];
  state.selectedFib = null;
  renderFibs();
  chart.setAutoFib(null);
  renderFvg(null);
  state.journal = [];
  renderJournal();
  chart.setDailyEma(null);
  endPeek(null, { restore: false });
  if (!id) {
    state.session = null;
    state.interval = null;
    day.onSession(null);
    renderFolds();
    renderTimeframes();
    renderSessions();
    renderState();
    return;
  }
  const myGen = state.gen;
  const s = await fetchSession(id, sessionMode(id));
  if (myGen !== state.gen) return; // the user already switched again
  state.session = s;
  const modeChanged = state.bottomMode !== s.mode;
  state.bottomMode = s.mode;
  day.onSession(s);
  renderFolds();
  if (modeChanged) setBottom(s.mode !== 'day');   // day trading starts with the trades window small
  try { localStorage.setItem('replay.lastSession', id); } catch { /* ignore */ }
  state.interval = getPref('interval', s.base_interval);
  if (intervalMs(state.interval) < intervalMs(s.base_interval)) state.interval = s.base_interval;
  state.speed = parseInt(getPref('speed', '2'), 10) || 2;
  $('speed').value = state.speed;
  $('speed-label').textContent = `${state.speed}/s`;
  chart.intraday = isIntraday(state.interval);
  chart.intervalMs = intervalMs(state.interval);
  loadStrategyPref();
  renderTimeframes();
  renderSessions();
  renderState();
  if (s.status === 'ready') {
    indicators.load(s.id, DEFAULT_INDICATORS);   // triggers onChange -> applyIndicators (no fetch yet)
    loadFibPrefs();
    loadFvgPrefs();
    loadDailyEmaPref();
    loadNearLevelsPref();
    loadSrTolPref();
    loadWholeCoinsPref();
    $('btn-journal-csv').href = api.snapshotsCsvUrl(state.session.id);
    await Promise.all([refreshView({ fit }), refreshTrades(), refreshFibs(), refreshJournal()]);
  } else if (s.status === 'preparing') {
    pollSession();
  }
}

function pollSession() {
  clearTimeout(state.pollTimer);
  const t = token();
  state.pollTimer = setTimeout(async () => {
    if (!fresh(t)) return;
    try {
      const s = await fetchSession(t.sid, sessionMode(t.sid));
      if (!fresh(t)) return;
      if (s.status === 'preparing') { applyState(s); pollSession(); }
      else { await refreshSessions(); if (fresh(t)) await loadSession(s.id); }
    } catch (e) { toast(e.message, true); }
  }, 1000);
}

async function retryPrepare() {
  const s = state.session;
  if (!s || s.status !== 'error') return;
  const t = token();
  try {
    const st = await (isDay() ? api.dayPrepare(s.id) : api.prepare(s.id));
    if (!fresh(t)) return;
    applyState({ ...st, prepare_progress: null });
    if (st.status === 'preparing') pollSession();
  } catch (e) { toast(e.message, true); }
}

async function refreshView({ fit = false } = {}) {
  if (!ready()) return;
  const t = token();
  const seq = ++state.viewSeq;
  const btc = state.peek === 'btc';
  const v = await (btc ? api.context(state.session.id, contextRequest()) : api.view(state.session.id, viewRequest()));
  if (!fresh(t) || seq !== state.viewSeq) return;  // a newer request is on its way or already painted
  if (btc) applyContext(v); else applyView(v);
  if (fit) chart.fit();
}

// ------------------------------------------------------------------- replay
async function step(n = 1) {
  const s = state.session;
  if (!ready() || state.busy || s.finished) return;
  state.busy = true;
  // Day mode: the step that leaves a trading day is preceded by the day's journal picture.
  if (isDay()) { try { await day.beforeStep(); } catch (e) { toast(e.message, true); } }
  const t = token();
  const peeking = state.peek;
  try {
    const dayMode = isDay();
    const res = await (dayMode ? api.dayStep(s.id, { n, view: viewRequest() }) : api.step(s.id, { interval: state.interval, n, view: viewRequest() }));
    if (!fresh(t)) return;
    const view = res.view;
    delete res.view;
    const auto = res.auto_sells || [];
    delete res.auto_sells;
    const events = res.events || [];
    delete res.events;
    applyState(res);
    if (auto.length) handleAutoSells(auto);
    // A peek started or ended meanwhile, or the Bitcoin context is on screen: the step's view is not what is shown.
    if (peeking !== state.peek || state.peek === 'btc') { await refreshView(); return; }
    state.viewSeq++;  // a step's view supersedes any refresh still in flight
    applyView(view);
    if (dayMode) { await day.handleEvents(events); return; }
    schedulePerf();  // the equity curve moves with every candle
  } catch (e) {
    toast(e.message, true);
    pause();
    // The cursor may have moved even though the view failed: resync the state. A day session needs
    // its own endpoint: the swing one has no day block, so the pending review (and its Save button) vanished.
    if (fresh(t)) fetchSession(t.sid, s.mode).then(st => { if (fresh(t)) applyState(st); }).catch(() => {});
  } finally {
    state.busy = false;
    renderState();   // the day panel drew its buttons while busy: draw them again
  }
}

// Day mode: one 15-minute candle back, never before the latest trade action or jump (the server
// refuses; the button is disabled there).
async function stepBack() {
  const s = state.session;
  if (!ready() || !isDay() || state.busy || !(s.day && s.day.can_step_back)) return;
  pause();
  state.busy = true;
  const t = token();
  try {
    const res = await api.dayStepBack(s.id, { view: viewRequest() });
    if (!fresh(t)) return;
    const view = res.view;
    delete res.view;
    applyState(res);
    if (state.peek) { await refreshView(); return; }
    state.viewSeq++;
    applyView(view);
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.busy = false;
    renderState();
  }
}

function play() {
  if (!ready() || state.session.finished) return;
  state.playing = true;
  clearInterval(state.timer);
  state.timer = setInterval(() => { if (!state.busy) step(1); }, Math.round(1000 / state.speed));
  renderState();
}

function pause() {
  state.playing = false;
  clearInterval(state.timer);
  state.timer = null;
  renderState();
}

async function reset() {
  const s = state.session;
  if (!ready()) return;
  if (!confirm('Back to the start? All trades in this session will be deleted.')) return;
  pause();
  bump();
  const t = token();
  try {
    state.selectedBucket = null;
    state.lastSignalTime = null;
    const st = await (isDay() ? api.dayReset(s.id) : api.reset(s.id));
    if (!fresh(t)) return;
    applyState(st);
    cancelFibDraw();
    state.fibs = [];
    state.selectedFib = null;
    renderFibs();
    state.journal = [];
    renderJournal();
    loadFibPrefs();
    loadFvgPrefs();
    loadDailyEmaPref();
    loadNearLevelsPref();
    loadSrTolPref();
    loadWholeCoinsPref();
    $('btn-journal-csv').href = api.snapshotsCsvUrl(state.session.id);
    await Promise.all([refreshView({ fit: true }), refreshTrades(), refreshFibs(), refreshJournal()]);
  } catch (e) { toast(e.message, true); }
}

async function setInterval_(name) {
  if (!state.session || name === state.interval) return;
  state.interval = name;
  bump();
  setPref('interval', name);
  chart.intraday = isIntraday(name);
  chart.intervalMs = intervalMs(name);
  state.lastSignalTime = null;
  renderTimeframes();
  renderState();
  try { await refreshView({ fit: true }); } catch (e) { toast(e.message, true); }
}

// -------------------------------------------------------------- held peeks
// Hold D or the 1Y button: this coin's daily candles of the last year up to the replay day. Hold Y or
// the BTC 2Y button: Bitcoin's daily candles of the last two years with three support and three
// resistance levels, whatever the session trades. Releasing returns to the timeframe and zoom from
// before. Only one peek can be held at a time.
const PEEK_BUTTON = { year: 'btn-peek', btc: 'btn-peek-btc' };

async function startPeek(kind = 'year') {
  if (!ready() || state.peek) return;
  cancelFibDraw();
  state.peek = kind;
  // A restore may still be painting: keep its saved zoom instead of reading the peek's range.
  state.peekSaved = state.peekPending || { range: chart.currentRange(), intraday: chart.intraday, intervalMs: chart.intervalMs };
  state.peekPending = null;
  chart.intraday = false;
  chart.intervalMs = intervalMs(PEEK_INTERVAL);
  state.lastSignalTime = null;
  $(PEEK_BUTTON[kind]).setAttribute('aria-pressed', 'true');
  $('chart').classList.add('peeking');
  chart.setQuiet(true);  // the line labels sit over the latest candles
  showHint(kind === 'btc' ? 'Loading Bitcoin daily candles…' : (isDay() ? 'Daily candles, last year, with the EMA 200 of the trend check. Release to return.' : 'Daily candles, last year. Release to return.'));
  renderTimeframes();
  if (peekInstances().length) applyIndicators();   // the peek's own lines need their series
  try { await refreshView(); } catch (e) { toast(e.message, true); if (state.peek === kind) endPeek(kind); return; }
  if (state.peek !== kind) return;
  if (kind === 'btc') chart.fit(chart.payload ? chart.payload.count : PEEK_BARS); else chart.fit(PEEK_BARS);
}

// `kind` limits the call to that peek (a D key-up must not end a held Y); omit it to end any peek.
async function endPeek(kind = null, { restore = true } = {}) {
  if (!state.peek || (kind && state.peek !== kind)) return;
  const was = state.peek;
  state.peek = null;
  const saved = state.peekSaved;
  state.peekSaved = null;
  $(PEEK_BUTTON[was]).setAttribute('aria-pressed', 'false');
  $('chart').classList.remove('peeking');
  chart.setQuiet(false);
  showHint(state.drawing ? FIB_HINT : '');
  state.lastSignalTime = null;
  chart.setLevels(null); chart.setNearLevels(null);
  if (was === 'year' && isDay()) applyIndicators();   // drop the peek's EMA 200 again
  if (!restore || !saved) return;
  chart.intraday = saved.intraday;
  chart.intervalMs = saved.intervalMs;
  renderTimeframes();
  if (was === 'btc') { renderFibs(); renderBuckets(); }
  state.peekPending = saved;
  try { await refreshView(); } catch (e) { toast(e.message, true); }
  if (state.peekPending === saved) state.peekPending = null;
  if (!state.peek) chart.setRange(saved.range);
}

// ------------------------------------------------------------------ trading
// Buy: check the amount, then ask for the reasons; the order goes out on Confirm (`doBuy`).
function buy() {
  if (!ready() || state.trading || isDay()) return;
  const amount = parseFloat($('buy-amount').value);
  if (!Number.isFinite(amount) || amount <= 0) { $('buy-amount').focus(); return; }
  let coins = null;
  if (state.wholeCoins) {
    coins = parseInt($('buy-coins').value, 10);
    if (!Number.isFinite(coins) || coins < 1) { roundAmountToCoins(); coins = parseInt($('buy-coins').value, 10); }
    if (!Number.isFinite(coins) || coins < 1) { $('buy-coins').focus(); return; }
  }
  const what = coins !== null ? `${coins} ${baseAsset()}` : `${money(amount)} USDT of ${baseAsset()}`;
  const tp = planValue('buy-tp'), sl = planValue('buy-sl');
  const plan = `${tp !== null ? ` · target +${tp} %` : ''}${sl !== null ? ` · stop −${sl} %` : ''}`;
  chart.setPlan(null);
  openReasons('buy', `Buy ${what} @ ${fmtPrice(state.session.price)}${plan}`, (reasons, note) => doBuy(amount, coins, reasons, note, tp, sl));
}
async function doBuy(amount, coins, reasons = [], note = null, tp = null, sl = null) {
  const s = state.session;
  if (!ready() || state.trading) return;
  state.trading = true;
  renderState();
  const t = token();
  try {
    const res = await api.buy(s.id, amount, state.interval, false, coins, state.srTol, reasons, note, tp, sl);
    if (!fresh(t)) return;
    applyState(res.state);
    toast(`Bought bucket #${res.bucket.seq}: ${qtyFmt(res.bucket.qty)} ${baseAsset()} @ ${fmtPrice(res.bucket.entry_price)}`);
    await refreshTrades();
    await captureSnapshot(res.bucket.id, 'buy');
  } catch (e) { toast(e.message, true); }
  finally { state.trading = false; renderState(); }
}

// Sell `part` (0.25, 0.5 or 1) of what is left in a bucket; a part smaller than the rest becomes its
// own History row and the bucket stays open with the remainder. Asks for the reasons first;
// the order goes out on Confirm (`doSell`).
function sell(bucketId, part = 1) {
  if (!ready() || state.trading || isDay()) return;
  const b = (state.session.buckets || []).find(x => x.id === bucketId);
  if (!b) return;
  const label = (SELL_PARTS.find(([p]) => p === part) || [part, `${(part * 100).toFixed(0)} %`])[1];
  const qty = part < 1 && state.wholeCoins ? Math.floor(b.qty * part + 1e-9) : b.qty * part;
  const what = part < 1 ? `${label} of bucket #${b.seq}` : `all of bucket #${b.seq}`;
  openReasons('sell', `Sell ${what}: ${qtyFmt(qty)} ${baseAsset()} @ ${fmtPrice(state.session.price)}`, (reasons, note) => doSell(bucketId, part, reasons, note));
}
async function doSell(bucketId, part = 1, reasons = [], note = null) {
  const s = state.session;
  if (!ready() || state.trading) return;
  state.trading = true;
  renderState();
  const t = token();
  try {
    const res = await api.sell(s.id, bucketId, state.interval, state.srTol, part, state.wholeCoins, reasons, note);
    if (!fresh(t)) return;
    if (state.selectedBucket === bucketId && res.bucket.id === bucketId) state.selectedBucket = null;
    applyState(res.state);
    const b = res.bucket;
    const what = b.part ? (b.part_pct >= 100 ? `the rest of bucket #${b.seq} (part ${b.part})` : `${b.part_pct.toFixed(0)} % of bucket #${b.seq} (part ${b.part})`) : `bucket #${b.seq}`;
    toast(`Sold ${what} @ ${fmtPrice(b.exit_price)}: ${money(b.realized_pnl, true)} (${pct(b.realized_pnl_pct)})`);
    await refreshTrades();
    await captureSnapshot(b.id, 'sell');
  } catch (e) { toast(e.message, true); }
  finally { state.trading = false; renderState(); }
}

// ------------------------------------------------------------------ reasons
// The checklist dialog: `run(reasons, note)` is called on Confirm with the ticked keys.
function openReasons(side, title, run) {
  const dlg = $('dlg-reasons');
  if (!state.reasons || dlg.open) return;
  state.pending = { side, run };
  dlg.classList.toggle('buy', side === 'buy');
  dlg.classList.toggle('sell', side === 'sell');
  $('reasons-title').textContent = title;
  $('reasons-version').textContent = `Checklist ${state.reasons.version}`;
  const list = $('reasons-list');
  list.innerHTML = '';
  for (const r of state.reasons[side]) {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = r.key;
    cb.name = 'reason';
    label.append(cb, document.createTextNode(r.label));
    list.appendChild(label);
  }
  $('reasons-note').value = '';
  dlg.showModal();
  const first = list.querySelector('input');
  if (first) first.focus();
}
function closeReasons() {
  state.pending = null;
  const dlg = $('dlg-reasons');
  if (dlg.open) dlg.close();
}
function confirmReasons() {
  const p = state.pending;
  if (!p) return;
  const reasons = [...$('reasons-list').querySelectorAll('input:checked')].map(cb => cb.value);
  const note = $('reasons-note').value.trim() || null;
  closeReasons();
  p.run(reasons, note);
}
function initReasons() {
  const dlg = $('dlg-reasons');
  $('form-reasons').addEventListener('submit', (e) => { e.preventDefault(); confirmReasons(); });
  $('btn-cancel-reasons').addEventListener('click', closeReasons);
  $('btn-close-reasons').addEventListener('click', closeReasons);
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); closeReasons(); });  // Esc
  dlg.addEventListener('click', (e) => { if (e.target === dlg) closeReasons(); });
}
// Label of a checklist key, whichever list it is from ("Other" reads the same in both).
function reasonLabel(key) {
  if (!state.reasons) return key;
  const hit = state.reasons.buy.find(r => r.key === key) || state.reasons.sell.find(r => r.key === key);
  return hit ? hit.label : key;
}
function reasonList(keys) { return Array.isArray(keys) && keys.length ? keys.map(reasonLabel).join(', ') : ''; }

// The Reasons statistics: over all sessions, or one coin; refreshed when the tab is shown and
// after every trade while it is shown.
async function loadStats() {
  try {
    const st = await api.reasonStats(state.statsSymbol);
    state.stats = st;
    renderStats();
  } catch (e) { toast(`Statistics not loaded: ${e.message}`, true); }
}
function renderStats() {
  const st = state.stats;
  if (!st) return;
  const sel = $('stats-symbol');
  const keep = state.statsSymbol;
  sel.innerHTML = '';
  for (const [value, text] of [['', 'All coins'], ...st.symbols.map(s => [s, s.replace(/USDT$/, '')])]) {
    const o = document.createElement('option');
    o.value = value; o.textContent = text;
    sel.appendChild(o);
  }
  sel.value = st.symbols.includes(keep) ? keep : '';
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const parts = [`${plural(st.trades, 'closed trade')}`, plural(st.sells, 'sell')];
  if (st.trades_avg_pct !== null && st.trades_avg_pct !== undefined) parts.push(`average ${pct(st.trades_avg_pct)} per trade`);
  if (st.unlisted) parts.push(`${plural(st.unlisted, 'older row')} without a checklist left out`);
  parts.push(`groups under ${st.min_group} are too few to judge`);
  $('stats-summary').textContent = parts.join(' · ');
  const empty = st.trades === 0 && st.sells === 0;
  $('stats-empty').classList.toggle('hidden', !empty);
  $('stats-buy-table').classList.toggle('hidden', empty);
  $('stats-sell-table').classList.toggle('hidden', empty);
  const group = (tr, g, what) => {
    const n = document.createElement('td');
    n.className = 'num count';
    n.textContent = String(g.count);
    n.title = `${plural(g.count, what)} in this group`;
    const a = document.createElement('td');
    a.className = `num avg ${g.count ? pnlClass(g.avg_pct) : ''}`;
    a.textContent = g.count ? pct(g.avg_pct) : '–';
    a.title = g.count ? `Average realized P&L of ${plural(g.count, what)}` : 'Nothing in this group yet';
    if (g.few) {
      const tag = document.createElement('span');
      tag.className = 'tag few';
      tag.textContent = 'too few to judge';
      tag.title = `Fewer than ${st.min_group} ${what}s in this group`;
      a.appendChild(tag);
    }
    tr.append(n, a);
  };
  const buy = $('stats-buy');
  buy.innerHTML = '';
  for (const r of st.buy) {
    const tr = document.createElement('tr');
    tr.dataset.key = r.key;
    const td = document.createElement('td'); td.textContent = r.label; tr.appendChild(td);
    group(tr, r.ticked, 'trade');
    group(tr, r.unticked, 'trade');
    buy.appendChild(tr);
  }
  const sell = $('stats-sell');
  sell.innerHTML = '';
  for (const r of st.sell) {
    const tr = document.createElement('tr');
    tr.dataset.key = r.key;
    const td = document.createElement('td'); td.textContent = r.label; tr.appendChild(td);
    group(tr, r.ticked, 'sell');
    sell.appendChild(tr);
  }
}
function initStats() {
  $('stats-symbol').addEventListener('change', () => { state.statsSymbol = $('stats-symbol').value; loadStats(); });
}
function statsShown() { return !$('panel-stats').classList.contains('hidden'); }

// ------------------------------------------------------------- performance
// The Performance tab: strategy-tester numbers, the equity curve with its drawdown, a bar per
// trade, the win/loss donut and the monthly returns, for this session or all sessions together.
let perfCharts = null;
function perfShown() { return !$('panel-perf').classList.contains('hidden'); }
async function loadPerf() {
  if (state.perfTimer) { clearTimeout(state.perfTimer); state.perfTimer = null; }
  if (state.perfScope === 'session' && !ready()) { state.perf = null; renderPerf(); return; }
  const sid = ready() ? state.session.id : null;
  try {
    const rep = state.perfScope === 'all' ? await api.performanceAll() : await api.performance(sid);
    if (state.perfScope === 'session' && (!ready() || state.session.id !== sid)) return;
    state.perf = rep;
    renderPerf();
  } catch (e) { toast(`Performance not loaded: ${e.message}`, true); }
}
// Refresh soon after trades or steps while the tab is open, at most a couple of times a second.
function schedulePerf() {
  if (!perfShown() || state.perfTimer) return;
  state.perfTimer = setTimeout(() => { state.perfTimer = null; loadPerf(); }, 600);
}
function setPerfScope(scope) {
  state.perfScope = scope;
  for (const chip of $('perf-tools').querySelectorAll('.chip')) {
    chip.classList.toggle('on', chip.dataset.scope === scope);
    chip.setAttribute('aria-pressed', String(chip.dataset.scope === scope));
  }
  loadPerf();
}
function renderPerf() {
  const r = state.perf;
  const tiles = $('perf-tiles');
  tiles.innerHTML = '';
  $('perf-empty').classList.toggle('hidden', !!r);
  $('perf-grid').classList.toggle('hidden', !r);
  $('perf-summary').textContent = '';
  if (!r) return;
  const sm = r.summary, all = r.scope === 'all';
  const n = (v, w) => `${v} ${w}${v === 1 ? '' : 's'}`;
  $('perf-summary').textContent = all
    ? `${n(r.sessions, 'session')} · start balances ${money(r.start_balance)} · a part sold counts as its own trade · the curve adds the realized P&L trade by trade`
    : `${baseAsset()} · ${isoShort(new Date(r.start_ms).toISOString().slice(0, 16))} to ${isoShort(state.session.cursor_iso)} · a part sold counts as its own trade`;
  const tile = (k, v, cls, s, title) => {
    const el = document.createElement('div');
    el.className = 'tile';
    el.dataset.key = k.toLowerCase().replace(/[^a-z]+/g, '-');
    const a = document.createElement('div'); a.className = 'k'; a.textContent = k;
    const b = document.createElement('div'); b.className = `v ${cls || ''}`; b.textContent = v;
    const c = document.createElement('div'); c.className = 's'; c.textContent = s || ''; if (s) c.title = s;  // the full text when it is cut short
    if (title) el.title = title;
    el.append(a, b, c);
    tiles.appendChild(el);
  };
  const pf = sm.profit_factor === 'inf' ? '∞' : sm.profit_factor === null || sm.profit_factor === undefined ? '–' : sm.profit_factor.toFixed(2);
  const who = (t) => t ? `${t.label}${all && t.symbol ? ` ${t.symbol.replace(/USDT$/, '')}` : ''} · ${isoShort(t.closed_iso).slice(0, 10)}` : '';
  tile('Net profit', money(sm.net_pnl, true), pnlClass(sm.net_pnl), sm.net_pnl_pct === null ? '' : `${pct(sm.net_pnl_pct)} of the start balance`, 'Realized P&L of all closed trades, fees included');
  tile('Trades', String(sm.trades), '', `${sm.winners} won · ${sm.losers} lost${sm.flat ? ` · ${sm.flat} flat` : ''}${sm.auto_sells ? ` · ${sm.auto_sells} automatic` : ''}`, 'Closed History rows; a part sold counts on its own');
  tile('Win rate', sm.win_rate_pct === null ? '–' : `${sm.win_rate_pct.toFixed(1)}%`, sm.win_rate_pct === null ? '' : sm.win_rate_pct >= 50 ? 'up' : 'down', sm.winners || sm.losers ? `streaks: ${sm.win_streak} wins · ${sm.loss_streak} losses in a row` : '', 'Winners divided by all closed trades');
  tile('Profit factor', pf, sm.profit_factor === 'inf' || sm.profit_factor > 1 ? 'up' : sm.profit_factor === null ? '' : 'down', `won ${money(sm.gross_profit)} · lost ${money(sm.gross_loss)}`, 'Gross profit divided by gross loss; above 1 means the winners paid for the losers');
  tile('Max drawdown', sm.max_drawdown_pct === null ? '–' : `−${sm.max_drawdown_pct.toFixed(2)}%`, sm.max_drawdown_pct > 0 ? 'down' : '', sm.max_drawdown ? `${money(-sm.max_drawdown, true)} · peak ${isoShort(sm.peak.iso || '').slice(0, 10)} → low ${isoShort(sm.trough.iso || '').slice(0, 10)}` : 'no fall from a peak yet', all ? 'Deepest fall of the realized P&L from its peak, in percent of the start balances' : 'Deepest fall of the account value from its highest point, open buckets at market');
  tile('Average trade', sm.avg_pct === null ? '–' : pct(sm.avg_pct), pnlClass(sm.avg_pct), sm.avg_pnl === null ? '' : `${money(sm.avg_pnl, true)} · winners ${pctOrDash(sm.avg_win_pct)} · losers ${pctOrDash(sm.avg_loss_pct)}`, 'Average realized P&L per closed trade, percent of its cost');
  tile('Best trade', sm.best ? pct(sm.best.pct) : '–', sm.best ? 'up' : '', sm.best ? `${who(sm.best)} · ${money(sm.best.pnl, true)}` : '');
  tile('Worst trade', sm.worst ? pct(sm.worst.pct) : '–', sm.worst ? pnlClass(sm.worst.pct) : '', sm.worst ? `${who(sm.worst)} · ${money(sm.worst.pnl, true)}` : '');
  tile('Average hold', sm.avg_held_ms === null ? '–' : ageText(sm.avg_held_ms), '', sm.max_held_ms === null ? '' : `longest ${ageText(sm.max_held_ms)}`, 'Time between buy and sell, measured on the replay clock');
  tile('Open buckets', String(sm.open_count), '', sm.open_count ? `${money(sm.open_pnl, true)} at market` : 'nothing open', 'Buckets still held, valued at the current price');
  tile(all ? 'Equity, all sessions' : 'Equity', money(sm.equity), pnlClass(sm.equity_pct), sm.equity_pct === null ? '' : `${pct(sm.equity_pct)} vs start · high ${money(sm.equity_high)}${all ? '' : ` · low ${money(sm.equity_low)}`}`, 'Cash plus open buckets at market');
  tile('Fees paid', money(sm.fees), '', '', 'Buy and sell fees of every trade');
  $('perf-equity-title').textContent = all ? 'Realized P&L, trade by trade (drawdown shaded)' : 'Equity curve, every candle, open buckets at market (drawdown shaded)';
  if (perfCharts) perfCharts.render(r);
  renderMonths($('perf-months'), r.months, all);
}
function initPerf() {
  perfCharts = new PerfCharts({ equity: $('perf-equity'), bars: $('perf-bars'), donut: $('perf-donut') });
  for (const chip of $('perf-tools').querySelectorAll('.chip')) {
    chip.setAttribute('aria-pressed', String(chip.classList.contains('on')));
    chip.addEventListener('click', () => setPerfScope(chip.dataset.scope));
  }
  window.addEventListener('resize', () => { if (perfShown() && state.perf && perfCharts) perfCharts.render(state.perf); });
}

function preset(p) {
  if (!ready()) return;
  const amount = Math.floor(state.session.cash * p) / 100;
  $('buy-amount').value = amount > 0 ? amount.toFixed(2) : '';
  if (state.wholeCoins) roundAmountToCoins(); else updateEstimate();
}

// --------------------------------------------------------------- indicators
// Called by the panel after the user adds or removes an instance. The change is sent to the
// server; if it is rejected the panel rolls back to `previous` and nothing is persisted.
async function onIndicatorsChange(instances, previous) {
  bump();
  applyIndicators();
  if (instances === previous) { state.lastGoodIndicators = instances; return; } // initial load: loadSession fetches the view
  const t = token();
  try {
    await refreshView();
    if (!fresh(t)) return;
    state.lastGoodIndicators = indicators.instances;
    indicators.save();
    indicators.showError('');
  } catch (e) {
    if (!fresh(t)) return;
    indicators.setInstances(state.lastGoodIndicators);
    indicators.showError(e.message);
    bump();
    applyIndicators();
    refreshView().catch(() => {});
  }
}

// ----------------------------------------------------------------- strategy
function loadStrategyPref() {
  let saved = null;
  try { saved = JSON.parse(getPref('strategy', 'null')); } catch { saved = null; }
  if (saved && state.strategies[saved.key]) setStrategy(saved.key, saved.params, { silent: true });
  else setStrategy('', null, { silent: true });
}

function saveStrategyPref() { setPref('strategy', JSON.stringify(state.strategy)); }

function showStrategyError(msg) {
  const el = $('strategy-error');
  el.textContent = msg || '';
  el.classList.toggle('hidden', !msg);
}

function renderStrategyControls() {
  const spec = state.strategy && state.strategies[state.strategy.key];
  $('strategy-key').value = spec ? state.strategy.key : '';
  $('btn-strategy-params').disabled = !spec;
  if (spec) buildParamInputs($('strategy-params'), spec.params, state.strategy.params);
  else {
    $('strategy-params').innerHTML = '';
    $('strategy-params').classList.add('hidden');
    $('btn-strategy-params').setAttribute('aria-expanded', 'false');
  }
}

async function setStrategy(key, params, { silent = false } = {}) {
  const spec = state.strategies[key];
  if (!spec) state.strategy = null;
  else {
    const defaults = Object.fromEntries(spec.params.map(p => [p.name, p.default]));
    state.strategy = { key, params: { ...defaults, ...(params || {}) } };
  }
  state.lastSignalTime = null;
  renderStrategyControls();
  showStrategyError('');
  if (silent) { state.lastGoodStrategy = state.strategy; return; }
  await commitStrategy();
}

async function onStrategyParamsChange() {
  if (!state.strategy) return;
  const { params, error, message } = readParamInputs($('strategy-params'));
  if (error) { showStrategyError(message); return; }
  state.strategy = { key: state.strategy.key, params };
  state.lastSignalTime = null;
  showStrategyError('');
  await commitStrategy();
}

// Apply the current strategy to the chart and fetch. Commits run one after another; if the
// server rejects the strategy, roll back to the last one it accepted.
function commitStrategy() {
  const run = async () => {
    bump();
    applyIndicators();
    const t = token();
    try {
      await refreshView();
      if (!fresh(t)) return;
      state.lastGoodStrategy = state.strategy;
      saveStrategyPref();
    } catch (e) {
      if (!fresh(t)) return;
      showStrategyError(e.message);
      state.strategy = state.lastGoodStrategy;
      renderStrategyControls();
      bump();
      applyIndicators();
      refreshView().catch(() => {});
    }
  };
  state.strategyQueue = state.strategyQueue.then(run, run);
  return state.strategyQueue;
}

// ------------------------------------------------------------------ dialog
function openSessionDialog() {
  const dlg = $('dlg-session');
  $('form-error').classList.add('hidden');
  const sym = $('f-symbol');
  sym.innerHTML = '';
  for (const c of state.coins) {
    const o = document.createElement('option');
    o.value = c.symbol; o.textContent = `${c.base} · ${c.name}`;
    sym.appendChild(o);
  }
  const base = $('f-base');
  base.innerHTML = '';
  for (const iv of state.intervals) {
    const o = document.createElement('option');
    o.value = iv.name; o.textContent = iv.name; o.selected = iv.name === '1h';
    base.appendChild(o);
  }
  if (!$('f-start').value) {
    const d = new Date(); d.setUTCMonth(d.getUTCMonth() - 3);
    $('f-start').value = d.toISOString().slice(0, 10);
  }
  renderSessionMode();
  applySessionLength();
  dlg.showModal();
}
// The Length choice fills the end date: 1 / 2 / 5 years after the start (capped at today),
// "Until now" clears it (the replay then runs to the latest closed candle). Typing an end
// date yourself switches the choice to "Custom end date".
function applySessionLength() {
  const choice = $('f-length').value;
  const end = $('f-end');
  if (choice === 'custom') { $('f-end-note').textContent = '(optional)'; return; }
  if (choice === 'now') { end.value = ''; $('f-end-note').textContent = '(until now)'; return; }
  const start = $('f-start').value;
  if (!start) return;
  const d = new Date(start + 'T00:00:00Z');
  d.setUTCFullYear(d.getUTCFullYear() + parseInt(choice, 10));
  const today = new Date().toISOString().slice(0, 10);
  const iso = d.toISOString().slice(0, 10);
  end.value = iso > today ? today : iso;
  $('f-end-note').textContent = iso > today ? '(capped at today)' : `(${choice} year${choice === '1' ? '' : 's'})`;
}
// Day trading fixes the base timeframe (15m) and the fee (0.1 %): those fields disappear.
function renderSessionMode() {
  const dayMode = $('f-mode').value === 'day';
  $('f-base-wrap').classList.toggle('hidden', dayMode);
  $('f-fee-wrap').classList.toggle('hidden', dayMode);
  $('f-note-swing').classList.toggle('hidden', dayMode);
  $('f-note-day').classList.toggle('hidden', !dayMode);
  $('f-name').placeholder = dayMode ? 'e.g. BTC day trading May' : 'e.g. BTC summer 2025';
  // each mode's default balance, unless the user typed another one
  const bal = $('f-balance');
  if (bal.value === (dayMode ? SWING_START_BALANCE : DAY_START_BALANCE)) bal.value = dayMode ? DAY_START_BALANCE : SWING_START_BALANCE;
}

async function submitSessionDialog(e) {
  e.preventDefault();
  const f = e.target.elements;
  const body = {
    symbol: f.symbol.value,
    base_interval: f.base_interval.value,
    start: f.start.value,
    end: f.end.value || null,
    start_balance: parseFloat(f.start_balance.value),
    fee_rate: parseFloat(f.fee_pct.value) / 100,
    name: f.name.value.trim() || null,
  };
  const btn = $('btn-create-session');
  btn.disabled = true;
  try {
    const s = await (f.mode.value === 'day'
      ? api.createDaySession({ symbol: body.symbol, start: body.start, end: body.end, start_balance: body.start_balance, name: body.name })
      : api.createSession(body));
    $('dlg-session').close();
    await refreshSessions();
    await loadSession(s.id);
  } catch (err) {
    const el = $('form-error');
    el.textContent = err.message;
    el.classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
}

async function deleteSession() {
  const s = state.session;
  if (!s) return;
  if (!confirm(`Delete session "${s.name}" and all its trades?`)) return;
  pause();
  try {
    await api.deleteSession(s.id);
    await refreshSessions();
    await loadSession(state.sessions.length ? state.sessions[0].id : null);
  } catch (e) { toast(e.message, true); }
}

// -------------------------------------------------------------------- tabs
// `open` forces the bottom area open (a click on a tab) or closed; without it the area keeps its state.
function selectTab(id, { open } = {}) {
  for (const tab of document.querySelectorAll('.tabs .tab')) {
    const on = tab.id === id;
    tab.setAttribute('aria-selected', String(on));
    $(tab.getAttribute('aria-controls')).classList.toggle('hidden', !on);
  }
  if (open !== undefined) state.bottomOpen = open;
  if (id === 'tab-stats') loadStats();
  if (id === 'tab-day') day.renderTrades();
  if (id === 'tab-days') day.renderDays();
  if (id === 'tab-auto') day.renderAuto();
  if (id === 'tab-cal') day.renderCal();
  if (id === 'tab-cond') day.loadConditions();
  renderBottom();
  if (id === 'tab-perf') loadPerf();
}

function activeTab() {
  const t = document.querySelector('.tabs .tab[aria-selected="true"]');
  return t ? t.id : null;
}

// The bottom area (the tabs with the trades, days, buckets ...): open at its usual height, or
// collapsed to the tab bar alone. Day-trading mode starts collapsed; a click on a tab opens it,
// a click on the open tab or on the arrow button collapses it again.
function renderBottom() {
  const open = state.bottomOpen;
  const center = document.querySelector('.center');
  center.classList.toggle('collapsed', !open);
  center.classList.toggle('tall', open && ['tab-perf', 'tab-day', 'tab-auto'].includes(activeTab()));  // room for the diagrams and the trade tables
  const b = $('btn-bottom');
  b.textContent = open ? '\u25BE' : '\u25B4';
  b.setAttribute('aria-expanded', String(open));
  b.title = open ? 'Make the trades window small' : 'Make the trades window big';
}
function setBottom(open) { state.bottomOpen = open; renderBottom(); }

// Minimizable side cards and sub-sections: every element with `data-fold` folds to its head when
// its title button is clicked. The choice is remembered per mode (not per session); without one,
// day trading starts with the cards it hardly uses minimized. A minimized card shows a one-line
// summary next to its title, read from the values the card itself shows.
const FOLDED_BY_DEFAULT = { day: ['extremes-card', 'account-card'], swing: [] };
const foldKey = (el) => `replay.ui.fold.${isDay() ? 'day' : 'swing'}.${el.id}`;
const txt = (id) => ($(id).textContent || '').trim();
const CARD_SUMS = {
  'day-card': () => [txt('day-date'), txt('day-time'), txt('day-phase'), $('day-trade').classList.contains('hidden') ? '' : txt('dt-pnl')],
  'day-signal-card': () => [$('day-strategy').selectedOptions[0] ? $('day-strategy').selectedOptions[0].textContent : ''],
  'strategy-card': () => [$('strategy-key').selectedOptions[0] ? $('strategy-key').selectedOptions[0].textContent : ''],
  'gauge-card': () => [`${txt('gauge-rsi-label')} ${txt('gauge-rsi')}`, `vs EMA ${txt('gauge-ema-pct')}`],
  'account-card': () => [`Equity ${txt('acc-equity')}`],
};
function renderCardSums() {
  for (const [id, parts] of Object.entries(CARD_SUMS)) {
    const card = $(id);
    if (!card.classList.contains('min')) continue;
    card.querySelector('.card-sum').textContent = parts().filter(p => p && p !== '–').join(' · ');
  }
}
function setFold(el, min, remember = true) {
  el.classList.toggle('min', min);
  el.querySelector('.fold').setAttribute('aria-expanded', String(!min));
  if (remember) { try { localStorage.setItem(foldKey(el), min ? '1' : '0'); } catch { /* no storage */ } }
  renderCardSums();
}
// After a session switch (the mode may have changed): every fold as remembered for this mode.
function renderFolds() {
  const defaults = FOLDED_BY_DEFAULT[isDay() ? 'day' : 'swing'];
  for (const el of document.querySelectorAll('[data-fold]')) {
    let v = null;
    try { v = localStorage.getItem(foldKey(el)); } catch { /* no storage */ }
    setFold(el, v === null ? defaults.includes(el.id) : v === '1', false);
  }
}
function initFolds() {
  for (const el of document.querySelectorAll('[data-fold]')) {
    el.querySelector('.fold').addEventListener('click', () => setFold(el, !el.classList.contains('min')));
  }
  renderFolds();
}

// -------------------------------------------------------------------- init
// ------------------------------------------------------------------ fibs
function openFibPopover(open) {
  $('fib-popover').classList.toggle('hidden', !open);
  $('btn-fib').setAttribute('aria-expanded', String(open));
}
function showHint(text) {
  const el = $('draw-hint');
  el.textContent = text || '';
  el.classList.toggle('hidden', !text);
}
function startFibDraw() {
  if (!ready()) return;
  state.drawing = true;
  state.fibDraft = null;
  chart.setFibDraft(null);
  openFibPopover(false);
  $('chart').classList.add('drawing');
  showHint(FIB_HINT);
}
function cancelFibDraw() {
  state.drawing = false;
  state.fibDraft = null;
  chart.setFibDraft(null);
  $('chart').classList.remove('drawing');
  showHint(null);
}
async function onChartClick(param) {
  if (!ready()) return;
  if (await day.onChartClick(param)) return;
  if (!state.drawing) {
    const hit = chart.fibAt(param);
    if (hit || state.selectedFib !== null) selectFib(hit ? hit.id : null);
    return;
  }
  const snap = chart.snapClick(param);
  if (!snap) return;
  if (!state.fibDraft) {
    state.fibDraft = snap;
    chart.setFibDraft(snap);
    showHint(`Fib 2 of 2: ${snap.kind} ${fmtPrice(snap.price)} taken. Click the swing end.`);
    return;
  }
  const a = state.fibDraft;
  if (a.price === snap.price) { toast('The second anchor needs a different price', true); return; }
  const s = state.session;
  const t = token();
  try {
    const fib = await api.addFib(s.id, { interval: state.interval, t1_ms: a.t_ms, p1: a.price, t2_ms: snap.t_ms, p2: snap.price });
    if (!fresh(t)) return;
    cancelFibDraw();
    state.fibs.push(fib);
    state.selectedFib = fib.id;
    renderFibs();
    toast(`Fib drawn: ${fmtPrice(fib.p1)} → ${fmtPrice(fib.p2)}`);
  } catch (e) {
    if (fresh(t)) { toast(e.message, true); cancelFibDraw(); }
  }
}
function selectFib(id) {
  state.selectedFib = id;
  renderFibs();
}
async function deleteFib(id) {
  const s = state.session;
  if (!ready() || id === null || id === undefined) return;
  const t = token();
  try {
    await api.deleteFib(s.id, id);
    if (!fresh(t)) return;
    state.fibs = state.fibs.filter(f => f.id !== id);
    if (state.selectedFib === id) state.selectedFib = null;
    renderFibs();
  } catch (e) { if (fresh(t)) toast(e.message, true); }
}
async function refreshFibs() {
  if (!ready()) return;
  const t = token();
  const list = await api.fibs(state.session.id);
  if (!fresh(t)) return;
  state.fibs = list;
  if (!list.some(f => f.id === state.selectedFib)) state.selectedFib = null;
  renderFibs();
}
function renderFibs() {
  chart.setFibs(state.fibs);
  chart.selectFib(state.selectedFib);
  const ul = $('fib-list');
  ul.innerHTML = '';
  for (const f of state.fibs) {
    const li = document.createElement('li');
    li.className = `fib${f.id === state.selectedFib ? ' selected' : ''}`;
    li.dataset.id = f.id;
    const sw = document.createElement('span'); sw.className = 'sw';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = `${f.direction === 'up' ? '↑' : '↓'} ${fmtPrice(f.p1)} → ${fmtPrice(f.p2)} · ${f.interval}`;
    name.title = `${isoShort(f.t1_iso)} → ${isoShort(f.t2_iso)}`;
    const del = document.createElement('button');
    del.type = 'button';
    del.textContent = '×';
    del.setAttribute('aria-label', 'Delete retracement');
    del.addEventListener('click', (e) => { e.stopPropagation(); deleteFib(f.id); });
    li.append(sw, name, del);
    li.addEventListener('click', () => selectFib(f.id === state.selectedFib ? null : f.id));
    ul.appendChild(li);
  }
  $('fib-count').textContent = String(state.fibs.length);
  $('fib-empty').classList.toggle('hidden', state.fibs.length > 0);
}
function clampLookback(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? Math.min(2000, Math.max(2, n)) : 100;
}
function loadFibPrefs() {
  state.autoFib = { on: getPref('autofib', '0') === '1', lookback: clampLookback(getPref('autofib.lookback', '100')) };
  $('fib-auto').checked = state.autoFib.on;
  $('fib-lookback').value = String(state.autoFib.lookback);
}
function onAutoFibChange() {
  state.autoFib = { on: $('fib-auto').checked, lookback: clampLookback($('fib-lookback').value) };
  $('fib-lookback').value = String(state.autoFib.lookback);
  setPref('autofib', state.autoFib.on ? '1' : '0');
  setPref('autofib.lookback', String(state.autoFib.lookback));
  refreshView();
}

// ------------------------------------------------------------------- fvg
function openFvgPopover(open) {
  $('fvg-popover').classList.toggle('hidden', !open);
  $('btn-fvg').setAttribute('aria-expanded', String(open));
}
function clampMinPct(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? Math.min(10, Math.max(0, n)) : 0.1;
}
function loadFvgPrefs() {
  state.fvg = {
    on: getPref('fvg', '0') === '1',
    minPct: clampMinPct(getPref('fvg.min', '0.1')),
    hideFilled: getPref('fvg.hidefilled', '0') === '1',
  };
  $('fvg-on').checked = state.fvg.on;
  $('fvg-min').value = String(state.fvg.minPct);
  $('fvg-hide-filled').checked = state.fvg.hideFilled;
  $('btn-fvg').setAttribute('aria-pressed', String(state.fvg.on));
}
function onFvgChange() {
  state.fvg = { on: $('fvg-on').checked, minPct: clampMinPct($('fvg-min').value), hideFilled: $('fvg-hide-filled').checked };
  $('fvg-min').value = String(state.fvg.minPct);
  $('btn-fvg').setAttribute('aria-pressed', String(state.fvg.on));
  setPref('fvg', state.fvg.on ? '1' : '0');
  setPref('fvg.min', String(state.fvg.minPct));
  setPref('fvg.hidefilled', state.fvg.hideFilled ? '1' : '0');
  if (state.fvg.on) refreshView(); else renderFvg(null);
}
function toggleFvg() {
  if (!ready()) return;
  $('fvg-on').checked = !state.fvg.on;
  onFvgChange();
}
function renderFvg(gaps) {
  const list = state.fvg.on && Array.isArray(gaps) ? gaps : [];
  chart.setFvg(list, state.fvg.hideFilled);
  const open = list.filter(g => !g.filled).length;
  $('fvg-count').textContent = String(open);
  $('fvg-summary').textContent = state.fvg.on
    ? `${list.length} gap${list.length === 1 ? '' : 's'} on screen, ${open} still open.`
    : 'Gaps are hidden.';
}

// Cluster tolerance of the Bitcoin support/resistance levels (Indicators panel).
function loadSrTolPref() {
  const v = parseFloat(getPref('srtol', String(SR_TOL_DEFAULT)));
  state.srTol = Number.isFinite(v) && v > 0 && v <= 10 ? v : SR_TOL_DEFAULT;
  $('sr-tol').value = String(state.srTol);
}
function setSrTol(v) {
  if (!Number.isFinite(v) || v <= 0 || v > 10) { $('sr-tol').value = String(state.srTol); return; }
  state.srTol = v;
  if (ready() && !state.peek) refreshView();  // the next support/resistance readings use it too
  setPref('srtol', String(v));
  if (state.peek === 'btc') refreshView();
}

// L: tubes at the next support / resistance (the Readings rows) and the 6-month high / low lines, on by default.
function loadNearLevelsPref() {
  state.nearLevels = getPref('nearlevels', '1') === '1';
  $('near-levels').checked = state.nearLevels;
}
function setNearLevels(on) {
  state.nearLevels = !!on;
  $('near-levels').checked = state.nearLevels;
  setPref('nearlevels', state.nearLevels ? '1' : '0');
  const p = chart.payload;
  chart.setNearLevels(state.nearLevels && !state.peek && p ? p.gauges : null);
}

// S: daily EMA 200 overlay on whatever timeframe is on screen.
function loadDailyEmaPref() {
  state.dailyEma = getPref('dailyema', '0') === '1';
  $('daily-ema').checked = state.dailyEma;
}
function setDailyEma(on) {
  if (!ready()) return;
  state.dailyEma = !!on;
  $('daily-ema').checked = state.dailyEma;
  setPref('dailyema', state.dailyEma ? '1' : '0');
  if (state.dailyEma) refreshView(); else chart.setDailyEma(null);
}

// O: Bollinger Bands 20/2 on the chart, through the normal indicator list (so they can be edited there).
function toggleBollinger() {
  if (!ready()) return;
  const added = indicators.toggleKey('bbands', { period: 20, mult: 2 });
  toast(added ? 'Bollinger Bands 20/2 added' : 'Bollinger Bands removed');
}

// ---------------------------------------------------------------- journal
// Every buy and sell gets a chart picture plus the readings of that moment (server side); the
// picture and the UI-only readings (strategy, lights, gaps) are added here right after the trade.
async function captureSnapshot(bucketId, side) {
  const s = state.session;
  if (!ready()) return;
  let image = null;
  try { image = chart.chart.takeScreenshot().toDataURL('image/png'); } catch { image = null; }
  const extra = {
    strategy: state.strategy ? state.strategy.key : null,
    strategy_name: state.strategy && state.strategies[state.strategy.key] ? state.strategies[state.strategy.key].name : null,
    light_enter: $('lamp-enter').classList.contains('on'),
    light_exit: $('lamp-exit').classList.contains('on'),
    open_fvgs: state.fvg.on ? chart.fvg.gaps.filter(g => !g.filled).length : null,
    fibs: state.fibs.length,
    indicators: indicators.requestList().map(i => `${i.key}(${Object.values(i.params).join('/')})`).join(' '),
  };
  const t = token();
  if (image) {
    try { await api.snapshotImage(s.id, bucketId, side, { image, extra }); }
    catch (e) { if (fresh(t)) toast(`Journal picture not saved: ${e.message}`, true); }
  } else {
    toast('Chart picture failed; journal row kept without it', true);
  }
  if (fresh(t)) await refreshJournal();
  if (statsShown()) loadStats();
}
async function refreshJournal() {
  if (!ready()) return;
  const t = token();
  const rows = await api.snapshots(state.session.id);
  if (!fresh(t)) return;
  state.journal = rows.map(r => ({
    ...r.readings, id: r.id, bucket_id: r.bucket_id, side: r.side, time_ms: r.time_ms, time_iso: r.time_iso,
    interval: r.interval, has_image: r.has_image,
    image_url: r.has_image ? `${api.snapshotImageUrl(state.session.id, r.id)}?v=${r.time_ms}` : null,
  }));
  renderJournal();
}
function setJournalFilter(side) {
  state.journalFilter = side;
  for (const chip of $('journal-tools').querySelectorAll('.chip')) {
    chip.classList.toggle('on', chip.dataset.side === side);
    chip.setAttribute('aria-pressed', String(chip.dataset.side === side));
  }
  renderJournal();
}
const num = (v, d = 2, suffix = '') => (v === null || v === undefined ? '–' : `${Number(v).toFixed(d)}${suffix}`);
function renderJournal() {
  const list = $('journal-list');
  list.innerHTML = '';
  const all = state.journal;
  const shown = sortedRows(all.filter(r => state.journalFilter === 'all' || r.side === state.journalFilter), 'journal');
  for (const r of shown) {
    const tr = document.createElement('tr');
    tr.className = `bucket journal ${r.side}`;
    tr.dataset.id = r.id;
    const cls = r.side === 'sell' ? pnlClass(r.realized_pnl) : '';
    const pic = document.createElement('td');
    if (r.has_image) {
      const img = document.createElement('img');
      img.className = 'thumb';
      img.src = r.image_url;
      img.alt = `${r.side} ${bucketLabel(r.bucket_seq, r.part)} chart`;
      img.addEventListener('click', (e) => { e.stopPropagation(); openSnapshot(r); });
      pic.appendChild(img);
    } else { pic.textContent = '–'; pic.className = 'muted'; }
    tr.appendChild(pic);
    const cells = [
      [`side ${r.side}`, r.side === 'buy' ? 'BUY' : 'SELL'],
      ['seq', bucketLabel(r.bucket_seq, r.part), r.part ? `Part ${r.part} of bucket #${r.bucket_seq}` : ''],
      ['date', isoShort(r.time_iso)],
      ['', r.interval],
      ['num', fmtPrice(r.price)],
      ['num', money(r.quote)],
      [`num rsi ${rsiClass(r.rsi)}`, rsiText(r.rsi)],
      ['num', num(r.bb_pctb)],
      [`num ${pnlClass(r.ema200_pct)}`, r.ema200_pct === null || r.ema200_pct === undefined ? '–' : pct(r.ema200_pct)],
      [`num ${pnlClass(r.ath_pct)}`, pctOrDash(r.ath_pct), `1 year ${pctOrDash(r.high_1y_pct)} · 6 months ${pctOrDash(r.high_6m_pct)} · 3 months ${pctOrDash(r.high_3m_pct)}`],
      [`num ${pnlClass(r.atl_pct)}`, pctOrDash(r.atl_pct), `1 year ${pctOrDash(r.low_1y_pct)} · 6 months ${pctOrDash(r.low_6m_pct)} · 3 months ${pctOrDash(r.low_3m_pct)}`],
      [`num ${pnlClass(r.vs_last_buy_pct)}`, r.vs_last_buy_pct === null || r.vs_last_buy_pct === undefined ? '–' : pct(r.vs_last_buy_pct)],
      [`num ${pnlClass(r.vs_last_sell_pct)}`, r.vs_last_sell_pct === null || r.vs_last_sell_pct === undefined ? '–' : pct(r.vs_last_sell_pct)],
      ['', r.strategy_name || r.strategy || '–'],
      ['', r.light_enter ? 'ENTER' : r.light_exit ? 'EXIT' : (r.light_enter === undefined ? '–' : 'off')],
      ['reasons', r.trigger ? `Auto: ${r.trigger} reached` : reasonList(r.reasons) || (r.checklist ? '–' : ''), `${reasonList(r.reasons) || (r.checklist ? 'No reason ticked' : 'Logged before the checklist existed')}${r.reason_note ? ` — ${r.reason_note}` : ''}`],
      [`num pnl ${cls}`, r.side === 'sell' ? money(r.realized_pnl, true) : ''],
      [`num pnl ${cls}`, r.side === 'sell' ? pct(r.realized_pnl_pct) : ''],
      ['num', r.side === 'sell' ? ageText(r.held_ms) : ''],
    ];
    for (const [c, text, title] of cells) {
      const td = document.createElement('td');
      td.className = c;
      td.textContent = text;
      if (title) td.title = title;
      tr.appendChild(td);
    }
    tr.addEventListener('click', () => openSnapshot(r));
    list.appendChild(tr);
  }
  if (all.length > 0 && shown.length === 0) {
    const tr = document.createElement('tr');
    tr.className = 'filtered';
    const td = document.createElement('td');
    td.colSpan = JOURNAL_KEYS.length;
    td.className = 'muted';
    td.textContent = 'No entry matches the filter.';
    tr.appendChild(td);
    list.appendChild(tr);
  }
  $('journal-count').textContent = String(all.length);
  $('journal-empty').classList.toggle('hidden', all.length > 0);
  $('journal-table').classList.toggle('hidden', all.length === 0);
}
const READING_LABELS = {
  price: 'Price', qty: 'Quantity', quote: 'Amount (USDT)', fee: 'Fee', interval: 'Timeframe', rsi: 'RSI 14',
  bb_pctb: 'Bollinger %B', bb_width_pct: 'Band width %', bb_upper: 'Upper band', bb_lower: 'Lower band',
  ema200: 'EMA 200 (1D)', ema200_pct: 'Price vs EMA 200 %', sma200: 'SMA 200 (1D)', sma200_pct: 'Price vs SMA 200 %', vs_last_buy_pct: 'vs last buy %', vs_last_buy_basis: 'vs last buy basis',
  vs_last_sell_pct: 'vs last sell %', vs_last_sell_basis: 'vs last sell basis', cash_after: 'Cash after', equity_after: 'Equity after',
  open_buckets_after: 'Open buckets after', realized_pnl: 'Realized P&L', realized_pnl_pct: 'Realized %', held_ms: 'Held',
  held_hours: 'Held (hours)', strategy: 'Strategy key', strategy_name: 'Strategy', light_enter: 'Enter light', light_exit: 'Exit light',
  open_fvgs: 'Open fair value gaps', fibs: 'Fibs drawn', indicators: 'Indicators on chart', time_iso: 'Time (UTC)', bucket_seq: 'Bucket',
  part: 'Part of the bucket', part_pct: 'Share of what was left %',
  reasons: 'Reasons ticked', reason_note: 'Note', checklist: 'Checklist',
  tp_pct: 'Target %', sl_pct: 'Stop %', tp_price: 'Target price', sl_price: 'Stop price', trigger: 'Sold',
  armed_at: 'Auto switched on at (UTC)', target_hit: 'Target reached while held', target_hit_at: 'Target first reached (UTC)',
  stop_hit: 'Stop reached while held', stop_hit_at: 'Stop first reached (UTC)',
  next_resistance: 'Next resistance zone centre (1D)', next_resistance_low: 'Next resistance zone bottom', next_resistance_high: 'Next resistance zone top',
  next_resistance_pct: 'Distance to next resistance zone %', next_resistance_touches: 'Next resistance touches', next_resistance_breaks: 'Next resistance breaks',
  next_resistance_first: 'Next resistance first touch', next_resistance_last: 'Next resistance last touch',
  next_support: 'Next support zone centre (1D)', next_support_low: 'Next support zone bottom', next_support_high: 'Next support zone top',
  next_support_pct: 'Distance to next support zone %', next_support_touches: 'Next support touches', next_support_breaks: 'Next support breaks',
  next_support_first: 'Next support first touch', next_support_last: 'Next support last touch',
  held_high: 'Highest high while held', held_high_pct: 'Highest high vs buy %', held_high_time: 'Highest high at (UTC)',
  held_low: 'Lowest low while held', held_low_pct: 'Lowest low vs buy %', held_low_time: 'Lowest low at (UTC)',
  ema200_change_pct: 'EMA 200 change since the buy %', ema200_dist_change: 'Price vs EMA 200 change since the buy (pp)',
  sma200_change_pct: 'SMA 200 change since the buy %', sma200_dist_change: 'Price vs SMA 200 change since the buy (pp)',
  ath: 'All-time high', ath_pct: 'Price vs all-time high %', ath_date: 'All-time high date',
  atl: 'All-time low', atl_pct: 'Price vs all-time low %', atl_date: 'All-time low date',
  high_1y: '1-year high', high_1y_pct: 'Price vs 1-year high %', high_1y_date: '1-year high date',
  low_1y: '1-year low', low_1y_pct: 'Price vs 1-year low %', low_1y_date: '1-year low date',
  high_6m: '6-month high', high_6m_pct: 'Price vs 6-month high %', high_6m_date: '6-month high date',
  low_6m: '6-month low', low_6m_pct: 'Price vs 6-month low %', low_6m_date: '6-month low date',
  high_3m: '3-month high', high_3m_pct: 'Price vs 3-month high %', high_3m_date: '3-month high date',
  low_3m: '3-month low', low_3m_pct: 'Price vs 3-month low %', low_3m_date: '3-month low date',
};
const READING_SKIP = new Set(['id', 'bucket_id', 'side', 'time_ms', 'has_image', 'image_url', 'at_buy']);
function readingText(k, v) {
  if (v === null || v === undefined || v === '') return '–';
  if (Array.isArray(v)) return v.length ? v.map(reasonLabel).join(', ') : 'none';
  if (k === 'trigger') return v === 'target' ? 'automatically at the target' : v === 'stop' ? 'automatically at the stop' : String(v);
  if (k === 'held_ms') return ageText(v);
  if (/_dist_change$/.test(k)) return pp(v);
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'number') {
    if (/_pct$/.test(k)) return pct(v);
    if (/^(price|tp_price|sl_price|bb_upper|bb_lower|ema200|sma200|ath|atl|high_1y|low_1y|high_6m|low_6m|high_3m|low_3m|next_(resistance|support)(_low|_high)?|held_high|held_low)$/.test(k)) return fmtPrice(v);
    if (/^(quote|fee|cash_after|equity_after|realized_pnl)$/.test(k)) return money(v, k === 'realized_pnl');
    if (k === 'qty') return qtyFmt(v);
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
  }
  return String(v);
}
function openSnapshot(r) {
  $('snap-title').textContent = `${r.side === 'buy' ? 'Buy' : 'Sell'} ${bucketLabel(r.bucket_seq, r.part)} · ${isoShort(r.time_iso)} UTC · ${r.interval}`;
  const img = $('snap-img');
  img.classList.toggle('hidden', !r.has_image);
  $('snap-nopic').classList.toggle('hidden', !!r.has_image);
  img.src = r.has_image ? r.image_url : '';
  const tb = $('snap-readings');
  tb.innerHTML = '';
  const addSection = (title) => { const tr = document.createElement('tr'); tr.className = 'section'; const td = document.createElement('td'); td.colSpan = 2; td.textContent = title; tr.appendChild(td); tb.appendChild(tr); };
  const addRow = (k, v) => {
    const tr = document.createElement('tr');
    const a = document.createElement('td'); a.textContent = READING_LABELS[k] || k;
    const b = document.createElement('td'); b.className = 'v'; b.textContent = readingText(k, v);
    tr.append(a, b); tb.appendChild(tr);
  };
  addSection(`At the ${r.side}`);
  for (const k of Object.keys(r)) if (!READING_SKIP.has(k)) addRow(k, r[k]);
  if (r.at_buy) {
    addSection('At the buy of this bucket');
    for (const k of Object.keys(r.at_buy)) if (!READING_SKIP.has(k)) addRow(k, r.at_buy[k]);
  }
  $('dlg-snapshot').showModal();
}
function initJournal() {
  for (const chip of $('journal-tools').querySelectorAll('.chip')) {
    chip.setAttribute('aria-pressed', String(chip.classList.contains('on')));
    chip.addEventListener('click', () => setJournalFilter(chip.dataset.side));
  }
  $('btn-close-snapshot').addEventListener('click', () => $('dlg-snapshot').close());
  $('dlg-snapshot').addEventListener('click', (e) => { if (e.target === $('dlg-snapshot')) $('dlg-snapshot').close(); });
}

function openHelp(open) {
  $('help-popover').classList.toggle('hidden', !open);
  $('btn-help').setAttribute('aria-expanded', String(open));
}

function onKeydown(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if ($('dlg-session').open || $('dlg-reasons').open || $('dlg-snapshot').open) return;
  const target = e.target;
  if (target && target.closest && target.closest('input, select, textarea, [contenteditable]')) return;
  if (day.onKey(e)) return;
  // Space and Enter belong to a focused button; the other shortcuts never conflict with one.
  const onButton = target && target.closest && target.closest('button, [role="button"]');
  if (e.code === 'Space') { if (onButton) return; e.preventDefault(); chart.fit(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); chart.zoomIn(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); chart.zoomOut(); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
  else if (e.key === 'ArrowLeft') { if (isDay()) { e.preventDefault(); stepBack(); } }
  else if (e.key === 'p' || e.key === 'P') { e.preventDefault(); state.playing ? pause() : play(); }
  else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); state.drawing ? cancelFibDraw() : startFibDraw(); }
  else if (e.key === 'g' || e.key === 'G') { e.preventDefault(); toggleFvg(); }
  else if (e.key === 'o' || e.key === 'O') { e.preventDefault(); toggleBollinger(); }
  else if (e.key === 's' || e.key === 'S') { e.preventDefault(); setDailyEma(!state.dailyEma); }
  else if (e.key === 'l' || e.key === 'L') { e.preventDefault(); setNearLevels(!state.nearLevels); }
  else if (e.key === 'h' || e.key === 'H') { e.preventDefault(); openHelp($('help-popover').classList.contains('hidden')); }
  else if (e.key === 'd' || e.key === 'D') { e.preventDefault(); if (!e.repeat) startPeek('year'); }
  else if (e.key === 'y' || e.key === 'Y') { e.preventDefault(); if (!e.repeat) startPeek('btc'); }
  else if (e.key === 'Escape') { cancelFibDraw(); openFibPopover(false); openFvgPopover(false); openHelp(false); }
  else if (e.key === 'Delete' || e.key === 'Backspace') { if (state.selectedFib !== null) { e.preventDefault(); deleteFib(state.selectedFib); } }
  else if (e.key === 'r' || e.key === 'R') { reset(); }
  else if (e.key === 'b' || e.key === 'B') { e.preventDefault(); $('buy-amount').focus(); }
}

async function init() {
  const [coins, intervals, specs, strategies, reasons] = await Promise.all([api.coins(), api.intervals(), api.indicators(), api.strategies(), api.reasons()]);
  state.reasons = reasons;
  state.coins = coins;
  state.intervals = intervals;
  for (const spec of specs) state.catalog[spec.key] = spec;
  for (const spec of strategies) {
    state.strategies[spec.key] = spec;
    const o = document.createElement('option');
    o.value = spec.key; o.textContent = spec.name; o.title = spec.description;
    $('strategy-key').appendChild(o);
  }

  day = new DayMode({
    $, api, chart, state, toast, fmtPrice, qtyFmt, baseAsset, token, fresh, pause, applyState, refreshView, selectTab, showHint, cancelFibDraw,
    setInterval: setInterval_, intervalMs, renderCardSums,
  });
  day.init();
  initFolds();
  try { chart.setLegendMin(localStorage.getItem('replay.ui.legendMin') === '1'); } catch { /* no storage */ }
  chart.onLegendFold = (min) => { try { localStorage.setItem('replay.ui.legendMin', min ? '1' : '0'); } catch { /* no storage */ } };
  // Minimized indicator panes (their -/+ button), remembered in this browser like the legend fold.
  try { chart.setMinPanes(JSON.parse(localStorage.getItem('replay.ui.minPanes') || '[]')); } catch { /* no storage or bad value */ }
  chart.onPaneFold = (keys) => { try { localStorage.setItem('replay.ui.minPanes', JSON.stringify(keys)); } catch { /* no storage */ } };
  $('f-mode').addEventListener('change', renderSessionMode);
  $('f-length').addEventListener('change', applySessionLength);
  $('f-start').addEventListener('change', applySessionLength);
  $('f-end').addEventListener('input', () => { $('f-length').value = 'custom'; $('f-end-note').textContent = '(optional)'; });
  indicators = new IndicatorPanel({
    popover: $('indicator-popover'), key: $('indicator-key'), params: $('indicator-params'),
    add: $('btn-add-indicator'), list: $('indicator-list'), count: $('indicator-count'),
    open: $('btn-indicators'), close: $('btn-close-indicators'), error: $('indicator-error'),
  }, state.catalog, onIndicatorsChange);

  $('btn-new-session').addEventListener('click', openSessionDialog);
  $('btn-cancel-session').addEventListener('click', () => $('dlg-session').close());
  $('form-session').addEventListener('submit', submitSessionDialog);
  $('btn-delete-session').addEventListener('click', deleteSession);
  $('session-select').addEventListener('change', (e) => loadSession(e.target.value || null).catch(err => toast(err.message, true)));
  $('btn-play').addEventListener('click', () => (state.playing ? pause() : play()));
  $('btn-step').addEventListener('click', () => step(1));
  $('btn-step-back').addEventListener('click', () => stepBack());
  $('btn-reset').addEventListener('click', reset);
  $('speed').addEventListener('input', (e) => {
    state.speed = parseInt(e.target.value, 10);
    $('speed-label').textContent = `${state.speed}/s`;
    setPref('speed', String(state.speed));
    if (state.playing) play();
  });
  $('btn-buy').addEventListener('click', buy);
  $('buy-amount').addEventListener('input', () => { if (state.wholeCoins) $('buy-coins').value = ''; updateEstimate(); });
  $('buy-amount').addEventListener('change', () => { if (state.wholeCoins) roundAmountToCoins(); });
  $('buy-coins').addEventListener('input', amountFromCoins);
  $('buy-coins').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); buy(); } });
  $('btn-whole').addEventListener('click', () => setWholeCoins(!state.wholeCoins));
  $('buy-amount').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); buy(); } });
  for (const b of $('buy-presets').querySelectorAll('button')) b.addEventListener('click', () => preset(parseFloat(b.dataset.pct)));
  $('strategy-key').addEventListener('change', (e) => setStrategy(e.target.value, null));
  $('btn-strategy-params').addEventListener('click', () => {
    const grid = $('strategy-params');
    const show = grid.classList.contains('hidden');
    grid.classList.toggle('hidden', !show);
    $('btn-strategy-params').setAttribute('aria-expanded', String(show));
  });
  $('strategy-params').addEventListener('change', onStrategyParamsChange);
  initReasons();
  initStats();
  initPlan();
  initPerf();
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('keyup', (e) => {
    if (e.key === 'd' || e.key === 'D') endPeek('year');
    else if (e.key === 'y' || e.key === 'Y') endPeek('btc');
  });
  window.addEventListener('blur', () => endPeek());  // the key-up is lost when the window loses focus
  // The peek buttons work like their keys: held with the mouse, or toggled when activated from the keyboard.
  for (const [kind, id] of Object.entries(PEEK_BUTTON)) {
    const btn = $(id);
    btn.addEventListener('pointerdown', (e) => { if (e.button === 0) { e.preventDefault(); startPeek(kind); } });
    for (const ev of ['pointerup', 'pointercancel', 'pointerleave']) btn.addEventListener(ev, () => endPeek(kind));
    btn.addEventListener('click', (e) => { if (e.detail === 0) (state.peek === kind ? endPeek(kind) : startPeek(kind)); });
  }
  $('sr-tol').addEventListener('change', () => setSrTol(parseFloat($('sr-tol').value)));
  // A mouse-clicked button would otherwise keep focus and swallow Space (its native activation key),
  // so the shortcut could not reach the chart. Keyboard-focused buttons (e.detail === 0) keep focus.
  document.addEventListener('click', (e) => {
    if (e.detail === 0) return;
    const btn = e.target && e.target.closest ? e.target.closest('button') : null;
    if (btn && !btn.closest('dialog, .popover')) btn.blur();
  });
  for (const tab of document.querySelectorAll('.tabs .tab')) {
    tab.addEventListener('click', () => {
      const on = tab.getAttribute('aria-selected') === 'true';
      if (on && state.bottomOpen) setBottom(false); else selectTab(tab.id, { open: true });
    });
  }
  $('btn-bottom').addEventListener('click', () => setBottom(!state.bottomOpen));
  chart.onClick = onChartClick;
  $('btn-fib').addEventListener('click', () => openFibPopover($('fib-popover').classList.contains('hidden')));
  $('btn-close-fib').addEventListener('click', () => openFibPopover(false));
  $('btn-fib-draw').addEventListener('click', startFibDraw);
  $('fib-auto').addEventListener('change', onAutoFibChange);
  $('fib-lookback').addEventListener('change', onAutoFibChange);
  $('btn-fvg').addEventListener('click', () => openFvgPopover($('fvg-popover').classList.contains('hidden')));
  $('btn-close-fvg').addEventListener('click', () => openFvgPopover(false));
  $('btn-help').addEventListener('click', () => openHelp($('help-popover').classList.contains('hidden')));
  $('btn-close-help').addEventListener('click', () => openHelp(false));
  for (const id of ['fvg-on', 'fvg-min', 'fvg-hide-filled']) $(id).addEventListener('change', onFvgChange);
  $('daily-ema').addEventListener('change', () => setDailyEma($('daily-ema').checked));
  $('near-levels').addEventListener('change', () => setNearLevels($('near-levels').checked));
  initTableTools();
  initJournal();

  renderTimeframes();
  renderState();
  await refreshSessions();
  let last = null;
  try { last = localStorage.getItem('replay.lastSession'); } catch { /* ignore */ }
  const pick = state.sessions.find(s => s.id === last) || state.sessions[0];
  if (pick) await loadSession(pick.id);
  else openSessionDialog();
}

init().catch(e => { console.error(e); toast(`Failed to start: ${e.message}`, true); });

// Exposed for automated UI tests.
window.__app = {
  ageText,
  state, chart, step, stepBack, play, pause, loadSession, refreshView, setStrategy, buy, sell, doBuy, doSell, openReasons, confirmReasons, closeReasons, loadStats,
  selectBucket, selectTab, setSort, setRsiFilter, arm, showPlan, loadPerf, setPerfScope, get perf() { return perfCharts; },
  startFibDraw, cancelFibDraw, deleteFib, refreshFibs, selectFib, toggleFvg, toggleBollinger, setNearLevels, refreshJournal, openSnapshot,
  startPeek, endPeek, setSrTol, setWholeCoins, roundAmountToCoins,
  setInterval: setInterval_, get indicators() { return indicators; }, get day() { return day; },
};
