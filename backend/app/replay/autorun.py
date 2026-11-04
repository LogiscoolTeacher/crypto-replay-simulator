"""Autorun: a day session trades a jump strategy's signals by itself (the "CTest" data collection).

The run plays the replay the way a trader would with the buttons, and every decision only reads
candles that have closed by then:

1. **Next signal** (E): jump to the next RLine_Retest signal (its 15-minute candle, see
   `DayEngine.next_signal`).
2. **Confirmation**: step through the next `confirm_bars` 15-minute candles one by one. For each:
   - a close below the zone bottom cancels the signal (`zone_broken`)
   - a **hammer** (a body, lower wick >= `hammer_wick_body` x body, upper wick <= `hammer_upper_body`
     x body, low reaching the signal's entry level = zone top + near-zone buffer) confirms it
   - so does a **bullish engulfing** (a green candle whose body covers the previous red one's)
   If none of them confirms, the outcome is `no_confirmation`.
3. **Entry** at the close of the confirming candle. The stop is the zone bottom - `stop_atr_mult` x
   ATR 14 of the last closed `stop_atr_interval` candle; the target is `ratio` R. The size comes
   from the session's fixed risk.
   **Fee filter**: the estimated round trip (the buy fee twice: buy and sell at the entry price) must
   stay below `max_fee_share` of the risk, else the outcome is `fees_too_high`.
4. **Trade**: step until the stop or the target is hit (the engine's normal exits). Then look for
   the next signal again.

Signals the run passes while a confirmation window or a trade is running are logged too
(`waiting` / `trade_open`), so the signal log (`day_signal_log`) holds every signal of the period.
The run ends after `max_trades` trades, at `entry_until` (no entry for a later signal; the last
trade still runs to its exit) or at the end of the data.

**Pictures** (`pictures`, on by default): the frontend takes them (day.js). `advance` therefore
returns before every step or jump that leaves a day whose day picture is missing (`need:
"day_picture"`), and after a `picture` event (the trade picture at 24:00 of the exit day). The
frontend loop (`DayMode.autoRun`) takes the picture and calls `advance` again. Without pictures
(tests, headless) nothing waits.

**Report** (`report`): the signal funnel, the trades, and two "what if" results per signal. *Raw*
enters at the signal candle's close; *confirmed* enters at the confirmation close (for every
confirmed signal, traded or filtered). Both use the same stop rule and target, walk forward until
the stop or target is hit (both in one candle = the stop), and every figure is split by the daily
trend (close above EMA 200). The what-ifs look into the future on purpose: they are filled only
by the report and never feed a decision.
"""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import Any

import numpy as np

from .. import config
from ..indicators import core
from ..indicators.registry import validate_params
from ..strategies.rline import get_jump_strategy
from .day_engine import ATR_PERIOD, BASE_INTERVAL, DayEngine, day_bounds, local_str, trading_day
from .engine import Conflict, Invalid, _iso, _now_iso

# What became of a signal (day_signal_log.outcome); NULL while its confirmation window runs.
OUTCOMES = {
    "traded": "Traded",
    "no_confirmation": "No confirmation within the window",
    "zone_broken": "Zone broken (a close below the zone)",
    "fees_too_high": "Fees too high for the risk",
    "trade_open": "Skipped: a trade was open",
    "waiting": "Skipped: another signal's window was running",
    "invalid": "No valid trade (stop or ATR missing)",
    "end_of_data": "The replay ended during the window",
}
TRENDS = ("all", "above", "below", "unknown")   # the daily close above / below the EMA 200 at the signal
MAX_STEPS_PER_CALL = 400   # one advance call never runs longer than this many 15m steps (the UI stays fresh)


