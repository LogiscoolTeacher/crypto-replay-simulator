# Handoff

State of the project for the next Claude Code instance. Read this first, then the code it points
to. Update the **History** and **Open / ideas** sections when you finish a change.

Last updated: 2026-10-29 (quarter scaling, optional review, zone merging).

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

**Clock**: Europe/Malta. A trading day runs 06:00 → 06:00 and is named after its start date.
06:00–24:00 is the entry window, 00:00–06:00 the night; both are labels only. Buying is allowed
whenever no trade is open, also at night and several times a day. Stepping and Play run through the
night. Crossing 06:00 leaves the day: its journal row records how (`No trade`, `Trade closed`,
`Trade held past 06:00`, `Skipped`), and the day picture is taken just before. Play pauses when the
new day's trend check opens.

**Session**: 100 000 USDT start, risk fixed at 0.1 % of the start (100 USDT) per trade, fee 0.1 %
per side. Base interval 15m; the chart shows 15m / 1h / 4h / 1D. Zones are drawn on 1h / 4h.

**Trend check** per day: "daily close above EMA 200" (computed) + "higher highs and higher lows"
(the user's Yes/No). Only saved with the trade; it doesn't block buying.

**New trade**: entry at the close of the current 15m candle. Default stop = middle of the chosen
zone; ATR stop (zone lower edge − 0.5 × ATR 14) offered; any stop below entry allowed. Target by
price or ratio (R:R < 2 only warns). Size = risk / (entry − stop), capped by cash. Confidence 0–10
is saved once. Stop / target can be moved while open (every move is logged).

**Quarter steps** (`DayEngine.scale_trade`, `POST …/scale`, buttons Sell ¼ / Add ¼): while a trade
is open, a quarter of the ORIGINAL size can be sold (partial profit; a reason from `EXIT_REASONS`
is required) or added back, never above the original size. The last quarter goes with the normal
Sell. Cash moves at once. `day_trades.quarters_open` (NULL = 4) and `scales` (JSON steps) hold it;
`coins` / `position_value` / `buy_fee` / `risk_usdt` stay those of the buy, so the twin, Alt 1/2
and MAE/MFE keep the full size. `scaled_result` gives the P&L = every sale − every purchase − every
fee (= the cash change); R = P&L / risk_amount. Condition `partial` = a quarter was sold.

**Exits**: on each new 15m candle the low ≤ stop exits at the stop, the high ≥ target at the
target; both in one candle count as the stop. Fills are exactly at the level, with no gap model.
Early exit by hand needs a reason (`EXIT_REASONS`, incl. "Held the position too long").

**After an exit by day** the replay stays on the exit candle and the side panel asks:
- **End the day → 24:00** (`POST …/end-day`): jumps to the candle closing at 24:00 of the exit's
  trading day.
- **Keep trading** (`POST …/keep-trading`): a step or a buy does the same.

The **trade picture** becomes due when the replay reaches 24:00 of the exit's trading day
(`day.picture_due`): a step stops there with a `picture` event, the frontend takes the picture, and
Play goes on. A night exit is due at once. Only trades with `review_due = 1` (exits since `e128c09`;
the column name is historic) get one. Code: `_exit`, `end_day`, `keep_trading`, `_picture_due`,
`_picture_event`.

The **review** (3 true/false marks: stop too tight, went over the target, stop saved me) is
optional since 2026-10-29: it never blocks; answer or edit it from the trade's dialog (Answer
review). The Trades tab tints closed trades without a review light red (`tr.review-open`).

**Per trade, also recorded**:
- the automatic twin (same entry, hands-off original stop / target, runs through the night)
- Alt 1 / Alt 2: hands-off alternatives closed by 24:00 at the latest, filled once the replay passed
  that 24:00
- MAE / MFE in % and R
- the "what if" high / low after the exit until 24:00
- the entry conditions (`CONDITIONS`, evaluated in the Conditions tab)

**Pictures**:
- Trade picture: taken when it becomes due (24:00 of the exit day); always 15m, framed from 06:00
  of the entry's trading day to 24:00 of the exit's (`picture_frame`, `day.js capturePicture`),
  plus 8 empty bars on the right (`PIC_MARGIN_BARS`) so the line labels don't cover the candles.
  Lines: E (entry, blue), SL (white, dashed), TP (yellow, dashed), X (exit, green/red), labels
  short with the % from the entry (`chart.js setDayLines`, `SL_COLOR` / `TP_COLOR`).
- Day picture: taken when the day is left.

Both are stored under `data/snapshots/<session>/`.

**Navigation**:
- Skip day / Continue → next 06:00
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

**Step back** (⏴ button between Back to start and Play, key ←; day mode only): one 15m candle back,
never before the latest of: the first 06:00, the landing candle of the latest jump
(`day_sessions.back_floor_ms`), the open trade's entry or its last stop/target move, the latest
exit. Nothing is undone. Crossing 06:00 backwards clears that day's "left" mark. Code:
`_back_floor`, `_step_back_target`, `step_back`.

**Keys** (day mode): N/B new trade, X sell, M move stop/target, K skip (keep trading while the exit
choice shows), Enter (end the day), Z draw zone, J, E, → step, ← step back, P play. 1-2-3 only in
the review dialog. Sell ¼ / Add ¼ have no keys.

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

- 2026-10-29 Day trading: Sell ¼ / Add ¼ (quarter steps of the original size), the review no longer
  blocks (picture_due instead; unreviewed trades tinted red), close zones merge, short E / SL / TP / X
  labels with a right margin in the picture, white SL / yellow TP, folded trade-dialog sections, exit
  reason "Held the position too long".
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
8. **Run the UI smoke test**: install Playwright so `test_ui.py` runs; add a day-mode path
   (exit → choice → picture, Sell ¼ / Add ¼, step back).
