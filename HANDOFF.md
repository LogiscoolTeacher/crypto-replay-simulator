# Handoff

State of the project for the next Claude Code instance. Read this first, then the code it points
to. Update the **History** and **Open / ideas** sections when you finish a change.

Last updated: 2026-10-25.

## What the app is

A local replay simulator for crypto trading. Historical Binance candles are replayed candle by
candle in the browser; you trade on them with simulated money. FastAPI backend, vanilla-JS
frontend (TradingView lightweight-charts), SQLite storage.

```
python run.py            # http://127.0.0.1:8000  (API docs at /docs)
```

Two session modes:

- **Swing** (the original app; README.md and WHAT_IT_DOES.txt describe it): any base interval,
  buckets with their own entries, targets and stops, strategy lights, journal with pictures,
  performance report.
- **Day trading** (`mode = "day"`): a manual test of an uptrend day-trading strategy on 15-minute
  candles. **It is not in README.md or WHAT_IT_DOES.txt yet**; this file and the docstring at the
  top of `backend/app/replay/day_engine.py` are its documentation.

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
backend/app/replay/engine.py        swing engine + shared session/cursor/view logic
backend/app/replay/day_engine.py    day-trading engine (~1850 lines)
backend/app/replay/day_store.py     day tables + additive MIGRATIONS dict
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

**Exits**: on each new 15m candle the low ≤ stop exits at the stop, the high ≥ target at the
target; both in one candle count as the stop. Fills are exactly at the level, with no gap model.
Early exit by hand needs a reason (`EXIT_REASONS`).

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

The **review** (4 true/false marks: stop too tight, went over the target, stop saved me, moved the
stop too far up) becomes due
when the replay reaches 24:00 of the exit's trading day. A step stops there with a `review` event,
and nothing moves (step, jumps, buy, step back) until it is answered; Step / Play are disabled then.
Only trades with `review_due = 1` (exits since `e128c09`) can block; older trades never do.
Code: `DayEngine._exit`, `end_day`, `keep_trading`, `_pending_review`, `_review_gate`,
`_review_event`.

**Per trade, also recorded**:
- the automatic twin (same entry, hands-off original stop / target, runs across days until one is hit)
- Alt 1 / Alt 2: hands-off alternatives closed by 24:00 at the latest, filled once the replay passed
  that 24:00
- MAE / MFE in % and R
- the "what if" high / low after the exit until 24:00
- the entry conditions (`CONDITIONS`, evaluated in the Conditions tab)

**Pictures**:
- Trade picture: taken when its review becomes due; always 15m, framed from 00:00 of the entry's
  trading day to 24:00 of the exit's (`picture_frame`, `day.js capturePicture`). A trade whose stop /
  target was moved also shows every level as a stepped line with "SL ↑" / "TP ↓" marks at the moves
  (`chart.setLevelHistory`; pictures only, not the live chart).
- Day picture: taken when the day is left (with the same level history for the day's closed trade).

Both are stored under `data/snapshots/<session>/`.

**Navigation**:
- Skip day → next 00:00
- Calendar (goto a later day)
- Next day > EMA (J)
- Next signal (E): jumps to the next RLine_Retest signal and adds its zone as a temporary
  "signal" zone, removed when unused and the day is left

**Step back** (⏮ button between Back to start ⏴ and Play, key ←; day mode only): one 15m candle back,
never before the latest of: the first candle, the landing candle of the latest jump
(`day_sessions.back_floor_ms`), the open trade's entry or its last stop/target move, the latest
exit. Nothing is undone. Crossing 00:00 backwards clears that day's "left" mark. Code:
`_back_floor`, `_step_back_target`, `step_back`.

**Keys** (day mode): N/B new trade, X sell, K skip (keep trading while the exit
choice shows), Enter (end the day / save review), 1-2-3-4 review marks, Z draw zone, J, E, → step,
← step back, P play.

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

None of these are started. Roughly in order of usefulness:

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
8. **Run the UI smoke tests**: install Playwright so `test_ui.py` and `test_ui_day.py` run (neither
   has run yet in this environment); extend the day path (exit → choice → review, step back).