@dataclass
class AutorunRules:
    """The rules of one run; saved with it, so every CTest__NN keeps its own and the runs stay comparable."""
    strategy: str = "rline_retest"
    params: dict = field(default_factory=dict)   # the jump strategy's parameters (defaults when empty)
    confirm_bars: int = 10
    hammer: bool = True
    engulfing: bool = True
    hammer_wick_body: float = 2.0
    hammer_upper_body: float = 1.0
    stop_atr_mult: float = 0.25
    stop_atr_interval: str = "1h"
    ratio: float = 2.0
    max_fee_share: float = 0.2
    max_trades: int = 100
    entry_until: str | None = None   # ISO date: no entry for a signal whose candle closes after its 00:00 UTC
    pictures: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> AutorunRules:
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        bad = sorted(set(d) - known)
        if bad:
            raise Invalid(f"Unknown autorun rules: {', '.join(bad)}.")
        r = cls(**d)
        try:
            js = get_jump_strategy(r.strategy)
            r.params = validate_params(js.params, r.params)
        except ValueError as e:
            raise Invalid(str(e)) from None
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)  # noqa: E731
        if not (isinstance(r.confirm_bars, int) and 1 <= r.confirm_bars <= 500):
            raise Invalid("confirm_bars must be a whole number from 1 to 500.")
        if not (isinstance(r.max_trades, int) and 1 <= r.max_trades <= 100_000):
            raise Invalid("max_trades must be a whole number from 1 to 100000.")
        for name in ("hammer_wick_body", "hammer_upper_body", "stop_atr_mult", "ratio", "max_fee_share"):
            if not num(getattr(r, name)) or getattr(r, name) < 0:
                raise Invalid(f"{name} must be a number >= 0.")
        if r.ratio <= 0:
            raise Invalid("ratio must be above 0.")
        if r.stop_atr_interval not in ("15m", "1h", "4h"):
            raise Invalid("stop_atr_interval must be 15m, 1h or 4h.")
        if not (r.hammer or r.engulfing):
            raise Invalid("Choose at least one confirmation pattern.")
        if r.entry_until is not None:
            try:
                date.fromisoformat(r.entry_until)
            except (TypeError, ValueError):
                raise Invalid("entry_until must be an ISO date (YYYY-MM-DD).") from None
        return r


# ---------------------------------------------------------------- patterns
Candle = tuple[float, float, float, float]   # open, high, low, close


def is_hammer(c: Candle, entry_level: float, wick_body: float = 2.0, upper_body: float = 1.0) -> bool:
    """A hammer at the zone: a real body, a lower wick of at least `wick_body` x the body, an upper
    wick of at most `upper_body` x the body, and the low reaching `entry_level` (either colour)."""
    o, h, l, cl = c
    body = abs(cl - o)
    if body <= 0:
        return False
    return (min(o, cl) - l) >= wick_body * body and (h - max(o, cl)) <= upper_body * body and l <= entry_level


def is_bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    """A green candle whose body covers the previous red candle's body."""
    po, _, _, pc = prev
    o, _, _, c = cur
    return pc < po and c > o and o <= pc and c >= po


def confirmation(prev: Candle | None, cur: Candle, zone_bottom: float, entry_level: float, rules: AutorunRules) -> str | None:
    """What candle `cur` says about a waiting signal: "broken" (it closes below the zone bottom),
    "hammer", "engulfing" or None (keep waiting). The break wins over a pattern."""
    if cur[3] < zone_bottom:
        return "broken"
    if rules.hammer and is_hammer(cur, entry_level, rules.hammer_wick_body, rules.hammer_upper_body):
        return "hammer"
    if rules.engulfing and prev is not None and is_bullish_engulfing(prev, cur):
        return "engulfing"
    return None


def walk(times: np.ndarray, highs: np.ndarray, lows: np.ndarray, start: int, stop: float, target: float) -> tuple[str, int | None]:
    """The first candle from index `start` on that reaches the stop or the target: ("stop" | "target",
    its open time ms), or ("open", None) when neither is reached. Both in one candle count as the stop."""
    lo = np.flatnonzero(lows[start:] <= stop)
    hi = np.flatnonzero(highs[start:] >= target)
    i_lo = int(lo[0]) if len(lo) else None
    i_hi = int(hi[0]) if len(hi) else None
    if i_lo is None and i_hi is None:
        return "open", None
    if i_hi is None or (i_lo is not None and i_lo <= i_hi):
        return "stop", int(times[start + i_lo])
    return "target", int(times[start + i_hi])


def r_results(result: str, entry: float, stop: float, target: float, fee_rate: float) -> tuple[float | None, float | None]:
    """(gross R, net R) of a what-if exit. Net subtracts the fees of buying at the entry and selling at
    the exit, in R (the fees per coin over the risk per coin)."""
    if result not in ("stop", "target"):
        return None, None
    exit_price = stop if result == "stop" else target
    risk = entry - stop
    gross = (exit_price - entry) / risk
    return gross, gross - fee_rate * (entry + exit_price) / risk


