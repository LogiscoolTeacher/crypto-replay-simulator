# Replay Trading

A local trading replay simulator for crypto. Historical Binance candles are replayed
candle by candle; you watch indicators and strategy lights, and place simulated trades
into individual buckets.

## Status

| Step | Scope | State |
|------|-------|-------|
| 1 | Project skeleton, Binance downloader, SQLite candle store, API | done |
| 2 | Indicators (RSI, MACD, EMA/SMA, Bollinger) and four strategies | done |
| 3 | Replay engine, account, buckets, trades, persistence | done |
| 4 | Chart UI with indicators and replay controls | done |
| 5 | Trading UI: buy panel, bucket list, entry lines, lights, journal | done |
| 6 | Polish and review | done |

## Requirements

Python 3.12 (tested with 3.12.7). Install dependencies once:

```
cd backend
python -m pip install -r requirements.txt
```

## Run

```
python run.py            # http://127.0.0.1:8000
python run.py --port 9000
```

Open http://127.0.0.1:8000 in a browser. Interactive API docs: http://127.0.0.1:8000/docs

## Using the app

1. **New session**: pick a coin, the base timeframe (the finest you can view), a start date,
   optionally an end date, the starting balance and the fee. Data downloads in the background.
   The replay never starts earlier than 200 daily candles after the coin's first Binance
   candle, so the moving averages exist from the first bar: an earlier start date is moved
   forward and a yellow notice above the chart says so (the automatic session name follows;
   a name you typed stays). A coin younger than 200 days starts where you asked, and the
   notice says from which date the 200-day averages will appear.
2. **Timeframes**: switch between the base timeframe and any coarser one at any time.
   The current coarser candle is drawn translucent while it is still forming.
3. **Indicators**: add any number of EMA, SMA, RSI, MACD or Bollinger Band instances with
   your own parameters. Overlays draw on the price chart; oscillators get their own pane.
4. **Replay**: play/pause (P), step one candle (Right arrow), speed 1-10 candles per second,
   back to start (R). The step size is the selected timeframe.
   Press H (or the ? button) for the list of shortcuts. O puts Bollinger Bands 20/2 on the chart
   and takes them off again. S draws the daily SMA 200 on any timeframe (also a checkbox at the
   bottom of the Indicators panel); on intraday charts each bar carries the value of the last
   completed day, so the line never uses a close from the bar's future. L draws the next
   resistance zone as a translucent red tube and the next support zone as a green one across
   the chart (the two Readings rows), labelled with touches, breaks and the distance from the
   current price to the band, plus dashed lines at the 6-month high and low with the distance in
   percent; on by default, also a checkbox at the bottom of the Indicators panel.
   Hold D (or hold the 1Y button with the mouse) to peek at the daily candles of the last
   365 days up to the replay day; release the key or button and the chart returns to the
   timeframe and zoom you had; while a peek is held the labels of the price lines (buckets,
   6-month high and low, target and stop) are hidden so the latest candles stay visible. Hold Y
   (or the BTC 2Y button) for the same with Bitcoin's daily
   candles of the last 2 years, whatever coin the session trades, plus the 3 major resistance
   lines above the price and the 3 major support lines below it. Levels are swing highs and
   lows of those two years grouped within 1.5 % (adjustable in the Indicators panel), ranked by
   touches with a bonus for recent ones and for the window's extremes; each line is labelled
   R1-R3 / S1-S3 nearest-first with its number of touches. Only candles known at the replay
   day are used, so nothing from the future leaks in.
   Chart zoom: Up arrow zooms in, Down arrow zooms out (hold the
   key to keep going), Space returns to the default zoom of the last 160 candles. Zooming keeps the latest candle at the right edge;
   mouse wheel and drag still work as usual.
5. **Strategy**: pick one of the built-in strategies and tune its parameters with the gear button.
   Its indicators are drawn automatically. The ENTER light is on while the strategy sees an entry
   bias, the EXIT light while it sees an exit bias, judged on the last closed candle of the viewed
   timeframe. A light flashes on the candle where the signal flips. Small dots mark past signals.
