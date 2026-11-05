"""RLine_Retest entered at the signal candle (the rules of the autorun's CTest__02 run).

No confirmation window: the entry is the close of the candle in which the retest happens, but only
while the daily close is above the EMA 200. Beside the stop and the target, the strategy wants out
when a candle closes below the zone bottom (the setup is broken).
"""
from __future__ import annotations

from ..patterns import Candle
from ..plugin import Decision, Reason
from ._rline_retest import RLineRetest


class RLineRetestSignal(RLineRetest):
    key = "rline_retest_signal"
    name = "RLine_Retest signal candle"
    version = "1"
    description = ("A broken resistance zone is retested (1h); the entry is the close of the retest candle "
                   "itself. Uptrend only (daily close above the EMA 200), with an exit when the zone breaks.")

    entry_rule = "signal"
    trend_filter = "above"
    ratio = 2.5
    break_exit: bool = True           # out at the close of a candle below the zone bottom

    def settings(self) -> dict:
        return {**super().settings(), "break_exit": self.break_exit}

    def rules_text(self) -> list[str]:
        return [
            "Entry: at the close of the retest signal's candle.",
            "Cancelled when that candle closes below the zone bottom.",
            self._stop_text(),
            f"Target: {self.ratio:g} R.",
            "Exit: a candle closing below the zone bottom (after the stop and the target).",
            *self._filter_texts(),
        ]

    def entry(self, signal: dict, n: int, cur: Candle | None, prev: Candle | None) -> Decision:
        if cur is None:
            return Decision.cancel("invalid", "No valid trade (the signal candle is missing).")
        if self._closes_below_zone(signal, cur):
            return Decision.cancel("zone_broken", "Zone broken (a close below the zone).")
        return Decision.enter("signal", "Entry at the close of the signal candle.")

    def exit_signal(self, signal: dict, cur: Candle) -> Reason | None:
        if self.break_exit and self._closes_below_zone(signal, cur):
            return Reason("setup_broken", "A candle closed below the zone.")
        return None


STRATEGY = RLineRetestSignal()
