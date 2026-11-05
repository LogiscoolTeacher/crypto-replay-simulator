"""The Report tab: results across day sessions, with filters, a trade table and a CSV export.

Which sessions: every day session that is ready. Its **mode** in the report:
- a strategy session: its `run_mode` (Automatic / Manual) and its strategy (name and version);
- an old day session with an autorun (the CTest__NN runs): Automatic, strategy "Autorun" with the
  rules saved with that run (`day_sessions.autorun`), so those runs stay apart from the plug-ins;
- any other old day session: "By hand", no strategy.
Fields an old trade never stored stay empty.

**Filters** (all optional, combined with AND): `sessions` (ids), `end` (all / timeout = the
session ended with ENDED_BY_TIMEOUT / non_timeout = any other end or none yet), `mode`, `strategy`,
`symbol`, `date_from` / `date_to` (the trade's entry day, YYYY-MM-DD, on the day clock). The trade
table may also be narrowed by `outcome` (WIN / LOSS / BREAKEVEN / open) and `exit_kind`; the
numbers follow the same trades.

**Numbers** (`stats`): over the CLOSED trades of the selection, in the order of their exit (ties by
session and trade number); open trades are listed but not counted.
- WIN / LOSS / BREAKEVEN is the trade's stored `outcome` (after fees, -0.05 R to +0.05 R = BREAKEVEN).
- Win rate = wins / closed trades (breakevens count in the total).
- Average win / loss: over the wins / losses only, in R and in USDT. Expectancy = the average R.
- Total: USDT after fees, percent of the start balance (when every selected session started with
  the same balance; otherwise empty), R. Price units are per trade only: they do not add up across coins.
- Duration: hours from the fill to the exit, average and median.
- Max drawdown in R: the deepest fall of the running sum of R below its highest point so far
  (starting at 0).
- Streaks: the longest run of WINs and of LOSSes in a row; a BREAKEVEN ends both.
The per-session rows hold the same numbers for each session alone, with its end reason.

**Times** are on the day clock (UTC, "YYYY-MM-DD HH:MM"): entry and exit at the close of their
15-minute candle (where the fill is), a high or low at the open of the candle that made it, as the
chart labels candles.
"""
from __future__ import annotations

import csv
import io
import json
import re
import statistics
from typing import Any

from .. import config
from .day_engine import (EXIT_EARLY, EXIT_STOP, EXIT_STOPPED, EXIT_TIMEOUT, EXIT_TP, BASE_INTERVAL, DayEngine,
                         hold_hours, local_str)
from .engine import Invalid, _iso
from .strat import AUTOMATIC, END_FLAGS, ENDED_BY_TIMEOUT, MODES

HAND = "hand"
REPORT_MODES = {**MODES, HAND: "By hand"}
END_SCOPES = ("all", "timeout", "non_timeout")
OUTCOMES = ("WIN", "LOSS", "BREAKEVEN", "open")
# how a trade ended, in the words of the spec (SL / TP / manual close / session timeout)
EXIT_KINDS = {EXIT_STOP: "SL", EXIT_TP: "TP", EXIT_TIMEOUT: "session timeout", EXIT_STOPPED: "session stopped"}
STRATEGY_EXIT = "strategy exit"   # an early exit made by an Automatic run (the strategy's own exit)
MANUAL_CLOSE = "manual close"
EXIT_KIND_LIST = ("SL", "TP", MANUAL_CLOSE, STRATEGY_EXIT, "session timeout", "session stopped")
AUTORUN_PREFIX = "autorun:"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The trade table / CSV columns, in order (spec section 5 first, then what the engine adds).
CSV_COLUMNS = (
    "trade_id", "session_id", "session_name", "seq", "mode", "strategy", "strategy_version", "symbol", "timeframe",
    "status", "entry_time", "entry_price", "initial_sl", "initial_tp", "exit_time", "exit_price", "exit_kind",
    "exit_type", "exit_reason", "duration_h", "outcome", "result_price", "result_pct", "result_r", "pnl_usdt",
    "gross_pnl_usdt", "gross_r", "fees_usdt", "high_price", "high_time", "low_price", "low_time", "mfe_r", "mae_r",
    "after_high", "after_high_time", "after_high_pct", "after_low", "after_low_time", "after_low_pct", "after_complete",
    "sl_tp_changes", "changes", "confidence", "final_sl", "final_tp", "risk_usdt", "coins", "position_value",
    "fee_rate", "slippage", "fill_model", "price_source", "timeout_rule", "session_end", "screenshot", "notes",
)


