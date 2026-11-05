"""Trading-strategy plug-ins: what the Automatic and Manual session modes ask a strategy.

A plug-in is ONE file in `app/strategies/plugins/` that defines `STRATEGY`, an instance of a
`TradingStrategy` subclass. The registry at the bottom of this module finds every such file by
itself, so adding a strategy means adding a file: the engine is never edited for it.

The engine plays the replay candle by candle and asks the strategy, in this order. It only ever
hands over candles that have closed, so a strategy cannot look ahead:

1. `find_signals(candles)`   the setups on the strategy's own `timeframe` (one causal pass)
2. `locate(signal, base)`    which of the engine's finer candles inside the signal's candle is "the moment"
3. `pre_filter(signal, market)`  skip the signal without looking further (e.g. a trend filter)
4. `entry(signal, n, cur, prev)` candle by candle from the signal on: enter, wait or cancel
5. `levels(signal, entry_price, market)`  the stop loss and the take profit of the trade
6. `accept(plan)`            the last word once the engine has sized the trade (e.g. a fee filter)
7. `exit_signal(signal, cur)`    while the trade is open: an exit of the strategy's own beside stop / target

**Entry price**: the engine fills at the close of the candle on which `entry` answers "enter"
(the app's fill rule everywhere), so "when" is the strategy's and "at what price" follows from it.

**Stop and target** come from the strategy alone (`levels`): a fixed distance, a percentage, an
ATR multiple, anything it can compute from `market`. `Levels.kind` says which, for the record.

**Name and version** are stored with every trade. The rules of a version never change: a
different stop or target is a new version (or a new file), so results stay comparable.
`settings()` returns every value that shapes a result and is stored with the session.

In Automatic mode the engine acts on every answer. In Manual mode it proposes the entry with the
strategy's stop and target, the trader takes or skips it, and `exit_signal` only shows a notice.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd

from .. import config
from .patterns import Candle

ENTER, WAIT, CANCEL = "enter", "wait", "cancel"
LEVEL_KINDS = ("fixed", "percent", "atr")   # how a strategy derived its stop (Levels.kind)
PLUGIN_PACKAGE = f"{__package__}.plugins"   # every module in it without a leading "_" is one strategy
_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")


class StrategyError(Exception):
    """A plug-in file that cannot be used (no STRATEGY, a missing name, a key used twice, ...)."""


# ------------------------------------------------------------------ answers
@dataclass(frozen=True)
class Decision:
    """The answer of `entry` for one closed candle. `code` is a short word for the log ("hammer",
    "zone_broken"), `text` the same in plain English."""
    action: str
    code: str = ""
    text: str = ""

    @classmethod
    def enter(cls, code: str, text: str = "") -> Decision:
        return cls(ENTER, code, text)

    @classmethod
    def wait(cls) -> Decision:
        return cls(WAIT)

    @classmethod
    def cancel(cls, code: str, text: str = "") -> Decision:
        return cls(CANCEL, code, text)


@dataclass(frozen=True)
class Verdict:
    """The answer of a filter (`pre_filter`, `accept`). `data` holds the numbers worth logging with
    the signal either way (e.g. the fee share), `code` / `text` say why a signal was turned down."""
    ok: bool
    code: str = ""
    text: str = ""
    data: dict = field(default_factory=dict)

    @classmethod
    def yes(cls, **data: Any) -> Verdict:
        return cls(True, data=data)

    @classmethod
    def no(cls, code: str, text: str = "", **data: Any) -> Verdict:
        return cls(False, code, text, data)


@dataclass(frozen=True)
class Reason:
    """Why the strategy wants out of an open trade (`exit_signal`)."""
    code: str
    text: str = ""


@dataclass(frozen=True)
class Levels:
    """The stop loss and take profit of one trade. `kind`: how the stop was derived (LEVEL_KINDS),
    `text`: the rule in plain English, `atr`: the ATR it used, if any (logged with the signal)."""
    stop: float
    take_profit: float
    kind: str
    text: str = ""
    atr: float | None = None


@dataclass(frozen=True)
class PlannedTrade:
    """The trade as the engine would open it, handed to `accept`: `risk_amount` is the money lost
    at the stop before fees, `buy_fee` the fee of the buy, `fee_rate` the fee per side (0.001 = 0.1 %)."""
    entry: float
    stop: float
    take_profit: float
    risk_amount: float
    buy_fee: float
    fee_rate: float


class Market(Protocol):
    """What a strategy may read at a decision. The engine implements it; everything is as of the
    close of the candle being decided on, so only closed candles are visible."""

    def candles(self, interval: str, bars: int) -> pd.DataFrame:
        """The last `bars` closed candles of `interval` (time in seconds, open, high, low, close, volume)."""

    def atr(self, interval: str, period: int = 14) -> float | None:
        """ATR of the last closed `interval` candle (Wilder), None while there is too little data."""

    def daily_trend(self) -> int | None:
        """1 = the last completed daily candle closed above its EMA 200, 0 = it did not, None = unknown."""


# ----------------------------------------------------------------- strategy
class TradingStrategy:
    """Base class of a plug-in. A subclass sets the five class attributes and at least
    `find_signals` and `levels`; every other method has a neutral default."""

    key: str = ""           # lowercase id, stored with sessions and trades (never rename it)
    name: str = ""          # as shown in the strategy list
    version: str = ""       # stored with every trade; bump it whenever a rule changes
    description: str = ""   # one or two plain sentences for the strategy list
    timeframe: str = ""     # the interval `find_signals` reads (a session takes it from here)

    # ------------------------------------------------------------- identity
    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"

    def settings(self) -> dict:
        """Every value that shapes a result, stored with the session so a run can be reproduced."""
        return {}

    def rules_text(self) -> list[str]:
        """The rules in plain English, one line each (entry, stop, target, filters, exits)."""
        return []

    def describe(self) -> dict:
        return {"key": self.key, "name": self.name, "version": self.version, "label": self.label,
                "description": self.description, "timeframe": self.timeframe,
                "rules": self.rules_text(), "settings": self.settings()}

    # -------------------------------------------------------------- signals
    def find_signals(self, candles: pd.DataFrame) -> list[dict]:
        """Every setup in `candles` (the strategy's timeframe, warm-up included), oldest first.
        One causal pass: a signal may depend only on candles up to its own. Each signal is a dict
        with at least `bar_ms`, the open time (ms) of the candle it happened in; the engine hands
        the same dict back to every later question and stores it with the signal log."""
        raise NotImplementedError

    def locate(self, signal: dict, base: pd.DataFrame) -> int | None:
        """The open time (ms) of the engine's finer candle the replay jumps to: `base` holds the
        base candles inside the signal's own candle. None = no usable candle (a data gap).
        Default: the first of them."""
        return int(base["time"].iloc[0]) * 1000 if len(base) else None

    def describe_signal(self, signal: dict) -> str:
        """One line about this signal for the jump notice."""
        return f"{self.name} signal"

    def zone(self, signal: dict) -> tuple[float, float] | None:
        """(top, bottom) of the price band to draw for this signal, or None."""
        return None

    # ---------------------------------------------------------------- entry
    def pre_filter(self, signal: dict, market: Market) -> Verdict:
        """Turn a signal down before the replay even jumps to it (it is logged with the reason)."""
        return Verdict.yes()

    def entry(self, signal: dict, n: int, cur: Candle | None, prev: Candle | None) -> Decision:
        """Asked once per closed base candle from the signal on: `n` = 0 is the located signal
        candle, 1 the next one, and so on. `cur` is that candle, `prev` the one before; either is
        None on a data gap. Enter (at `cur`'s close), wait for the next candle, or cancel the
        signal. Default: enter on the signal candle."""
        if cur is None:
            return Decision.cancel("invalid", "The signal candle is missing.")
        return Decision.enter("signal", "Entry at the close of the signal candle.")

    def levels(self, signal: dict, entry_price: float, market: Market) -> Levels | None:
        """The stop loss and the take profit for an entry at `entry_price`. None = no valid trade
        (e.g. the ATR is missing, or the stop would not be below the entry)."""
        raise NotImplementedError

    def accept(self, plan: PlannedTrade) -> Verdict:
        """The last check once the trade is sized (it is logged with the reason when turned down)."""
        return Verdict.yes()

    # ----------------------------------------------------------------- exit
    def exit_signal(self, signal: dict, cur: Candle) -> Reason | None:
        """Asked on every closed base candle while the trade is open, after the stop and the target
        were checked: a reason to close at this candle's close, or None."""
        return None


# ----------------------------------------------------------------- registry
_registry: dict[str, TradingStrategy] | None = None


def _check(st: Any, where: str) -> TradingStrategy:
    if not isinstance(st, TradingStrategy):
        raise StrategyError(f"{where}: STRATEGY must be an instance of a TradingStrategy subclass.")
    if not (isinstance(st.key, str) and _KEY.match(st.key)):
        raise StrategyError(f"{where}: key {st.key!r} must be lowercase letters, digits and underscores.")
    for attr in ("name", "version", "description"):
        if not (isinstance(getattr(st, attr), str) and getattr(st, attr).strip()):
            raise StrategyError(f"{where}: {attr} is missing.")
    if st.timeframe not in config.INTERVALS_MS:
        raise StrategyError(f"{where}: timeframe {st.timeframe!r} must be one of {list(config.INTERVALS_MS)}.")
    for method in ("find_signals", "levels"):
        if getattr(type(st), method) is getattr(TradingStrategy, method):
            raise StrategyError(f"{where}: {method}() is not implemented.")
    return st


def discover(package: str = PLUGIN_PACKAGE) -> dict[str, TradingStrategy]:
    """Import every plug-in file of `package` and return its strategies by key (sorted by key).
    Files whose name starts with "_" are shared helpers, not strategies."""
    pkg = importlib.import_module(package)
    found: dict[str, TradingStrategy] = {}
    origin: dict[str, str] = {}
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        where = f"{package}.{info.name}"
        mod = importlib.import_module(where)
        if not hasattr(mod, "STRATEGY"):
            raise StrategyError(f"{where}: the file defines no STRATEGY.")
        st = _check(mod.STRATEGY, where)
        if st.key in found:
            raise StrategyError(f"{where}: key {st.key!r} is already used by {origin[st.key]}.")
        found[st.key] = st
        origin[st.key] = where
    return dict(sorted(found.items()))


def trading_strategies() -> dict[str, TradingStrategy]:
    """The registered strategies by key (the plug-in files are read on the first call)."""
    global _registry
    if _registry is None:
        _registry = discover()
    return _registry


def get_trading_strategy(key: str) -> TradingStrategy:
    reg = trading_strategies()
    try:
        return reg[key]
    except KeyError:
        raise ValueError(f"unknown strategy {key!r}; expected one of {sorted(reg)}") from None


def describe_trading_strategies() -> list[dict]:
    """The strategy list as the New session dialog needs it."""
    return [st.describe() for st in trading_strategies().values()]
