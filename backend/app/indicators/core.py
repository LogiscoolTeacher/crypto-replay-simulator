"""Indicator math on pandas Series.

Conventions (matching TradingView / TA-Lib so values agree with what traders see):
* EMA and Wilder's RMA are seeded with the simple mean of the first `period`
  valid values, then recursed. Warm-up bars are NaN.
* RSI uses Wilder smoothing; when the average loss is zero RSI is 100.
* Bollinger Bands use the population standard deviation (ddof=0).

Input series may start with NaNs (e.g. the MACD line feeding the signal EMA)
but must not contain NaNs after the first valid value.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _check_period(period: int, name: str = "period") -> int:
    if not isinstance(period, (int, np.integer)) or isinstance(period, bool) or period < 1:
        raise ValueError(f"{name} must be a positive integer, got {period!r}")
    return int(period)


def _seeded_ewm(s: pd.Series, period: int, alpha: float) -> pd.Series:
    """Exponential recursion y_t = (1-a)*y_{t-1} + a*x_t seeded with the SMA of the first `period` values."""
    out = pd.Series(np.nan, index=s.index, dtype=float)
    vals = s.to_numpy(dtype=float)
    valid = np.flatnonzero(~np.isnan(vals))
    if len(valid) == 0:
        return out
    p0 = int(valid[0])
    tail = vals[p0:]
    if np.isnan(tail).any():
        raise ValueError("series has NaN after its first valid value; indicators need contiguous data")
    if len(tail) < period:
        return out
    x = tail[period - 1 :].copy()
    x[0] = tail[:period].mean()
    y = pd.Series(x).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    out.iloc[p0 + period - 1 :] = y
    return out


def sma(s: pd.Series, period: int) -> pd.Series:
    period = _check_period(period)
    return s.astype(float).rolling(period, min_periods=period).mean()


def ema(s: pd.Series, period: int) -> pd.Series:
    period = _check_period(period)
    return _seeded_ewm(s, period, alpha=2.0 / (period + 1))


def rma(s: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average (used inside RSI)."""
    period = _check_period(period)
    return _seeded_ewm(s, period, alpha=1.0 / period)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    period = _check_period(period)
    delta = close.astype(float).diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = rma(up, period)
    avg_down = rma(down, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + avg_up / avg_down)
    # Zero average loss (including the degenerate flat case) reads as 100, like TradingView.
    out = out.where(~((avg_down == 0.0) & avg_up.notna()), 100.0)
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range: max(high - low, |high - previous close|, |low - previous close|).
    The first bar, which has no previous close, uses high - low (TradingView's `tr(true)`)."""
    h, l, c = high.astype(float), low.astype(float), close.astype(float)
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    if len(tr):
        tr.iloc[0] = h.iloc[0] - l.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average true range: Wilder's moving average of the true range (TradingView's ATR default)."""
    period = _check_period(period)
    return rma(true_range(high, low, close), period)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    fast = _check_period(fast, "fast")
    slow = _check_period(slow, "slow")
    signal = _check_period(signal, "signal")
    if fast >= slow:
        raise ValueError(f"fast period ({fast}) must be smaller than slow period ({slow})")
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0) -> pd.DataFrame:
    period = _check_period(period)
    if not (mult > 0):
        raise ValueError(f"mult must be positive, got {mult!r}")
    c = close.astype(float)
    middle = sma(c, period)
    sd = c.rolling(period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"upper": middle + mult * sd, "middle": middle, "lower": middle - mult * sd})


def volume_delta(volume: pd.Series, taker_buy: pd.Series) -> pd.Series:
    """Volume delta per candle: taker-buy minus taker-sell volume = 2 x taker_buy - volume (base
    units). Positive: market buyers were more aggressive. NaN where the taker-buy volume is unknown."""
    return 2.0 * taker_buy.astype(float) - volume.astype(float)