class Report:
    def __init__(self, day: DayEngine) -> None:
        self.day = day
        self.store = day.store

    # ------------------------------------------------------------------ API
    def report(self, **filters: Any) -> dict:
        f = self._filters(filters)
        sessions = self._sessions()
        options = self._options(sessions)
        chosen = [s for s in sessions if self._session_ok(s, f)]
        trades: list[dict] = []
        for s in chosen:
            trades.extend(t for t in self._trades(s) if self._trade_ok(t, f))
        trades.sort(key=lambda t: (t["exit_ms"] is None, t["exit_ms"] or t["entry_ms"], t["session_id"], t["seq"]))
        rows = []
        for s in chosen:
            mine = [t for t in trades if t["session_id"] == s["id"]]
            rows.append({**{k: s[k] for k in ("id", "name", "symbol", "mode", "mode_label", "strategy", "strategy_label",
                                              "end_flag", "end_label", "end_detail", "start_iso", "cursor_iso", "start_balance")},
                         "stats": stats(mine, s["start_balance"])})
        balances = {s["start_balance"] for s in chosen if any(t["session_id"] == s["id"] for t in trades)}
        return {
            "filters": {k: v for k, v in f.items() if v not in (None, [], "all")},
            "options": options,
            "summary": stats(trades, balances.pop() if len(balances) == 1 else None),
            "sessions": rows,
            "trades": trades,
        }

    def csv(self, **filters: Any) -> str:
        data = self.report(**filters)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for t in data["trades"]:
            w.writerow({k: _csv_value(t.get(k)) for k in CSV_COLUMNS})
        return buf.getvalue()

    # -------------------------------------------------------------- filters
    @staticmethod
    def _filters(raw: dict) -> dict:
        f = {"sessions": [x for x in (raw.get("sessions") or []) if x], "end": raw.get("end") or "all",
             "mode": raw.get("mode") or None, "strategy": raw.get("strategy") or None, "symbol": raw.get("symbol") or None,
             "date_from": raw.get("date_from") or None, "date_to": raw.get("date_to") or None,
             "outcome": raw.get("outcome") or None, "exit_kind": raw.get("exit_kind") or None}
        if f["end"] not in END_SCOPES:
            raise Invalid(f"end must be one of {', '.join(END_SCOPES)}.")
        if f["mode"] is not None and f["mode"] not in REPORT_MODES:
            raise Invalid(f"mode must be one of {', '.join(REPORT_MODES)}.")
        if f["outcome"] is not None and f["outcome"] not in OUTCOMES:
            raise Invalid(f"outcome must be one of {', '.join(OUTCOMES)}.")
        if f["exit_kind"] is not None and f["exit_kind"] not in EXIT_KIND_LIST:
            raise Invalid(f"exit_kind must be one of {', '.join(EXIT_KIND_LIST)}.")
        for k in ("date_from", "date_to"):
            if f[k] is not None and not DATE_RE.match(f[k]):
                raise Invalid(f"{k} must be a date like 2024-01-31.")
        if f["date_from"] and f["date_to"] and f["date_from"] > f["date_to"]:
            raise Invalid("The start of the date range is after its end.")
        return f

    @staticmethod
    def _session_ok(s: dict, f: dict) -> bool:
        if f["sessions"] and s["id"] not in f["sessions"]:
            return False
        if f["end"] == "timeout" and s["end_flag"] != ENDED_BY_TIMEOUT:
            return False
        if f["end"] == "non_timeout" and s["end_flag"] == ENDED_BY_TIMEOUT:
            return False
        return all(f[k] is None or s[k] == f[k] for k in ("mode", "strategy", "symbol"))

    @staticmethod
    def _trade_ok(t: dict, f: dict) -> bool:
        day = t["entry_date"]
        if (f["date_from"] and day < f["date_from"]) or (f["date_to"] and day > f["date_to"]):
            return False
        if f["outcome"] is not None and (t["outcome"] or "open") != f["outcome"]:
            return False
        return f["exit_kind"] is None or t["exit_kind"] == f["exit_kind"]

    # ------------------------------------------------------------- sessions
    def _sessions(self) -> list[dict]:
        """Every ready day session with its report mode and strategy, newest first."""
        out = []
        for s in self.day.replay.list_sessions():
            if s["mode"] != "day" or s["status"] != "ready":
                continue
            with self.store.tx() as conn:
                ds = self.store.get_day_session(conn, s["id"])
            if ds is None:
                continue
            mode, key, label, version = self._mode_of(ds)
            flag = ds.get("end_flag")
            out.append({
                "id": s["id"], "name": s["name"], "symbol": s["symbol"], "mode": mode, "mode_label": REPORT_MODES[mode],
                "strategy": key, "strategy_label": label, "strategy_version": version,
                "timeframe": ds.get("strategy_timeframe") if ds.get("run_mode") else None,
                "end_flag": flag, "end_label": END_FLAGS.get(flag) if flag else ("not ended" if ds.get("run_mode") else None),
                "end_detail": ds.get("end_detail"), "start_iso": s["start_iso"], "cursor_iso": s["cursor_iso"],
                "start_balance": float(s["start_balance"]),
            })
        return out

    @staticmethod
    def _mode_of(ds: dict) -> tuple[str, str | None, str | None, str | None]:
        """(mode, strategy key, strategy label, version) of a day session in the report."""
        if ds.get("run_mode"):
            name, version = ds.get("strategy_name") or ds.get("strategy_key"), ds.get("strategy_version")
            return ds["run_mode"], ds.get("strategy_key"), f"{name} v{version}" if version else name, version
        run = json.loads(ds["autorun"]) if ds.get("autorun") else None
        if run:   # an old autorun (CTest__NN): Automatic with the rules saved with it
            rules = run.get("rules") or {}
            entry = "signal" if rules.get("entry") == "signal" else "confirm"
            words = ["entry at the signal candle" if entry == "signal" else "entry at the confirmation",
                     f"{float(rules.get('ratio', 2.0)):g} R"]
            if rules.get("trend_filter") == "above":
                words.append("uptrend only")
            if rules.get("break_exit"):
                words.append("break exit")
            return AUTOMATIC, f"{AUTORUN_PREFIX}{entry}", f"Autorun ({', '.join(words)})", None
        return HAND, None, None, None

    @staticmethod
    def _options(sessions: list[dict]) -> dict:
        """What the filter menus offer: only values some session has."""
        strategies = {s["strategy"]: s["strategy_label"] for s in sessions if s["strategy"]}
        return {
            "sessions": [{"id": s["id"], "name": s["name"], "symbol": s["symbol"], "mode_label": s["mode_label"]} for s in sessions],
            "ends": [{"key": "all", "label": "All sessions"}, {"key": "timeout", "label": "Timeout only"},
                     {"key": "non_timeout", "label": "Non-timeout only"}],
            "modes": [{"key": k, "label": v} for k, v in REPORT_MODES.items() if any(s["mode"] == k for s in sessions)],
            "strategies": [{"key": k, "label": v} for k, v in sorted(strategies.items(), key=lambda kv: kv[1])],
            "symbols": sorted({s["symbol"] for s in sessions}),
            "outcomes": list(OUTCOMES), "exit_kinds": list(EXIT_KIND_LIST),
        }

    # --------------------------------------------------------------- trades
    def _trades(self, s: dict) -> list[dict]:
        """The session's trades as report rows (the day engine's listing first fills what is missing:
        outcome, high / low, the 24 hours after the exit)."""
        rows = self.day.trades(s["id"])["trades"]
        return [self._row(s, t) for t in rows]

    @staticmethod
    def _row(s: dict, t: dict) -> dict:
        closed = t["status"] == "closed"
        kind = None
        if closed:
            kind = EXIT_KINDS.get(t["exit_type"])
            if kind is None and t["exit_type"] == EXIT_EARLY:
                kind = STRATEGY_EXIT if s["mode"] == AUTOMATIC else MANUAL_CLOSE
            kind = kind or t["exit_type"]
        changes = t.get("changes") or []
        at = lambda ms: local_str(ms) if ms is not None else None  # noqa: E731   (the candle's open on the day clock)
        fill = lambda ms: local_str(ms + config.interval_ms(BASE_INTERVAL)) if ms is not None else None  # noqa: E731   (the fill: the close)
        return {
            "id": t["id"], "trade_id": t["id"], "session_id": s["id"], "session_name": s["name"], "seq": t["seq"],
            "mode": s["mode"], "mode_label": s["mode_label"],
            "strategy": t.get("strategy_name") or s["strategy_label"], "strategy_key": t.get("strategy_key") or s["strategy"],
            "strategy_version": t.get("strategy_version") or s["strategy_version"], "symbol": s["symbol"],
            "timeframe": t.get("timeframe") or s["timeframe"], "status": t["status"],
            "entry_ms": t["entry_ms"], "entry_time": fill(t["entry_ms"]), "entry_date": t["day_date"], "entry_price": t["entry_price"],
            "initial_sl": t["stop_initial"], "initial_tp": t["tp_initial"],
            "final_sl": t["stop_used"], "final_tp": t["take_profit"],
            "exit_ms": t["exit_ms"], "exit_time": fill(t["exit_ms"]), "exit_price": t["exit_price"],
            "exit_kind": kind, "exit_type": t["exit_type"], "exit_reason": t.get("exit_reason_label") or t.get("exit_reason"),
            "duration_h": hold_hours(t["entry_ms"], t["exit_ms"]),
            "outcome": t.get("outcome") if closed else None,
            "result_price": t.get("result_price"), "result_pct": t.get("pnl_pct"), "result_r": t.get("r_multiple"),
            "pnl_usdt": t.get("pnl"), "gross_pnl_usdt": t.get("gross_pnl"), "gross_r": t.get("gross_r"), "fees_usdt": t.get("fees"),
            "high_price": t.get("high_price"), "high_time": at(t.get("high_ms")),
            "low_price": t.get("low_price"), "low_time": at(t.get("low_ms")),
            "mfe_r": t.get("mfe_r"), "mae_r": t.get("mae_r"),
            "after_high": t.get("after_high"), "after_high_time": at(t.get("after_high_ms")), "after_high_pct": t.get("after_high_pct"),
            "after_low": t.get("after_low"), "after_low_time": at(t.get("after_low_ms")), "after_low_pct": t.get("after_low_pct"),
            "after_complete": None if t.get("after_complete") is None else bool(t["after_complete"]),
            "sl_tp_changes": len(changes),
            "changes": "; ".join(f"{c['at_local']} {'SL' if c['field'] == 'stop' else 'TP'} {c['from']:g} -> {c['to']:g}" for c in changes) or None,
            "change_list": changes,
            "confidence": t.get("confidence"), "risk_usdt": t.get("risk_usdt"), "coins": t.get("coins"),
            "position_value": t.get("position_value"),
            "fee_rate": t.get("fee_rate"), "slippage": t.get("slippage"),
            "fill_model": t.get("fill_model"), "price_source": t.get("price_source"), "timeout_rule": t.get("timeout_rule"),
            "session_end": s["end_flag"], "screenshot": t.get("image_path"), "has_image": bool(t.get("image_path")),
            "notes": t.get("notes"),
        }


