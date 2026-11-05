# Handoff

State of the project for the next Claude Code instance. Read this first, then the code it points
to. Update the **History** and **Open / ideas** sections when you finish a change.

Last updated: 2026-11-05 (strategy sessions, all six steps done on branch `strategy-sessions`).

## What the app is

A local replay simulator for crypto trading. Historical Binance candles are replayed candle by
candle in the browser; you trade on them with simulated money. FastAPI backend, vanilla-JS
frontend (TradingView lightweight-charts), SQLite storage.

```
python run.py            # http://127.0.0.1:8000  (API docs at /docs)
```

Session modes:

- **Swing** (the original app; README.md and WHAT_IT_DOES.txt describe it): any base interval,
  buckets with their own entries, targets and stops, strategy lights, journal with pictures,
  performance report.
- **Day trading** (`mode = "day"`): a manual test of an uptrend day-trading strategy on 15-minute
  candles. **It is not in README.md or WHAT_IT_DOES.txt yet**; this file and the docstring at the
  top of `backend/app/replay/day_engine.py` are its documentation.
- **Strategy sessions** (a day session with `run_mode` automatic / manual): a registered strategy
  plug-in trades it (Automatic) or proposes the trades (Manual); plus the Report tab. The user
  guide is the README section "Strategy sessions (Automatic and Manual)"; the details are below.

## Layout

```
run.py                              starts uvicorn
backend/app/main.py                 all HTTP routes (swing /api/sessions/..., day /api/day/...)
backend/app/config.py               paths (DATA_DIR, env REPLAY_DATA_DIR), intervals, symbols
backend/app/data/binance_client.py  public REST client (retries, weight limit)
backend/app/data/candle_store.py    SQLite candle cache with coverage tracking
backend/app/indicators/             RSI, MACD, EMA/SMA, Bollinger, ATR ...
backend/app/strategies/builtin.py   the swing-mode strategy lights
backend/app/strategies/rline.py     RLine_Retest jump strategy (day mode "Next signal")
backend/app/strategies/plugin.py    trading-strategy plug-in interface + registry (strategy sessions)
backend/app/strategies/plugins/     one file per trading strategy (found by the registry itself)
backend/app/strategies/patterns.py  hammer / bullish engulfing (autorun and the plug-ins)
backend/app/replay/engine.py        swing engine + shared session/cursor/view logic
backend/app/replay/day_engine.py    day-trading engine (~1850 lines)
backend/app/replay/day_store.py     day tables + additive MIGRATIONS dict
backend/app/replay/autorun.py       autorun: a day session trades a jump strategy's signals by itself
backend/app/replay/strat.py         strategy sessions: setup, limits and end flags of a day session with a run mode
backend/app/replay/strat_run.py     Automatic mode: the session's strategy plug-in trades the replay by itself
frontend/index.html                 the single page
frontend/static/js/app.js           app state, replay controls, swing trading, keys
frontend/static/js/day.js           day-trading side panel, dialogs, Trades/Days tabs, pictures
frontend/static/js/chart.js         chart wrapper (lines, ranges, screenshots)
backend/tests/                      pytest (fake exchange in fakes.py, no network)
data/                               gitignored: replay.db, snapshots/ (pictures)
```

## Day-trading mode as it stands

**Clock**: UTC (Europe/Malta until 2026-10-24; `TIMEZONE` in day_engine.py). A trading day is the
UTC calendar day, 00:00 → 24:00 (`DAY_START_HOUR = 0`; until 2026-10-25 it ran 06:00 → 06:00 with a
"night"). A candle belongs to the day its **close** falls in, so the candle closing at 00:00 starts
the new day: "24:00 of a day" and "00:00 of the next" are the same candle (the cursor of every jump).
Buying is allowed whenever no trade is open, several times a day. Crossing 00:00 leaves the day: its
journal row records how (`No trade`, `Trade closed`, `Trade held overnight`, `Skipped`), and the day
picture is taken just before. A new session starts on the start date: on its first candle
(00:00–00:15), because the candle closing at 00:00 lies before the session's data (a re-prepare
would reset a cursor before `data_start_ms`). Old sessions keep their stored days; from the cursor
on, days start at 00:00.

**Session**: 100 000 USDT start, risk fixed at 0.1 % of the start (100 USDT) per trade, fee 0.1 %
per side (the side panel no longer shows the risk row). Base interval 15m; the chart shows 15m / 1h / 4h / 1D. Zones are drawn on 1h / 4h.

**Trend check** per day: "daily close above EMA 200" (computed). Only saved with the trade; it
doesn't block buying. The daily "higher highs and higher lows" Yes/No and its 06:00 popup were
removed on 2026-10-24; old rows keep `hh_hl` / `uptrend` in the database, nothing reads them.

**New trade**: entry at the close of the current 15m candle. Default stop = middle of the chosen
zone; ATR stop (zone lower edge − 0.5 × ATR 14) offered; any stop below entry allowed. Target by
price or ratio (R:R < 2 only warns). Size = risk / (entry − stop), capped by cash. Confidence 0–10
is saved once. Stop / target can be moved while open by dragging their dotted chart lines (every
move is logged; the old Move form and the M key are gone).

**Quarter steps** (`DayEngine.scale_trade`, `POST …/scale`, buttons Sell ¼ / Add ¼): while a trade
is open, a quarter of the ORIGINAL size can be sold (partial profit; a reason from `EXIT_REASONS`
is required) or added back, never above the original size. The last quarter goes with the normal
Sell. Cash moves at once. `day_trades.quarters_open` (NULL = 4) and `scales` (JSON steps) hold it;
`coins` / `position_value` / `buy_fee` / `risk_usdt` stay those of the buy, so the twin, Alt 1/2
and MAE/MFE keep the full size. `scaled_result` gives the P&L = every sale − every purchase − every
fee (= the cash change); R = P&L / risk_amount. Condition `partial` = a quarter was sold. Exits
A / C stay whole-size versions (like Alt 1/2): Sell ¼ hides their choice and doesn't touch them;
the real exit (the last of A / C, or a stop / target hit) sells only the quarters still held.

