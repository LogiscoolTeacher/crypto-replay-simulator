"""Day-trading mode: a manual replay test of an uptrend day-trading strategy (spec: daytrading-mode-v1).

The mode shares the session row, the candle data, the view endpoint and the chart with the swing
(bucket) mode, but has its own clock, rules and trades:

Clock (all times in UTC; until 2026-10-24 it was Europe/Malta)
* The session's base interval is 15 minutes; the chart may show 15m, 1h, 4h or 1D.
* A trading day is the UTC calendar day, 00:00 to 24:00 (until 2026-10-25 it ran 06:00 to 06:00
  with a "night" from 00:00 to 06:00). A candle belongs to the day its close falls in, and the
  candle closing at 00:00 starts the new day: "24:00 of a day" and "00:00 of the next" are the same
  candle. A buy is possible whenever no trade is open, several times a day.
* Crossing 00:00 leaves the trading day (its journal row records how). Skip, the calendar and the
  jump buttons land on the candle closing at 00:00. After an exit the replay stands on the exit
  candle: "End the day" jumps to the next 00:00 (it leaves the day), "Keep trading" (or a step or
  a buy) goes on as if no trade had happened.
* The four review questions of a closed trade are asked once the replay reaches 24:00 of its
  exit's trading day (= the next 00:00); nothing moves on until they are answered.
* "Step back" moves one 15-minute candle back, never before the latest trade action (entry, exit,
  stop / target move) or the landing candle of the latest jump; nothing is undone.
* The trend check of a day: "daily close above EMA 200", computed from the completed daily
  candles (UTC days, the exchange's boundary) known at the cursor. It never blocks a buy; it is
  saved with the trade. (Until 2026-10-24 the user also answered "higher highs and higher lows"
  once a day; old rows keep that answer in `hh_hl` / `uptrend`, nothing reads it any more.)

Trading
* Risk per trade = 0.1 % of the STARTING capital (100 USDT at the default 100 000), fixed. Fee 0.1 % of the trade value on every buy
  and every sell (the session's fee rate).
* Entry at the close of the current 15-minute candle. Suggested stop = lower edge of the chosen
  support zone - 0.5 x ATR 14 (15m, at the entry candle); the user may change it. The stop must be
  below the entry, the take-profit above it. Reward-to-risk = (tp - entry) / (entry - stop); below
  2.0 only warns.
* coins = risk_amount / (entry - stop); position_value = coins x entry. If position value plus the
  buy fee exceeds the cash, the size is capped to what the cash allows (flag `capped`).
* On every new 15-minute candle after the entry: low <= stop -> exit at the stop ("stop");
  high >= take-profit -> exit at the take-profit ("take-profit"); both in one candle -> stop. Fills
  are exactly at the level (no gap or slippage model: a known limitation of v1).
* Early exit by hand at the close of the current candle ("early exit") with a required reason.
* Exits A and C: two whole-size versions of the exit, recorded side by side (like Alt 1 / Alt 2).
  A sell closes A, C or both; the trade stays open until both are closed and then closes at the
  later one (its time, price and reason book the cash, P&L and R). A stop / take-profit hit closes
  whatever is still open at that level.
* P&L = sell value - position value - buy fee - sell fee; P&L % of the position value;
  R = P&L / risk_amount (the nominal 0.1 %). "What if" after the exit: highest high and lowest low of
  the 15-minute candles after the exit candle until 24:00 of the exit day (local date of the exit
  candle's open), as percentages of the exit price and of the entry price; "n/a" when empty.
"""
from __future__ import annotations

import logging
import csv
import io
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .. import config
from ..indicators import core
from ..indicators.registry import validate_params
from ..strategies.rline import JUMP_STRATEGIES, JumpStrategy, get_jump_strategy
from .day_store import DayStore
from .engine import (
    EMA_LONG_INTERVAL,
    EMA_LONG_PERIOD,
    EMA_LONG_WARMUP_BARS,
    RSI_WARMUP_BARS,
    WARMUP_BARS,
    Conflict,
    Invalid,
    NotFound,
    ReplayEngine,
    _iso,
    _now_iso,
    decode_png_data_url,
)

log = logging.getLogger(__name__)

BASE_INTERVAL = "15m"
VIEW_INTERVALS = ("15m", "1h", "4h", "1D")
ZONE_INTERVALS = ("1h", "4h")      # the user draws zones on these charts (they show on all)
TIMEZONE = "UTC"                   # the day clock (was Europe/Malta): no daylight-saving shifts, same as the exchange
TZ = ZoneInfo(TIMEZONE)
DAY_START_HOUR = 0                 # the trading day is the UTC calendar day (it started at 06:00 until 2026-10-25)
RISK_PCT = 0.1                     # of the starting capital, fixed for the session (was 1 % on 10k)
DAY_START_BALANCE = 100_000.0      # default starting capital: 0.1 % = 100 USDT risk, no leverage needed down to a ~0.1 % stop
ATR_PERIOD = 14
ATR_WARMUP_BARS = 600              # bars fed to RSI and ATR at the entry; far beyond where the seed matters
STOP_ATR_MULT = 0.5                # ATR stop = zone lower edge - 0.5 x ATR (offered next to the default)
RATIO_MAX = 100.0                  # reward-to-risk ratio accepted for a computed take-profit
RR_WARN_BELOW = 2.0                # reward-to-risk below this only warns
RSI_DIP_MAX = 40.0                 # entry rule 3: RSI 14 dropped to 40 or below during the dip
NOTE_MAX_CHARS = 500
DAY_NOTES_MAX_CHARS = 4000         # free text per trading day (the daily journal)
# How a trading day was left (stored on its check row when the replay crosses or jumps past the next 00:00).
LEFT_END, LEFT_SKIP, LEFT_EXIT, LEFT_HELD = "end", "skip", "exit", "held"
LEFT_LABELS = {LEFT_END: "No trade", LEFT_SKIP: "Skipped", LEFT_EXIT: "Trade closed", LEFT_HELD: "Trade held overnight"}

# The entry conditions saved with every trade and evaluated afterwards (key, label). Each is
# yes / no / unknown for a trade; see `trade_conditions`.
CONDITIONS: tuple[tuple[str, str], ...] = (
    ("ema_above", "Daily close above EMA 200"),
    ("price_in_zone", "Price inside the support zone"),
    ("rsi_dipped", f"RSI 14 (15m) dipped to {RSI_DIP_MAX:.0f} or below that day"),
    ("close_above_prev_high", "Entry candle closed above the previous high"),
    ("rr_ok", f"Reward-to-risk at least {RR_WARN_BELOW:.0f}"),
    ("stop_middle", "Stop at the zone middle (the default)"),
    ("stop_atr", "Stop at the ATR stop"),
    ("stop_own", "Stop set by hand (neither middle nor ATR)"),
    ("zone_4h", "Zone drawn on the 4h chart (else 1h)"),
    ("morning", "Entry before 12:00"),
    ("afternoon", "Entry 12:00 to 18:00"),
    ("evening", "Entry after 18:00"),
    ("stop_moved", "Stop moved during the trade"),
    ("tp_moved", "Target moved during the trade"),
)
MOVE_FIELDS = {"stop": "stop_used", "take_profit": "take_profit"}
STOP_MATCH_PCT = 0.01   # a stop within this percent of the middle / ATR stop counts as that choice
CONDITION_SCOPES = ("session", "all")


def trade_conditions(t: dict) -> dict[str, bool | None]:
    """The yes / no / unknown of every condition in CONDITIONS for one trade row."""
    flag = lambda v: None if v is None else bool(v)  # noqa: E731
    ema = flag(t.get("ema_above"))
    rr = t.get("rr")
    tol = abs(float(t["entry_price"])) * STOP_MATCH_PCT / 100.0
    stop0 = t["stop_used"] if t.get("stop_initial") is None else t["stop_initial"]
    tp0 = t["take_profit"] if t.get("tp_initial") is None else t["tp_initial"]
    near = lambda ref: None if ref is None else abs(float(stop0) - float(ref)) <= tol  # noqa: E731
    mid, atr = near(t.get("stop_suggested")), near(t.get("stop_atr"))
    hour = to_local(int(t["entry_ms"]) + config.interval_ms(BASE_INTERVAL)).hour
    zone_iv = t.get("zone_interval")
    return {
        "ema_above": ema,
        "price_in_zone": flag(t.get("price_in_zone")),
        "rsi_dipped": flag(t.get("rsi_dipped")),
        "close_above_prev_high": flag(t.get("close_above_prev_high")),
        "rr_ok": None if rr is None else float(rr) >= RR_WARN_BELOW,
        "stop_middle": mid,
        "stop_atr": atr,
        "stop_own": None if mid is None else not (mid or bool(atr)),
        "zone_4h": None if not zone_iv else zone_iv == "4h",
        "morning": hour < 12,
        "afternoon": 12 <= hour < 18,
        "evening": hour >= 18,
        "stop_moved": float(stop0) != float(t["stop_used"]),
        "tp_moved": float(tp0) != float(t["take_profit"]),
    }


def trade_changes(t: dict) -> list[dict]:
    """The saved moves of the stop / take-profit (oldest first); [] without any."""
    raw = t.get("changes")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []
MAX_PRICE = 1e12
EXIT_STOP, EXIT_TP, EXIT_EARLY = "stop", "take-profit", "early exit"
# how the alternative trades end when neither level is hit: the close of the 24:00 candle, or of the
# last candle there is; Alt 2 takes its target at ALT_RATIO times the risk above the entry
EXIT_EOD, EXIT_DATA_END = "end of day", "end of data"
ALT_RATIO = 2.0
ALT_KEYS = ("alt1", "alt2")
AC_KEYS = ("A", "C")                # the two exits a sell chooses from (see DayEngine.sell)
AC_CHOICES = {"A": ("A",), "C": ("C",), "both": AC_KEYS}
HOUR_MS = 3_600_000
EXIT_REASONS: tuple[tuple[str, str], ...] = (
    ("stalls", "Price stalls at resistance"),
    ("setup_broken", "Setup broken (a 15-min candle closed below the support zone)"),
    ("adverse_move", "Sudden strong move against the trade"),
    ("news", "News or an event"),
    ("no_overnight", "Don't want to hold overnight"),
    ("other", "Other"),
)
EXIT_REASON_LABELS = dict(EXIT_REASONS)
# The review of a closed trade, asked at 24:00 of the exit day: (key, column, label). Every mark
# starts as False; "over_target" is suggested from the candles after the exit (see _public_trade).
REVIEW_QUESTIONS: tuple[tuple[str, str, str], ...] = (
    ("stop_tight", "review_stop_tight", "The stop was too tight"),
    ("over_target", "review_over_target", "The price later went over the target (out too early)"),
    ("stop_saved", "review_stop_saved", "The stop saved me"),
    ("stop_moved_up", "review_stop_moved_up", "I moved the stop too far up"),
)
WHATIF_FIELDS = ("whatif_n", "whatif_high", "whatif_low", "whatif_above_exit_pct", "whatif_below_exit_pct",
                 "whatif_high_vs_entry_pct", "whatif_low_vs_entry_pct")


# ------------------------------------------------------------------ local clock
def to_local(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TZ)


def local_ms(d: date, hour: int = 0) -> int:
    """Milliseconds of `hour`:00 local time on the date `d` (daylight saving handled by the zone)."""
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=TZ).timestamp() * 1000)


def local_str(ms: int | None) -> str | None:
    return None if ms is None else to_local(ms).strftime("%Y-%m-%d %H:%M")


def trading_day(close_ms: int) -> date:
    """The trading day a candle closing at `close_ms` belongs to: the date of the close, where the
    close at 00:00 (= 24:00 of the day before) already counts for the new day."""
    loc = to_local(close_ms)
    return loc.date() if (loc.hour, loc.minute, loc.second, loc.microsecond) >= (DAY_START_HOUR, 0, 0, 0) else loc.date() - timedelta(days=1)


def day_bounds(d: date) -> tuple[int, int]:
    """(00:00, 24:00) of the trading day `d` in ms; the second is also 00:00 of the next day."""
    return local_ms(d, DAY_START_HOUR), local_ms(d + timedelta(days=1), 0)


def alt_window_end(entry_ms: int) -> int:
    """24:00 (ms) at which the alternative trades of a buy at the candle opening at `entry_ms` close
    at the latest: the 24:00 of the entry's trading day (a buy on the candle closing at 00:00 belongs
    to the new day, so it runs to that day's 24:00)."""
    close = entry_ms + config.interval_ms(BASE_INTERVAL)
    d = trading_day(close)
    _, end = day_bounds(d)
    if close >= end:
        _, end = day_bounds(d + timedelta(days=1))
    return end


