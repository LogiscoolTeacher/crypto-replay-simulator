"""Performance of the trades, in the spirit of TradingView's Strategy Tester: the numbers
(trades, winners, win rate, net profit, profit factor, average trade, best and worst, maximum
drawdown), the equity curve with its drawdown, one row per closed trade and the monthly returns.

Every closed History row counts as one trade, so a bucket sold in three parts gives three
trades. Open buckets are reported separately (count, P&L at market, equity at market).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

MAX_CURVE_POINTS = 1500   # the curve sent to the browser is thinned to about this many points


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="minutes")


def trade_rows(buckets: list[dict], symbol: str | None = None) -> list[dict]:
    """One row per closed bucket row (a part counts on its own), oldest sell first."""
    rows = []
    for b in buckets:
        if b["status"] != "closed" or not b.get("cost"):
            continue
        rows.append({
            "id": b["id"],
            "seq": b["seq"],
            "part": b.get("part"),
            "label": f"#{b['seq']}" + (f".{b['part']}" if b.get("part") else ""),
            "symbol": symbol,
            "opened_ms": b["opened_ms"],
            "closed_ms": b["closed_ms"],
            "closed_iso": _iso(b["closed_ms"]),
            "cost": b["cost"],
            "pnl": b["realized_pnl"],
            "pct": b["realized_pnl"] / b["cost"] * 100.0,
            "held_ms": b["closed_ms"] - b["opened_ms"],
            "trigger": b.get("trigger"),
        })
    rows.sort(key=lambda r: (r["closed_ms"], r["id"]))
    return rows


def summarize(rows: list[dict], start_balance: float) -> dict:
    """The Strategy Tester numbers over closed trades. Percentages of trades are of their cost;
    `net_pnl_pct` is of the start balance."""
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    gross_profit = sum(r["pnl"] for r in wins)
    gross_loss = -sum(r["pnl"] for r in losses)
    net = sum(r["pnl"] for r in rows)
    n = len(rows)
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    best = max(rows, key=lambda r: r["pct"]) if rows else None
    worst = min(rows, key=lambda r: r["pct"]) if rows else None
    pick = lambda r: None if r is None else {"label": r["label"], "symbol": r["symbol"], "pnl": r["pnl"], "pct": r["pct"], "closed_iso": r["closed_iso"]}  # noqa: E731
    # Longest run of winners and of losers in time order.
    streak_w = streak_l = cur_w = cur_l = 0
    for r in rows:
        if r["pnl"] > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif r["pnl"] < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)
    return {
        "trades": n,
        "winners": len(wins),
        "losers": len(losses),
        "flat": n - len(wins) - len(losses),
        "win_rate_pct": (len(wins) / n * 100.0) if n else None,
        "net_pnl": net,
        "net_pnl_pct": (net / start_balance * 100.0) if start_balance else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
        "avg_pnl": avg([r["pnl"] for r in rows]),
        "avg_pct": avg([r["pct"] for r in rows]),
        "avg_win_pct": avg([r["pct"] for r in wins]),
        "avg_loss_pct": avg([r["pct"] for r in losses]),
        "avg_win_pnl": avg([r["pnl"] for r in wins]),
        "avg_loss_pnl": avg([r["pnl"] for r in losses]),
        "best": pick(best),
        "worst": pick(worst),
        "avg_held_ms": avg([r["held_ms"] for r in rows]),
        "max_held_ms": max((r["held_ms"] for r in rows), default=None),
        "win_streak": streak_w,
        "loss_streak": streak_l,
        "auto_sells": sum(1 for r in rows if r.get("trigger")),
    }


def equity_curve(start_balance: float, trades: list[dict], candles: pd.DataFrame, start_ms: int) -> tuple[list[int], list[float]]:
    """Account value at every base candle: cash plus the coins held, valued at the candle's close.
    `trades` are the session's buy/sell rows (time_ms, side, qty, cash_after); a trade at a
    candle's open time is filled at that candle's close, so it applies from that candle on.
    Returns (times in seconds, equity), starting with the start balance at `start_ms`."""
    trades = sorted(trades, key=lambda t: (t["time_ms"], t["id"]))
    times: list[int] = [int(start_ms // 1000)]
    equity: list[float] = [float(start_balance)]
    cash, qty, i = float(start_balance), 0.0, 0
    if candles is None or len(candles) == 0:
        return times, equity
    t_arr = candles["time"].to_numpy()
    c_arr = candles["close"].to_numpy()
    for k in range(len(t_arr)):
        t_ms = int(t_arr[k]) * 1000
        while i < len(trades) and trades[i]["time_ms"] <= t_ms:
            tr = trades[i]
            cash = float(tr["cash_after"])
            qty += float(tr["qty"]) if tr["side"] == "buy" else -float(tr["qty"])
            i += 1
        if qty < 1e-12:
            qty = 0.0
        times.append(int(t_arr[k]))
        equity.append(cash + qty * float(c_arr[k]))
    return times, equity


def drawdown(values: list[float]) -> dict:
    """Drawdown of a curve: percent below the running peak at every point, the deepest fall
    (percent of the peak and absolute), and the indexes of that peak and its trough."""
    dd: list[float] = []
    peak = values[0] if values else 0.0
    peak_i = 0
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    max_peak_i = max_trough_i = 0
    for i, v in enumerate(values):
        if v > peak:
            peak, peak_i = v, i
        fall = peak - v
        pct = (fall / peak * 100.0) if peak > 0 else 0.0
        dd.append(-pct)
        if fall > max_dd_abs:
            max_dd_abs, max_dd_pct, max_peak_i, max_trough_i = fall, pct, peak_i, i
    return {"drawdown_pct": dd, "max_drawdown_pct": max_dd_pct, "max_drawdown": max_dd_abs, "peak_index": max_peak_i, "trough_index": max_trough_i}


def thin(n: int, keep: list[int], limit: int = MAX_CURVE_POINTS) -> list[int]:
    """Indexes to keep when a curve of `n` points is sent to the browser: every k-th point, the
    first and last, and the ones in `keep` (the peak and the trough)."""
    if n <= limit:
        return list(range(n))
    step = max(1, n // limit)
    idx = set(range(0, n, step)) | {n - 1} | {k for k in keep if 0 <= k < n}
    return sorted(idx)


def monthly_from_curve(times: list[int], equity: list[float]) -> list[dict]:
    """Return of every calendar month: the equity at the month's last point against the equity
    at the previous month's last point (the first month starts from the first point)."""
    if not times:
        return []
    months: list[dict] = []
    prev_end = equity[0]
    cur_key = None
    for t, e in zip(times, equity):
        key = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")
        if key != cur_key:
            if cur_key is not None:
                months.append({"month": cur_key, "pnl": last - prev_end, "pct": (last / prev_end - 1.0) * 100.0 if prev_end else None})
                prev_end = last
            cur_key = key
        last = e
    months.append({"month": cur_key, "pnl": last - prev_end, "pct": (last / prev_end - 1.0) * 100.0 if prev_end else None})
    return months