**Exits**: on each new 15m candle the low ≤ stop exits at the stop, the high ≥ target at the
target; both in one candle count as the stop. Fills are exactly at the level, with no gap model.
Early exit by hand needs a reason (`EXIT_REASONS`, incl. "Held the position too long").

**Exits A and C**: two whole-size versions of the exit, recorded like Alt 1 / Alt 2
(`day_trades.exits_ac` JSON, `ac_legs` / `ac_public`, `DayEngine._close_legs`). The Sell dialog
closes A, C or both (`exits` on POST …/sell; "both" = whatever is still open). While one is open
the trade stays open and the cash is untouched (event `exit_leg`); the sell that closes the last
one is the real exit (its time, price and reason book the cash). A stop / target hit closes
whatever is still open at that level. A closed exit is a step-back floor. Trades closed before
this have no A / C (`exits_ac` is None in the API).

**After an exit by day** the replay stays on the exit candle and the side panel asks:
- **End the day → 00:00** (`POST …/end-day`): jumps to the candle closing at 24:00 of the exit's
  trading day = 00:00 of the next, so it leaves the day (day picture first) and the review is asked
  on landing. It is the same jump as the old Continue (`resume`); the Continue button is gone, the
  `/continue` route stays for the API.
- **Keep trading** (`POST …/keep-trading`): a step or a buy does the same.

The **trade picture** becomes due when the replay reaches 24:00 of the exit's trading day (= the
next 00:00; `day.picture_due`): a step stops there with a `picture` event, the frontend takes the
picture, and Play goes on. Only trades with `review_due = 1` (exits since `e128c09`; the column name
is historic) get one. Code: `_exit`, `end_day`, `keep_trading`, `_picture_due`, `_picture_event`.

The **review** (4 true/false marks: stop too tight, went over the target, stop saved me, moved the
stop too far up) is optional since 2026-10-29: it never blocks (no gate, Step / Play stay enabled);
answer or edit it from the trade's dialog (Answer review, keys 1-4 there). The Trades tab tints
closed trades without a review light red (`tr.review-open`).

**Per trade, also recorded**:
- the automatic twin (same entry, hands-off original stop / target, runs across days until one is hit)
- Alt 1 / Alt 2: hands-off alternatives closed by 24:00 at the latest, filled once the replay passed
  that 24:00
- MAE / MFE in % and R
- the "what if" high / low after the exit until 24:00
- the entry conditions (`CONDITIONS`, evaluated in the Conditions tab)

**Pictures**:
- Trade picture: taken when it becomes due (24:00 of the exit day); always 15m, framed from 00:00
  of the entry's trading day to 24:00 of the exit's (`picture_frame`, `day.js capturePicture`),
  plus 8 empty bars on the right (`PIC_MARGIN_BARS`) so the line labels don't cover the candles.
  Lines: E (entry, blue), SL (white, dashed), TP (yellow, dashed), X (exit, green/red), labels
  short with the % from the entry (`chart.js setDayLines`, `SL_COLOR` / `TP_COLOR`). A trade whose
  stop / target was moved also shows every level as a stepped line (same colours) with "SL ↑" /
  "TP ↓" marks at the moves (`chart.setLevelHistory`; pictures only, not the live chart).
