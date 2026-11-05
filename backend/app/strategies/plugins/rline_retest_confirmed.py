"""RLine_Retest with a confirmation candle (the rules of the autorun's CTest__01 run).

After the retest signal the next `confirm_bars` candles are watched one by one. A hammer at the
zone or a bullish engulfing confirms the signal: the entry is that candle's close. A close below
the zone bottom cancels it, and so does a window without a confirmation.
"""
from __future__ import annotations

from ..patterns import Candle, is_bullish_engulfing, is_hammer
from ..plugin import Decision
from ._rline_retest import RLineRetest


class RLineRetestConfirmed(RLineRetest):
    key = "rline_retest_confirmed"
    name = "RLine_Retest confirmed"
    version = "1"
    description = ("A broken resistance zone is retested (1h); the entry waits for a hammer or a bullish "
                   "engulfing candle within the next 10 candles. All trends.")

    entry_rule = "confirm"
    confirm_bars: int = 10            # candles after the signal candle that may confirm
    hammer: bool = True
    engulfing: bool = True
    hammer_wick_body: float = 2.0     # hammer: lower wick >= this x body
    hammer_upper_body: float = 1.0    # hammer: upper wick <= this x body
    ratio = 2.0

    def settings(self) -> dict:
        return {**super().settings(), "confirm_bars": self.confirm_bars, "hammer": self.hammer, "engulfing": self.engulfing,
                "hammer_wick_body": self.hammer_wick_body, "hammer_upper_body": self.hammer_upper_body, "break_exit": False}

    def rules_text(self) -> list[str]:
        return [
            f"Entry: at the close of a hammer or bullish engulfing candle within {self.confirm_bars} candles after the retest signal.",
            "Cancelled by a close below the zone bottom.",
            self._stop_text(),
            f"Target: {self.ratio:g} R.",
            *self._filter_texts(),
        ]

    def entry(self, signal: dict, n: int, cur: Candle | None, prev: Candle | None) -> Decision:
        if n == 0:   # the signal candle itself never confirms; the window starts with the next one
            return Decision.wait()
        if cur is not None:   # a data gap gives nothing to read but still uses up a candle of the window
            if self._closes_below_zone(signal, cur):   # the break wins over a pattern
                return Decision.cancel("zone_broken", "Zone broken (a close below the zone).")
            level = signal.get("entry_level", signal["zone_top"])
            if self.hammer and is_hammer(cur, level, self.hammer_wick_body, self.hammer_upper_body):
                return Decision.enter("hammer", "A hammer at the zone.")
            if self.engulfing and prev is not None and is_bullish_engulfing(prev, cur):
                return Decision.enter("engulfing", "A bullish engulfing candle.")
        if n >= self.confirm_bars:
            return Decision.cancel("no_confirmation", "No confirmation within the window.")
        return Decision.wait()


STRATEGY = RLineRetestConfirmed()