def _csv_value(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.10g}"
    return v


def stats(trades: list[dict], start_balance: float | None = None) -> dict:
    """The report's numbers over the closed trades in `trades` (already in exit order); see the module docstring."""
    closed = [t for t in trades if t["status"] == "closed"]
    n = len(closed)
    wins = [t for t in closed if t["outcome"] == "WIN"]
    losses = [t for t in closed if t["outcome"] == "LOSS"]
    rs = [float(t["result_r"]) for t in closed if t["result_r"] is not None]
    usdt = sum(float(t["pnl_usdt"] or 0.0) for t in closed)
    hours = [float(t["duration_h"]) for t in closed if t["duration_h"] is not None]
    mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    r_of = lambda ts: [float(t["result_r"]) for t in ts if t["result_r"] is not None]  # noqa: E731
    usdt_of = lambda ts: [float(t["pnl_usdt"]) for t in ts if t["pnl_usdt"] is not None]  # noqa: E731
    run = peak = dd = 0.0
    for r in rs:
        run += r
        peak = max(peak, run)
        dd = max(dd, peak - run)
    best = {"WIN": 0, "LOSS": 0}
    cur_kind, cur_len = None, 0
    for t in closed:
        k = t["outcome"] if t["outcome"] in best else None
        cur_len = cur_len + 1 if k is not None and k == cur_kind else (1 if k is not None else 0)
        cur_kind = k
        if k is not None:
            best[k] = max(best[k], cur_len)
    return {
        "trades": n, "open": sum(1 for t in trades if t["status"] == "open"),
        "wins": len(wins), "losses": len(losses), "breakevens": sum(1 for t in closed if t["outcome"] == "BREAKEVEN"),
        "win_rate": len(wins) / n * 100.0 if n else None,
        "avg_win_r": mean(r_of(wins)), "avg_loss_r": mean(r_of(losses)),
        "avg_win_usdt": mean(usdt_of(wins)), "avg_loss_usdt": mean(usdt_of(losses)),
        "expectancy_r": mean(rs),
        "total_usdt": usdt if n else None,
        "total_pct": usdt / start_balance * 100.0 if n and start_balance else None,
        "total_r": sum(rs) if rs else None,
        "avg_hours": mean(hours), "median_hours": statistics.median(hours) if hours else None,
        "max_drawdown_r": dd if rs else None,
        "win_streak": best["WIN"], "loss_streak": best["LOSS"],
        "first_iso": _iso(closed[0]["entry_ms"]) if closed else None, "last_iso": _iso(closed[-1]["exit_ms"]) if closed else None,
    }
