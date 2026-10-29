"""Shared test doubles."""
from __future__ import annotations

from app.config import interval_ms
from app.data.binance_client import MAX_KLINES_PER_REQUEST, Kline
from app.data.candle_store import floor_to_slot

H = interval_ms("1h")
DAY = interval_ms("1D")
LISTING = 1_500_000_000_000 - (1_500_000_000_000 % DAY)  # a day-aligned "listing" time

_BINANCE_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": H, "4h": 4 * H, "1d": DAY}


def hourly_price(t: int) -> float:
    """Deterministic synthetic price for the hourly candle opening at t (ms)."""
    return float(100 + (t // H) % 50)


class FakeClient:
    """Serves synthetic candles from LISTING up to `now_ms` at any interval >= 1h.

    Hourly candles are the source of truth; coarser intervals are aggregated
    from them exactly as an exchange would. `holes` removes hourly candles.
    """

    def __init__(self, now_ms: int, holes: set[int] | None = None) -> None:
        self.now_ms = now_ms
        self.holes = holes or set()
        self.calls: list[tuple[int, int | None]] = []

    def server_time_ms(self) -> int:
        return self.now_ms

    def klines(self, symbol, binance_interval, start_ms, end_ms=None, limit=MAX_KLINES_PER_REQUEST):
        self.calls.append((start_ms, end_ms))
        step = _BINANCE_MS[binance_interval]
        assert step >= H, "fake exchange only serves 1h and coarser"
        out: list[Kline] = []
        t = floor_to_slot(start_ms, step)
        if t < start_ms:  # start_ms not slot aligned: next slot
            t += step
        t = max(t, LISTING)
        # Binance includes the still-forming candle whose open_time <= now.
        while t <= self.now_ms and (end_ms is None or t <= end_ms) and len(out) < limit:
            hours = [h for h in range(t, min(t + step, self.now_ms + 1), H) if h not in self.holes]
            if hours:
                prices = [hourly_price(h) for h in hours]
                out.append(
                    Kline(
                        open_time=t,
                        open=prices[0],
                        high=max(prices) + 1,
                        low=min(prices) - 1,
                        close=prices[-1] + 0.5,
                        volume=10.0 * len(hours),
                        close_time=t + step - 1,
                        taker_buy=6.0 * len(hours),
                    )
                )
            t += step
        return out


Q = interval_ms("15m")


def quarter_price(t: int) -> float:
    """Deterministic close of the 15-minute candle opening at t (ms): a slow uptrend of 0.05 per
    day with a small two-hour wave."""
    wave = (0.0, 0.1, 0.2, 0.3, 0.2, 0.1, 0.0, -0.1)[(t // Q) % 8]
    return 100.0 + (t - LISTING) / DAY * 0.05 + wave


def quarter_candle(t: int) -> tuple[float, float, float, float, float]:
    """(open, high, low, close, volume) of the 15-minute candle opening at t."""
    o, c = quarter_price(t - Q), quarter_price(t)
    return o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1.0


def fake_taker_buy(q: tuple[float, float, float, float, float]) -> float:
    """Taker-buy volume of a fake 15-minute candle: 60 % of the volume when it closes up, else 40 %."""
    return (0.6 if q[3] >= q[0] else 0.4) * q[4]


class FineFakeClient:
    """Like FakeClient, with 15-minute candles as the source of truth (every interval >= 15m served,
    coarser ones aggregated). `overrides` maps a 15-minute open time to (open, high, low, close)
    to script a scenario; the coarser candles follow."""

    def __init__(self, now_ms: int, overrides: dict[int, tuple[float, float, float, float]] | None = None) -> None:
        self.now_ms = now_ms
        self.overrides = overrides or {}
        self.calls: list[tuple[int, int | None]] = []

    def server_time_ms(self) -> int:
        return self.now_ms

    def quarter(self, t: int) -> tuple[float, float, float, float, float]:
        if t in self.overrides:
            o, h, l, c = self.overrides[t]
            return o, h, l, c, 1.0
        return quarter_candle(t)

    def klines(self, symbol, binance_interval, start_ms, end_ms=None, limit=MAX_KLINES_PER_REQUEST):
        self.calls.append((start_ms, end_ms))
        step = _BINANCE_MS[binance_interval]
        assert step >= Q, "fine fake exchange serves 15m and coarser"
        out: list[Kline] = []
        t = floor_to_slot(start_ms, step)
        if t < start_ms:
            t += step
        t = max(t, LISTING)
        while t <= self.now_ms and (end_ms is None or t <= end_ms) and len(out) < limit:
            qs = [self.quarter(q) for q in range(t, min(t + step, self.now_ms + 1), Q)]
            if qs:
                out.append(
                    Kline(
                        open_time=t,
                        open=qs[0][0],
                        high=max(q[1] for q in qs),
                        low=min(q[2] for q in qs),
                        close=qs[-1][3],
                        volume=sum(q[4] for q in qs),
                        close_time=t + step - 1,
                        taker_buy=sum(fake_taker_buy(q) for q in qs),
                    )
                )
            t += step
        return out