def stats(values: list[float]) -> dict:
    """Count, wins, win rate, average and total R, and the largest drawdown of the running R total."""
    n = len(values)
    total = float(sum(values))
    run = peak = dd = 0.0
    for v in values:
        run += v
        peak = max(peak, run)
        dd = max(dd, peak - run)
    wins = sum(1 for v in values if v > 0)
    return {"count": n, "wins": wins, "losses": n - wins, "win_rate": wins / n * 100.0 if n else None,
            "avg_r": total / n if n else None, "total_r": total, "max_dd_r": dd}


def trend_key(ema_above: int | None) -> str:
    return "unknown" if ema_above is None else "above" if ema_above else "below"


# ------------------------------------------------------------------ runner
class Autorun:
    """The automatic run of one day session (one run per session, kept in day_sessions.autorun)."""

    def __init__(self, day: DayEngine) -> None:
        self.day = day
        self.store = day.store
        self.candles = day.candles

    # ------------------------------------------------------------ public
    def start(self, session_id: str, rules: dict | None = None) -> dict:
        r = AutorunRules.from_dict(rules)
        with self.day._lock:
            s = self.day._ready(session_id)
            ds = self.day._day_row(session_id)
            if ds.get("autorun"):
                raise Conflict("This session already has an autorun (reset the session, or start a new one).")
            with self.store.tx() as conn:
                if self.store.open_trade(conn, session_id) is not None:
                    raise Conflict("A trade is open: close it before starting an autorun.")
            run = {"rules": asdict(r), "phase": "idle", "signal": None, "waited": 0, "seen_ms": s["cursor_ms"],
                   "trades": 0, "done": None, "started_at": _now_iso(), "start_ms": s["cursor_ms"]}
            self._save(session_id, run)
        return self.status(session_id)

    def status(self, session_id: str) -> dict:
        run = self._load(session_id)
        with self.store.tx() as conn:
            rows = self.store.list_signals(conn, session_id)
            trades = self.store.list_trades(conn, session_id)
        counts = {k: 0 for k in OUTCOMES}
        for row in rows:
            if row["outcome"]:
                counts[row["outcome"]] += 1
        closed = [t for t in trades if t["status"] == "closed"]
        return {
            "phase": run["phase"], "done": run["done"], "rules": run["rules"],
            "signals": len(rows), "outcomes": counts, "waiting_signal": run["signal"], "waited": run["waited"],
            "trades_opened": run["trades"], "trades_closed": len(closed),
            "r_total": sum(t["r_multiple"] or 0.0 for t in closed),
            "pnl_total": sum(t["pnl"] or 0.0 for t in closed),
        }

    def advance(self, session_id: str, max_steps: int = MAX_STEPS_PER_CALL) -> dict:
        """Run until the frontend has something to do (see the module docstring), the run is done or
        `max_steps` candles have been stepped. Returns the session state with `events` and `autorun`
        (the status plus `need`: "day_picture" or None)."""
        events: list[dict] = []
        need = None
        with self.day._lock:
            run = self._load(session_id)
            rules = AutorunRules.from_dict(run["rules"])
            st = self.day.state(session_id)
            steps = 0
            while run["phase"] != "done" and steps < max_steps:
                if st["finished"]:
                    self._end_of_data(session_id, run, st)
                    break
                phase = run["phase"]
                if phase == "idle":
                    st, need, stop, stepped = self._idle(session_id, run, rules, st, events)
                elif phase == "waiting":
                    st, need, stop, stepped = self._waiting(session_id, run, rules, st, events)
                elif phase == "trade":
                    st, need, stop, stepped = self._trade(session_id, run, rules, st, events)
                else:   # finishing: only the last trade picture is still to come
                    st, need, stop, stepped = self._finishing(session_id, run, rules, st, events)
                steps += stepped
                self._save(session_id, run)
                if need or stop:
                    break
            self._save(session_id, run)
        st = self.day.state(session_id)
        st["events"] = events
        st["autorun"] = {**self.status(session_id), "need": need}
        return st

    # ------------------------------------------------------------ phases
    def _idle(self, sid: str, run: dict, rules: AutorunRules, st: dict, events: list) -> tuple:
        s = self.day._ready(sid)
        if run["trades"] >= rules.max_trades:
            return self._finish(run, f"{rules.max_trades} trades taken", st)
        sigs = self._upcoming(s, rules, run)
        if not sigs:
            return self._finish(run, "no more signals before the end of the data", st)
        nxt = sigs[0]
        base = self.day._base_ms
        if rules.entry_until is not None and nxt["cursor_ms"] + base > self._until_ms(rules):
            return self._finish(run, f"entry period over ({rules.entry_until})", st)
        if rules.pictures:
            # a closed trade's picture becomes due (its 24:00) before the signal: step there first
            with self.store.tx() as conn:
                due = self.store.due_pictures(conn, sid)
            if any(self.day._review_end(t) <= nxt["cursor_ms"] + base for t in due):
                st, need, stop = self._step(sid, rules, st, events)
                return st, need, stop, 0 if need else 1
            leaves = trading_day(nxt["cursor_ms"] + base) != trading_day(s["cursor_ms"] + base)
            if leaves and not st["day"]["check"]["has_image"]:
                return st, "day_picture", True, 0   # the jump leaves the day: its picture first
        st = self.day.next_signal(sid, rules.strategy, rules.params)
        evs = st.pop("events", [])
        events.extend(evs)
        ev = next((e for e in evs if e["kind"] == "jump"), None)
        sig = {**nxt}
        if ev is not None and ev.get("signal"):
            sig.update(ev["signal"])
        sig["cursor_ms"] = st["cursor_ms"]
        s = self.day._ready(sid)
        with self.store.tx() as conn:
            row = self.store.add_signal(conn, session_id=sid, **self._signal_fields(s, sig), outcome=None,
                                        created_at=_now_iso(), updated_at=_now_iso())
        run["signal"] = {"log_id": row["id"], "cursor_ms": sig["cursor_ms"], "bar_ms": sig["bar_ms"],
                         "zone_top": sig["zone_top"], "zone_bottom": sig["zone_bottom"],
                         "entry_level": sig.get("entry_level", sig["zone_top"]), "rules": sig.get("rules", []),
                         "touches": sig.get("touches"), "breakout_ms": sig.get("breakout_ms"), "psych": sig.get("psych")}
        run["waited"] = 0
        run["seen_ms"] = sig["cursor_ms"]
        run["phase"] = "waiting"
        return st, None, True, 0

    def _waiting(self, sid: str, run: dict, rules: AutorunRules, st: dict, events: list) -> tuple:
        sig = run["signal"]
        if run["waited"] >= rules.confirm_bars:
            self._close_signal(sid, run, "no_confirmation")
            return st, None, False, 0
        st, need, stop = self._step(sid, rules, st, events)
        if need:
            return st, need, True, 0
        run["waited"] += 1
        s = self.day._ready(sid)
        cur = self._candle(s, s["cursor_ms"])
        if cur is None:   # a data gap: nothing to read on this step
            return st, None, stop, 1
        prev = self._candle(s, s["cursor_ms"] - self.day._base_ms)
        res = confirmation(prev, cur, sig["zone_bottom"], sig["entry_level"], rules)
        if res == "broken":
            self._close_signal(sid, run, "zone_broken", confirm_ms=s["cursor_ms"])
        elif res is not None:
            st, bought = self._enter(sid, run, rules, st, res, events)
            stop = stop or bought
        elif run["waited"] >= rules.confirm_bars:
            self._close_signal(sid, run, "no_confirmation")
        return st, None, stop, 1

    def _trade(self, sid: str, run: dict, rules: AutorunRules, st: dict, events: list) -> tuple:
        if st["day"]["trade"] is None:   # closed (stop or target): look for the next signal
            run["phase"] = "idle"
            return st, None, False, 0
        st, need, stop = self._step(sid, rules, st, events)
        if need:
            return st, need, True, 0
        if st["day"]["trade"] is None:
            run["phase"] = "idle"
        return st, None, stop, 1

    def _finishing(self, sid: str, run: dict, rules: AutorunRules, st: dict, events: list) -> tuple:
        with self.store.tx() as conn:
            due = self.store.due_pictures(conn, sid) if rules.pictures else []
        if not due:
            run["phase"] = "done"
            return st, None, True, 0
        st, need, stop = self._step(sid, rules, st, events)   # on to the last trade's 24:00
        return st, need, stop or bool(need), 0 if need else 1

    def _finish(self, run: dict, reason: str, st: dict) -> tuple:
        run["done"] = reason
        run["phase"] = "finishing"
        return st, None, False, 0

    def _end_of_data(self, sid: str, run: dict, st: dict) -> None:
        if run["phase"] == "waiting" and run["signal"]:
            self._close_signal(sid, run, "end_of_data")
        open_trade = st["day"]["trade"] is not None
        run["done"] = run["done"] or ("end of the data, a trade is still open" if open_trade else "end of the data")
        run["phase"] = "done"

    # ------------------------------------------------------------- steps
    def _step(self, sid: str, rules: AutorunRules, st: dict, events: list) -> tuple[dict, str | None, bool]:
        """One 15m step, after the day picture if this step leaves a day without one. Returns
        (state, need, stop): stop after an exit and after a trade picture became due."""
        d = st["day"]
        if rules.pictures and d.get("leaving_next") and not d["check"]["has_image"]:
            return st, "day_picture", True
        st = self.day.step(sid, 1)
        evs = st.pop("events", [])
        events.extend(evs)
        stop = any(e["kind"] == "exit" or (rules.pictures and e["kind"] == "picture") for e in evs)
        return st, None, stop

    def _enter(self, sid: str, run: dict, rules: AutorunRules, st: dict, pattern: str, events: list) -> tuple[dict, bool]:
        """The confirmation candle closed: plan the trade, apply the fee filter, buy. Returns (state, bought)."""
        sig = run["signal"]
        s = self.day._ready(sid)
        atr = self._atr(s, rules.stop_atr_interval, s["cursor_ms"] + self.day._base_ms)
        fields_: dict[str, Any] = {"confirm_ms": s["cursor_ms"], "pattern": pattern, "atr": atr}
        if atr is None:
            self._close_signal(sid, run, "invalid", **fields_)
            return st, False
        stop = sig["zone_bottom"] - rules.stop_atr_mult * atr
        zone = self.day._signal_zone(sid, sig)   # back if crossing 00:00 removed the unused signal zone
        p = self.day.plan(sid, zone["id"], stop=stop, ratio=rules.ratio)
        entry = p["entry_price"]
        fields_.update(entry=entry, stop=stop, take_profit=p["take_profit"],
                       stop_dist_pct=(entry - stop) / entry * 100.0 if entry else None)
        if p["errors"] or p["blocked"] or p["take_profit"] is None:
            self._close_signal(sid, run, "invalid", **fields_)
            return st, False
        est = 2.0 * p["buy_fee"]
        share = est / p["risk_amount"]
        fields_.update(est_fees=est, fee_share=share)
        if share >= rules.max_fee_share:
            self._close_signal(sid, run, "fees_too_high", **fields_)
            return st, False
        note = (f"Autorun: {pattern} at {local_str(s['cursor_ms'] + self.day._base_ms)} after the {rules.strategy} signal "
                f"at {local_str(sig['cursor_ms'] + self.day._base_ms)}; stop zone bottom - {rules.stop_atr_mult:g} x ATR "
                f"{rules.stop_atr_interval}, target {rules.ratio:g} R.")
        res = self.day.buy(sid, zone["id"], take_profit=None, stop=stop, ratio=rules.ratio, notes=note)
        st, trade = res["state"], res["trade"]
        run["trades"] += 1
        self._close_signal(sid, run, "traded", trade_id=trade["id"], **fields_)
        run["phase"] = "trade"
        events.append({"kind": "autorun_buy", "n": run["trades"], "pattern": pattern, "trade": trade})
        return st, True

    def _close_signal(self, sid: str, run: dict, outcome: str, **f: Any) -> None:
        with self.store.tx() as conn:
            self.store.update_signal(conn, run["signal"]["log_id"], outcome=outcome, updated_at=_now_iso(), **f)
        run["signal"] = None
        run["waited"] = 0
        if run["phase"] == "waiting":
            run["phase"] = "idle"

    # ----------------------------------------------------------- signals
    def _upcoming(self, s: dict, rules: AutorunRules, run: dict) -> list[dict]:
        """The signals after the cursor (refined to their 15m candle), in order. The ones between the
        last signal the run handled and the cursor were passed during a window or a trade: they are
        logged here (`waiting` / `trade_open`)."""
        js = get_jump_strategy(rules.strategy)
        iv_ms = config.interval_ms(js.interval)
        passed, upcoming = [], []
        for sig in self.day._strategy_signals(s, js, rules.params):
            if sig["bar_ms"] + iv_ms <= run["seen_ms"]:
                continue
            if "cursor_ms" not in sig:
                sig["cursor_ms"] = self.day._refine_signal(s, sig, iv_ms)
            c = sig["cursor_ms"]
            if c is None or c <= run["seen_ms"] or c > s["data_end_ms"]:
                continue
            (passed if c <= s["cursor_ms"] else upcoming).append(sig)
        if passed:
            with self.store.tx() as conn:
                trades = self.store.list_trades(conn, s["id"])
                for sig in passed:
                    busy = any(t["entry_ms"] <= sig["cursor_ms"] and (t["exit_ms"] is None or sig["cursor_ms"] <= t["exit_ms"]) for t in trades)
                    self.store.add_signal(conn, session_id=s["id"], **self._signal_fields(s, sig),
                                          outcome="trade_open" if busy else "waiting", created_at=_now_iso(), updated_at=_now_iso())
            run["seen_ms"] = max(sig["cursor_ms"] for sig in passed)
        return upcoming

    def _signal_fields(self, s: dict, sig: dict) -> dict:
        ema = self.day._daily_ema(dict(s, cursor_ms=sig["cursor_ms"]))["ema_above"]
        return {"signal_ms": sig["cursor_ms"], "bar_ms": sig["bar_ms"], "zone_top": float(sig["zone_top"]),
                "zone_bottom": float(sig["zone_bottom"]), "entry_level": float(sig.get("entry_level", sig["zone_top"])),
                "rules": ",".join(sig.get("rules") or []), "touches": sig.get("touches"), "ema_above": ema}

    # ----------------------------------------------------------- candles
    def _candle(self, s: dict, open_ms: int) -> Candle | None:
        df = self.candles.get_candles(s["symbol"], BASE_INTERVAL, open_ms, open_ms)
        if len(df) != 1:
            return None
        r = df.iloc[0]
        return float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])

    def _atr(self, s: dict, interval: str, close_ms: int) -> float | None:
        """ATR 14 of the last `interval` candle closed by `close_ms` (Wilder, like the app's ATR)."""
        iv = config.interval_ms(interval)
        df = self.candles.get_candles(s["symbol"], interval, close_ms - (ATR_PERIOD * 10 + 1) * iv, close_ms - iv)
        if len(df) <= ATR_PERIOD:
            return None
        v = float(core.atr(df["high"], df["low"], df["close"], ATR_PERIOD).iloc[-1])
        return v if math.isfinite(v) else None

    @staticmethod
    def _until_ms(rules: AutorunRules) -> int:
        return day_bounds(date.fromisoformat(rules.entry_until))[0]   # type: ignore[arg-type]

    # ------------------------------------------------------------ report
    def report(self, session_id: str) -> dict:
        """The run's results: the signal funnel, the trades, and the raw / confirmed what-ifs, each
        split by trend. Fills the what-if columns of the signal log on the way."""
        with self.day._lock:
            run = self._load(session_id)
            rules = AutorunRules.from_dict(run["rules"])
            s = self.day._ready(session_id)
            self._fill_what_ifs(s, rules)
            with self.store.tx() as conn:
                rows = self.store.list_signals(conn, session_id)
                trades = {t["id"]: t for t in self.store.list_trades(conn, session_id)}
        by_trend = lambda pick: {k: stats([v for row, v in pick if k == "all" or trend_key(row["ema_above"]) == k]) for k in TRENDS}  # noqa: E731
        funnel = {k: {o: 0 for o in OUTCOMES} for k in TRENDS}
        for row in rows:
            if row["outcome"]:
                funnel["all"][row["outcome"]] += 1
                funnel[trend_key(row["ema_above"])][row["outcome"]] += 1
        traded = [(row, trades[row["trade_id"]]) for row in rows if row["trade_id"] in trades]
        closed = [(row, t) for row, t in traded if t["status"] == "closed"]
        closed.sort(key=lambda x: x[1]["exit_ms"])
        out = {
            "session_id": session_id, "name": s["name"], "rules": run["rules"], "phase": run["phase"], "done": run["done"],
            "signals": len(rows),
            "funnel": funnel,
            "trades": by_trend([(row, t["r_multiple"] or 0.0) for row, t in closed]),
            "pnl": {k: sum(t["pnl"] or 0.0 for row, t in closed if k == "all" or trend_key(row["ema_above"]) == k) for k in TRENDS},
            "open_trades": sum(1 for _, t in traded if t["status"] == "open"),
            "what_if_raw": by_trend([(row, row["raw_net_r"]) for row in rows if row["raw_net_r"] is not None]),
            "what_if_confirmed": by_trend([(row, row["conf_net_r"]) for row in rows if row["conf_net_r"] is not None]),
            "what_if_open": {"raw": sum(1 for row in rows if row["raw_result"] == "open"),
                             "confirmed": sum(1 for row in rows if row["conf_result"] == "open")},
        }
        return out

    def _fill_what_ifs(self, s: dict, rules: AutorunRules) -> None:
        """Raw and confirmed what-if results for every logged signal still without them (the candles
        up to the session's end; a result still "open" is retried on the next report)."""
        with self.store.tx() as conn:
            rows = [r for r in self.store.list_signals(conn, s["id"])
                    if r["signal_ms"] is not None and (r["raw_result"] in (None, "open") or (r["entry"] is not None and r["conf_result"] in (None, "open")))]
        if not rows:
            return
        df = self.candles.get_candles(s["symbol"], BASE_INTERVAL, min(r["signal_ms"] for r in rows), s["data_end_ms"])
        times = df["time"].to_numpy(dtype="int64") * 1000
        highs, lows, closes = (df[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
        fee = float(s["fee_rate"])
        base = self.day._base_ms
        for r in rows:
            f: dict[str, Any] = {}
            if r["raw_result"] in (None, "open"):
                i = int(np.searchsorted(times, r["signal_ms"]))
                atr = self._atr(s, rules.stop_atr_interval, r["signal_ms"] + base)
                if i >= len(times) or times[i] != r["signal_ms"] or atr is None:
                    f.update(raw_result="invalid")
                else:
                    entry = float(closes[i])
                    stop = r["zone_bottom"] - rules.stop_atr_mult * atr
                    if entry <= stop:
                        f.update(raw_entry=entry, raw_stop=stop, raw_result="invalid")
                    else:
                        tp = entry + rules.ratio * (entry - stop)
                        res, exit_ms = walk(times, highs, lows, i + 1, stop, tp)
                        gross, net = r_results(res, entry, stop, tp, fee)
                        f.update(raw_entry=entry, raw_stop=stop, raw_result=res, raw_r=gross, raw_net_r=net, raw_exit_ms=exit_ms)
            if r["entry"] is not None and r["stop"] is not None and r["take_profit"] is not None and r["conf_result"] in (None, "open"):
                i = int(np.searchsorted(times, r["confirm_ms"]))
                res, exit_ms = walk(times, highs, lows, i + 1, r["stop"], r["take_profit"])
                gross, net = r_results(res, r["entry"], r["stop"], r["take_profit"], fee)
                f.update(conf_result=res, conf_r=gross, conf_net_r=net, conf_exit_ms=exit_ms)
            if f:
                with self.store.tx() as conn:
                    self.store.update_signal(conn, r["id"], updated_at=_now_iso(), **f)

    def signals(self, session_id: str) -> list[dict]:
        self._load(session_id)
        with self.store.tx() as conn:
            rows = self.store.list_signals(conn, session_id)
        for r in rows:
            r["outcome_label"] = OUTCOMES.get(r["outcome"]) if r["outcome"] else None
            r["trend"] = trend_key(r["ema_above"])
            for k in ("signal_ms", "bar_ms", "confirm_ms", "raw_exit_ms", "conf_exit_ms"):
                r[k.replace("_ms", "_iso")] = _iso(r[k]) if r[k] is not None else None
        return rows

    def signals_csv(self, session_id: str) -> str:
        rows = self.signals(session_id)
        buf = io.StringIO()
        cols = [c for c in (rows[0].keys() if rows else ["seq"]) if c not in ("session_id", "id", "created_at", "updated_at")]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    # ----------------------------------------------------------- storage
    def _load(self, session_id: str) -> dict:
        ds = self.day._day_row(session_id)
        if not ds.get("autorun"):
            raise Conflict("This session has no autorun.")
        return json.loads(ds["autorun"])

    def _save(self, session_id: str, run: dict) -> None:
        with self.store.tx() as conn:
            self.store.update_day_session(conn, session_id, autorun=json.dumps(run), updated_at=_now_iso())
