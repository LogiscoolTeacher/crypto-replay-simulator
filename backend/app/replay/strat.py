"""Strategy sessions: a day session with a setup chosen before the start, limits and an end flag.

The session itself is an ordinary day-trading session (15-minute candles, the same account, risk
and fee: see day_engine.py), so the chart, the trades and the pictures work as they do there. What
this module adds lives in a few columns of `day_sessions`:

**Setup** (`create`): the mode, the strategy and the two limits.
- `run_mode`: "automatic" (the strategy places entry, stop and target; the trade runs until one is
  hit) or "manual" (the replay jumps to the strategy's next entry point and proposes the trade; the
  trader takes or skips it and may move stop and target). Automatic runs in `strat_run.py`; the
  Manual engine comes with the next build step. This module is what both stand on.
- the strategy, picked from the registered plug-ins (`strategies/plugin.py`). Its name, version,
  timeframe and settings are copied into the session and into every trade, so a result stays
  readable when the plug-in file changes later. The timeframe is the strategy's, never typed.
- `max_trades` (N) and the time limit. Manual: `time_limit_hours` of REAL time, counted only while
  the session is open in the browser (`add_clock`; 0 = no limit). Automatic: the replay's end
  date is the time limit, so hours are not accepted there.
- `pictures`: trade pictures on or off.

An **Automatic** session is traded by its strategy alone (`strat_run.py`, the Start button): the
buttons that trade or move the replay by hand are refused for it (`manual_guard`). Its run's
status is the `run` part of the session block.

**End** (`settle`, called by `DayEngine.state` after every action; `stop`). The first limit reached
ends the session, and the flag says which:
- `ENDED_BY_TRADE_LIMIT`: N trades were taken and the last one has closed.
- `ENDED_BY_TIMEOUT`: the replay reached its last candle (its end date), or the Manual hours ran out.
- `ENDED_BY_USER`: the Stop button.
A trade still open at a timeout or a stop is closed at market: at the close of the cursor's candle,
with the exit type "session timeout" / "session stopped" (`timeout_rule` = "close_at_market",
stored with the session and every trade). Its hands-off twin keeps the original stop and target, so
it shows what letting the trade run would have made. Once a session has ended nothing decides
anything any more, so the twins and the 24 hours after each exit are read to the end of the data.
An ended session takes no more trades; a reset starts it over with the same setup.
"""
from __future__ import annotations

import json
import math

from .. import config
from ..strategies.plugin import get_trading_strategy
from .day_engine import AC_KEYS, BASE_INTERVAL, EXIT_STOPPED, EXIT_TIMEOUT, DayEngine, local_str
from .engine import Conflict, Invalid, _iso, _now_iso

AUTOMATIC, MANUAL = "automatic", "manual"
MODES = {AUTOMATIC: "Automatic", MANUAL: "Manual"}
ENDED_BY_TRADE_LIMIT, ENDED_BY_TIMEOUT, ENDED_BY_USER = "ENDED_BY_TRADE_LIMIT", "ENDED_BY_TIMEOUT", "ENDED_BY_USER"
END_FLAGS = {
    ENDED_BY_TRADE_LIMIT: "Trade limit reached",
    ENDED_BY_TIMEOUT: "Time limit reached",
    ENDED_BY_USER: "Stopped by the user",
}
TIMEOUT_RULE = "close_at_market"   # the only rule so far; stored so another one can be told apart later
TIMEOUT_RULES = {TIMEOUT_RULE: "An open trade is closed at the close of the current candle"}
MAX_TRADES_LIMIT = 100_000
MAX_HOURS = 1_000.0
MAX_CLOCK_STEP = 120.0             # seconds one add_clock call may add (the browser reports every few seconds)
DEFAULTS = {"run_mode": AUTOMATIC, "max_trades": 100, "time_limit_hours": 0.0, "pictures": True}
# What became of a signal (day_signal_log.outcome): the engine's own outcomes. Every other one is a
# strategy's code (e.g. "zone_broken", "fees_too_high"), shown with the sentence the strategy gave.
RUN_OUTCOMES = {
    "traded": "Traded",
    "trade_open": "Skipped: a trade was open",
    "waiting": "Skipped: another signal's entry window was running",
    "invalid": "No valid trade",
    "end_of_data": "The replay ended during the entry window",
    "session_ended": "The session ended during the entry window",
}
RUN_PHASES = {
    "new": "Not started", "idle": "Looking for the next signal", "waiting": "Waiting for the entry",
    "trade": "In a trade", "finishing": "Taking the last pictures", "done": "Finished",
}


