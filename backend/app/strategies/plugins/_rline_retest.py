"""What the RLine_Retest strategies share: the signal, the stop, the target and the fee filter.

The signal itself comes from the existing entry-signal module (`strategies/rline.py`, used as it
is, never edited here): a qualified resistance zone is broken and price comes back to it. The
strategy files next to this one differ only in WHEN they enter after that signal and in a few rule
values (class attributes). The values are those of the autorun's CTest runs, so the plug-ins trade
like those runs did.
"""
from __future__ import annotations

import pandas as pd

from ...indicators.registry import validate_params
from ..patterns import Candle
from ..plugin import Levels, Market, PlannedTrade, TradingStrategy, Verdict
from ..rline import get_jump_strategy

DETECTOR = get_jump_strategy("rline_retest")   # the existing signal module, reused unchanged


class RLineRetest(TradingStrategy):
    timeframe = DETECTOR.interval   # 1h

    # --- rule values (a subclass may set its own; changing one means a new `version`) ---
    detector_params: dict = {}        # overrides of the detector's defaults (none: the defaults are the rule)
    stop_atr_mult: float = 0.25       # stop = zone bottom - this x ATR
    stop_atr_interval: str = "1h"     # ... of the last closed candle of this interval
    atr_period: int = 14
    ratio: float = 2.0                # target = entry + ratio x (entry - stop)
    max_fee_share: float = 0.2        # no trade when the estimated round-trip fees reach this share of the risk
    trend_filter: str | None = None   # "above": only signals whose daily close is above the EMA 200
    entry_rule: str = ""              # a word for `settings()`: how the subclass enters

    # ------------------------------------------------------------- identity
    def _detector_params(self) -> dict:
        return validate_params(DETECTOR.params, self.detector_params)

    def settings(self) -> dict:
        return {
            "detector": DETECTOR.key, "detector_params": self._detector_params(),
            "entry": self.entry_rule,
            "stop_atr_mult": self.stop_atr_mult, "stop_atr_interval": self.stop_atr_interval,
            "atr_period": self.atr_period, "ratio": self.ratio,
            "max_fee_share": self.max_fee_share, "trend_filter": self.trend_filter,
        }

    def _stop_text(self) -> str:
        return f"Stop: the zone bottom - {self.stop_atr_mult:g} x ATR {self.atr_period} ({self.stop_atr_interval})."

    def _filter_texts(self) -> list[str]:
        out = [f"No trade when the estimated fees (buy and sell) reach {self.max_fee_share * 100:g} % of the risk."]
        if self.trend_filter == "above":
            out.append("Only signals whose last daily close is above the EMA 200.")
        return out

    # -------------------------------------------------------------- signals
    def find_signals(self, candles: pd.DataFrame) -> list[dict]:
        return DETECTOR.detect(candles, self._detector_params())["signals"]

    def locate(self, signal: dict, base: pd.DataFrame) -> int | None:
        """The first base candle inside the signal's candle whose low reaches the signal's entry
        level (the zone top plus the near-zone buffer)."""
        hit = base[base["low"] <= signal.get("entry_level", signal["zone_top"])]
        return int(hit["time"].iloc[0]) * 1000 if len(hit) else None

    def describe_signal(self, signal: dict) -> str:
        return DETECTOR.describe_signal(signal)

    def zone(self, signal: dict) -> tuple[float, float] | None:
        return float(signal["zone_top"]), float(signal["zone_bottom"])

    # ---------------------------------------------------------------- entry
    def pre_filter(self, signal: dict, market: Market) -> Verdict:
        if self.trend_filter == "above" and market.daily_trend() != 1:
            return Verdict.no("trend_filter", "The daily close is not above the EMA 200.")
        return Verdict.yes()

    def levels(self, signal: dict, entry_price: float, market: Market) -> Levels | None:
        atr = market.atr(self.stop_atr_interval, self.atr_period)
        if atr is None:
            return None
        stop = signal["zone_bottom"] - self.stop_atr_mult * atr
        if not entry_price > stop:   # the entry is already at or below the stop: no trade to take
            return None
        return Levels(stop=stop, take_profit=entry_price + self.ratio * (entry_price - stop), kind="atr",
                      text=f"{self._stop_text()} Target: {self.ratio:g} R.", atr=atr)

    def accept(self, plan: PlannedTrade) -> Verdict:
        """The fee filter: the round trip is estimated as the buy fee twice (buy and sell at the entry price)."""
        est = 2.0 * plan.buy_fee
        share = est / plan.risk_amount
        if share >= self.max_fee_share:
            return Verdict.no("fees_too_high", "The fees are too high for the risk.", est_fees=est, fee_share=share)
        return Verdict.yes(est_fees=est, fee_share=share)

    # --------------------------------------------------------------- helper
    @staticmethod
    def _closes_below_zone(signal: dict, cur: Candle) -> bool:
        return cur[3] < signal["zone_bottom"]
