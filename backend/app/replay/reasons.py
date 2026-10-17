"""Reason checklists ticked when a buy or a sell is logged, and the statistics over them.

Every trade stores the keys it ticked, an optional note and the checklist version (so a later
list can be told apart from this one). The statistics measure each reason by the realized
P&L in percent of the cost: for a buy reason, the trades (one buy with all its sells) with it
ticked against those without; for a sell reason, the sells with it ticked. Groups with fewer
than `MIN_GROUP` members are marked as too few to judge.
"""
from __future__ import annotations

from typing import Any

CHECKLIST_VERSION = "v1"
MIN_GROUP = 10          # a group smaller than this is "too few to judge"
NOTE_MAX_CHARS = 500
MAX_REASONS = 32

BUY_REASONS: tuple[tuple[str, str], ...] = (
    ("at_support", "At support"),
    ("rsi_low", "RSI low (<30)"),
    ("above_ema200", "Above EMA 200"),
    ("lower_bb", "Lower Bollinger band"),
    ("fib_level", "Fibonacci level (38.2/50/61.8 %)"),
    ("volume_support", "High volume at support"),
    ("bounce_candle", "Bounce candle (long lower wick)"),
    ("rsi_div_up", "RSI divergence up"),
    ("breakout", "Breakout above resistance"),
    ("big_drop", "Big drop from recent high (>=15 %)"),
    ("other", "Other"),
)
SELL_REASONS: tuple[tuple[str, str], ...] = (
    ("at_resistance", "At resistance"),
    ("rsi_high", "RSI high (>70)"),
    ("upper_bb", "Upper Bollinger band"),
    ("profit_target", "Profit target reached"),
    ("trim", "Trim"),
    ("weak_volume", "Weak volume on the rise"),
    ("rsi_div_down", "RSI divergence down"),
    ("rejection_candle", "Rejection candle (long upper wick)"),
    ("support_broken", "Support broken (loss)"),
    ("below_ema200", "Below EMA 200 (loss)"),
    ("other", "Other"),
)
REASONS: dict[str, tuple[tuple[str, str], ...]] = {"buy": BUY_REASONS, "sell": SELL_REASONS}


def describe() -> dict:
    """The checklists for the UI: version, the buy and sell reasons in order, the group size."""
    return {
        "version": CHECKLIST_VERSION,
        "buy": [{"key": k, "label": l} for k, l in BUY_REASONS],
        "sell": [{"key": k, "label": l} for k, l in SELL_REASONS],
        "min_group": MIN_GROUP,
        "note_max_chars": NOTE_MAX_CHARS,
    }


def clean(side: str, reasons: Any, note: Any) -> tuple[list[str], str | None]:
    """Validate the ticked reasons and the note of a trade. Unknown keys and non-strings raise
    ValueError; duplicates are dropped; a blank note becomes None."""
    known = [k for k, _ in REASONS[side]]
    if reasons is None:
        reasons = []
    if not isinstance(reasons, (list, tuple)):
        raise ValueError("reasons must be a list of checklist keys")
    if len(reasons) > MAX_REASONS:
        raise ValueError(f"at most {MAX_REASONS} reasons")
    out: list[str] = []
    for r in reasons:
        if not isinstance(r, str) or r not in known:
            raise ValueError(f"unknown {side} reason {r!r}; expected one of {known}")
        if r not in out:
            out.append(r)
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be text")
    note = (note or "").strip() or None
    if note is not None and len(note) > NOTE_MAX_CHARS:
        raise ValueError(f"note must be at most {NOTE_MAX_CHARS} characters")
    return out, note


def _group(values: list[float]) -> dict:
    n = len(values)
    return {"count": n, "avg_pct": (sum(values) / n) if n else None, "few": n < MIN_GROUP}


def stats(trades: list[dict], sells: list[dict]) -> dict:
    """Per buy reason: the trades with it ticked against those without; per sell reason: the
    sells with it ticked. `trades` and `sells` carry `reasons` (ticked keys) and `pnl_pct`."""
    buy_rows = []
    for key, label in BUY_REASONS:
        ticked = [t["pnl_pct"] for t in trades if key in t["reasons"]]
        unticked = [t["pnl_pct"] for t in trades if key not in t["reasons"]]
        buy_rows.append({"key": key, "label": label, "ticked": _group(ticked), "unticked": _group(unticked)})
    sell_rows = []
    for key, label in SELL_REASONS:
        ticked = [x["pnl_pct"] for x in sells if key in x["reasons"]]
        sell_rows.append({"key": key, "label": label, "ticked": _group(ticked)})
    return {
        "checklist": CHECKLIST_VERSION,
        "min_group": MIN_GROUP,
        "trades": len(trades),
        "sells": len(sells),
        "trades_avg_pct": (sum(t["pnl_pct"] for t in trades) / len(trades)) if trades else None,
        "buy": buy_rows,
        "sell": sell_rows,
    }