class StrategySessions:
    """The setup, limits and end of day sessions that have a `run_mode`."""

    def __init__(self, day: DayEngine) -> None:
        self.day = day
        self.store = day.store
        day.strat = self   # DayEngine.state asks `settle` and `block` for every such session

    # ------------------------------------------------------------- catalogue
    @staticmethod
    def setup_options() -> dict:
        """What the New session dialog offers."""
        from ..strategies.plugin import describe_trading_strategies
        return {
            "modes": [{"key": k, "label": v} for k, v in MODES.items()],
            "strategies": describe_trading_strategies(),
            "end_flags": [{"key": k, "label": v} for k, v in END_FLAGS.items()],
            "timeout_rule": {"key": TIMEOUT_RULE, "label": TIMEOUT_RULES[TIMEOUT_RULE]},
            "defaults": dict(DEFAULTS), "max_trades_limit": MAX_TRADES_LIMIT, "max_hours": MAX_HOURS,
            "base_interval": BASE_INTERVAL,
        }

    # ---------------------------------------------------------------- create
    def create(self, symbol: str, start_ms: int, end_ms: int | None, run_mode: str, strategy: str, max_trades: int,
               time_limit_hours: float = 0.0, pictures: bool = True, name: str | None = None) -> dict:
        if run_mode not in MODES:
            raise Invalid(f"run_mode must be one of {list(MODES)}.")
        try:
            st = get_trading_strategy(strategy)
        except ValueError as e:
            raise Invalid(str(e)) from None
        if config.interval_ms(st.timeframe) < config.interval_ms(BASE_INTERVAL):
            raise Invalid(f"{st.label} reads {st.timeframe} candles, finer than the {BASE_INTERVAL} candles of the replay.")
        if isinstance(max_trades, bool) or not isinstance(max_trades, int) or not 1 <= max_trades <= MAX_TRADES_LIMIT:
            raise Invalid(f"The maximum number of trades must be a whole number from 1 to {MAX_TRADES_LIMIT}.")
        hours = time_limit_hours
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or not 0 <= hours <= MAX_HOURS:
            raise Invalid(f"The time limit must be 0 (none) to {MAX_HOURS:g} hours.")
        if run_mode == AUTOMATIC and hours > 0:
            raise Invalid("An Automatic session ends at its end date, not after hours: set the time limit to 0.")
        if not isinstance(pictures, bool):
            raise Invalid("pictures must be true or false.")
        s = self.day.create_session(symbol, start_ms, end_ms,
                                    name=name or f"{MODES[run_mode]} {st.name} {symbol.upper()} from {_iso(start_ms)[:10]}")
        with self.store.tx() as conn:
            self.store.update_day_session(
                conn, s["id"], run_mode=run_mode, strategy_key=st.key, strategy_name=st.name, strategy_version=st.version,
                strategy_timeframe=st.timeframe, strategy_settings=json.dumps(st.settings()),
                max_trades=max_trades, time_limit_hours=float(hours), pictures=int(pictures), timeout_rule=TIMEOUT_RULE,
                clock_seconds=0.0, updated_at=_now_iso())
        return self.day.state(s["id"])

    # ------------------------------------------------------------------ state
    def block(self, st: dict, ds: dict) -> dict:
        """The session's setup, limits and end as the UI shows them (`day.session` of the state)."""
        with self.store.tx() as conn:
            trades = self.store.list_trades(conn, st["id"])
        taken = len(trades)
        hours = float(ds.get("time_limit_hours") or 0.0)
        clock = float(ds.get("clock_seconds") or 0.0)
        try:   # the rules as the plug-in words them today; the stored name / version / settings are the record
            rules = get_trading_strategy(ds["strategy_key"]).rules_text()
        except ValueError:
            rules = []
        return {
            "run_mode": ds["run_mode"], "run_mode_label": MODES.get(ds["run_mode"], ds["run_mode"]),
            "strategy": {"key": ds["strategy_key"], "name": ds["strategy_name"], "version": ds["strategy_version"],
                         "label": f"{ds['strategy_name']} v{ds['strategy_version']}", "timeframe": ds["strategy_timeframe"],
                         "settings": json.loads(ds["strategy_settings"]) if ds.get("strategy_settings") else {}, "rules": rules},
            "max_trades": ds["max_trades"], "trades_taken": taken,
            "trades_closed": sum(1 for t in trades if t["status"] == "closed"),
            "trades_left": max(0, (ds["max_trades"] or 0) - taken),
            "time_limit_hours": hours, "clock_seconds": clock,
            "clock_left_seconds": max(0.0, hours * 3600.0 - clock) if hours > 0 else None,
            "pictures": bool(ds.get("pictures")),
            "timeout_rule": ds.get("timeout_rule"), "timeout_rule_label": TIMEOUT_RULES.get(ds.get("timeout_rule")),
            "ended": bool(ds.get("end_flag")), "end_flag": ds.get("end_flag"),
            "end_flag_label": END_FLAGS.get(ds.get("end_flag")), "end_detail": ds.get("end_detail"),
            "ended_at": ds.get("ended_at"),
            # where the replay stood when the session ended (the close of that candle, on the day clock)
            "ended_local": local_str(ds["ended_ms"] + self.day._base_ms) if ds.get("ended_ms") is not None else None,
            "can_stop": not ds.get("end_flag"),
            # Automatic: the strategy's own run (None for Manual)
            "run": self._run_block(st, ds) if ds["run_mode"] == AUTOMATIC else None,
        }

    def _run_block(self, st: dict, ds: dict) -> dict:
        """The Automatic run as the card shows it: where it stands and what became of the signals so far."""
        run = json.loads(ds["run_state"]) if ds.get("run_state") else None
        with self.store.tx() as conn:
            rows = self.store.list_signals(conn, st["id"])
        counts: dict[str, dict] = {}
        for r in rows:
            if r["outcome"]:
                c = counts.setdefault(r["outcome"], {"key": r["outcome"], "count": 0,
                                                     "label": self.outcome_label(r["outcome"], r.get("outcome_text"))})
                c["count"] += 1
        phase = run["phase"] if run else "new"
        return {
            "started": run is not None, "phase": phase, "phase_label": RUN_PHASES.get(phase, phase), "done": phase == "done",
            # Start / Continue is offered until the run is done; a session ended before its first Start has nothing to run
            "can_run": st["status"] == "ready" and phase != "done" and (run is not None or not ds.get("end_flag")),
            "signals": len(rows), "outcomes": sorted(counts.values(), key=lambda c: -c["count"]),
            "window_candle": run["n"] if run and phase == "waiting" else None,
        }

    @staticmethod
    def outcome_label(outcome: str, text: str | None = None) -> str:
        """An outcome in plain words: the engine's own label, else the strategy's sentence, else its code."""
        return RUN_OUTCOMES.get(outcome) or (text.rstrip(".") if text else outcome.replace("_", " "))

    def manual_guard(self, session_id: str) -> None:
        """Refuse a trade or a move of the replay made by hand in an Automatic session: its strategy
        alone decides (the routes of the buttons call this; the run itself calls the engine directly)."""
        ds = self.store_row(session_id)
        if ds is not None and ds.get("run_mode") == AUTOMATIC:
            raise Conflict("This is an Automatic session: its strategy trades it. Use Start and Pause in the Strategy session card.")

    def store_row(self, session_id: str) -> dict | None:
        with self.store.tx() as conn:
            return self.store.get_day_session(conn, session_id)

    def settle(self, st: dict, ds: dict) -> bool:
        """End the session when a limit is reached (see the module docstring). Returns True when it
        ended just now; the caller reads the session again then. Never calls `DayEngine.state`."""
        if ds.get("end_flag"):
            return False
        with self.store.tx() as conn:
            trades = self.store.list_trades(conn, st["id"])
        n = ds.get("max_trades") or 0
        hours = float(ds.get("time_limit_hours") or 0.0)
        if n and len(trades) >= n and all(t["status"] == "closed" for t in trades):
            self._end(st["id"], ENDED_BY_TRADE_LIMIT, f"{n} trade{'s' if n != 1 else ''} taken", EXIT_TIMEOUT)
        elif st["finished"]:
            self._end(st["id"], ENDED_BY_TIMEOUT, "end of the replay", EXIT_TIMEOUT)
        elif ds["run_mode"] == MANUAL and hours > 0 and float(ds.get("clock_seconds") or 0.0) >= hours * 3600.0:
            self._end(st["id"], ENDED_BY_TIMEOUT, f"time limit of {hours:g} hour{'s' if hours != 1 else ''}", EXIT_TIMEOUT)
        else:
            return False
        return True

    # ---------------------------------------------------------------- actions
    def stop(self, session_id: str) -> dict:
        """The Stop button: the session ends now (ENDED_BY_USER); an open trade is closed at market."""
        with self.day._lock:
            ds = self._row(session_id)
            self.day._ready(session_id)
            if ds.get("end_flag"):
                raise Conflict("The session has already ended.")
            self._end(session_id, ENDED_BY_USER, "stopped by the user", EXIT_STOPPED)
        return self.day.state(session_id)

    def add_clock(self, session_id: str, seconds: float) -> dict:
        """Manual mode's real-time clock: the browser reports the seconds the session was open since
        its last report. Reaching the time limit ends the session (in `settle`, on the state read)."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= MAX_CLOCK_STEP:
            raise Invalid(f"seconds must be above 0 and at most {MAX_CLOCK_STEP:g}.")
        with self.day._lock:
            ds = self._row(session_id)
            if ds["run_mode"] != MANUAL:
                raise Conflict("Only a Manual session has a real-time clock.")
            if not ds.get("end_flag"):
                with self.store.tx() as conn:
                    self.store.update_day_session(conn, session_id, clock_seconds=float(ds.get("clock_seconds") or 0.0) + float(seconds),
                                                  updated_at=_now_iso())
        return self.day.state(session_id)

    # ---------------------------------------------------------------- helpers
    def _row(self, session_id: str) -> dict:
        ds = self.day._day_row(session_id)
        if not ds.get("run_mode"):
            raise Conflict("This is not a strategy session.")
        return ds

    def _end(self, session_id: str, flag: str, detail: str, exit_type: str) -> None:
        """Close an open trade at market (the timeout rule), set the end flag, then let the twins and
        the after-exit windows run to the end of the data: nothing is decided after the end."""
        day = self.day
        with day._lock:
            s = day._ready(session_id)
            with self.store.tx() as conn:
                trade = self.store.open_trade(conn, session_id)
            if trade is not None:
                price = day.replay._price(s)
                day._close_legs(s, trade, AC_KEYS, s["cursor_ms"], price, exit_type)
                day._exit(s, trade, s["cursor_ms"], price, exit_type, note=f"Session ended: {detail}.")
            now = _now_iso()
            with self.store.tx() as conn:
                # no "End the day / Keep trading" choice after the closing exit: the session is over
                self.store.update_day_session(conn, session_id, end_flag=flag, end_detail=detail, ended_at=now,
                                              ended_ms=s["cursor_ms"], phase="trading", last_exit_id=None, updated_at=now)
            day._settle_auto(dict(s, cursor_ms=s["data_end_ms"]))
            with self.store.tx() as conn:
                for t in self.store.list_trades(conn, session_id):
                    day._fill_after(s, conn, t, s["data_end_ms"])
