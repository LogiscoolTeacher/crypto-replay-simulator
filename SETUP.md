# Replay Trading – setup in 5 minutes

A local web app that replays historical crypto candles (Binance data) so you can practise
buying and selling into "buckets" with play money, with indicators, strategy lights, a trade
journal with chart pictures, and statistics over your reasons. Nothing is sent anywhere;
everything runs on your own computer.

## 1. Install Python

You need **Python 3.12** (3.12.7 is what the app was built with).
Download from https://www.python.org/downloads/ and, on Windows, tick
"Add python.exe to PATH" in the installer.

Check in a terminal (PowerShell, Terminal or cmd):

```
python --version
```

## 2. Unzip and install the dependencies (once)

Unzip the file somewhere, open a terminal in the `replay_trading` folder and run:

```
cd backend
python -m pip install -r requirements.txt
cd ..
```

## 3. Start the app

From the `replay_trading` folder:

```
python run.py
```

Then open http://127.0.0.1:8000 in your browser (Chrome, Edge or Firefox).
Leave the terminal window open while you use the app; Ctrl+C stops it.
If port 8000 is taken: `python run.py --port 9000` and open http://127.0.0.1:9000.

## 4. First session

1. Click **New session**, pick a coin, the base timeframe (1h is a good start), a start date
   and the play-money balance. The candles are downloaded from Binance's public API on the
   first use (an internet connection is needed; later sessions on the same coin are faster).
2. Use **Play / Pause**, **Step** and the speed slider at the bottom to move through time.
   Press **H** in the app for the full list of keyboard shortcuts.
3. Type an amount (or use the 10/15/20/25 % presets) and click **Buy**. Tick the reasons that
   apply and Confirm. Every buy is its own bucket.
4. Sell 25 %, 50 % or All of a bucket with the buttons in the **Open buckets** table; tick the
   sell reasons and Confirm.
5. **History**, **Journal** (chart picture and readings of every trade, CSV export) and
   **Statistics** (which reasons paid off) live in the tabs under the chart.

Your sessions, trades and pictures are stored in the `data` folder next to `run.py`.
Delete that folder to start from scratch.

## Trouble?

- `python` not found: reinstall Python with "Add to PATH", or try `py -3.12` instead of `python`.
- Red error at the first session: Binance's API could not be reached; check the internet
  connection or a company firewall, then create the session again.
- The full documentation is in `README.md`.
