"""Candle patterns the strategies read (moved here from replay/autorun.py, which still uses them).

A candle is the plain tuple (open, high, low, close) of one closed candle.
"""
from __future__ import annotations

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