def picture_frame(entry_ms: int, exit_ms: int | None) -> dict | None:
    """The time span (unix seconds, candle open times like the chart's) a trade's picture shows:
    from the candle closing at 00:00 of the entry's trading day to the candle closing at 24:00 of
    the exit's trading day, so an overnight trade shows every day it was held. None while the trade
    is open."""
    if exit_ms is None:
        return None
    base = config.interval_ms(BASE_INTERVAL)
    start, _ = day_bounds(trading_day(entry_ms + base))
    _, end = day_bounds(trading_day(exit_ms + base))
    return {"from_s": (start - base) // 1000, "to_s": max(exit_ms, end - base) // 1000}


# ---------------------------------------------------------------- pure numbers
def position_size(risk_amount: float, entry: float, stop: float, fee_rate: float, cash: float) -> dict:
    """coins = risk / (entry - stop), capped to what the cash allows (position value + buy fee)."""
    if not (math.isfinite(entry) and entry > 0 and math.isfinite(stop) and stop > 0):
        raise Invalid("entry and stop must be positive prices")
    if stop >= entry:
        raise Invalid("The stop must be below the entry price.")
    if not (math.isfinite(cash) and cash > 0):
        raise Invalid("No cash left to open a trade.")
    coins = risk_amount / (entry - stop)
    capped = False
    if coins * entry * (1.0 + fee_rate) > cash + 1e-9:
        coins = cash / (entry * (1.0 + fee_rate))
        capped = True
    position_value = coins * entry
    return {
        "coins": coins,
        "position_value": position_value,
        "buy_fee": position_value * fee_rate,
        "risk_usdt": coins * (entry - stop),
        "capped": capped,
    }


def reward_to_risk(entry: float, stop: float, take_profit: float) -> float:
    return (take_profit - entry) / (entry - stop)


def hold_hours(entry_ms: int, exit_ms: int | None) -> float | None:
    """Hours between the fill (close of the entry candle) and the exit candle's close; None while open."""
    return None if exit_ms is None else (exit_ms - entry_ms) / HOUR_MS


def trade_result(entry: float, exit_price: float, coins: float, position_value: float, buy_fee: float,
                 fee_rate: float, risk_amount: float) -> dict:
    """P&L after both fees, as a percentage of the position value, and in R (P&L / risk_amount)."""
    sell_value = coins * exit_price
    sell_fee = sell_value * fee_rate
    pnl = sell_value - position_value - buy_fee - sell_fee
    return {
        "sell_value": sell_value,
        "sell_fee": sell_fee,
        "fees": buy_fee + sell_fee,
        "pnl": pnl,
        "pnl_pct": pnl / position_value * 100.0 if position_value else None,
        "r_multiple": pnl / risk_amount if risk_amount else None,
    }


def alts_public(t: dict) -> dict | None:
    """The stored alternatives of a trade for the API: exit times in local clock, the hours held and
    the difference to the manual result (None while the manual trade is open)."""
    raw = t.get("alts")
    if not raw:
        return None
    a = json.loads(raw) if isinstance(raw, str) else dict(raw)
    out = {"end_ms": a["end_ms"], "end_local": local_str(a["end_ms"])}
    for k in ALT_KEYS:
        x = dict(a[k])
        x["exit_local"] = local_str(x["exit_ms"] + config.interval_ms(BASE_INTERVAL))
        x["hold_hours"] = hold_hours(t["entry_ms"], x["exit_ms"])
        x["diff"] = (x["pnl"] - t["pnl"]) if t.get("status") == "closed" and t.get("pnl") is not None else None
        out[k] = x
    return out


def ac_legs(t: dict) -> dict[str, dict | None]:
    """Exits A and C of a trade: {"A": leg or None, "C": leg or None}; None = still open (also for
    trades from before the exits existed, whose column is NULL)."""
    raw = t.get("exits_ac")
    data = (json.loads(raw) if isinstance(raw, str) else dict(raw)) if raw else {}
    return {k: data.get(k) for k in AC_KEYS}


def ac_public(t: dict) -> dict | None:
    """Exits A and C for the API: per exit "open", or the closed leg with its local time, hours held,
    reason label and the difference to the real result (once the trade is closed). None for a trade
    closed before the exits existed (nothing was recorded)."""
    if not t.get("exits_ac") and t.get("status") == "closed":
        return None
    out: dict[str, Any] = {}
    for k, leg in ac_legs(t).items():
        if leg is None:
            out[k] = {"status": "open"}
            continue
        x = dict(leg, status="closed")
        x["exit_local"] = local_str(x["exit_ms"] + config.interval_ms(BASE_INTERVAL))
        x["hold_hours"] = hold_hours(t["entry_ms"], x["exit_ms"])
        x["reason_label"] = EXIT_REASON_LABELS.get(x.get("reason") or "")
        x["diff"] = (x["pnl"] - t["pnl"]) if t.get("status") == "closed" and t.get("pnl") is not None else None
        out[k] = x
    return out


def what_if(entry: float, exit_price: float, highest_high: float, lowest_low: float) -> dict:
    """The four "what if" percentages, rounded to 2 decimals."""
    pct = lambda a, b: round((a - b) / b * 100.0, 2)  # noqa: E731
    return {
        "whatif_above_exit_pct": pct(highest_high, exit_price),
        "whatif_below_exit_pct": pct(lowest_low, exit_price),
        "whatif_high_vs_entry_pct": pct(highest_high, entry),
        "whatif_low_vs_entry_pct": pct(lowest_low, entry),
    }


# -------------------------------------------------------------------- engine
class DayEngine:
    """Day-trading sessions on top of the replay engine (shared sessions, data, lock and pictures)."""

    def __init__(self, replay: ReplayEngine, store: DayStore) -> None:
        self.replay = replay
        self.store = store
        self.candles = replay.candles
        self.sessions = replay.sessions
        self._lock = replay._lock  # one lock for both engines: they write the same session rows
        self._signals: dict[tuple, list[dict]] = {}   # jump-strategy signals per session, strategy and params
        with store.tx() as conn:
            store.upgrade_risk(conn, RISK_PCT)   # sessions made under an older risk rule keep their risk amount

    # ---------------------------------------------------------------- sessions
    def create_session(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int | None = None,
        start_balance: float = DAY_START_BALANCE,
        name: str | None = None,
    ) -> dict:
        s = self.replay.create_session(
            symbol, start_ms, end_ms, base_interval=BASE_INTERVAL, start_balance=start_balance,
            fee_rate=config.DEFAULT_FEE_RATE, name=name or self._default_name(symbol, start_ms), mode="day",
        )
        now = _now_iso()
        with self.store.tx() as conn:
            self.store.create_day_session(
                conn, session_id=s["id"], timezone=TIMEZONE, risk_pct=RISK_PCT,
                risk_amount=float(start_balance) * RISK_PCT / 100.0, phase="trading",
                start_cursor_ms=None, last_exit_id=None, created_at=now, updated_at=now,
            )
        return self.state(s["id"])

    @staticmethod
    def _default_name(symbol: str, start_ms: int) -> str:
        return f"Day {symbol} from {_iso(start_ms)[:10]}"

    def prepare(self, session_id: str, progress=None) -> dict:
        """Download the data (shared logic), then put the cursor on the first day's start (_first_day_cursor). Safe to re-run."""
        self._day_row(session_id)
        self.replay.prepare(session_id, progress=progress)
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            if ds["start_cursor_ms"] is None:
                cursor = self._first_day_cursor(s, s["data_start_ms"])
                self._set_cursor(s, cursor)
                with self.store.tx() as conn:
                    self.store.update_day_session(conn, session_id, start_cursor_ms=cursor, updated_at=_now_iso())
        return self.state(session_id)

    def delete_session(self, session_id: str) -> None:
        self._day_row(session_id)
        self.replay.delete_session(session_id)  # the day tables cascade

    def reset(self, session_id: str) -> dict:
        """Back to the first day's start with the starting capital: trades, trend checks, zones and pictures gone."""
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            self.replay._wipe(s)
            s["cash"] = s["start_balance"]
            now = _now_iso()
            with self.store.tx() as conn:
                self.store.delete_auto_trades(conn, session_id)
                self.store.delete_trades(conn, session_id)
                self.store.delete_checks(conn, session_id)
                self.store.delete_zones(conn, session_id)
                self.store.update_day_session(conn, session_id, phase="trading", last_exit_id=None, back_floor_ms=None, updated_at=now)
            cursor = ds["start_cursor_ms"]
            if cursor is None or not (s["data_start_ms"] <= cursor <= s["data_end_ms"]):
                cursor = self._first_day_cursor(s, s["data_start_ms"])
            self._set_cursor(s, cursor)
        return self.state(session_id)

    # ------------------------------------------------------------------- state
    def state(self, session_id: str) -> dict:
        ds = self._day_row(session_id)
        st = self.replay.state(session_id)
        if st["status"] == "ready":
            self._settle_auto(st)   # the twins catch up with the candles up to the cursor
        st["day"] = self._day_block(st, ds)
        st["account"] = self._account(st, st["day"])
        return st

    def _day_block(self, st: dict, ds: dict) -> dict:
        out: dict[str, Any] = {
            "timezone": TIMEZONE, "risk_pct": RISK_PCT, "risk_amount": ds["risk_amount"], "phase": ds["phase"],
            "base_interval": BASE_INTERVAL, "view_intervals": list(VIEW_INTERVALS), "zone_intervals": list(ZONE_INTERVALS),
            "stop_atr_mult": STOP_ATR_MULT, "atr_period": ATR_PERIOD, "rr_warn_below": RR_WARN_BELOW, "rsi_dip_max": RSI_DIP_MAX,
            "exit_reasons": [{"key": k, "label": v} for k, v in EXIT_REASONS],
            "date": None, "trade": None, "last_exit": None, "entries_allowed": False, "block_reason": None,
        }
        if st["status"] != "ready":
            return out
        sid = st["id"]
        close = st["cursor_ms"] + self._base_ms
        d = trading_day(close)
        start, end = day_bounds(d)
        check = self._trend_check(st, d, start, end)
        with self.store.tx() as conn:
            trade = self.store.open_trade(conn, sid)
            traded = self.store.trades_on_day(conn, sid, d.isoformat())
            last_exit = self.store.get_trade(conn, sid, ds["last_exit_id"]) if ds["phase"] == "exited" and ds["last_exit_id"] else None
            pending = self._pending_review(st, conn)
            if pending is not None:
                self._fill_alternatives(st, conn, pending)   # its 24:00 has passed: the alternatives can be settled
        # just closed before its 24:00: end the day there or keep trading
        exit_choice = last_exit is not None and close < self._review_end(last_exit)
        allowed, reason = self._entry_gate(st, ds, close, end, trade, traded, pending)
        loc = to_local(close)
        out.update({
            "date": d.isoformat(),
            "day_start_ms": start, "day_end_ms": end,
            "day_start_local": local_str(start), "day_end_local": local_str(end),
            "local_time": local_str(close), "local_iso": loc.isoformat(),
            "utc_offset_min": int((loc.utcoffset() or timedelta()).total_seconds() // 60),
            "check": self._public_check(check),
            "entries_allowed": allowed, "block_reason": reason,
            "trade": self._public_trade(trade, st["fee_rate"], st["price"]) if trade else None,
            "auto": self._auto_of(sid, trade["id"], trade) if trade else None,   # the open trade's automatic twin
            "traded_today": traded,
            "last_exit": self._public_trade(last_exit, st["fee_rate"]) if last_exit else None,
            # the trade whose review is still open: nothing moves on until it is answered
            "review_pending": self._public_trade(pending, st["fee_rate"]) if pending else None,
            "review_questions": [{"key": k, "label": label} for k, _, label in REVIEW_QUESTIONS],
            "leaving_next": self._leaving_next(st, ds, trade),
            "can_skip": ds["phase"] == "trading" and trade is None and not st["finished"],
            "can_sell": ds["phase"] == "trading" and trade is not None,
            # which of exits A / C a sell may still close (both while neither is closed)
            "open_exits": [k for k, leg in ac_legs(trade).items() if leg is None] if trade is not None else [],
            "can_continue": ds["phase"] == "exited" and pending is None and not exit_choice,
            "exit_choice": exit_choice and pending is None,
            "can_step_back": pending is None and self._step_back_target(st, ds) is not None,
        })
        return out

    def _leaving_next(self, st: dict, ds: dict, trade: dict | None) -> bool:
        """True when the next step leaves this trading day: the next candle closes at or after 00:00
        of the next one. The frontend takes the day's picture before that step."""
        if st["finished"]:
            return False
        base = self._base_ms
        nxt = self.candles.nth_at_or_after(st["symbol"], BASE_INTERVAL, st["cursor_ms"] + base, 1)
        if nxt is None or nxt > st["data_end_ms"]:
            return False
        return trading_day(nxt + base) != trading_day(st["cursor_ms"] + base)

    @staticmethod
    def _entry_gate(st: dict, ds: dict, close: int, end: int, trade: dict | None, traded: int,
                    pending: dict | None = None) -> tuple[bool, str | None]:
        if st["finished"]:
            return False, "The replay has reached its end."
        if pending is not None:
            return False, f"Answer the review questions for trade #{pending['seq']} first."
        if trade is not None:
            return False, "A trade is open."
        # Nothing else blocks: not the time of day, not a trade already made today, not a trade just closed,
        # not the trend check. The conditions are saved with the trade and evaluated later.
        return True, None

    def _account(self, st: dict, day: dict) -> dict:
        with self.store.tx() as conn:
            trades = self.store.list_trades(conn, st["id"])
        closed = [t for t in trades if t["status"] == "closed"]
        trade = day.get("trade")
        market_value = trade["market_value"] if trade and trade.get("market_value") is not None else 0.0
        equity = st["cash"] + market_value
        return {
            "cash": st["cash"],
            "invested": trade["position_value"] if trade else 0.0,
            "market_value": market_value,
            "unrealized_pnl": trade["unrealized_pnl"] if trade and trade.get("unrealized_pnl") is not None else 0.0,
            "realized_pnl": sum(t["pnl"] for t in closed),
            "fees_paid": sum(t["buy_fee"] for t in trades) + sum(t["sell_fee"] or 0.0 for t in closed),
            "equity": equity,
            "total_return": equity - st["start_balance"],
            "total_return_pct": (equity / st["start_balance"] - 1.0) * 100.0,
            "open_trades": 1 if trade else 0,
            "closed_trades": len(closed),
            "wins": sum(1 for t in closed if t["pnl"] > 0),
            "losses": sum(1 for t in closed if t["pnl"] <= 0),
            "r_total": sum(t["r_multiple"] or 0.0 for t in closed),
        }

    # ------------------------------------------------------------- trend check
    def _trend_check(self, s: dict, d: date, start: int, end: int) -> dict:
        """The stored check of trading day `d`, created on first sight from the completed daily candles."""
        with self.store.tx() as conn:
            row = self.store.get_check(conn, s["id"], d.isoformat())
        if row is not None:
            return row
        ema = self._daily_ema(s)
        try:
            with self.store.tx() as conn:
                return self.store.add_check(conn, session_id=s["id"], day_date=d.isoformat(), day_start_ms=start, day_end_ms=end,
                                            updated_at=_now_iso(), **ema)
        except sqlite3.IntegrityError:  # created meanwhile by a parallel request
            with self.store.tx() as conn:
                return self.store.get_check(conn, s["id"], d.isoformat())  # type: ignore[return-value]

    def _daily_ema(self, s: dict) -> dict:
        """Close of the last completed daily candle at the cursor and the EMA 200 over the completed
        days (None with fewer than 200 of them), plus whether the close lies above it."""
        day_ms = config.interval_ms(EMA_LONG_INTERVAL)
        cur_close = s["cursor_ms"] + self._base_ms
        daily = self.replay._candles_at_cursor(s, EMA_LONG_INTERVAL, EMA_LONG_WARMUP_BARS + 1)
        done = daily[(daily["time"].astype("int64") * 1000 + day_ms) <= cur_close] if len(daily) else daily
        if len(done) == 0:
            return {"daily_close": None, "daily_close_ms": None, "ema200": None, "ema_above": None}
        close = float(done["close"].iloc[-1])
        close_ms = int(done["time"].iloc[-1]) * 1000
        ema = None
        if len(done) >= EMA_LONG_PERIOD:
            v = float(core.ema(done["close"], EMA_LONG_PERIOD).iloc[-1])
            ema = v if math.isfinite(v) else None
        return {"daily_close": close, "daily_close_ms": close_ms, "ema200": ema, "ema_above": None if ema is None else int(close > ema)}

    @staticmethod
    def _public_check(c: dict) -> dict:
        flag = lambda v: None if v is None else bool(v)  # noqa: E731
        return {
            "date": c["day_date"],
            "daily_close": c["daily_close"],
            "daily_close_ms": c["daily_close_ms"],
            "daily_close_iso": _iso(c["daily_close_ms"]) if c["daily_close_ms"] is not None else None,
            "ema200": c["ema200"],
            "ema_above": flag(c["ema_above"]),
            "notes": c.get("notes"),
            "has_image": bool(c.get("image_path")),
            "image_local": local_str(c["image_ms"] + config.interval_ms(BASE_INTERVAL)) if c.get("image_ms") is not None else None,
            "left_how": c.get("left_how"),
            "left_label": LEFT_LABELS.get(c["left_how"]) if c.get("left_how") else None,
            "left_local": local_str(c["left_ms"] + config.interval_ms(BASE_INTERVAL)) if c.get("left_ms") is not None else None,
            "updated_at": c["updated_at"],
        }

    # ------------------------------------------------------------------- zones
    def zones(self, session_id: str) -> list[dict]:
        self._day_row(session_id)
        with self.store.tx() as conn:
            return [self._public_zone(z) for z in self.store.list_zones(conn, session_id)]

    def add_zone(self, session_id: str, interval: str, p1: float, p2: float) -> dict:
        self._day_row(session_id)
        upper, lower = self._zone_edges(interval, p1, p2)
        now = _now_iso()
        with self._lock, self.store.tx() as conn:
            z = self.store.add_zone(conn, session_id=session_id, interval=interval, upper=upper, lower=lower, created_at=now, updated_at=now)
        return self._public_zone(z)

    def update_zone(self, session_id: str, zone_id: int, p1: float, p2: float) -> dict:
        with self._lock, self.store.tx() as conn:
            z = self.store.get_zone(conn, session_id, int(zone_id))
            if z is None:
                raise NotFound(f"zone {zone_id} not found in session {session_id}")
            upper, lower = self._zone_edges(z["interval"], p1, p2)
            # a resized signal zone becomes the trader's own: it is never removed by itself any more
            self.store.update_zone(conn, z["id"], upper=upper, lower=lower, source=None, updated_at=_now_iso())
            z = self.store.get_zone(conn, session_id, z["id"])
        return self._public_zone(z)  # type: ignore[arg-type]

    def delete_zone(self, session_id: str, zone_id: int) -> None:
        with self._lock, self.store.tx() as conn:
            if not self.store.delete_zone(conn, session_id, int(zone_id)):
                raise NotFound(f"zone {zone_id} not found in session {session_id}")

    @staticmethod
    def _zone_edges(interval: str, p1: float, p2: float) -> tuple[float, float]:
        if interval not in ZONE_INTERVALS:
            raise Invalid(f"zones are drawn on the {' or '.join(ZONE_INTERVALS)} chart")
        for p in (p1, p2):
            if not (isinstance(p, (int, float)) and math.isfinite(p) and 0 < p <= MAX_PRICE):
                raise Invalid("zone edges must be positive prices")
        upper, lower = max(float(p1), float(p2)), min(float(p1), float(p2))
        if upper == lower:
            raise Invalid("a zone needs two different prices")
        return upper, lower

    @staticmethod
    def _public_zone(z: dict) -> dict:
        out = dict(z)
        out["mid"] = (z["upper"] + z["lower"]) / 2.0
        out["height_pct"] = (z["upper"] / z["lower"] - 1.0) * 100.0
        return out

    def _zone(self, session_id: str, zone_id: int | None) -> dict:
        if zone_id is None:
            raise Invalid("Select a support zone.")
        with self.store.tx() as conn:
            z = self.store.get_zone(conn, session_id, int(zone_id))
        if z is None:
            raise NotFound(f"zone {zone_id} not found in session {session_id}")
        return z

    # -------------------------------------------------------------------- plan
    def plan(self, session_id: str, zone_id: int | None, stop: float | None = None, take_profit: float | None = None,
             ratio: float | None = None) -> dict:
        """Everything the "New trade" panel shows before the Buy: entry price (the current close),
        the default stop (the middle of the zone) and the ATR stop (zone lower edge - 0.5 x ATR),
        the stop in use, the take-profit (given, or entry + ratio x risk distance when a ratio is
        given instead), position size, risk, reward-to-risk, the rule readings, and the errors that
        forbid the buy / the warnings that only caution."""
        st = self.state(session_id)
        if st["status"] != "ready":
            raise Conflict(f"session is {st['status']}")
        s = st
        day = st["day"]
        price = st["price"]
        zone = self._zone(session_id, zone_id)
        df = self.replay._candles_at_cursor(s, BASE_INTERVAL, max(ATR_WARMUP_BARS, RSI_WARMUP_BARS))
        atr = self._atr_from_df(df)
        rsi_series = core.rsi(df["close"], 14) if len(df) > 14 else None
        rsi = None
        rsi_day_min = None
        rsi_1h = self.replay._rsi_at_cursor(s, "1h")   # the 1-hour chart's RSI 14, forming candle included
        if rsi_series is not None:
            v = float(rsi_series.iloc[-1])
            rsi = v if math.isfinite(v) else None
            closes = df["time"].astype("int64") * 1000 + self._base_ms
            today = rsi_series[(closes >= day["day_start_ms"]) & (closes <= s["cursor_ms"] + self._base_ms)].dropna()
            rsi_day_min = float(today.min()) if len(today) else None
        prev_high = float(df["high"].iloc[-2]) if len(df) >= 2 else None

        errors: list[str] = []
        warnings: list[str] = []
        blocked = None if day["entries_allowed"] else day["block_reason"]
        suggested = (zone["upper"] + zone["lower"]) / 2.0            # the default: the middle of the zone
        stop_atr = zone["lower"] - STOP_ATR_MULT * atr if atr is not None else None
        stop_used = float(stop) if stop is not None else suggested
        stop_ok = False
        if stop_used is None:
            errors.append("Set the stop.")
        elif not (math.isfinite(stop_used) and 0 < stop_used <= MAX_PRICE):
            errors.append("The stop must be a positive price.")
        elif stop_used >= price:
            errors.append("The stop must be below the entry price.")
        else:
            stop_ok = True
        size: dict[str, Any] = {"coins": None, "position_value": None, "buy_fee": None, "risk_usdt": None, "capped": False}
        if stop_ok:
            try:
                size = position_size(float(s["day"]["risk_amount"]), price, stop_used, s["fee_rate"], s["cash"])
            except Invalid as exc:
                errors.append(str(exc))
            if size["capped"]:
                warnings.append(f"Not enough cash for the full size: the position is capped and the risk is below {RISK_PCT:g} %.")
        rr = None
        tp = None
        if ratio is not None and not (math.isfinite(float(ratio)) and 0 < float(ratio) <= RATIO_MAX):
            errors.append(f"The ratio must be between 0 and {RATIO_MAX:.0f}.")
            ratio = None
        if take_profit is None and ratio is not None and stop_ok:
            take_profit = price + float(ratio) * (price - stop_used)   # target from the chosen reward-to-risk
        if take_profit is not None:
            tp = float(take_profit)
            if not (math.isfinite(tp) and 0 < tp <= MAX_PRICE):
                errors.append("The take-profit must be a positive price.")
                tp = None
            elif tp <= price:
                errors.append("The take-profit must be above the entry price.")
            elif stop_ok:
                rr = reward_to_risk(price, stop_used, tp)
                if rr < RR_WARN_BELOW:
                    warnings.append(f"Reward-to-risk {rr:.2f} is below {RR_WARN_BELOW:.1f}.")
        ema_above = day["check"]["ema_above"]
        if ema_above is None:
            warnings.append("EMA 200 not available: the trend is saved as unknown with the trade.")
        elif not ema_above:
            warnings.append("The daily close is below the EMA 200 (saved with the trade).")
        signals = {
            "ema_above": ema_above,
            "price_in_zone": zone["lower"] <= price <= zone["upper"],
            "rsi": rsi,
            "rsi_1h": rsi_1h,
            "rsi_day_min": rsi_day_min,
            "rsi_dipped": None if rsi_day_min is None else rsi_day_min <= RSI_DIP_MAX,
            "prev_high": prev_high,
            "close_above_prev_high": None if prev_high is None else price > prev_high,
        }
        return {
            "entry_price": price,
            "entry_ms": s["cursor_ms"],
            "entry_local": local_str(s["cursor_ms"] + self._base_ms),
            "atr": atr, "atr_period": ATR_PERIOD, "stop_atr_mult": STOP_ATR_MULT,
            "zone": self._public_zone(zone),
            "stop_suggested": suggested, "stop_atr": stop_atr, "stop_used": stop_used,
            "take_profit": tp, "rr": rr, "rr_warn_below": RR_WARN_BELOW, "ratio": None if ratio is None else float(ratio),
            **size,
            "risk_amount": s["day"]["risk_amount"], "cash": s["cash"], "fee_rate": s["fee_rate"],
            "signals": signals,
            "blocked": blocked, "errors": errors, "warnings": warnings,
            "can_buy": blocked is None and not errors and tp is not None,
        }

    # ------------------------------------------------------------------- trade
    def buy(self, session_id: str, zone_id: int | None, take_profit: float | None, stop: float | None = None,
            notes: str | None = None, ratio: float | None = None, confidence: int | None = None) -> dict:
        """Open the day's trade at the close of the current 15-minute candle.

        `confidence` is the trader's own score for the trade, a whole number from 0 to 10; it is
        saved once with the trade and cannot be changed later.
        """
        notes = self._note(notes, "notes")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 10:
                raise Invalid("Confidence must be a whole number from 0 to 10.")
        with self._lock:
            s = self._ready(session_id)
            p = self.plan(session_id, zone_id, stop, take_profit, ratio)
            if p["blocked"]:
                raise Conflict(p["blocked"])
            if p["errors"]:
                raise Invalid(" ".join(p["errors"]))
            if p["take_profit"] is None:   # neither a price nor a ratio that gave one
                raise Invalid("Set a take-profit or a ratio.")
            st = self.state(session_id)
            day = st["day"]
            check = day["check"]
            sg = p["signals"]
            now = _now_iso()
            cash_after = s["cash"] - p["position_value"] - p["buy_fee"]
            if abs(cash_after) < 1e-6:  # a capped trade uses the whole cash: no "-0.00"
                cash_after = 0.0
            with self.store.tx() as conn:
                t = self.store.add_trade(
                    conn, session_id=session_id, status="open", day_date=day["date"],
                    ema_above=None if check["ema_above"] is None else int(check["ema_above"]),
                    entry_ms=s["cursor_ms"], entry_price=p["entry_price"], entry_rsi=p["signals"]["rsi"], entry_rsi_1h=sg["rsi_1h"], entry_atr=p["atr"],
                    zone_id=p["zone"]["id"], zone_upper=p["zone"]["upper"], zone_lower=p["zone"]["lower"], zone_interval=p["zone"]["interval"],
                    price_in_zone=int(bool(sg["price_in_zone"])), rsi_dipped=None if sg["rsi_dipped"] is None else int(sg["rsi_dipped"]),
                    rsi_day_min=sg["rsi_day_min"], prev_high=sg["prev_high"],
                    close_above_prev_high=None if sg["close_above_prev_high"] is None else int(sg["close_above_prev_high"]),
                    stop_suggested=p["stop_suggested"], stop_atr=p["stop_atr"], stop_used=p["stop_used"], take_profit=p["take_profit"], rr=p["rr"],
                    stop_initial=p["stop_used"], tp_initial=p["take_profit"], changes=None,
                    coins=p["coins"], position_value=p["position_value"], risk_usdt=p["risk_usdt"], risk_amount=p["risk_amount"],
                    capped=int(bool(p["capped"])), buy_fee=p["buy_fee"], cash_after_entry=cash_after, overnight=0,
                    notes=notes, confidence=confidence, created_at=now, updated_at=now,
                )
                # the automatic-exit twin: same entry and size, the original stop and target, hands off
                self.store.add_auto_trade(
                    conn, session_id=session_id, trade_id=t["id"], seq=t["seq"], status="open", day_date=day["date"],
                    entry_ms=s["cursor_ms"], entry_price=p["entry_price"], stop=p["stop_used"], take_profit=p["take_profit"],
                    coins=p["coins"], position_value=p["position_value"], buy_fee=p["buy_fee"], risk_amount=p["risk_amount"],
                    created_at=now, updated_at=now,
                )
                if self.store.get_day_session(conn, session_id)["phase"] == "exited":
                    # a new trade right after an exit: the day goes on, no jump to the next 00:00
                    self.store.update_day_session(conn, session_id, phase="trading", last_exit_id=None, updated_at=now)
            with self.sessions.tx() as conn:
                self.sessions.update_session(conn, session_id, cash=cash_after, updated_at=now)
        st = self.state(session_id)
        return {"trade": self._public_trade(t, s["fee_rate"], st["price"]), "state": st}

    def move_trade(self, session_id: str, stop: float | None = None, take_profit: float | None = None) -> dict:
        """Move the stop and / or the take-profit of the open trade. The new stop must stay below the
        current price and the new target above it (else it would fire on the spot). Every move is
        appended to the trade's change list, with the candle it was made on; the risk (R) and the
        conditions at the buy keep the levels of the buy."""
        if stop is None and take_profit is None:
            raise Invalid("Give a stop, a take-profit or both.")
        for name, v in (("stop", stop), ("take-profit", take_profit)):
            if v is not None and not (math.isfinite(float(v)) and 0 < float(v) <= MAX_PRICE):
                raise Invalid(f"The {name} must be a positive price.")
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            if ds["phase"] == "exited":
                raise Conflict("The trade has already closed. Continue to the next day.")
            with self.store.tx() as conn:
                trade = self.store.open_trade(conn, session_id)
            if trade is None:
                raise Conflict("No trade is open.")
            price = self.replay._price(s)
            new_stop = trade["stop_used"] if stop is None else float(stop)
            new_tp = trade["take_profit"] if take_profit is None else float(take_profit)
            if new_stop >= price:
                raise Invalid(f"The stop must be below the current price ({price:g}).")
            if new_tp <= price:
                raise Invalid(f"The take-profit must be above the current price ({price:g}).")
            changes = trade_changes(trade)
            at_local = local_str(s["cursor_ms"] + self._base_ms)
            fields: dict = {}
            for field, col in MOVE_FIELDS.items():
                new = new_stop if field == "stop" else new_tp
                if not math.isclose(new, trade[col], rel_tol=1e-9, abs_tol=0.0):   # float noise from a rounded field is no move
                    changes.append({"at_ms": s["cursor_ms"], "at_local": at_local, "price": price,
                                    "field": field, "from": trade[col], "to": new})
                    fields[col] = new
            if fields:
                fields["changes"] = json.dumps(changes)
                fields["updated_at"] = _now_iso()
                if trade.get("stop_initial") is None:   # a trade from before this feature: its first move fixes the initial levels
                    fields["stop_initial"], fields["tp_initial"] = trade["stop_used"], trade["take_profit"]
                with self.store.tx() as conn:
                    self.store.update_trade(conn, trade["id"], **fields)
                trade.update(fields)
        st = self.state(session_id)
        return {"trade": self._public_trade(trade, s["fee_rate"], st["price"]), "state": st}

    def sell(self, session_id: str, reason: str, note: str | None = None, exits: str = "both") -> dict:
        """Early exit by hand at the close of the current candle; a reason from EXIT_REASONS is
        required, "other" needs a note. `exits` closes exit A, C or both (= whatever is still open):
        while one of them is still open the trade stays open (cash untouched, an "exit_leg" event);
        the sell that closes the last one closes the trade at this candle (the later exit is the real one)."""
        if exits not in AC_CHOICES:
            raise Invalid(f"exits must be one of {list(AC_CHOICES)}")
        if reason not in EXIT_REASON_LABELS:
            raise Invalid(f"reason must be one of {list(EXIT_REASON_LABELS)}")
        note = self._note(note, "note")
        if reason == "other" and not note:
            raise Invalid("Say what the reason is under Other.")
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            if ds["phase"] == "exited":
                raise Conflict("The trade has already closed. Continue to the next day.")
            with self.store.tx() as conn:
                trade = self.store.open_trade(conn, session_id)
            if trade is None:
                raise Conflict("No trade is open.")
            legs = ac_legs(trade)
            if exits != "both" and legs[exits] is not None:   # "both" closes whatever is still open
                raise Conflict(f"Exit {exits} is already closed.")
            price = self.replay._price(s)
            still_open = self._close_legs(s, trade, AC_CHOICES[exits], s["cursor_ms"], price, EXIT_EARLY, reason, note)
            if still_open:
                ev = {"kind": "exit_leg", "closed": [k for k in AC_CHOICES[exits] if legs[k] is None], "open": still_open,
                      "trade": self._public_trade(trade, s["fee_rate"], price)}
            else:
                ev = self._exit(s, trade, s["cursor_ms"], price, EXIT_EARLY, reason, note)
        st = self.state(session_id)
        st["events"] = [ev]
        return st

    def _close_legs(self, s: dict, trade: dict, keys: tuple[str, ...], exit_ms: int, exit_price: float,
                    exit_type: str, reason: str | None = None, note: str | None = None) -> list[str]:
        """Close exits `keys` (those still open) of `trade` at `exit_price`, each a whole-size result
        like the alternatives; saved at once. Returns the exits still open afterwards."""
        legs = ac_legs(trade)
        for k in keys:
            if legs[k] is not None:
                continue
            res = trade_result(trade["entry_price"], exit_price, trade["coins"], trade["position_value"], trade["buy_fee"],
                               s["fee_rate"], trade["risk_amount"])
            legs[k] = {"exit_ms": exit_ms, "exit_price": exit_price, "exit_type": exit_type, "reason": reason, "note": note,
                       "pnl": res["pnl"], "pnl_pct": res["pnl_pct"], "r_multiple": res["r_multiple"]}
        raw = json.dumps(legs)
        with self.store.tx() as conn:
            self.store.update_trade(conn, trade["id"], exits_ac=raw, updated_at=_now_iso())
        trade["exits_ac"] = raw
        return [k for k in AC_KEYS if legs[k] is None]

    def _exit(self, s: dict, trade: dict, exit_ms: int, exit_price: float, exit_type: str,
              reason: str | None = None, note: str | None = None) -> dict:
        res = trade_result(trade["entry_price"], exit_price, trade["coins"], trade["position_value"], trade["buy_fee"],
                           s["fee_rate"], trade["risk_amount"])
        cash_after = s["cash"] + res["sell_value"] - res["sell_fee"]
        now = _now_iso()
        fields = {
            "status": "closed", "exit_ms": exit_ms, "exit_price": exit_price, "exit_type": exit_type,
            "exit_reason": reason, "exit_note": note,
            "sell_fee": res["sell_fee"], "fees": res["fees"], "pnl": res["pnl"], "pnl_pct": res["pnl_pct"],
            "r_multiple": res["r_multiple"], "cash_after_exit": cash_after, "review_due": 1,
            **self._what_if(s, trade, exit_ms, exit_price),
            **self._excursion(s, trade, exit_ms),
            "updated_at": now,
        }
        with self.store.tx() as conn:
            self.store.update_trade(conn, trade["id"], **fields)
            self.store.update_day_session(conn, s["id"], phase="exited", last_exit_id=trade["id"], updated_at=now)
        with self.sessions.tx() as conn:
            self.sessions.update_session(conn, s["id"], cash=cash_after, updated_at=now)
        s["cash"] = cash_after
        trade.update(fields)
        # the user chooses: end the day (end_day, the jump to the next 00:00) or keep trading; the
        # exit candle always closes before its day's 24:00, so the choice is always offered
        choice = exit_ms + self._base_ms < self._review_end(trade)
        return {"kind": "exit", "exit_type": exit_type, "trade": self._public_trade(trade, s["fee_rate"]),
                "choice": choice, "jumped_to_ms": None, "jumped_to_local": None}

    def end_day(self, session_id: str) -> dict:
        """After an exit ("End the day"): jump to the candle closing at 24:00 of the exit's trading day.
        That candle is 00:00 of the next day, so the day is left ("Trade closed") and the review is
        asked on landing. Since the 00:00 day start this is the same jump as Continue (resume)."""
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            with self.store.tx() as conn:
                last = self.store.get_trade(conn, session_id, ds["last_exit_id"]) if ds["phase"] == "exited" and ds["last_exit_id"] else None
            if last is None:
                raise Conflict("No trade has just closed.")
            if s["cursor_ms"] >= s["data_end_ms"]:
                raise Conflict("The replay has reached its end.")
            ev = self._resume(s, ds)
        st = self.state(session_id)
        st["events"] = [ev]
        return st

    def keep_trading(self, session_id: str) -> dict:
        """After an exit ("Keep trading"): the day goes on from the exit candle as if no trade had
        happened; the review waits for 24:00 of the exit's trading day."""
        with self._lock:
            self._ready(session_id)
            ds = self._day_row(session_id)
            if ds["phase"] == "exited":
                with self.store.tx() as conn:
                    self.store.update_day_session(conn, session_id, phase="trading", last_exit_id=None, updated_at=_now_iso())
        return self.state(session_id)

    def _review_end(self, trade: dict) -> int:
        """24:00 (ms) of the exit's trading day: the review is asked once a candle closes there."""
        return day_bounds(trading_day(trade["exit_ms"] + self._base_ms))[1]

    def _pending_review(self, s: dict, conn: sqlite3.Connection) -> dict | None:
        """The closed trade whose review holds the replay: the oldest unanswered one whose 24:00
        the cursor has reached."""
        close = s["cursor_ms"] + self._base_ms
        return next((t for t in self.store.due_reviews(conn, s["id"]) if close >= self._review_end(t)), None)

    def _excursion(self, s: dict, trade: dict, exit_ms: int) -> dict:
        """MAE / MFE: the lowest low and the highest high of the 15-minute candles after the entry
        candle up to and including the exit candle, against the entry price (percent of the entry,
        and in R = multiples of the risk taken). The adverse side is at most 0, the favourable at
        least 0. All zero when the exit is in the entry candle itself."""
        entry = float(trade["entry_price"])
        df = self.candles.get_candles(s["symbol"], BASE_INTERVAL, trade["entry_ms"] + self._base_ms, exit_ms)
        if len(df) == 0:
            return {"mae_pct": 0.0, "mfe_pct": 0.0, "mae_r": 0.0, "mfe_r": 0.0}
        hi, lo = float(df["high"].max()), float(df["low"].min())
        risk = trade.get("risk_usdt")
        coins = float(trade["coins"])
        to_r = (lambda move: move * coins / risk) if risk else (lambda move: None)
        return {
            "mae_pct": min(0.0, (lo / entry - 1.0) * 100.0), "mfe_pct": max(0.0, (hi / entry - 1.0) * 100.0),
            "mae_r": None if not risk else min(0.0, to_r(lo - entry)), "mfe_r": None if not risk else max(0.0, to_r(hi - entry)),
        }

    def _alternatives(self, s: dict, trade: dict) -> dict | None:
        """The two hands-off alternatives of a trade, both closed at the latest with the 24:00 candle
        (alt_window_end): Alt 1 keeps the stop and the target set at the buy, Alt 2 keeps the stop and
        takes a target ALT_RATIO times the risk above the entry. On every candle after the entry
        candle a low at or below the stop exits at the stop (the stop wins when both levels are hit
        in one candle), a high at or above the target exits at the target; otherwise the close of the
        last candle ends it ("end of day", or "end of data" when the data stops first). None while
        the replay has not reached that last candle yet, so no alternative ever knows the future."""
        base = self._base_ms
        last, final = alt_window_end(trade["entry_ms"]) - base, EXIT_EOD    # open time of the 24:00 candle
        if s["data_end_ms"] < last:
            last, final = s["data_end_ms"], EXIT_DATA_END
        if s["cursor_ms"] < last:
            return None
        df = self.candles.get_candles(s["symbol"], BASE_INTERVAL, trade["entry_ms"] + base, last)
        if len(df) == 0:
            return None
        entry = float(trade["entry_price"])
        stop = float(trade["stop_used"] if trade.get("stop_initial") is None else trade["stop_initial"])
        tp1 = float(trade["take_profit"] if trade.get("tp_initial") is None else trade["tp_initial"])
        tp2 = entry + ALT_RATIO * (entry - stop)
        rows = list(zip(df["time"].astype("int64"), df["high"], df["low"], df["close"]))

        def run(tp: float) -> dict:
            for t_open, high, low, close in rows:
                if float(low) <= stop:
                    exit_ms, exit_price, exit_type = int(t_open) * 1000, stop, EXIT_STOP
                    break
                if float(high) >= tp:
                    exit_ms, exit_price, exit_type = int(t_open) * 1000, tp, EXIT_TP
                    break
            else:
                t_open, _, _, close = rows[-1]
                exit_ms, exit_price, exit_type = int(t_open) * 1000, float(close), final
            res = trade_result(entry, exit_price, trade["coins"], trade["position_value"], trade["buy_fee"], s["fee_rate"], trade["risk_amount"])
            return {"stop": stop, "take_profit": tp, "exit_ms": exit_ms, "exit_price": exit_price, "exit_type": exit_type,
                    "pnl": res["pnl"], "pnl_pct": res["pnl_pct"], "r_multiple": res["r_multiple"]}

        return {"end_ms": last + base, "alt1": run(tp1), "alt2": run(tp2)}

    def _fill_alternatives(self, s: dict, conn: sqlite3.Connection, trade: dict) -> None:
        """Store the alternatives of a trade that has none yet, when the replay allows it."""
        if trade.get("alts"):
            return
        a = self._alternatives(s, trade)
        if a is not None:
            raw = json.dumps(a)
            self.store.update_trade(conn, trade["id"], alts=raw)
            trade["alts"] = raw

    def _backfill(self, st: dict, conn: sqlite3.Connection, rows: list[dict]) -> None:
        """Numbers added after a trade was made: MAE / MFE of closed trades, the alternatives of
        every trade whose 24:00 the replay has passed."""
        for t in rows:
            if t["status"] == "closed" and t["exit_ms"] is not None and t.get("mae_pct") is None:
                f = self._excursion(st, t, t["exit_ms"])
                self.store.update_trade(conn, t["id"], **f)
                t.update(f)
            self._fill_alternatives(st, conn, t)

    def _what_if(self, s: dict, trade: dict, exit_ms: int, exit_price: float) -> dict:
        """Highest high and lowest low of the 15-minute candles after the exit candle until 24:00 of
        the exit's trading day (the window the review shows); all None when there are none, e.g.
        after an exit on the day's last candle."""
        _, end = day_bounds(trading_day(exit_ms + self._base_ms))
        df = self.candles.get_candles(s["symbol"], BASE_INTERVAL, exit_ms + self._base_ms, end - self._base_ms)
        if len(df) == 0:
            return {k: (0 if k == "whatif_n" else None) for k in WHATIF_FIELDS}
        hi, lo = float(df["high"].max()), float(df["low"].min())
        return {"whatif_n": int(len(df)), "whatif_high": hi, "whatif_low": lo, **what_if(trade["entry_price"], exit_price, hi, lo)}

    # ------------------------------------------------------- automatic twins
    def _settle_auto(self, s: dict) -> None:
        """Close every open automatic twin whose exit the candles up to the cursor already show: the
        original stop (a candle low at or below it; the stop wins when both levels are hit in one
        candle) or the original target (a high at or above it). The twin runs overnight and
        across days until one of them is hit. Only the candles after the last checked one are read,
        and nothing beyond the cursor, so a twin never knows the future."""
        with self._lock:
            with self.store.tx() as conn:
                opens = self.store.open_auto_trades(conn, s["id"])
            if not opens:
                return
            base = self._base_ms
            for a in opens:
                start = (a.get("checked_ms") or a["entry_ms"]) + base
                if start > s["cursor_ms"]:
                    continue                                       # nothing new since the last check
                df = self.candles.get_candles(s["symbol"], BASE_INTERVAL, start, s["cursor_ms"])
                if not len(df):
                    continue
                hit = None
                for t_open, high, low in zip(df["time"].astype("int64"), df["high"], df["low"]):
                    if float(low) <= a["stop"]:
                        hit = (int(t_open) * 1000, a["stop"], EXIT_STOP)
                        break
                    if float(high) >= a["take_profit"]:
                        hit = (int(t_open) * 1000, a["take_profit"], EXIT_TP)
                        break
                if hit is not None:
                    self._close_auto(a, s["fee_rate"], *hit)
                else:
                    with self.store.tx() as conn:
                        self.store.update_auto_trade(conn, a["id"], checked_ms=int(df["time"].iloc[-1]) * 1000, updated_at=_now_iso())

    def _close_auto(self, a: dict, fee_rate: float, exit_ms: int, exit_price: float, exit_type: str) -> None:
        res = trade_result(a["entry_price"], exit_price, a["coins"], a["position_value"], a["buy_fee"], fee_rate, a["risk_amount"])
        fields = {"status": "closed", "exit_ms": exit_ms, "exit_price": exit_price, "exit_type": exit_type,
                  "sell_fee": res["sell_fee"], "fees": res["fees"], "pnl": res["pnl"], "pnl_pct": res["pnl_pct"],
                  "r_multiple": res["r_multiple"], "updated_at": _now_iso()}
        with self.store.tx() as conn:
            self.store.update_auto_trade(conn, a["id"], **fields)
        a.update(fields)

    def _auto_of(self, session_id: str, trade_id: int, manual: dict | None) -> dict | None:
        with self.store.tx() as conn:
            a = self.store.auto_trade_of(conn, session_id, trade_id)
        return self._public_auto(a, manual) if a else None

    def auto_trades(self, session_id: str) -> dict:
        """The automatic-exit twins of this session with the comparison to the manual trades."""
        st = self.state(session_id)   # settles the twins first
        with self.store.tx() as conn:
            rows = self.store.list_auto_trades(conn, session_id)
            trades = self.store.list_trades(conn, session_id)
            self._backfill(st, conn, trades)
            manual = {t["id"]: t for t in trades}
        out = [self._public_auto(a, manual.get(a["trade_id"])) for a in rows]
        have = {a["trade_id"] for a in rows}
        out += [self._public_auto_none(t) for t in trades if t["id"] not in have]   # trades from before the twins: alternatives only
        out.sort(key=lambda a: a["seq"])
        closed = [a for a in out if a["status"] == "closed"]
        both = [a for a in closed if a["manual"] and a["manual"]["status"] == "closed"]
        avg = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
        summary = {
            "count": len(out), "open": sum(1 for a in out if a["status"] == "open"), "closed": len(closed),
            "wins": sum(1 for a in closed if a["pnl"] > 0),
            "pnl": sum(a["pnl"] for a in closed), "avg_r": avg([a["r_multiple"] for a in closed if a["r_multiple"] is not None]),
            "by_type": {k: sum(1 for a in closed if a["exit_type"] == k) for k in (EXIT_STOP, EXIT_TP)},
            "avg_hold_hours": avg([a["hold_hours"] for a in closed]),
            "compared": len(both),
            "manual_pnl": sum(a["manual"]["pnl"] for a in both), "auto_pnl": sum(a["pnl"] for a in both),
            "manual_avg_r": avg([a["manual"]["r_multiple"] for a in both if a["manual"]["r_multiple"] is not None]),
            "auto_avg_r": avg([a["r_multiple"] for a in both if a["r_multiple"] is not None]),
            "manual_avg_hold_hours": avg([a["manual"]["hold_hours"] for a in both]),
            "auto_avg_hold_hours": avg([a["hold_hours"] for a in both]),
        }
        summary["diff"] = summary["auto_pnl"] - summary["manual_pnl"]
        for k in ALT_KEYS:   # the two hands-off alternatives, over the trades that have them
            alts = [a["alts"][k] for a in out if a["alts"]]
            same = [(a["alts"][k], a["manual"]) for a in out if a["alts"] and a["manual"] and a["manual"]["status"] == "closed"]
            summary[k] = {
                "count": len(alts), "wins": sum(1 for x in alts if x["pnl"] > 0),
                "pnl": sum(x["pnl"] for x in alts), "avg_r": avg([x["r_multiple"] for x in alts if x["r_multiple"] is not None]),
                "by_type": {typ: sum(1 for x in alts if x["exit_type"] == typ) for typ in (EXIT_STOP, EXIT_TP, EXIT_EOD, EXIT_DATA_END)},
                "avg_hold_hours": avg([hold_hours(a["entry_ms"], a["alts"][k]["exit_ms"]) for a in out if a["alts"]]),
                "compared": len(same), "manual_pnl": sum(m["pnl"] for _, m in same), "alt_pnl": sum(x["pnl"] for x, _ in same),
            }
            summary[k]["diff"] = summary[k]["alt_pnl"] - summary[k]["manual_pnl"]
        for k in AC_KEYS:   # exits A and C, over the trades where that exit has closed
            legs = [a["exits_ac"][k] for a in out if a["exits_ac"] and a["exits_ac"][k]["status"] == "closed"]
            same = [(a["exits_ac"][k], a["manual"]) for a in out
                    if a["exits_ac"] and a["exits_ac"][k]["status"] == "closed" and a["manual"] and a["manual"]["status"] == "closed"]
            summary[k] = {
                "count": len(legs), "wins": sum(1 for x in legs if x["pnl"] > 0),
                "pnl": sum(x["pnl"] for x in legs), "avg_r": avg([x["r_multiple"] for x in legs if x["r_multiple"] is not None]),
                "by_type": {typ: sum(1 for x in legs if x["exit_type"] == typ) for typ in (EXIT_STOP, EXIT_TP, EXIT_EARLY)},
                "compared": len(same), "manual_pnl": sum(m["pnl"] for _, m in same), "leg_pnl": sum(x["pnl"] for x, _ in same),
            }
            summary[k]["diff"] = summary[k]["leg_pnl"] - summary[k]["manual_pnl"]
        return {"trades": out, "summary": summary, "price": st["price"]}

    @classmethod
    def _public_auto_none(cls, manual: dict) -> dict:
        """The row of a trade without a twin (made before the twins existed): status "none", the
        twin's fields empty, the manual side and the alternatives as usual."""
        a = {"id": None, "trade_id": manual["id"], "seq": manual["seq"], "status": "none", "day_date": manual["day_date"],
             "entry_ms": manual["entry_ms"], "entry_price": manual["entry_price"], "stop": None, "take_profit": None,
             "coins": manual["coins"], "position_value": manual["position_value"], "buy_fee": manual["buy_fee"], "risk_amount": manual["risk_amount"],
             "exit_ms": None, "exit_price": None, "exit_type": None, "sell_fee": None, "fees": None, "pnl": None, "pnl_pct": None, "r_multiple": None}
        out = cls._public_auto({**a, "stop": manual["entry_price"], "take_profit": manual["entry_price"]}, manual)
        out.update(stop=None, take_profit=None, stop_pct=None, tp_pct=None)
        return out

    @staticmethod
    def _public_auto(a: dict, manual: dict | None) -> dict:
        out = dict(a)
        out["entry_local"] = local_str(a["entry_ms"] + config.interval_ms(BASE_INTERVAL))
        out["exit_local"] = local_str(a["exit_ms"] + config.interval_ms(BASE_INTERVAL)) if a["exit_ms"] is not None else None
        out["stop_pct"] = (a["stop"] / a["entry_price"] - 1.0) * 100.0
        out["tp_pct"] = (a["take_profit"] / a["entry_price"] - 1.0) * 100.0
        out["hold_hours"] = hold_hours(a["entry_ms"], a["exit_ms"])   # None while open (the UI counts from the entry)
        m = None
        if manual is not None:
            m = {"id": manual["id"], "status": manual["status"], "exit_type": manual["exit_type"], "exit_price": manual["exit_price"],
                 "exit_local": local_str(manual["exit_ms"] + config.interval_ms(BASE_INTERVAL)) if manual["exit_ms"] is not None else None,
                 "pnl": manual["pnl"], "r_multiple": manual["r_multiple"], "moved": bool(trade_changes(manual)),
                 "hold_hours": hold_hours(manual["entry_ms"], manual["exit_ms"])}
        out["manual"] = m
        out["diff"] = (a["pnl"] - manual["pnl"]) if (m and a["status"] == "closed" and manual["status"] == "closed") else None
        out["alts"] = alts_public(manual) if manual is not None else None
        out["exits_ac"] = ac_public(manual) if manual is not None else None
        return out

    # ------------------------------------------------------------------ clock
    def step(self, session_id: str, n: int = 1) -> dict:
        """Advance `n` 15-minute candles, firing the stop / take-profit of an open trade on the way;
        stops at an exit and where a review becomes due (24:00 of an exit's trading day). After an
        exit a step means "keep trading": the day goes on (End the day is the jump to the next 00:00)."""
        if not isinstance(n, int) or n < 1:
            raise Invalid("n must be >= 1")
        events: list[dict] = []
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            self._review_gate(s)
            if ds["phase"] == "exited":   # stepping on after an exit: the day goes on
                with self.store.tx() as conn:
                    self.store.update_day_session(conn, session_id, phase="trading", last_exit_id=None, updated_at=_now_iso())
                ds["phase"] = "trading"
            for _ in range(n):
                if s["cursor_ms"] >= s["data_end_ms"]:
                    break
                ev = self._advance(s)
                if ev:
                    events.append(ev)
                    if ev["kind"] in ("exit", "review"):
                        break
        st = self.state(session_id)
        st["events"] = events
        return st

    def resume(self, session_id: str) -> dict:
        """After an exit: jump to the next 00:00 (idempotent while already trading)."""
        events: list[dict] = []
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            self._review_gate(s)
            if ds["phase"] == "exited":
                events.append(self._resume(s, ds))
        st = self.state(session_id)
        st["events"] = events
        return st

    def skip_day(self, session_id: str) -> dict:
        """Jump to the next day's 00:00 (only without an open trade)."""
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            self._review_gate(s)
            if ds["phase"] == "exited":
                ev = self._resume(s, ds)
            else:
                with self.store.tx() as conn:
                    if self.store.open_trade(conn, session_id) is not None:
                        raise Conflict("A trade is open: close it before skipping the day.")
                if s["cursor_ms"] >= s["data_end_ms"]:
                    raise Conflict("The replay has reached its end.")
                ev = self._jump(s, trading_day(s["cursor_ms"] + self._base_ms) + timedelta(days=1), "Skipped to the next day at 00:00.", LEFT_SKIP)
        st = self.state(session_id)
        st["events"] = [ev]
        return st

    def goto_day(self, session_id: str, day_date: str) -> dict:
        """Jump forward to 00:00 of a later trading day (from the calendar). Never backwards, not
        with an open trade, not past the replay's end. The current day is left as skipped (or as
        'trade closed' when its trade had just closed); the days in between are never seen."""
        try:
            d = date.fromisoformat(day_date)
        except (TypeError, ValueError):
            raise Invalid("date must be YYYY-MM-DD") from None
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            self._review_gate(s)
            if s["cursor_ms"] >= s["data_end_ms"]:
                raise Conflict("The replay has reached its end.")
            with self.store.tx() as conn:
                if self.store.open_trade(conn, session_id) is not None:
                    raise Conflict("A trade is open: close it before jumping to another day.")
            cur = trading_day(s["cursor_ms"] + self._base_ms)
            if d <= cur:
                raise Invalid(f"Only forward: {day_date} is not after the current trading day {cur.isoformat()}.")
            if local_ms(d, DAY_START_HOUR) > s["data_end_ms"] + self._base_ms:
                raise Invalid(f"{day_date} is after the end of the replay.")
            how = LEFT_EXIT if ds["phase"] == "exited" else LEFT_SKIP
            if ds["phase"] == "exited":
                with self.store.tx() as conn:
                    self.store.update_day_session(conn, session_id, phase="trading", last_exit_id=None, updated_at=_now_iso())
            self._mark_left(s, cur, how)
            target = max(self._cursor_at_day_start(s, d), s["cursor_ms"])
            self._set_cursor(s, target)
            self._set_floor(s, target)
            ev = {"kind": "jump", "to_ms": target, "to_iso": _iso(target), "to_local": local_str(target + self._base_ms),
                  "reason": f"Jumped to {day_date} at 00:00."}
        st = self.state(session_id)
        st["events"] = [ev]
        return st

    def next_ema_day(self, session_id: str) -> dict:
        """Jump forward to 00:00 of the next trading day whose trend check says "daily close above
        EMA 200" (the button "Next day > EMA 200"). The rules of goto_day apply: never backwards,
        not with an open trade; Conflict when no such day is left before the replay's end."""
        with self._lock:
            s = self._ready(session_id)
            self._review_gate(s)
            if s["cursor_ms"] >= s["data_end_ms"]:
                raise Conflict("The replay has reached its end.")
            with self.store.tx() as conn:
                if self.store.open_trade(conn, session_id) is not None:
                    raise Conflict("A trade is open: close it before jumping to another day.")
            cur = trading_day(s["cursor_ms"] + self._base_ms)
            target = self._next_ema_above_day(s, cur)
            if target is None:
                raise Conflict("No later day with a daily close above the EMA 200 before the end of the replay.")
            return self.goto_day(session_id, target.isoformat())

    # --------------------------------------------------------- jump strategies
    @staticmethod
    def signal_strategies() -> list[dict]:
        """The strategies the "Next signal" button can jump with."""
        return [j.describe() for j in JUMP_STRATEGIES.values()]

    def next_signal(self, session_id: str, strategy: str, params: dict | None = None) -> dict:
        """Jump forward to the next entry signal of a jump strategy (the button "Next signal"): the
        15m candle in which the signal happens becomes the cursor, at any time of day. The zone the
        signal came from is added to the session's zones (1h) so the New trade panel can use it.
        The gates of the other jumps apply: the review first, no open trade, not past the end;
        Conflict when no signal is left before the replay's end."""
        try:
            js = get_jump_strategy(strategy)
            full = validate_params(js.params, params)
        except ValueError as e:
            raise Invalid(str(e)) from None
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            self._review_gate(s)
            if s["cursor_ms"] >= s["data_end_ms"]:
                raise Conflict("The replay has reached its end.")
            with self.store.tx() as conn:
                if self.store.open_trade(conn, session_id) is not None:
                    raise Conflict("A trade is open: close it before jumping to the next signal.")
            sig = self._next_signal_after(s, js, full, s["cursor_ms"])
            if sig is None:
                raise Conflict(f"No later {js.name} signal before the end of the replay.")
            base = self._base_ms
            target = sig["cursor_ms"]
            cur = trading_day(s["cursor_ms"] + base)
            if trading_day(target + base) > cur:
                self._mark_left(s, cur, LEFT_EXIT if ds["phase"] == "exited" else LEFT_SKIP)
            if ds["phase"] == "exited":
                with self.store.tx() as conn:
                    self.store.update_day_session(conn, session_id, phase="trading", last_exit_id=None, updated_at=_now_iso())
            self._set_cursor(s, target)
            self._set_floor(s, target)
            zone = self._signal_zone(session_id, sig)
            close = target + base
            ev = {"kind": "jump", "to_ms": target, "to_iso": _iso(target), "to_local": local_str(close),
                  "reason": f"{js.name}: {js.describe_signal(sig)}.",
                  "strategy": js.key, "signal": {k: v for k, v in sig.items() if k != "bar"}, "zone_id": zone["id"]}
        st = self.state(session_id)
        st["events"] = [ev]
        return st

    def _next_signal_after(self, s: dict, js: JumpStrategy, params: dict, after_ms: int) -> dict | None:
        """The first signal whose 15m candle opens after `after_ms` (and not after the data end),
        with `cursor_ms` = the open time of that 15m candle."""
        iv_ms = config.interval_ms(js.interval)
        for sig in self._strategy_signals(s, js, params):
            if sig["bar_ms"] + iv_ms <= after_ms:
                continue
            if "cursor_ms" not in sig:
                sig["cursor_ms"] = self._refine_signal(s, sig, iv_ms)
            if sig["cursor_ms"] is not None and after_ms < sig["cursor_ms"] <= s["data_end_ms"]:
                return sig
        return None

    def _strategy_signals(self, s: dict, js: JumpStrategy, params: dict) -> list[dict]:
        """All signals of the strategy over the session's candles (warm-up included), computed once:
        the detector is causal, so the signals after the cursor never depend on later candles."""
        key = (s["id"], js.key, tuple(sorted(params.items())), s["data_end_ms"])
        if key not in self._signals:
            iv_ms = config.interval_ms(js.interval)
            df = self.candles.get_candles(s["symbol"], js.interval, s["start_ms"] - WARMUP_BARS * iv_ms, s["data_end_ms"])
            self._signals = {k: v for k, v in self._signals.items() if k[0] != s["id"] or k[1:3] != key[1:3]}
            self._signals[key] = js.detect(df, params)["signals"]
        return self._signals[key]

    def _refine_signal(self, s: dict, sig: dict, iv_ms: int) -> int | None:
        """Open time of the first 15m candle inside the signal's candle whose low reaches the
        signal's entry level (the zone top plus the near-zone buffer), or None on a data gap."""
        q = self.candles.get_candles(s["symbol"], BASE_INTERVAL, sig["bar_ms"], sig["bar_ms"] + iv_ms - self._base_ms)
        hit = q[q["low"] <= sig.get("entry_level", sig["zone_top"])]
        return int(hit["time"].iloc[0]) * 1000 if len(hit) else None

    def _signal_zone(self, session_id: str, sig: dict) -> dict:
        """The signal's zone as a session zone (1h), reusing an identical one. The zones of earlier
        signals go unless a trade uses them, so the chart only shows the current signal's zone
        next to the zones drawn by hand."""
        top, bottom = float(sig["zone_top"]), float(sig["zone_bottom"])
        with self.store.tx() as conn:
            for z in self.store.list_zones(conn, session_id):
                if math.isclose(z["upper"], top) and math.isclose(z["lower"], bottom):
                    self.store.delete_unused_signal_zones(conn, session_id, keep=z["id"])
                    return self._public_zone(z)
            self.store.delete_unused_signal_zones(conn, session_id)
        z = self.add_zone(session_id, "1h", top, bottom)
        with self.store.tx() as conn:
            self.store.update_zone(conn, z["id"], source="signal")
        z["source"] = "signal"
        return z

    def _next_ema_above_day(self, s: dict, after: date) -> date | None:
        """The first trading day after `after` whose last completed daily candle closes above the
        EMA 200, judged at that day's 00:00 exactly like the trend check (`_daily_ema`)."""
        limit = s["data_end_ms"] + self._base_ms
        d = after + timedelta(days=1)
        while local_ms(d, DAY_START_HOUR) <= limit:
            probe = dict(s, cursor_ms=self._cursor_at_day_start(s, d))
            if self._daily_ema(probe)["ema_above"] == 1:
                return d
            d += timedelta(days=1)
        return None

    def _advance(self, s: dict) -> dict | None:
        """One 15-minute candle forward. Crossing 00:00 leaves the trading
        day. Returns an exit event, a review event (a closed trade's 24:00 reached) or None."""
        base = self._base_ms
        nxt = self.candles.nth_at_or_after(s["symbol"], BASE_INTERVAL, s["cursor_ms"] + base, 1)
        nxt = s["data_end_ms"] if nxt is None or nxt > s["data_end_ms"] else nxt
        with self.store.tx() as conn:
            trade = self.store.open_trade(conn, s["id"])
        cur = trading_day(s["cursor_ms"] + base)
        if trading_day(nxt + base) != cur:
            with self.store.tx() as conn:
                traded = self.store.trades_on_day(conn, s["id"], cur.isoformat())
            self._mark_left(s, cur, LEFT_HELD if trade is not None and trade["day_date"] == cur.isoformat()
                            else LEFT_EXIT if traded else LEFT_END)
        if trade is None:
            self._set_cursor(s, nxt)
            return self._review_event(s)
        row = self.candles.get_candles(s["symbol"], BASE_INTERVAL, nxt, nxt)
        self._set_cursor(s, nxt)
        if len(row) != 1:
            return self._review_event(s)
        high, low = float(row["high"].iloc[0]), float(row["low"].iloc[0])
        _, entry_day_end = day_bounds(date.fromisoformat(trade["day_date"]))
        if nxt + base > entry_day_end and not trade["overnight"]:
            with self.store.tx() as conn:
                self.store.update_trade(conn, trade["id"], overnight=1, updated_at=_now_iso())
            trade["overnight"] = 1
        if low <= trade["stop_used"]:  # both reached in one candle counts as the stop
            self._close_legs(s, trade, AC_KEYS, nxt, trade["stop_used"], EXIT_STOP)   # whatever of A / C is still open
            return self._exit(s, trade, nxt, trade["stop_used"], EXIT_STOP)
        if high >= trade["take_profit"]:
            self._close_legs(s, trade, AC_KEYS, nxt, trade["take_profit"], EXIT_TP)
            return self._exit(s, trade, nxt, trade["take_profit"], EXIT_TP)
        return self._review_event(s)

    def _review_event(self, s: dict) -> dict | None:
        """A review event when the candle just reached is 24:00 of a closed trade's trading day."""
        with self.store.tx() as conn:
            t = self._pending_review(s, conn)
        if t is None or self._review_end(t) != s["cursor_ms"] + self._base_ms:
            return None
        return {"kind": "review", "trade": self._public_trade(t, s["fee_rate"])}

    def _resume(self, s: dict, ds: dict) -> dict:
        with self.store.tx() as conn:
            last = self.store.get_trade(conn, s["id"], ds["last_exit_id"]) if ds["last_exit_id"] else None
            self.store.update_day_session(conn, s["id"], phase="trading", updated_at=_now_iso())
        ds["phase"] = "trading"
        close = (last["exit_ms"] if last and last["exit_ms"] is not None else s["cursor_ms"]) + self._base_ms
        return self._jump(s, trading_day(close) + timedelta(days=1), "Day ended: next day at 00:00.", LEFT_EXIT)

    def _jump(self, s: dict, d: date, reason: str, how: str) -> dict:
        self._mark_left(s, d - timedelta(days=1), how)
        target = max(self._cursor_at_day_start(s, d), s["cursor_ms"])  # never backwards
        self._set_cursor(s, target)
        self._set_floor(s, target)
        return {"kind": "jump", "to_ms": target, "to_iso": _iso(target), "to_local": local_str(target + self._base_ms), "reason": reason}

    def _mark_left(self, s: dict, d: date, how: str) -> None:
        """Record on the day's journal row when and how the replay left it. Leaving a day also
        removes the zones of "Next signal" that no trade used."""
        start, end = day_bounds(d)
        check = self._trend_check(s, d, start, end)
        with self.store.tx() as conn:
            self.store.update_check(conn, check["id"], left_ms=s["cursor_ms"], left_how=how, updated_at=_now_iso())
            self.store.delete_unused_signal_zones(conn, s["id"])

    def _first_day_cursor(self, s: dict, from_ms: int) -> int:
        """Where the replay starts: the day of the candle opening at `from_ms` when the data begins
        right at its 00:00 (then on that first candle, the 00:00 one is not in the session), else
        the candle closing at the next 00:00."""
        base = self._base_ms
        d = trading_day(from_ms + base)
        start, _ = day_bounds(d)
        if start < from_ms:
            d += timedelta(days=1)
        return max(self._cursor_at_day_start(s, d), from_ms)

    def _cursor_at_day_start(self, s: dict, d: date) -> int:
        """Open time of the stored base candle that closes at 00:00 of day `d` (the first one after
        it on a data gap), clamped to the session's last candle."""
        start, _ = day_bounds(d)
        nxt = self.candles.first_at_or_after(s["symbol"], BASE_INTERVAL, start - self._base_ms)
        return s["data_end_ms"] if nxt is None or nxt > s["data_end_ms"] else nxt

    def _set_cursor(self, s: dict, ms: int) -> None:
        with self.sessions.tx() as conn:
            self.sessions.update_session(conn, s["id"], cursor_ms=ms, updated_at=_now_iso())
        s["cursor_ms"] = ms

    def _set_floor(self, s: dict, ms: int) -> None:
        """A jump landed on `ms`: Step back never goes before it (the candles jumped over were never seen)."""
        with self.store.tx() as conn:
            self.store.update_day_session(conn, s["id"], back_floor_ms=ms, updated_at=_now_iso())

    # --------------------------------------------------------------- step back
    def _back_floor(self, s: dict, ds: dict) -> int:
        """The earliest candle Step back may return to: the latest of the first day's start, the latest
        jump's landing candle, the open trade's entry, its last stop / target move and its closed
        exit A / C, and the exit of the latest closed trade. Nothing before it is undone."""
        marks = [s["data_start_ms"], ds.get("start_cursor_ms"), ds.get("back_floor_ms")]
        with self.store.tx() as conn:
            trade = self.store.open_trade(conn, s["id"])
            closed = self.store.closed_trades(conn, s["id"])
        if trade is not None:
            marks.append(trade["entry_ms"])
            marks += [c["at_ms"] for c in trade_changes(trade)]
            marks += [leg["exit_ms"] for leg in ac_legs(trade).values() if leg is not None]
        marks += [t["exit_ms"] for t in closed if t["exit_ms"] is not None]
        return max(m for m in marks if m is not None)

    def _step_back_target(self, s: dict, ds: dict) -> int | None:
        """The candle one step back, or None at the floor."""
        prev = self.candles.last_at_or_before(s["symbol"], BASE_INTERVAL, s["cursor_ms"] - 1)
        return prev if prev is not None and prev >= self._back_floor(s, ds) else None

    def step_back(self, session_id: str) -> dict:
        """One 15-minute candle back (day trading only), never before the floor (_back_floor).
        Crossing 00:00 backwards re-opens the day it returns to (its journal row forgets how it was
        left). Blocked while a review is due."""
        with self._lock:
            s = self._ready(session_id)
            ds = self._day_row(session_id)
            self._review_gate(s)
            target = self._step_back_target(s, ds)
            if target is None:
                raise Conflict("Can't step back further: the latest trade action or jump is here.")
            base = self._base_ms
            back_to = trading_day(target + base)
            if back_to != trading_day(s["cursor_ms"] + base):
                start, end = day_bounds(back_to)
                check = self._trend_check(s, back_to, start, end)
                with self.store.tx() as conn:
                    self.store.update_check(conn, check["id"], left_ms=None, left_how=None, updated_at=_now_iso())
            self._set_cursor(s, target)
        return self.state(session_id)

    # ------------------------------------------------------------------ review
    def _review_gate(self, s: dict) -> None:
        """Conflict while a closed trade's review is due (its 24:00 reached) and unanswered: no step,
        no Continue, no skip, no jump, no step back (and no buy, see _entry_gate) until the four
        questions are answered."""
        with self.store.tx() as conn:
            t = self._pending_review(s, conn)
        if t is not None:
            raise Conflict(f"Answer the review questions for trade #{t['seq']} first.")

    def review(self, session_id: str, trade_id: int, answers: dict) -> dict:
        """Save the four review marks of a closed trade (all four required, True / False). The
        answers can be changed later; a trade still open cannot be reviewed."""
        fields: dict = {}
        for key, col, _ in REVIEW_QUESTIONS:
            v = answers.get(key) if isinstance(answers, dict) else None
            if not isinstance(v, bool):
                raise Invalid(f"{key} must be true or false.")
            fields[col] = int(v)
        with self._lock:
            s = self._get_day(session_id)
            with self.store.tx() as conn:
                t = self.store.get_trade(conn, session_id, trade_id)
                if t is None:
                    raise NotFound(f"trade {trade_id} not found")
                if t["status"] != "closed":
                    raise Conflict("The trade is still open: review it after the exit.")
                now = _now_iso()
                self.store.update_trade(conn, trade_id, **fields, reviewed_at=now, updated_at=now)
                t = self.store.get_trade(conn, session_id, trade_id)
        st = self.state(session_id)
        return {"trade": self._public_trade(t, s["fee_rate"], st["price"]), "state": st}

    # ------------------------------------------------------------------ trades
    def trades(self, session_id: str) -> dict:
        st = self.state(session_id)
        with self.store.tx() as conn:
            rows = self.store.list_trades(conn, session_id)
            self._backfill(st, conn, rows)
        return {"trades": [self._public_trade(t, st["fee_rate"], st["price"]) for t in rows]}

    # ------------------------------------------------------------- conditions
    def conditions(self, session_id: str, scope: str = "session") -> dict:
        """How the closed trades did with and without each condition: count, wins, win rate,
        average R, total P&L after fees. `scope` "session" = this session, "all" = every day session."""
        if scope not in CONDITION_SCOPES:
            raise Invalid(f"scope must be one of {', '.join(CONDITION_SCOPES)}")
        self._ready(session_id)   # NotFound / Invalid for a missing or non-day session
        with self.store.tx() as conn:
            trades = self.store.closed_trades(conn, None if scope == "all" else session_id)
        flags = [trade_conditions(t) for t in trades]

        def group(rows: list[dict]) -> dict:
            n = len(rows)
            wins = sum(1 for t in rows if (t["pnl"] or 0.0) > 0)
            rs = [float(t["r_multiple"]) for t in rows if t["r_multiple"] is not None]
            return {
                "trades": n, "wins": wins, "losses": n - wins,
                "win_rate": (wins / n * 100.0) if n else None,
                "avg_r": (sum(rs) / len(rs)) if rs else None,
                "pnl": sum(float(t["pnl"] or 0.0) for t in rows),
            }

        out = []
        for key, label in CONDITIONS:
            yes = [t for t, f in zip(trades, flags) if f[key] is True]
            no = [t for t, f in zip(trades, flags) if f[key] is False]
            unknown = sum(1 for f in flags if f[key] is None)
            out.append({"key": key, "label": label, "with": group(yes), "without": group(no), "unknown": unknown})
        return {
            "scope": scope, "trades": len(trades), "sessions": len({t["session_id"] for t in trades}),
            "all": group(trades), "conditions": out,
        }

    def conditions_csv(self, session_id: str, scope: str = "session") -> str:
        data = self.conditions(session_id, scope)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        cols = ("trades", "wins", "losses", "win_rate", "avg_r", "pnl")
        w.writerow(["condition", "label", *[f"with_{c}" for c in cols], *[f"without_{c}" for c in cols], "unknown"])
        fmt = lambda v: "" if v is None else (f"{v:.4f}" if isinstance(v, float) else v)  # noqa: E731
        for c in data["conditions"]:
            w.writerow([c["key"], c["label"], *[fmt(c["with"][k]) for k in cols], *[fmt(c["without"][k]) for k in cols], c["unknown"]])
        return buf.getvalue()

    # ----------------------------------------------------------- daily journal
    def days(self, session_id: str) -> dict:
        """One row per trading day the replay has seen: trend check, how the day was left, the day's
        trade (if any) in short, the end-of-day picture and the notes."""
        st = self.state(session_id)
        with self.store.tx() as conn:
            checks = self.store.list_checks(conn, session_id)
            trades = self.store.list_trades(conn, session_id)
        by_day: dict[str, list[dict]] = {}
        for t in trades:
            by_day.setdefault(t["day_date"], []).append(t)
        return {"days": [self._public_day(c, by_day.get(c["day_date"], []), st["fee_rate"], st["price"]) for c in checks]}

    def days_csv(self, session_id: str) -> str:
        cols = ["date", "daily_close", "ema200", "ema_above", "outcome", "left_label", "left_local",
                "trade_seq", "exit_type", "exit_reason", "pnl", "pnl_pct", "r_multiple", "has_image", "image_local", "notes"]
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for d in self.days(session_id)["days"]:
            t = d["trade"] or {}
            w.writerow({**d, "trade_seq": t.get("seq"), "exit_type": t.get("exit_type"), "exit_reason": t.get("exit_reason_label"),
                        "pnl": t.get("pnl"), "pnl_pct": t.get("pnl_pct"), "r_multiple": t.get("r_multiple")})
        return out.getvalue()

    def set_day_notes(self, session_id: str, day_date: str, notes: str | None) -> dict:
        """Free text for a trading day the replay has seen (the current one or any earlier one)."""
        if notes is not None and not isinstance(notes, str):
            raise Invalid("notes must be text")
        notes = (notes or "").strip() or None
        if notes is not None and len(notes) > DAY_NOTES_MAX_CHARS:
            raise Invalid(f"notes are limited to {DAY_NOTES_MAX_CHARS} characters")
        with self._lock:
            s = self._ready(session_id)
            c = self._check_row(session_id, day_date)
            with self.store.tx() as conn:
                self.store.update_check(conn, c["id"], notes=notes, updated_at=_now_iso())
        return self._day_public_by_date(s, day_date)

    def attach_day_image(self, session_id: str, day_date: str, image: str) -> dict:
        """Store the end-of-day chart picture (PNG data URL) of a trading day."""
        data = decode_png_data_url(image)
        with self._lock:
            s = self._ready(session_id)
            c = self._check_row(session_id, day_date)
            rel = f"{session_id}/day_{c['day_date']}.png"
            path = self.replay._snapshot_dir() / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            with self.store.tx() as conn:
                self.store.update_check(conn, c["id"], image_path=rel, image_ms=s["cursor_ms"], updated_at=_now_iso())
        return self._day_public_by_date(s, day_date)

    def day_image_path(self, session_id: str, day_date: str) -> Path:
        c = self._check_row(session_id, day_date)
        if not c["image_path"]:
            raise NotFound(f"trading day {day_date} has no picture")
        path = self.replay._snapshot_dir() / c["image_path"]
        if not path.is_file():
            raise NotFound(f"picture file for trading day {day_date} is missing")
        return path

    def _check_row(self, session_id: str, day_date: str) -> dict:
        self._day_row(session_id)
        try:
            key = date.fromisoformat(str(day_date)).isoformat()
        except ValueError as exc:
            raise Invalid("day_date must be YYYY-MM-DD") from exc
        with self.store.tx() as conn:
            c = self.store.get_check(conn, session_id, key)
        if c is None:
            raise NotFound(f"trading day {key} not found in session {session_id}")
        return c

    def _day_public_by_date(self, s: dict, day_date: str) -> dict:
        key = date.fromisoformat(str(day_date)).isoformat()
        with self.store.tx() as conn:
            c = self.store.get_check(conn, s["id"], key)
            trades = [t for t in self.store.list_trades(conn, s["id"]) if t["day_date"] == key]
        return self._public_day(c, trades[-1] if trades else None, s["fee_rate"], None)  # type: ignore[arg-type]

    def _public_day(self, c: dict, t: dict | list[dict] | None, fee_rate: float, price: float | None) -> dict:
        ts = t if isinstance(t, list) else ([t] if t else [])   # every trade of the day; the row shows the last one
        t = ts[-1] if ts else None
        out = self._public_check(c)
        if t is not None:
            pt = self._public_trade(t, fee_rate, price)
            out["trade"] = {k: pt[k] for k in ("id", "seq", "status", "entry_local", "entry_price", "exit_local", "exit_price", "exit_type",
                                                "exit_reason_label", "overnight", "pnl", "pnl_pct", "r_multiple", "has_image")}
            out["outcome"] = "open" if t["status"] == "open" else "trade"
        else:
            out["trade"] = None
            out["outcome"] = "skipped" if c.get("left_how") == LEFT_SKIP else "no trade" if c.get("left_how") else "current"
        out["trade_count"] = len(ts)
        out["trade_seqs"] = [x["seq"] for x in ts]
        out["day_pnl"] = sum(float(x["pnl"] or 0.0) for x in ts if x["status"] == "closed")   # after fees, closed trades only
        out["exit_types"] = [x["exit_type"] for x in ts if x["status"] == "closed"]            # in trade order, for the calendar tags
        return out

    def attach_image(self, session_id: str, trade_id: int, image: str) -> dict:
        """Store the chart picture (PNG data URL) taken at the exit of a trade."""
        data = decode_png_data_url(image)
        with self._lock:
            s = self._get_day(session_id)
            with self.store.tx() as conn:
                t = self.store.get_trade(conn, session_id, int(trade_id))
                if t is None:
                    raise NotFound(f"trade {trade_id} not found in session {session_id}")
                rel = f"{session_id}/day_{int(trade_id)}.png"
                path = self.replay._snapshot_dir() / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.store.update_trade(conn, t["id"], image_path=rel, updated_at=_now_iso())
                t = self.store.get_trade(conn, session_id, t["id"])
        return self._public_trade(t, s["fee_rate"])  # type: ignore[arg-type]

    def image_path(self, session_id: str, trade_id: int) -> Path:
        self._day_row(session_id)
        with self.store.tx() as conn:
            t = self.store.get_trade(conn, session_id, int(trade_id))
        if t is None:
            raise NotFound(f"trade {trade_id} not found in session {session_id}")
        if not t["image_path"]:
            raise NotFound(f"trade {trade_id} has no picture")
        path = self.replay._snapshot_dir() / t["image_path"]
        if not path.is_file():
            raise NotFound(f"picture file for trade {trade_id} is missing")
        return path

    @staticmethod
    def _public_trade(t: dict, fee_rate: float, price: float | None = None) -> dict:
        out = dict(t)
        flag = lambda v: None if v is None else bool(v)  # noqa: E731
        out["capped"] = bool(t["capped"])
        out["overnight"] = bool(t["overnight"])
        out["ema_above"] = flag(t["ema_above"])
        for key in ("hh_hl", "uptrend"):   # the retired daily answer (see the module docstring)
            out.pop(key, None)
        for key in ("price_in_zone", "rsi_dipped", "close_above_prev_high"):
            out[key] = flag(t.get(key))
        out["conditions"] = trade_conditions(t)
        out["confidence"] = t.get("confidence")   # 0..10 at the buy; None for trades from before the score existed
        out["changes"] = trade_changes(t)
        out["stop_initial"] = t["stop_used"] if t.get("stop_initial") is None else t["stop_initial"]
        out["tp_initial"] = t["take_profit"] if t.get("tp_initial") is None else t["tp_initial"]
        out["rr_now"] = reward_to_risk(t["entry_price"], t["stop_used"], t["take_profit"]) if t["stop_used"] < t["entry_price"] else None
        out["stop_pct"] = (t["stop_used"] / t["entry_price"] - 1.0) * 100.0        # stop and target against the entry price
        out["tp_pct"] = (t["take_profit"] / t["entry_price"] - 1.0) * 100.0
        out["entry_iso"] = _iso(t["entry_ms"])
        out["entry_local"] = local_str(t["entry_ms"] + config.interval_ms(BASE_INTERVAL))  # the fill is at the close
        out["exit_iso"] = _iso(t["exit_ms"]) if t["exit_ms"] is not None else None
        out["exit_local"] = local_str(t["exit_ms"] + config.interval_ms(BASE_INTERVAL)) if t["exit_ms"] is not None else None
        out["exit_reason_label"] = EXIT_REASON_LABELS.get(t["exit_reason"]) if t["exit_reason"] else None
        out["hold_hours"] = hold_hours(t["entry_ms"], t["exit_ms"])   # None while open (the UI counts from the entry)
        out["frame"] = picture_frame(t["entry_ms"], t["exit_ms"])     # what the trade's picture shows
        tp0 = out["tp_initial"]
        out["review"] = {
            **{key: flag(t.get(col)) for key, col, _ in REVIEW_QUESTIONS},
            "reviewed": bool(t.get("reviewed_at")), "reviewed_at": t.get("reviewed_at"),
            # the suggestion for "over the target": a high after the exit (until 24:00) at or above the original target
            "over_target_suggested": bool(t.get("whatif_high") is not None and tp0 is not None and t["whatif_high"] >= tp0),
        }
        out["has_image"] = bool(t["image_path"])
        out["entry_rsi_1h"] = t.get("entry_rsi_1h")
        out["excursion"] = None if t.get("mae_pct") is None else {k: t.get(k) for k in ("mae_pct", "mfe_pct", "mae_r", "mfe_r")}
        risk = t.get("risk_usdt")
        after_r = (lambda level: (level - t["exit_price"]) * t["coins"] / risk) if (risk and t.get("exit_price") is not None) else (lambda level: None)
        out["whatif"] = None if not t["whatif_n"] else {
            "n": t["whatif_n"], "high": t["whatif_high"], "low": t["whatif_low"],
            "above_exit_pct": t["whatif_above_exit_pct"], "below_exit_pct": t["whatif_below_exit_pct"],
            # the same two moves in R: the best move above and the worst move below the exit, in multiples of the risk
            "above_exit_r": after_r(t["whatif_high"]), "below_exit_r": after_r(t["whatif_low"]),
            "high_vs_entry_pct": t["whatif_high_vs_entry_pct"], "low_vs_entry_pct": t["whatif_low_vs_entry_pct"],
        }
        out["alts"] = alts_public(t)
        out["exits_ac"] = ac_public(t)
        if t["status"] == "open" and price is not None:
            mv = t["coins"] * price
            unrealized = mv * (1.0 - fee_rate) - t["position_value"] - t["buy_fee"]  # if sold now, after fees
            out["market_value"] = mv
            out["unrealized_pnl"] = unrealized
            out["unrealized_pnl_pct"] = unrealized / t["position_value"] * 100.0 if t["position_value"] else None
            out["r_now"] = unrealized / t["risk_amount"] if t["risk_amount"] else None
            # the price against the entry, the stop and the target (percent of the current price)
            out["price_change_pct"] = (price / t["entry_price"] - 1.0) * 100.0
            out["price_change_usdt"] = t["coins"] * (price - t["entry_price"])   # the position against the buy, before fees
            out["to_stop_pct"] = (t["stop_used"] / price - 1.0) * 100.0
            out["to_tp_pct"] = (t["take_profit"] / price - 1.0) * 100.0
        else:
            out["market_value"] = out["unrealized_pnl"] = out["unrealized_pnl_pct"] = out["r_now"] = None
            out["price_change_pct"] = out["price_change_usdt"] = out["to_stop_pct"] = out["to_tp_pct"] = None
        return out

    # ----------------------------------------------------------------- helpers
    @property
    def _base_ms(self) -> int:
        return config.interval_ms(BASE_INTERVAL)

    def _day_row(self, session_id: str) -> dict:
        with self.store.tx() as conn:
            ds = self.store.get_day_session(conn, session_id)
        if ds is None:
            self.replay._get(session_id)  # NotFound when there is no session at all
            raise Invalid(f"session {session_id} is not a day-trading session")
        return ds

    def _get_day(self, session_id: str) -> dict:
        s = self.replay._get(session_id)
        if (s.get("mode") or "swing") != "day":
            raise Invalid(f"session {session_id} is not a day-trading session")
        return s

    def _ready(self, session_id: str) -> dict:
        s = self.replay._get_ready(session_id)
        if (s.get("mode") or "swing") != "day":
            raise Invalid(f"session {session_id} is not a day-trading session")
        return s

    @staticmethod
    def _note(note: str | None, name: str) -> str | None:
        if note is None:
            return None
        if not isinstance(note, str):
            raise Invalid(f"{name} must be text")
        note = note.strip()
        if len(note) > NOTE_MAX_CHARS:
            raise Invalid(f"{name} must be at most {NOTE_MAX_CHARS} characters")
        return note or None

    @staticmethod
    def _atr_from_df(df: pd.DataFrame) -> float | None:
        if len(df) <= ATR_PERIOD:
            return None
        v = float(core.atr(df["high"], df["low"], df["close"], ATR_PERIOD).iloc[-1])
        return v if math.isfinite(v) else None