6. **Buy a bucket**: type an amount in USDT or use a preset share of your cash (B focuses the field).
   With **Whole coins** switched on, a Coins field appears and only whole coins are bought: a
   preset or typed USDT amount is rounded down to the whole coins it buys and the USDT field is
   rewritten to the cost of those coins, fee included, so a 15 % preset never spends more than
   15 %; typing a coin count rewrites the USDT field the same way. Fewer than one affordable coin
   is refused.
   Each buy creates a numbered bucket at the current price. The **Open buckets** table under the
   chart lists buy date, age (time held on the replay clock, e.g. `3d 5h`), amount, price, fee, total
   cost, current value, P&L and percent, with three sell buttons per row: **25 %**, **50 %** and
   **All** of what is left in the bucket. A part smaller than the rest becomes its own History row
   (#5.1, #5.2, ...) with its share of the coins, cost and buy fee, its own journal row with chart
   picture and readings, and its own S marker on the chart, while the bucket stays open with the
   remainder (the row shows `#5 ·2` after two parts) and closes as the last part when you sell
   All. The percentages always refer to what is left, so 50 % twice leaves a quarter. With Whole
   coins on, a part is rounded down to whole coins and the button is disabled when that leaves
   none. Open buckets draw a line at their entry price, green in profit and red in loss;
   click a row to highlight its line. The **High** and **Low** columns show the highest high and the
   lowest low of the base candles since the buy, in percent of the buy price (price and time on
   hover), so you see what the trade has had on the table and how far it went against you.
   Selling everything removes the row; the **History** tab keeps every closed
   bucket and every part sold (hover the number for the part's share) with buy and sell date, holding time, prices, fees, proceeds and realized P&L, plus four
   columns on the daily 200 lines: how the EMA 200 and the SMA 200 moved between your buy and your
   sell (percent of the value at the buy) and how the price's distance to each changed (percentage
   points, distance at the sell minus distance at the buy). Positive is green, negative red; hover
   a cell for the values at the buy and at the sell. The same four numbers sit in the sell row of
   the Journal and in its CSV export. The last four History columns are the highest high and the
   lowest low while the bucket was held (from the buy candle to the sell candle) with their
   percentages from the buy price; also in the Journal sell row and CSV, with the times.
   **Journal**: every buy and every sell is recorded with a picture of the chart as it was on
   screen (all panes, indicators, fibs and gaps) and the readings of that moment: price, amount,
   RSI, Bollinger %B and width, EMA 200 and SMA 200 distance, price of, date of and distance to the
   all-time, 1-year, 6-month and 3-month high and low, next resistance and support zones with edges,
   distance, touches, breaks and touch dates, distance to the last buy and sell, strategy and
   lights, open gaps, account after the trade. A sell row also stores the result (P&L, percent,
   holding time) and a copy of the readings at the buy. The **Journal** tab lists everything with
   thumbnails; click one for the full picture and the readings table; sort by any column, filter
   by side, and **Export CSV** for analysis elsewhere. Pictures live in `data/snapshots/`.
   **Reason checklists**: Buy and every sell button first open a checklist. Tick every reason
   that is true (buy: at support, RSI low, above EMA 200, lower Bollinger band, Fibonacci level,
   high volume at support, bounce candle, RSI divergence up, breakout above resistance, big drop
   from the recent high, other; sell: at resistance, RSI high, upper Bollinger band, profit
   target, trim, weak volume on the rise, RSI divergence down, rejection candle, support broken,
   below EMA 200, other), add a note if you like, and Confirm; Esc or Cancel aborts the trade.
   Nothing ticked is allowed. The ticked reasons, the note and the checklist version (`v1`) are
   stored with the journal row (Reasons column, readings table and CSV); a sell row also carries
   the reasons of its buy. **Statistics** tab: the Reasons view over all sessions (or one coin).
   Per buy reason the closed trades that ticked it against those that did not, per sell reason
   the sells that ticked it, each with the count and the average realized P&L in percent of the
   cost. A trade is one buy with all its sells and counts once its bucket is fully closed; its
   result is the sum of its sells over the whole cost. Groups under 10 are flagged "too few to
   judge". Journal rows from before the checklist existed are left out and counted.
   **Target and stop**: next to the buy amount set a take profit and a stop loss in percent of
   the buy price (remembered; leave one empty to skip it). While the mouse is over Buy the chart
   shows them as hatched areas from the current candle on: green up to the target, red down to
   the stop. They are stored with the bucket (Target and Stop columns, dotted lines for the
   selected bucket) and in the journal row and CSV. Nothing sells by itself: the **Auto** switch
   of an open bucket is off at the buy. Switched on during the trade, the replay sells everything
   left in the bucket on the first base candle whose high reaches the target or whose low reaches
   the stop, at that price (or at the candle's open when it gapped beyond it); a candle reaching
   both counts as the stop. Such a sell happens at that candle's time inside the step, gets its
   journal row and picture, and shows as "target" or "stop" in the History **Exit** column.
   Every sell row also records whether the target or the stop was reached while the bucket was
   held and when, armed or not, so you can see what the plan would have done.
   **Performance** tab: an evaluation like TradingView's Strategy Tester for this session or
   all sessions together. Tiles: net profit (USDT and percent of the start balance), trades
   with winners and losers, win rate and streaks, profit factor, maximum drawdown with its
   peak and low dates, average trade, best and worst trade, average and longest hold, open
   buckets at market, equity with its high and low, fees. Diagrams: the equity curve at every
   base candle with open buckets at market and the drawdown shaded (all sessions: the realized
   P&L added up trade by trade, since sessions replay different years), one bar per trade in
   percent with the average, the winners/losers donut and a year × month grid of returns.
   Hover any diagram for the numbers. A part sold counts as its own trade, like in History.
   **Fibonacci retracements**: press F (or Fib → Draw), click the swing start and the swing end;
   clicks snap to the candle high or low. Levels 0, 23.6, 38.2, 50, 61.8, 78.6 and 100 % are drawn
   from the swing to the right edge with their prices; 0 % sits at the second click. Click a level
   to select a fib, Delete removes it, Esc cancels a half-drawn one. Fibs are stored per session and
   cleared by Back to start. The Fib panel can also switch on an automatic retracement from the
   lowest low to the highest high of the last N candles of the timeframe on screen (dashed).
   **Fair value gaps**: press G or FVG. Every three-candle imbalance on the timeframe on screen is
   drawn as a box, green bullish or red bearish, from its middle candle to the right edge. Once a
   later candle trades through the whole gap the box stops there and turns grey. The FVG panel sets
   the minimum gap size in percent of price (default 0.1 %) and can hide filled gaps.
   The **Readings** card in the side panel shows the RSI(14) of the timeframe on screen (green at
   or below 30, red above), Bollinger %B of the timeframe on screen (0 = lower band, 1 = upper band;
   green below the lower band, red above the upper) with the band width, the EMA 200 and the
   SMA 200 of daily candles with the price's distance from each, and,
   once you have traded, the same percentages against the last buy and the last sell as the Now row.
   **Resistance / Support**: the next zone ("tube") above and the next at or below the price.
   A zone is a swing level of the closed daily candles of the last 365 days (same clustering as
   the BTC 2Y peek) widened to ± the tolerance from the Indicators popover (1.5 % by default).
   Its visits are counted: a visit starts when a daily candle reaches into the band and ends
   when a candle stays out of it again. Leaving on the side the price came from within 5 days
   is a *touch*, leaving on the other side a *break*, lingering longer a *stay*. Only zones
   with at least three touches count. Each row shows the distance from the price to the zone's
   nearer edge ("inside" when the price is in the band), the touches and how many days ago (on
   the replay clock) the last touch was; hover for the band's edges, every touch, break and stay
   date. A dash means no such zone exists on that side. Centre, edges, distance, touches and
   breaks are saved with every buy and sell in the journal.
   The **Highs & lows** card below it has one range bar per window: all-time, the last year,
   the last six months and the last three months. Each bar runs from the window's lowest low (left) to its highest high
   (right), daily candles with wicks, and the marker is the current price; the number on the left
   is how far the price is above the low, the number on the right how far below the high. Hover
   a row for the prices and dates of the high and low. Only candles known at the replay time
   count, the forming day included, so nothing from the future leaks in. "All-time" starts at the
   coin's Binance listing (for BTC August 2017); those daily candles are downloaded once per coin
   the first time a session of it is shown. A year is the replay day plus the 365 days before it,
   six months the replay day plus 182 days, three months the replay day plus 90 days.
   Every buy and sell records the RSI(14) of the timeframe on screen at that moment; the tables
   show it green below 30 and red above 30. Click any column header to sort (again to flip), and
   use the chips above a table to show only buckets bought below or above RSI 30.
   The **Now** row at the bottom of the open table is a template for a buy at this moment: the
   current price and how far it is, in percent, from your last buy and from your last sell. Once
   the market has closed above that trade price, the comparison switches to the highest close since
   the trade ("vs high since sell"), so you see how far below the last height you are. When
   nothing is open, the last sold bucket stays visible as a greyed reference row.

## Download candles from the command line

Run from the `backend` folder:

```
python -m app.data.download --symbol BTCUSDT --interval 1h --days 365
python -m app.data.download --symbol ETHUSDT --interval 1D --start 2017-01-01
python -m app.data.download --all --interval 1D --start 2017-01-01
```

Downloads are incremental. Ranges already fetched are never requested again,
including genuine gaps in Binance history. Only closed candles are stored.

## Review notes (step 6)

Two independent review passes (backend and frontend) found 21 issues; all are fixed and
covered by regression tests (`backend/tests/test_review_fixes.py`, `backend/tests/test_ui.py`).
Highlights:

- Indicators are computed over a sliding window of the last 3,000 + visible bars, so a
  one-year 1-minute session steps at constant cost. EMA and RSI have forgotten their seed
  long before that, so values are identical to a full-history computation.
- Downloads of the same series are serialised and coverage updates are atomic.
- Stepping by any count is a single indexed query; the engine lock is never held for long.
- Every server response in the UI is checked against the current session, timeframe and
  indicator set before it is applied, so nothing stale is ever rendered or traded.
- Parameters are validated in the browser and rejected changes roll back without being saved.

## Tests

```
cd backend
python -m pytest                 # backend + browser smoke test (needs: pip install playwright && playwright install chromium)
python -m pytest --deselect tests/test_ui.py   # backend only
```

## Layout

```
backend/app/config.py            coins, intervals, defaults
backend/app/data/binance_client  Binance public REST client (retries, rate limits)
backend/app/data/candle_store    SQLite candle store with coverage tracking
backend/app/data/download        command-line downloader
backend/app/indicators/core      indicator math (SMA, EMA, RSI, MACD, Bollinger)
backend/app/indicators/registry  indicator catalogue with parameter schemas
backend/app/strategies/          strategy framework and the four built-in strategies
backend/app/replay/engine        replay sessions, cursor, account, buckets, trades
backend/app/replay/session_store SQLite persistence for sessions, buckets, trades
backend/app/replay/payload       JSON payload builder shared by compute and view
backend/app/main.py              FastAPI application
backend/tests/                   pytest suite
frontend/index.html              web UI (plain JavaScript, no build step)
frontend/static/js/chart.js      Lightweight Charts wrapper: candles, panes, legend
frontend/static/js/app.js        sessions, replay controls, strategy lights, trading, account panel
frontend/static/js/indicators.js indicator popover; params.js builds parameter inputs
frontend/static/vendor/          TradingView Lightweight Charts 5.2.1 (Apache 2.0)
data/replay.db                   SQLite database (created on first run)
```

## Indicators and signals

Indicator math follows TradingView conventions: EMA and Wilder's RMA are seeded with
a simple average, RSI uses Wilder smoothing, Bollinger Bands use the population
standard deviation. RSI is verified against the published StockCharts reference series.

Each strategy produces, per closed candle:

- `state`: +1 entry bias (green), -1 exit bias (red), 0 neutral
- `enter` / `exit`: one-bar events on the bar where the state flips

Use `POST /api/compute` to get candles, indicator values and signals for a window.
Keep `start` fixed and move `end` forward during a replay so earlier values never change.

## Replay sessions

A session replays one coin from a start date at a *base* timeframe (its finest).
Creating a session downloads the candles it needs in the background (base timeframe
and every coarser one, each with 1,000 bars of warm-up before the start). The replay
starts at the chosen date, or at the 200th day after the coin's first candle when that is
later (`start_note` in the session explains a moved start or a coin younger than 200 days).

- `POST /api/sessions` create, `GET /api/sessions/{id}` state (account, open buckets, price)
- `POST .../step` advance by the base timeframe or any coarser one
- `POST .../buy` spend an amount of USDT into a new bucket; `POST .../sell` close one bucket or a
  part of it; both take `reasons` (ticked checklist keys) and `note`
- `POST .../buckets/{id}/arm` switch the automatic sell at the bucket's target or stop on or off;
  `POST .../step` returns the buckets it sold as `auto_sells`
- `GET .../performance` the Performance report of a session; `GET /api/stats/performance` over all sessions
- `GET /api/reasons` the buy and sell checklists; `GET /api/stats/reasons?symbol=` the Reasons
  statistics over all sessions
- `POST .../view` candles at any timeframe >= base, up to the cursor only, with indicators and signals.
  The in-progress coarser candle is aggregated from base candles and flagged `partial_last`.
- `POST .../reset` back to the start with a fresh account; `GET .../trades` journal

Fills happen at the close of the current base candle (the latest price you can see).
Fee is charged on both sides: buy qty = (amount - fee) / price; sell cash = proceeds - fee.

## Decisions

- Market: top 10 crypto coins by market cap, USDT spot pairs on Binance.
- Timeframes: 1m, 5m, 15m, 1h, 4h, 1D, switchable during replay.
- One account, 10,000 USDT default. Each buy creates its own bucket. A sell closes one whole bucket.
- Long only, fee 0.1% per side, editable.
- Strategy lights are advisory only; the user trades manually.
- Everything is stored locally in SQLite.