- Day picture: taken when the day is left (with the same level history for the day's closed trade).

Both are stored under `data/snapshots/<session>/`.

**Navigation**:
- Skip day → next 00:00
- Calendar (goto a later day)
- Next day > EMA (J)
- Next signal (E): jumps to the next RLine_Retest signal and adds its zone as a temporary
  "signal" zone, removed when unused and the day is left

**Zones merge**: a zone that is added or resized next to another one (overlap, or a gap of at most
half the smaller zone's height, `ZONE_MERGE_GAP`) is folded into the older one: union of the
edges, the larger interval, hand-drawn wins over "signal". Chains merge too (`_merge_zones`). The
API returns the surviving zone with `merged` = how many were folded in. Zones that already
overlapped before this rule stay until one of them is touched.

**Trade dialog** (Trades tab → click a row): every section headline is highlighted and starts
folded; click (or Enter / Space) opens it (`day.js renderReadings`). The Days dialog stays unfolded.

**Step back** (⏮ button between Back to start ⏴ and Play, key ←; day mode only): one 15m candle back,
never before the latest of: the first candle, the landing candle of the latest jump
(`day_sessions.back_floor_ms`), the open trade's entry or its last stop/target move, the latest
exit. Nothing is undone. Crossing 00:00 backwards clears that day's "left" mark. Code:
`_back_floor`, `_step_back_target`, `step_back`.

**Keys** (day mode): N/B new trade, X sell, K skip (keep trading while the exit choice shows),
Enter (end the day), Z draw zone, J, E, → step, ← step back, P play. 1-2-3-4 only in the review
dialog. Sell ¼ / Add ¼ have no keys.

## Autorun (`backend/app/replay/autorun.py`)

The data-collection runs ("CTest__NN" sessions): a day session trades the RLine_Retest signals by
itself, one 15m step at a time, so every decision reads only closed candles. The module docstring
has the full rules. In short:
- **Next signal**, then the next 10 × 15m candles: a close below the zone bottom gives `zone_broken`;
  a hammer (lower wick ≥ 2 × body, upper ≤ 1 × body, low reaching the zone top + buffer) or a
  bullish engulfing confirms.
- **Entry** at the confirming candle's close. Stop = zone bottom − 0.25 × ATR 14 (1h), target 2R,
  the session's normal risk (0.1 % = 100 USDT).
- **Fee filter**: estimated round-trip fees (2 × the buy fee) must stay below 20 % of the risk.
- Rules live in `AutorunRules` and are saved with the run (`day_sessions.autorun` JSON), so runs stay
  comparable.

**Signal log** `day_signal_log`: every signal of the period with its outcome (`traded`,
`no_confirmation`, `zone_broken`, `fees_too_high`, `trade_open`, `waiting`, …), the daily trend
(`ema_above`), the planned entry / stop / fees, and the what-ifs the report fills in:
- *raw*: entry at the signal close
- *confirmed*: entry at the confirmation close, also for filtered signals

Both use the same stop and target, with gross and net R.

Routes: `POST /api/day/sessions/{id}/autorun` (start, `rules`), `POST …/autorun/advance`,
`GET …/autorun`, `…/autorun/report`, `…/autorun/signals(.csv)`.

**Pictures**: the frontend takes them, so the browser drives the run: `__app.day.autoRun()` (stop with
`autoStop()`, last status in `autoStatus`, a stop reason in `autoError`). `advance` returns with
`need: "day_picture"` before a step or jump that leaves a day without its picture, and with `need:
"trade_picture"` while a due trade picture is missing. It never steps past a missing picture: the first
CTest__01 attempt lost one picture (`capturePicture` gives up silently) and then stepped for weeks,
logging the signals it passed as "waiting". The loop stops after 3 failed tries of the same picture.
`pictures: false` runs headless. There's no button yet. Keep the browser pane visible during a run
(requestAnimationFrame), and don't click in the page while it runs.

**Zones**: autorun signal zones aren't merged (`merge_zone=False` on `next_signal`, `merge=False` on
`add_zone`). Merging folded every new signal zone into the zone of an old trade (which stays), so that
zone grew to ~7 % and no new zone showed. `delete_closed_zones` (default on) deletes a trade's zone
once it has closed: nothing reads it afterwards (the trade row and the signal log keep the edges).
A reset clears the run and its log.

**Variants** (`AutorunRules`, CTest__02):
- `trend_filter: "above"`: only signals whose daily close is above the EMA 200. The others are logged
  as `trend_filter`, without a jump; `next_signal(after_ms=…)` skips them.
- `entry: "signal"`: enter at the signal candle's close, with no confirmation window.
- `break_exit`: an open trade exits at a 15m close below its zone bottom (reason `setup_broken`). The
  hard stop still comes first within a candle, then the target.
- `what_if_ratios`: per-signal what-ifs per target with the same exit rules
  (`day_signal_log.what_ifs` JSON, `wi_<k>R_*` CSV columns, `what_if_targets` in the report). The
  report also counts how trades ended (`exits`).

**Results so far**:
- **CTest__01** (confirmation, 2R, all trends, 2021 → Oct 2023): 100 trades, 31 % wins, −18.4 R net
  (−7.0 R before fees). Uptrend hammer trades: 14 trades, 57 % wins, +8.2 R. Uptrend engulfing:
  42 trades, −10.4 R.
- **CTest__02** (uptrend only, entry at the signal candle, hard stop + break exit, 2.5R, 2021–2023):
  87 trades, 23 % wins, −26.9 R net (−16.4 R before fees). Exits: 61 stops, 20 targets, 6 break exits.
  What-if targets for the same trades: 2R −19.0 R, 3R −24.9 R. 111 signals were filtered by the
  trend, 43 by fees. Entering at the signal candle is clearly worse than CTest__01's uptrend hammer
  trades.

## Strategy sessions: Automatic and Manual (done on branch `strategy-sessions`, not merged yet)

The user's spec (2026-11-05): a session with a chosen coin, strategy, trade limit, time limit and
screenshots on / off, run in one of two modes. It is built as an extension of day-trading mode
(the autorun and "Next signal"), not as a new program. Swing and Day trading stay as they are; the
New session dialog gets a third mode. **`strategies/rline.py` is the entry-signal module and must
not be edited for this.**

Agreed with the user (by question, before any code):
- **Automatic**: the strategy places entry, stop and target; the trade runs to one of them.
- **Manual**: the replay jumps to the strategy's next entry point and PROPOSES entry, stop and
  target; Take or Skip (a skip is logged and does not count as a trade); then stop / target can be
  dragged (every move logged). When the trade closes, it jumps to the next entry point. Kept tools:
  Sell ¼ / Add ¼ and the confidence score. Not in Manual: exits A / C, the review marks.
- **Same entry points in both modes**: the confirmation, the fee filter and the trend filter belong
  to the strategy. The confirmed strategy's entry point is the confirming candle.
- **Stop conditions**: a trade limit N, and a time limit. Manual: H hours of REAL time, counted only
  while the session is open in the browser (0 = none). Automatic: a replay end date instead.
- **End flags**: `ENDED_BY_TRADE_LIMIT`, `ENDED_BY_TIMEOUT`, `ENDED_BY_USER`. A trade still open at
  a timeout is closed at market (exit reason "session timeout"); its hands-off twin shows what
  letting it run would have made. The rule is stored per trade.
- **Outcome**: after fees; −0.05 R to +0.05 R is BREAKEVEN. The result before fees is stored too.
- **Break exit** of the signal-candle strategy: automatic in Automatic, only a notice in Manual.
- **Fills** stay exactly at the level (comparable with the CTest runs); every trade stores slippage
  0, the fee rate, the fill model's name and the price source. Gap fills: maybe later (Open, item 5).
- **Screenshots** on = trade pictures only (no day pictures).
- **Report tab**: new sessions and old day sessions; fields old trades never stored stay empty.
- Long only. The setup keeps a start date.
- **Stop by the user** with a trade open: closed at market too (exit type "session stopped").
- **The replay reaching its last candle** before another limit is `ENDED_BY_TIMEOUT` in both modes
  (detail "end of the replay"); no fourth flag.

Build steps, each with the user's go-ahead: **1 strategy plug-ins (done)**, **2 session setup +
storage + end flags (done)**, **3 Automatic with a Start button (done)**, 4 Manual, 5 Report tab +
CSV, 6 docs.

**Step 1: the plug-ins** (`strategies/plugin.py`, `strategies/plugins/`). The Automatic run (step
3) is their caller; the old autorun still runs on `AutorunRules`. A strategy is one file in `plugins/` defining
`STRATEGY`, an instance of a `TradingStrategy` subclass with `key`, `name`, `version`,
`description`, `timeframe`. The registry (`discover`, `trading_strategies`,
`get_trading_strategy`, `describe_trading_strategies`) imports every file of the package whose
name doesn't start with "_", so a new strategy needs no engine change. What the engine will ask
(the module docstring has the contract): `find_signals`, `locate`, `pre_filter`, `entry` (per
closed candle: enter / wait / cancel), `levels` (stop + target; `kind` fixed / percent / atr),
`accept` (after sizing), `exit_signal`. `Market` is what a strategy may read (closed candles, ATR,
the daily trend); the engine implements it in step 3. A version's rules are class attributes and
never change: another target is a new version. `settings()` returns every rule value for storage.

Registered:
- `rline_retest_confirmed` v1 = the CTest__01 rules (the `AutorunRules` defaults): hammer or
  bullish engulfing within 10 candles, stop = zone bottom − 0.25 × ATR 14 (1h), 2 R, fee filter
  20 %, all trends.
- `rline_retest_signal` v1 = the CTest__02 rules: entry at the signal candle's close, uptrend only,
  2.5 R, break exit. **Its stop multiple (0.25) and fee filter (20 %) are assumed to be the
  defaults**: the saved rules of CTest__02 could not be checked (see the gotcha below).

Both wrap `rline.py` through `get_jump_strategy("rline_retest")`; `_rline_retest.py` holds what
they share. Tests: `tests/test_strategy_plugins.py` (the confirmed entry is compared with the
autorun's `confirmation`, the settings with `AutorunRules()`).

**Step 2: setup, storage, end flags** (`replay/strat.py`, class `StrategySessions`; its module
docstring has the rules). A strategy session IS a day session (`sessions.mode` stays "day", every
`/api/day/sessions/{id}/...` route works on it) whose `day_sessions` row has a `run_mode`.
`tests/test_strat.py` trades both modes through the engine directly (the guards of steps 3 and 4
sit in the routes, not in the engine).
- **Setup** (`create`, `POST /api/strategy/sessions`; `GET /api/strategy/setup` lists modes and
  strategies): `run_mode` automatic / manual, the strategy (name, version, timeframe and
  `settings()` are copied into the session), `max_trades`, `time_limit_hours` (Manual only; an
  Automatic session refuses hours, its end date is the limit), `pictures`. Fixed like day trading:
  15m candles, 100 000 USDT, risk 100 USDT, fee 0.1 %.
- **End** (`settle`, called from `DayEngine.state` through `day.strat`, so every action checks the
  limits; `stop` = `POST /api/strategy/sessions/{id}/stop`): `ENDED_BY_TRADE_LIMIT` once N trades
  were taken and the last one closed, `ENDED_BY_TIMEOUT` at the replay's last candle or when the
  Manual clock (`add_clock`, `POST …/clock`, at most 120 s per call) reaches the hours,
  `ENDED_BY_USER` for Stop. `_end` closes an open trade at market (exit types `session timeout` /
  `session stopped`, note "Session ended: …"), stores `end_flag`, `end_detail`, `ended_at`,
  `ended_ms`, then settles the twins and the after-exit windows up to the END OF THE DATA: after
  the end nothing decides anything, so reading past the cursor is fine there (and only there).
  An ended session takes no more trades (`_entry_gate`); stepping still works. Reset keeps the
  setup and clears the end and the clock. `state["day"]["session"]` is the public block (None for
  plain day sessions).
- **Per trade, at the buy** (`DayEngine._stamp`, every day trade): `fee_rate`, `slippage` (0),
  `fill_model` ("exact level"), `price_source`; in a strategy session also `run_mode`,
  `strategy_key` / `_name` / `_version`, `timeframe`, `timeout_rule`.
- **Per trade, at the exit** (`_result_fields`, `_excursion`): `outcome` (`outcome_of`: after fees,
  ±0.05 R = BREAKEVEN), `result_price` (exit − entry per coin), `gross_pnl` / `gross_pnl_pct` /
  `gross_r` (before fees = pnl + fees, quarter steps included), `high_price` / `high_ms` /
  `low_price` / `low_ms` (entry to exit; MAE / MFE in R were there already).
- **The 24 hours after the exit** (`_fill_after`): `after_high` / `after_low` with their candles'
  times and the percent from the exit price, `after_until_ms`, `after_complete`. Filled by
  `_backfill` (the trades listing) once the replay has PASSED the whole window; at a session's end
  to the end of the data (`after_complete` 0 when the data ends inside the window).
- Old closed trades get outcome, gross results and high / low on the next trades listing
  (`_backfill`); the reproducibility fields and the strategy stay NULL for them.
- **UI**: the New session dialog has a third mode "Strategy session" (run mode, strategy, read-only
  timeframe, screenshots, maximum trades, hours for Manual; the note lists the strategy's rules).
  The side panel shows a "Strategy session" card above Day trading (mode, strategy, trades n / N,
  time limit, screenshots, running / ended with the flag, End session button); `day.js
  renderSession` / `stopSession`.
- Tests: `tests/test_strat.py` (engine and API). Checked in the browser on a data copy.

**Step 3: Automatic mode** (`replay/strat_run.py`, class `StrategyRun`; its module docstring has
the whole flow). The run knows no strategy: it asks the session's plug-in `find_signals` / `locate`
-> `pre_filter` -> jump -> `entry` per candle -> `levels` -> the engine sizes -> `accept` -> buy ->
`exit_signal` per candle, and plays the replay with the engine calls the buttons make.
- **API**: `POST /api/strategy/sessions/{id}/run` (`max_steps`) starts the run or runs on and
  returns the state with `events` and `run` (`need`: "trade_picture" or None); pausing is not
  calling it. `GET .../signals` is the signal log. The run's status is `state.day.session.run`
  (phase, signal count, outcomes with plain labels, `can_run`), so the card needs no extra call.
- **Same trades as the autorun**: `tests/test_strat_run.py` runs the two RLine_Retest plug-ins next
  to the autorun with the CTest rules on the same scripted candles and compares every signal row
  and every trade. Keep these two tests green when touching either side.
- **End**: the limits of step 2 end the session. No signal left -> the run jumps to the replay's
  last candle = the end date -> `ENDED_BY_TIMEOUT`. After the end the run logs the signals passed
  since its last look, finishes the pictures and is "done".
- **Pictures** (trade pictures only): `DayEngine._picture_due` has two rules for strategy sessions:
  screenshots off = never due; at the replay's last candle every missing one is due at once (its
  24:00 never comes). After a trade-limit end or a Stop the run steps on to 24:00 of the last exit's
  day for the last picture, so the cursor may stand after `ended_ms`. A missing picture holds the run.
- **No manual input**: `StrategySessions.manual_guard` (called by the routes of buy, sell, scale,
  move, step, step back, the jumps, end-day / keep-trading / continue and the swing step) answers 409
  for an Automatic session; reset, zones and notes stay allowed. The old autorun refuses strategy
  sessions. The run itself calls the engine directly.
- **Reproducible**: the run refuses (409) when the plug-in's version or `settings()` differ from
  what the session stored, or the plug-in is gone.
- **Engine additions**: `DayEngine.jump_to(session_id, target_ms, reason)` (a forward jump to any
  15m candle; `next_signal` shares `_jump_to`), `day_sessions.run_state` (JSON: phase, the signal /
  trade as an index into the strategy's signal list, `seen_ms`), `day_signal_log.strategy_key`,
  `outcome_text` (the strategy's sentence) and `details` (JSON of what its filters reported).
  `confirm_ms` is the candle a decision fell on (enter or cancel).
- **Outcomes**: the engine's own are in `strat.RUN_OUTCOMES` (traded, trade_open, waiting, invalid,
  end_of_data, session_ended); every other code is the strategy's (zone_broken, no_confirmation,
  fees_too_high, trend_filter, ...).
- **A strategy without a zone** works (tested with a throw-away plug-in): the trade's band is stop
  to entry, the log's band columns hold the signal candle's close.
- **Break exit** of the signal-candle strategy: `day.sell(reason="setup_broken")`, so the exit type
  is "early exit" with that reason, like CTest__02; the note starts with "Automatic (...)".
- **UI** (`day.js`: `runStrategy` / `runSteps` / `toggleRun` / `haltRun`, `locked()`): Start / Pause
  / Continue / Finished button and the rows Run and Signals in the Strategy session card; the badge
  says not started / running / paused / ended. One server call runs at most 96 candles
  (`RUN_STEPS_PER_CALL`) so Pause answers quickly. End session pauses the run first and lets it
  finish the pictures afterwards; a reset halts it first (`app.js reset`). The loop checks the
  session id, not the page's generation token: taking a picture switches the chart interval, which
  changes the token. In an Automatic session Step, Play, Step back, New trade, Sell, the jumps, the
  trade keys and dragging stop / target are off.
- Tests: `tests/test_strat_run.py` (engine and API), `tests/test_ui_strat.py` (Playwright: Start to
  Finished with a picture). Checked in the browser on a data copy (BTCUSDT Oct-Nov 2022).
- Speed: a step costs a full `DayEngine.state()` (like the autorun), roughly 20 ms; fine for runs
  of months, worth a look before runs of many years.

**Step 4: Manual mode** (the same `StrategyRun`; "Manual" in its module docstring). The search is
the Automatic one, so both modes have the same entry points and the same filters in the log
(`tests/test_strat_manual.py` plays a Manual session that takes everything next to an Automatic one).
- **Proposal**: where Automatic buys (after `levels`, the engine's size and `accept`), a Manual run
  stores `run["proposal"]` (entry = this candle's close, stop, target, size, risk, fees, the zone,
  the strategy's words), keeps the log row open with the planned levels, goes to phase `proposal`
  and returns. It is `state.day.session.run.proposal`, so a reload shows it again.
- **Answer**: `POST /api/strategy/sessions/{id}/take` (`confidence` 0-10 required, `notes` optional:
  the buy exactly as proposed, note "your note | Manual (strategy vN): ...") or `.../skip` (log
  outcome `skipped`; does not count toward the trade limit). `StrategyRun.take` / `skip`.
- **During the trade the trader moves the replay** (Step / Play); the run returns at once while the
  trade is open. Kept tools: drag stop / target (`move_trade` logs every change in
  `day_trades.changes`: replay time `at_ms` / `at_local`, the price then, field, `from`, `to`),
  Sell ¼ / Add ¼, Sell (the whole trade, with a reason). The strategy's `exit_signal` is only a
  notice: `run.exit_notice` on the candle it fires, a toast and a line under the buttons.
- **Auto-next**: once the trade has closed (or after a skip) `can_run` is true again and the browser
  calls `/run` by itself (`day.js autoNext`): no "end the day?" question, the zone of the closed
  trade is deleted, the trade picture is taken at its 24:00 on the way, the next proposal comes.
- **What Manual refuses** (`manual_guard(session_id, action)`, 409): a buy of its own, every jump,
  end-day / keep-trading / continue, Step back, selling exit A or C alone, and a step while no
  trade is open. Allowed: step with a trade open, sell (both exits), scale, move. Automatic still
  refuses everything.
- **Ends**: a time limit or End session while a proposal waits closes it in the log
  ("session_ended", "The session ended before the proposal was answered."); with a trade open the
  step-2 rule closes it at market. The run then closes up like in Automatic (log, last picture, done).
- **Real-time clock** (`day.js tickClock`): every 5 s the time since the last report goes to
  `POST .../clock`, only while the session is open, its run was STARTED, it has not ended and the
  browser tab is visible (`document.visibilityState`); a gap of more than 15 s between two ticks (a
  sleeping computer) counts as 15 s. Leaving the tab reports what is owed and stops counting. The
  answer only patches the clock fields into the state, unless the limit was reached just now.
- **UI**: the proposal panel in the Day trading card (`strat-proposal`: levels, size, risk,
  confidence chips, notes, Take / Skip) and its lines on the chart (not draggable until taken);
  the Start button reads Start / Pause / Continue / Take or skip / In a trade / Finished. In a
  Manual session New trade, the jumps and Step back are off, Step and Play only work while a trade
  is open (`day.stepLocked()`), the exit-choice box, the Exits A / C row, the A / C choice in the
  Sell dialog and the review marks are hidden. No day pictures in any strategy session
  (`captureDayPicture` returns early).
- Tests: `tests/test_strat_manual.py` (engine and API), `tests/test_ui_strat.py` (Playwright: Take
  to Finished with a picture; Skip). Checked in the browser on a data copy (BTCUSDT Oct-Nov 2022),
  including a time limit that ends the session by itself.
- Chosen without an explicit answer, reported to the user: the clock starts with Start (not when
  the session is opened); Play is NOT paused by the strategy's exit notice; Take and Skip have no
  keys; a taken trade's note keeps the trader's text first.

**Step 5: the Report tab** (`replay/report.py`, class `Report`; its module docstring has every
definition; `frontend/static/js/report.js`, class `ReportTab`).
- **Which sessions**: every READY day session. Mode in the report: a strategy session's `run_mode`;
  an old day session with an autorun (`day_sessions.autorun`, the CTest__NN runs) = Automatic with
  strategy key `autorun:confirm` / `autorun:signal` and a label from its saved rules ("Autorun
  (entry at the confirmation, 2 R)"), kept apart from the plug-ins; any other old day session =
  "By hand" (user's choice). Fields old trades never stored stay empty.
- **API**: `GET /api/report` and `GET /api/report.csv`, both with the query filters `sessions`
  (comma separated ids), `end` (all / timeout / non_timeout; non-timeout includes sessions without a
  flag), `mode` (automatic / manual / hand), `strategy`, `symbol`, `date_from` / `date_to` (the
  trade's entry day), `outcome` (WIN / LOSS / BREAKEVEN / open), `exit_kind`. Bad values: 422.
- **Numbers** (`stats`): closed trades only, in exit order. Win rate = wins / closed trades;
  average win / loss over the wins / losses (R and USDT); expectancy = average R; total in USDT, %
  of the start balance (only when all selected sessions share it; old sessions had other balances
  and risk amounts, so mixed totals in USDT and R differ in scale), R; duration average and median;
  max drawdown = deepest fall of the running R below its peak (start 0); streaks, a BREAKEVEN ends
  both. Several sessions: one sequence over all of them (user's choice), plus each session's own
  numbers and end reason in the session table.
- **Trade rows**: the spec's fields (initial SL / TP, exit kind SL / TP / manual close / strategy
  exit / session timeout / session stopped, duration, outcome, result in price / % / R, high / low
  with times, MFE / MAE R, the 24 hours after the exit, every SL / TP move, fee / slippage / fill
  model / price source / timeout rule, screenshot path). Rows come from `DayEngine.trades`, so the
  report fills what old trades miss (outcome, high / low, after-exit window) on the way.
- **UI**: bottom tab "Report" in every mode (not swing-only / day-only); a session switch does not
  leave it. Filters on top (a checkbox list of sessions with All / Open session, end chips, mode,
  strategy, coin, from / to), nine number tiles, the session table, outcome chips and an exit
  select for the trade table (the numbers follow them), sortable trade table, Export CSV (the
  link carries the same query). It starts with the open day session and follows it until the
  session list is changed by hand.
- Tests: `tests/test_report.py` (one database with all four kinds of session: the Automatic
  session and the old autorun give the same numbers; the definitions; filters; CSV; API),
  `tests/test_ui_strat.py::test_report_tab_follows_the_open_session_and_exports_csv`. Checked in the
  browser on a data copy (14 sessions, 89 trades).
- CTest__01 / __02: still not on this PC. Copying their `replay.db` here (or pointing
  `REPLAY_DATA_DIR` at it) is enough: the report recognises the autorun sessions by themselves.

**Step 6: documentation.** README.md has the user guide ("Strategy sessions (Automatic and
Manual)": setup, both modes, the end flags, what is stored per trade, the Report, adding a
strategy with an example plug-in, the API) and the new files in its Layout.
`tests/test_readme_strategy.py` takes the README's example plug-in, registers it and lets an
Automatic session trade with it: keep that example working when the plug-in interface changes.
WHAT_IT_DOES.txt only got a pointer at its top (it covers swing mode as of 2026-10-15).

Left for later on purpose:
- The strategy sessions' signal log has no CSV and no what-ifs yet (the autorun's report code reads
  `AutorunRules`, not a plug-in); the Report covers trades only.
- The session list still says "Day" for a strategy session (the default name starts with
  Automatic / Manual).

## RLine_Retest (`backend/app/strategies/rline.py`)

1h, strictly causal single pass (`detect_rline`).
1. **Touches**: fractal swing highs (5 left / 5 right) that reversed ≥ 1 ATR.
2. **Zones**: drawn wick to body, height clamped to 0.5–1.0 ATR.
3. **RLine qualifies** by any of:
   - A) ≥ 2 touches
   - B) a high-volume touch (≥ 2× the 20-bar average)
   - C) a round number inside or near the zone
4. **Breakout** = a close above the top.
5. **Armed** once a candle **closes ≥ 0.75 ATR above the top** (`away_atr`; wicks don't count, since
   `f951a37`).
6. **Retest** = a low within 0.25 ATR of the top (`near_atr`), at least 2 and at most 48 bars after
   the breakout.

The signal is refined to its 15m candle by the day engine. BTC 1h: 382 signals, median 6 bars
from breakout to retest (was 519 / 2 with the wick rule).

## Candle data

Each candle also stores `taker_buy` (Binance's taker-buy base volume, for the volume delta). Rows
from before 2026-10-24 have NULL there until a view with the Volume delta indicator fills the
window on screen and the next 1000 candles in a background thread (offline: logged, retried after
10 minutes).

`CandleStore` caches every closed candle in `data/replay.db` and keeps a `coverage` table of the
ranges already requested, so only gaps are downloaded (real exchange gaps aren't re-requested). A
request returns 1000 candles in ~0.4 s. A day session's first prepare takes seconds; the full
4-year BTC 15m history about a minute. Since the 2026-10-24 change, a range that's already covered
needs no network at all, not even the server time, so preparing works offline.

## Working on this repo (gotchas)

- **Python**: the dependencies live in the user site-packages of `/usr/bin/python3`, so don't use
  `python -I`. Tests: `cd backend && python3 -m pytest -q` (~45 s, ~190 tests). `test_ui.py` is
  skipped (Playwright not installed).
- **On the user's Windows PC** (2026-11-05): `python` on the PATH is the Store Python without
  fastapi / pytest. The app and the tests run with `C:\Users\logis\miniconda3\python.exe`
  (`cd backend; C:\Users\logis\miniconda3\python.exe -m pytest -q`, ~4 minutes, 250 tests).
  Playwright is installed there, so the UI tests run: 2 of `test_ui_day.py` fail, also on `main`
  (see Open, item 8). The user often has the app running on port 8000 against the real `data/`:
  leave that process alone and use another port with a data copy.
- **Trying things in the browser on this PC**: `.claude/launch.json` (local, listed in
  `.git/info/exclude`) starts a scratch launcher that copies `data/replay.db` with sqlite's backup
  API and serves the app on port 8011 with `REPLAY_DATA_DIR` set to the copy. The scratch folder
  is per session, so write the launcher again: ~20 lines (backup, set the env var, `uvicorn.run`).
  Old pictures 404 there (the copy has no `snapshots/`).
- **The CTest sessions are not in this PC's `data/replay.db`** (no session has an autorun there,
  and no other copy of the database was found). Their numbers exist only in this file.
- **Data and worktrees**: `DATA_DIR` is `<checkout>/data`, so a git worktree starts with an empty
  database and would re-download. To try things against real data, copy the main checkout's
  `data/replay.db` with sqlite's backup API into a scratch folder and run with
  `REPLAY_DATA_DIR=<that folder>`. **Never run tests or experiments against the user's real
  `data/`.** The copy has no `snapshots/`, so old pictures 404 there.
- **Browser checks**: `window.__app` exposes the UI functions (`loadSession`, `step`, `stepBack`,
  `day`, …). In the desktop app's built-in browser pane, `requestAnimationFrame` doesn't fire while
  the pane is hidden. Picture code now falls back to a timer, but screenshots taken then can be stale.
- **DB changes**: add columns through `MIGRATIONS` in `day_store.py` (additive only; old rows get
  NULL).
- **Style**: match the surrounding code: dense explanatory comments, plain English UI texts,
  tests for every behaviour change.
- **Git**: work on a branch or worktree, commit with the `Co-Authored-By` line, then fast-forward
  `main` when the user asks. `origin` = github.com/LogiscoolTeacher/crypto-replay-simulator.

## History (newest first)

- 2026-11-05 Strategy sessions, step 6: user guide in README.md (both modes, end flags, stored
  fields, Report, adding a strategy, API), a test that runs the README's example strategy, a pointer
  in WHAT_IT_DOES.txt.
- 2026-11-05 Strategy sessions, step 5: the Report tab. `replay/report.py` (every ready day session:
  Automatic, Manual, old autoruns as Automatic, old hand-traded sessions as "By hand"; filters,
  numbers, per-session rows, trade rows with the spec's fields), `/api/report` and `/api/report.csv`,
  `report.js` (filters, tiles, session table, sortable trade table, CSV link).
- 2026-11-05 Strategy sessions, step 4: Manual mode. The run proposes the trade at the strategy's
  entry point (`take` / `skip` routes), the trader holds it (stop / target moves logged, quarters,
  Sell) and the replay goes on to the next entry point by itself; the strategy's exit is a notice;
  `manual_guard` per action; the real-time clock from the browser (visible tab only); Step back, New
  trade, jumps, exits A / C and review marks off in Manual; no day pictures in strategy sessions.
- 2026-11-05 Strategy sessions, step 3: Automatic mode. `replay/strat_run.py` runs the session's
  plug-in (same trades as the autorun's CTest rules, tested side by side), `DayEngine.jump_to`, the
  picture rules of strategy sessions, `manual_guard` on the hand-trading routes, the run / signals
  routes, the Start / Pause button and run status in the Strategy session card.
- 2026-11-05 Strategy sessions, step 2: `replay/strat.py` (setup, trade limit, Manual clock, the three
  end flags, Stop; an open trade is closed at market at a timeout or a stop), `/api/strategy/...`
  routes, the per-trade fields (mode, strategy, fee / slippage / fill model / price source, outcome,
  results before fees, high / low with times, the 24 hours after the exit), the New session dialog's
  third mode and the Strategy session card.
- 2026-11-05 Strategy sessions, step 1: trading-strategy plug-ins (`strategies/plugin.py`: interface
  and a registry that finds the files in `strategies/plugins/` by itself) with `rline_retest_confirmed`
  v1 and `rline_retest_signal` v1; `is_hammer` / `is_bullish_engulfing` moved to
  `strategies/patterns.py` (the autorun imports them). Nothing uses the plug-ins yet.
- 2026-11-04 Autorun variants for CTest__02: trend filter, entry at the signal candle, break exit,
  closed trades' zones deleted, what-ifs per target; `next_signal(after_ms)`.
- 2026-11-04 Autorun fix: a missing trade picture holds the run (`need: "trade_picture"`), the frontend
  stops after 3 failed tries; autorun signal zones are not merged.
- 2026-11-04 Autorun (`autorun.py`, `day_signal_log`, `day_sessions.autorun`, `/autorun` routes,
  `DayMode.autoRun`): automatic RLine_Retest trading with hammer / engulfing confirmation, a zone-break
  cancel, a zone stop − 0.25 ATR 1h, a 2R target and a fee filter (< 20 % of the risk). Every signal is
  logged with its outcome, its trend and raw / confirmed what-ifs; the report splits everything by
  trend. Dry run on a data copy (Oct–Nov 2022): fees filter many signals, because zones on 15m entries
  are often less than 1 % from the entry.
- 2026-10-29 Day trading: Sell ¼ / Add ¼ (quarter steps of the original size), the review no longer
  blocks (picture_due instead; unreviewed trades tinted red), close zones merge, short E / SL / TP / X
  labels with a right margin in the picture, white SL / yellow TP, folded trade-dialog sections, exit
  reason "Held the position too long". Merged with the UTC-day work on main: `reviewWaits` (app.js) and the
  inline review form are gone again, review mark 4 kept, the level-history lines use the SL / TP colours.
- No more 06:00: the trading day is the UTC calendar day (`DAY_START_HOUR = 0`). A new session,
  Skip day, the calendar, Next day > EMA and End the day land on 00:00; there is no night, and "End
  the day" now leaves the day (it is the old Continue, whose button is gone). `in_entry_window` and
  the `in_window` / `night` state fields are removed.
- Indicator panes (RSI, MACD, Volume delta, the day-mode RSI pane) have a -/+ button at their top
  right: minimized they shrink to a 22 px strip with their lines hidden (`chart.togglePane`,
  `_renderPaneFolds`, remembered in localStorage `replay.ui.minPanes`). The pane's price scale stays
  visible on purpose: hiding it makes `takeScreenshot` throw, which would break the pictures.
- Pictures show every stop / target move: stepped lines from the entry to the exit plus a mark at
  each move (`chart.setLevelHistory`, from `trade.changes`). Pictures taken before keep the old look.
- Review mark 4, "I moved the stop too far up" (`stop_moved_up`, column `review_stop_moved_up`,
  key 4). Required like the other three; trades reviewed before show it as "–" (NULL).
- Review fix: a step while a review was due resynced the state from the swing endpoint (no `day`
  block), so the review form and its Save button vanished. The resync uses the day endpoint now
  (`fetchSession`), and Step / Play are disabled while the review waits (`reviewWaits` in app.js).
  Browser test: `tests/test_ui_day.py` (skipped without Playwright, like `test_ui.py`).
- Exits A and C: a sell closes exit A, C or both; the trade stays open until both are closed and
  then closes at the later one. Stored as JSON in `day_trades.exits_ac`, shown in the Sell dialog,
  the day panel, the Auto exits tab (columns + summary) and the trade detail.
- Volume delta: the background fill of old candles runs 1000 bars past the cursor (`TAKER_FILL_AHEAD`
  in engine.py); filling only up to the cursor left the newest bar empty after every step.
- No more "higher highs and higher lows": the Yes/No buttons, the Uptrend row, the 06:00 trend
  popup, the `/check` route, the `hh_hl` / `uptrend` conditions and columns are gone.
- UI declutter 5: long grey hint sentences (table toolbars, Highs & lows, New trade footer) are
  (i) tooltips (`.info`) now.
- UI declutter 4: Volume delta indicator (`vdelta`: 2 × taker buy − volume per candle, own pane,
  green/red bars). Candles keep `taker_buy` (column added on open); candles stored before get it in
  the background when the indicator is on (`CandleStore.fill_taker_buy_soon`, called from `view`).
- UI declutter 3: the day clock is UTC (`TIMEZONE = "UTC"`), "Malta" is gone from every text. Old day
  sessions keep their stored days; from the cursor on, days start at 06:00 UTC. API names `*_local`
  stay (they now hold UTC).
- UI declutter 2: every side card (and the day panel's Notes / Zones) folds to its title with a
  one-line summary (`[data-fold]`, `initFolds` / `renderCardSums` in app.js, remembered per mode in
  localStorage; day mode starts with Highs & lows and Account folded). The chart legend folds to one
  line (`ChartView.setLegendMin`).
- UI declutter 1: no zoom buttons (keys only), no Risk row and no Move stop/target form in the
  day panel (drag the dotted lines), date above time, Back to start ⏴ / Step back ⏮ glyphs swapped.
- 2026-10-24 Handoff file, CLAUDE.md, offline-friendly candle cache.
- `e128c09` Day trading: night trading, end-the-day choice, step back, stuck panel fix (the panel
  drew its buttons while a step was busy and wasn't redrawn).
- `78a72de` Day trading: "Next signal" zones are temporary.
- `f951a37` RLine_Retest: a close away before a retest; framed 15m trade pictures.
- `5da711f` RLine_Retest: wick-to-body zones and a near-zone retest buffer.
- `30f3747` Day trading: 100k start balance with a fixed 100 USDT risk (0.1 %).
- `9e4dd22` Day trading: RLine_Retest strategy and "Next signal" jump.
- `89fe366` Alternative trades (Alt 1 / Alt 2 closed at 24:00) and after-exit MAE/MFE in R.
- `a018d3e` Cleanup batch: EMA 200 everywhere, compact help, Trades columns, MAE/MFE, 1h RSI.
- `484ddf9` Day trading: automatic-exit twins, hold hours and the post-trade review.
- `14a1260` Day trading: "Next day > EMA" jump (button + J key).
- `59d0af4` Crypto replay trading simulator (swing mode, steps 1–6 of README).

## Open / ideas

**Strategy sessions are done** (all six steps) on branch `strategy-sessions`; nothing pushed and
`main` untouched: merge / push only when the user asks. CTest__01 / __02 are not in this PC's
database; the Report shows them as Automatic once their `replay.db` is here. Small choices made
without an explicit answer (listed under steps 3 and 4) still wait for the user's OK.

None of the following are started. Roughly in order of usefulness:

00. **Autorun follow-ups**: a button / progress card in the day panel instead of the console; a
   rule to skip the day pictures (a long run takes hundreds); compare runs (CTest__01 vs __02) in
   one report; the stop fill model (item 5) matters for the what-ifs too.
0. **Quarter steps, follow-ups**: show the steps on the "Auto exits" comparison (the twin keeps the
   full size, so a scaled trade's difference mixes exits and sizing); maybe keys for Sell ¼ / Add ¼;
   a one-off merge of zones that already overlap in old sessions.

1. **Document day trading** in README.md / WHAT_IT_DOES.txt (both cover only swing mode).
2. **Signal backtest report for RLine_Retest**: run the detector over the whole history, score each
   signal (Alt 1 / Alt 2 logic or a fixed R target), and tune `away_atr` / `near_atr` / `min_gap`
   on outcomes instead of signal counts.
3. **Confirmation candle** for the retest; the hook is `_retest_signal` in rline.py (e.g. a
   bullish close back above the top).
4. **Retake picture** for old trades: pictures saved before `f951a37` keep the old "as on
   screen" framing.
5. **Stop fill model**: a candle opening below the stop should fill at the open, not the stop.
   Results are currently a little optimistic (v1 limitation noted in day_engine.py).
6. **Split `day_engine.py`** into reviews / jumps & signals / journal modules.
7. **`has_image` vs. missing files**: a row can say it has a picture whose file is gone (404s).
   Check that the file exists, or hide the broken image.
8. **UI smoke tests**: they run on the user's PC (miniconda Python). `test_ui_day.py` fails twice
   there, also on `main`: it creates its session with `ADAUSDT`, which is no longer in
   `config.COINS` (422), so `test_unanswered_review_blocks_nothing` and
   `test_indicator_pane_can_be_minimized` break. Pick a listed coin no other module uses. Then
   extend the day path (exit → choice → picture, Sell ¼ / Add ¼, step back).