def monthly_from_trades(rows: list[dict], start_balance: float) -> list[dict]:
    """Realized P&L of the trades closed in every calendar month, percent of the start balance."""
    by: dict[str, float] = {}
    for r in rows:
        key = datetime.fromtimestamp(r["closed_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m")
        by[key] = by.get(key, 0.0) + r["pnl"]
    return [{"month": k, "pnl": v, "pct": (v / start_balance * 100.0) if start_balance else None} for k, v in sorted(by.items())]


def session_report(s: dict, buckets: list[dict], trades: list[dict], candles: pd.DataFrame, account: dict) -> dict:
    """The whole report of one session: numbers, equity curve (thinned), trades and months."""
    rows = trade_rows(buckets, s["symbol"])
    summary = summarize(rows, s["start_balance"])
    summary.update({
        "open_count": account["open_buckets"],
        "open_pnl": account["unrealized_pnl"],
        "equity": account["equity"],
        "equity_pct": account["total_return_pct"],
        "fees": account["fees_paid"],
    })
    times, equity = equity_curve(s["start_balance"], trades, candles, s["data_start_ms"] or s["start_ms"])
    dd = drawdown(equity)
    summary.update({
        "max_drawdown_pct": dd["max_drawdown_pct"],
        "max_drawdown": dd["max_drawdown"],
        "peak": {"equity": equity[dd["peak_index"]], "iso": _iso(times[dd["peak_index"]] * 1000)},
        "trough": {"equity": equity[dd["trough_index"]], "iso": _iso(times[dd["trough_index"]] * 1000)},
        "equity_high": max(equity),
        "equity_low": min(equity),
    })
    idx = thin(len(times), [dd["peak_index"], dd["trough_index"]])
    pos = {k: i for i, k in enumerate(idx)}
    return {
        "scope": "session",
        "session_id": s["id"],
        "symbol": s["symbol"],
        "start_balance": s["start_balance"],
        "start_ms": s["data_start_ms"] or s["start_ms"],
        "end_ms": s["cursor_ms"],
        "summary": summary,
        "curve": {
            "kind": "equity",
            "time": [times[i] for i in idx],
            "value": [equity[i] for i in idx],
            "drawdown_pct": [dd["drawdown_pct"][i] for i in idx],
            "peak_index": pos[dd["peak_index"]],
            "trough_index": pos[dd["trough_index"]],
            "points": len(times),
        },
        "trades": rows,
        "months": monthly_from_curve(times, equity),
    }


def combined_report(parts: list[tuple[dict, list[dict], dict]]) -> dict:
    """Every session together: the numbers over all closed trades, a curve of the realized P&L
    added up trade by trade (sessions replay different years, so there is no common time axis),
    and the months by the trades' closing dates. `parts` holds (session, buckets, account)."""
    rows: list[dict] = []
    start_total = 0.0
    open_count = 0
    open_pnl = equity = fees = 0.0
    for s, buckets, account in parts:
        rows.extend(trade_rows(buckets, s["symbol"]))
        start_total += s["start_balance"]
        if account:
            open_count += account["open_buckets"]
            open_pnl += account["unrealized_pnl"]
            equity += account["equity"]
            fees += account["fees_paid"]
    rows.sort(key=lambda r: (r["closed_ms"], r["id"]))
    summary = summarize(rows, start_total)
    cum: list[float] = [0.0]
    for r in rows:
        cum.append(cum[-1] + r["pnl"])
    # Drawdown of money, measured against the total start balance (a run of losses from zero
    # would otherwise be a fall from a zero peak).
    shifted = [start_total + c for c in cum]
    dd = drawdown(shifted)
    summary.update({
        "open_count": open_count,
        "open_pnl": open_pnl,
        "equity": equity,
        "equity_pct": ((equity / start_total) - 1.0) * 100.0 if start_total else None,
        "fees": fees,
        "max_drawdown_pct": dd["max_drawdown_pct"],
        "max_drawdown": dd["max_drawdown"],
        "peak": {"equity": cum[dd["peak_index"]], "iso": rows[dd["peak_index"] - 1]["closed_iso"] if dd["peak_index"] else None},
        "trough": {"equity": cum[dd["trough_index"]], "iso": rows[dd["trough_index"] - 1]["closed_iso"] if dd["trough_index"] else None},
        "equity_high": max(cum),
        "equity_low": min(cum),
    })
    idx = thin(len(cum), [dd["peak_index"], dd["trough_index"]])
    pos = {k: i for i, k in enumerate(idx)}
    return {
        "scope": "all",
        "sessions": len(parts),
        "symbol": None,
        "start_balance": start_total,
        "start_ms": min((r["opened_ms"] for r in rows), default=None),
        "end_ms": max((r["closed_ms"] for r in rows), default=None),
        "summary": summary,
        "curve": {
            "kind": "cumulative",
            "time": [idx_i for idx_i in idx],        # trade number (0 = before the first trade)
            "value": [cum[i] for i in idx],
            "drawdown_pct": [dd["drawdown_pct"][i] for i in idx],
            "peak_index": pos[dd["peak_index"]],
            "trough_index": pos[dd["trough_index"]],
            "points": len(cum),
        },
        "trades": rows,
        "months": monthly_from_trades(rows, start_total),
    }


def json_safe(obj: Any) -> Any:
    """Infinite profit factors become the string 'inf' (JSON has no infinity)."""
    if isinstance(obj, float) and obj == float("inf"):
        return "inf"
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj
