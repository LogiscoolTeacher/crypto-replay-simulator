"""Trading-strategy plug-ins: the registry finds the strategy files by itself, and the two
RLine_Retest strategies answer the engine's questions with the rules of the CTest runs."""
from __future__ import annotations

import sys
import textwrap

import pandas as pd
import pytest

from app.indicators.registry import validate_params
from app.replay.autorun import AutorunRules, confirmation
from app.strategies import plugin
from app.strategies.plugin import (
    CANCEL,
    ENTER,
    WAIT,
    PlannedTrade,
    StrategyError,
    TradingStrategy,
    describe_trading_strategies,
    discover,
    get_trading_strategy,
    trading_strategies,
)
from app.strategies.rline import RLINE_PARAMS, detect_rline
from tests.test_rline import TWO_TOUCHES, path

CONFIRMED = get_trading_strategy("rline_retest_confirmed")
SIGNAL = get_trading_strategy("rline_retest_signal")
SIG = {"bar_ms": 0, "zone_top": 100.0, "zone_bottom": 98.0, "entry_level": 100.5, "rules": ["A"], "touches": 2}

FLAT = (101.0, 101.5, 100.8, 101.2)        # nothing: a small green candle above the zone
HAMMER = (101.0, 101.3, 99.8, 101.2)       # body 0.2, lower wick 1.2, upper wick 0.1, low inside the zone's reach
RED = (101.5, 101.6, 100.9, 101.0)
ENGULF = (100.9, 101.9, 100.85, 101.7)     # green, body covers RED's
BROKEN = (99.0, 99.2, 97.0, 97.5)          # closes below the zone bottom


class FakeMarket:
    """The engine's side of a decision, scripted: an ATR and the daily trend."""

    def __init__(self, atr: float | None = 2.0, trend: int | None = 1) -> None:
        self._atr, self._trend, self.asked = atr, trend, []

    def candles(self, interval: str, bars: int) -> pd.DataFrame:
        return pd.DataFrame()

    def atr(self, interval: str, period: int = 14) -> float | None:
        self.asked.append((interval, period))
        return self._atr

    def daily_trend(self) -> int | None:
        return self._trend


# ----------------------------------------------------------------- registry
def test_registry_finds_both_strategies_with_name_version_and_timeframe():
    reg = trading_strategies()
    assert list(reg) == ["rline_retest_confirmed", "rline_retest_signal"]
    assert (CONFIRMED.name, CONFIRMED.version, CONFIRMED.timeframe) == ("RLine_Retest confirmed", "1", "1h")
    assert (SIGNAL.name, SIGNAL.version, SIGNAL.timeframe) == ("RLine_Retest signal candle", "1", "1h")
    assert CONFIRMED.label == "RLine_Retest confirmed v1"
    with pytest.raises(ValueError, match="unknown strategy 'nope'"):
        get_trading_strategy("nope")


def test_describe_lists_what_the_new_session_dialog_needs():
    rows = describe_trading_strategies()
    assert [r["key"] for r in rows] == ["rline_retest_confirmed", "rline_retest_signal"]
    for r in rows:
        assert set(r) == {"key", "name", "version", "label", "description", "timeframe", "rules", "settings"}
        assert r["rules"] and all(isinstance(line, str) for line in r["rules"])
    assert any("Target: 2.5 R." in line for line in rows[1]["rules"])


def _package(tmp_path, monkeypatch, name: str, files: dict[str, str]) -> str:
    """A throw-away plug-in package on disk."""
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for fname, body in files.items():
        (pkg / fname).write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        monkeypatch.delitem(sys.modules, mod)
    return name


PLUGIN_SRC = """
    from app.strategies.plugin import Levels, TradingStrategy

    class Mine(TradingStrategy):
        key = "{key}"
        name = "Mine"
        version = "3"
        description = "A test strategy."
        timeframe = "{timeframe}"

        def find_signals(self, candles):
            return [{{"bar_ms": 0}}]

        def levels(self, signal, entry_price, market):
            return Levels(stop=entry_price * 0.99, take_profit=entry_price * 1.02, kind="percent")

    STRATEGY = Mine()
"""


def test_a_new_strategy_is_one_new_file(tmp_path, monkeypatch):
    """Dropping a file into the package is all it takes; "_" files are helpers and are skipped."""
    pkg = _package(tmp_path, monkeypatch, "plug_ok", {
        "mine.py": PLUGIN_SRC.format(key="mine", timeframe="4h"),
        "_shared.py": "raise RuntimeError('helpers are never loaded as strategies')\n",
    })
    found = discover(pkg)
    assert list(found) == ["mine"]
    st = found["mine"]
    assert (st.label, st.timeframe) == ("Mine v3", "4h")
    # the defaults of the base class: enter on the signal candle, no filter, no exit of its own
    assert st.entry({"bar_ms": 0}, 0, FLAT, None).action == ENTER
    assert st.entry({"bar_ms": 0}, 0, None, None).action == CANCEL
    assert st.pre_filter({"bar_ms": 0}, FakeMarket()).ok and st.exit_signal({"bar_ms": 0}, FLAT) is None
    assert st.accept(PlannedTrade(100, 99, 102, 100, 1, 0.001)).ok
    base = pd.DataFrame({"time": [900, 1800], "low": [1.0, 1.0]})
    assert st.locate({"bar_ms": 0}, base) == 900_000 and st.locate({"bar_ms": 0}, base.iloc[:0]) is None
    lv = st.levels({"bar_ms": 0}, 100.0, FakeMarket())
    assert (lv.stop, lv.take_profit, lv.kind) == (99.0, 102.0, "percent")


@pytest.mark.parametrize("files, message", [
    ({"empty.py": "X = 1\n"}, "defines no STRATEGY"),
    ({"wrong.py": "STRATEGY = object()\n"}, "must be an instance of a TradingStrategy subclass"),
    ({"a.py": PLUGIN_SRC.format(key="same", timeframe="1h"), "b.py": PLUGIN_SRC.format(key="same", timeframe="1h")}, "already used by"),
    ({"tf.py": PLUGIN_SRC.format(key="tf", timeframe="2h")}, "timeframe '2h' must be one of"),
    ({"key.py": PLUGIN_SRC.format(key="Bad Key", timeframe="1h")}, "must be lowercase letters"),
    ({"bare.py": "from app.strategies.plugin import TradingStrategy\n"
                 "class B(TradingStrategy):\n    key = 'b'\n    name = 'B'\n    version = '1'\n    description = 'd'\n    timeframe = '1h'\n"
                 "STRATEGY = B()\n"}, r"find_signals\(\) is not implemented"),
    ({"nover.py": PLUGIN_SRC.format(key="nover", timeframe="1h").replace('version = "3"', 'version = ""')}, "version is missing"),
])
def test_a_broken_plugin_file_is_named(tmp_path, monkeypatch, files, message):
    pkg = _package(tmp_path, monkeypatch, "plug_bad", files)
    with pytest.raises(StrategyError, match=message):
        discover(pkg)


def test_base_class_leaves_signals_and_levels_to_the_plugin():
    with pytest.raises(NotImplementedError):
        TradingStrategy().find_signals(pd.DataFrame())
    with pytest.raises(NotImplementedError):
        TradingStrategy().levels({}, 1.0, FakeMarket())


# ------------------------------------------------------------------ signals
def test_signals_are_the_signal_modules_own():
    """The plug-ins reuse rline.py as it is: same signals, with the detector's default parameters."""
    df = path(88.0, TWO_TOUCHES)
    want = detect_rline(df)["signals"]
    assert len(want) == 1
    assert CONFIRMED.find_signals(df) == want and SIGNAL.find_signals(df) == want
    s = want[0]
    assert CONFIRMED.zone(s) == (s["zone_top"], s["zone_bottom"])
    assert "retest of" in CONFIRMED.describe_signal(s)
    assert CONFIRMED.settings()["detector_params"] == validate_params(RLINE_PARAMS, {})


def test_locate_is_the_first_base_candle_reaching_the_entry_level():
    base = pd.DataFrame({"time": [0, 900, 1800, 2700], "low": [101.0, 100.5, 99.0, 100.2]})
    assert CONFIRMED.locate(SIG, base) == 900_000                       # low <= 100.5 (the entry level)
    assert CONFIRMED.locate({**SIG, "entry_level": 99.5}, base) == 1_800_000
    no_level = {k: v for k, v in SIG.items() if k != "entry_level"}     # falls back to the zone top (100)
    assert CONFIRMED.locate(no_level, base) == 1_800_000
    assert CONFIRMED.locate({**SIG, "entry_level": 90.0}, base) is None


# -------------------------------------------------------------------- entry
def test_confirmed_waits_for_a_hammer_or_an_engulfing():
    e = CONFIRMED.entry
    assert e(SIG, 0, HAMMER, None).action == WAIT                       # the signal candle never confirms
    assert e(SIG, 1, FLAT, FLAT).action == WAIT
    d = e(SIG, 2, HAMMER, FLAT)
    assert (d.action, d.code) == (ENTER, "hammer")
    d = e(SIG, 3, ENGULF, RED)
    assert (d.action, d.code) == (ENTER, "engulfing")
    assert e(SIG, 3, ENGULF, None).action == WAIT                       # no previous candle: no engulfing
    d = e(SIG, 4, BROKEN, FLAT)
    assert (d.action, d.code) == (CANCEL, "zone_broken")
    broken_hammer = (97.6, 97.7, 96.0, 97.5)                            # hammer-shaped, but closing below the zone
    assert e(SIG, 4, broken_hammer, FLAT).code == "zone_broken"         # the break wins over a pattern


def test_confirmed_window_ends_after_ten_candles():
    e = CONFIRMED.entry
    assert e(SIG, 9, FLAT, FLAT).action == WAIT
    d = e(SIG, 10, FLAT, FLAT)
    assert (d.action, d.code) == (CANCEL, "no_confirmation")
    assert e(SIG, 10, HAMMER, FLAT).action == ENTER                     # the tenth candle may still confirm
    assert e(SIG, 5, None, FLAT).action == WAIT                         # a data gap uses up a candle
    assert e(SIG, 10, None, FLAT).code == "no_confirmation"


@pytest.mark.parametrize("prev, cur", [(FLAT, FLAT), (FLAT, HAMMER), (RED, ENGULF), (FLAT, BROKEN), (None, ENGULF)])
def test_confirmed_agrees_with_the_autoruns_confirmation(prev, cur):
    """The plug-in reads a candle exactly like the autorun of CTest__01 did."""
    want = confirmation(prev, cur, SIG["zone_bottom"], SIG["entry_level"], AutorunRules())
    got = CONFIRMED.entry(SIG, 1, cur, prev)
    assert {None: (WAIT, ""), "broken": (CANCEL, "zone_broken")}.get(want, (ENTER, want)) == (got.action, got.code)


def test_signal_candle_enters_at_once():
    e = SIGNAL.entry
    d = e(SIG, 0, FLAT, None)
    assert (d.action, d.code) == (ENTER, "signal")
    assert (e(SIG, 0, BROKEN, None).action, e(SIG, 0, BROKEN, None).code) == (CANCEL, "zone_broken")
    assert (e(SIG, 0, None, None).action, e(SIG, 0, None, None).code) == (CANCEL, "invalid")


# ---------------------------------------------------------- stop and target
def test_levels_stop_below_the_zone_and_target_by_ratio():
    m = FakeMarket(atr=2.0)
    lv = CONFIRMED.levels(SIG, 101.0, m)
    assert m.asked == [("1h", 14)]
    assert lv.stop == pytest.approx(97.5)                               # 98 - 0.25 x 2
    assert lv.take_profit == pytest.approx(101.0 + 2.0 * 3.5)           # 2 R
    assert (lv.kind, lv.atr) == ("atr", 2.0) and "0.25 x ATR 14 (1h)" in lv.text
    lv = SIGNAL.levels(SIG, 101.0, FakeMarket(atr=2.0))
    assert lv.stop == pytest.approx(97.5) and lv.take_profit == pytest.approx(101.0 + 2.5 * 3.5)   # 2.5 R


def test_levels_without_a_valid_trade():
    assert CONFIRMED.levels(SIG, 101.0, FakeMarket(atr=None)) is None   # the ATR is missing
    assert CONFIRMED.levels(SIG, 97.5, FakeMarket(atr=2.0)) is None     # the entry is at the stop
    assert CONFIRMED.levels(SIG, 97.0, FakeMarket(atr=2.0)) is None     # ... or below it


# ------------------------------------------------------------------ filters
def test_trend_filter_belongs_to_the_signal_candle_strategy():
    for trend in (1, 0, None):
        assert CONFIRMED.pre_filter(SIG, FakeMarket(trend=trend)).ok    # all trends
    assert SIGNAL.pre_filter(SIG, FakeMarket(trend=1)).ok
    for trend in (0, None):                                             # below, or unknown: not traded
        v = SIGNAL.pre_filter(SIG, FakeMarket(trend=trend))
        assert (v.ok, v.code) == (False, "trend_filter")


def test_fee_filter_turns_down_a_tight_stop():
    plan = lambda buy_fee: PlannedTrade(entry=100.0, stop=99.0, take_profit=102.0, risk_amount=100.0, buy_fee=buy_fee, fee_rate=0.001)  # noqa: E731
    v = CONFIRMED.accept(plan(5.0))                                     # 2 x 5 = 10 % of the risk
    assert v.ok and v.data == {"est_fees": 10.0, "fee_share": pytest.approx(0.1)}
    v = CONFIRMED.accept(plan(10.0))                                    # 20 %: at the limit is too much
    assert (v.ok, v.code) == (False, "fees_too_high") and v.data["fee_share"] == pytest.approx(0.2)
    assert not SIGNAL.accept(plan(12.0)).ok


def test_break_exit_only_for_the_signal_candle_strategy():
    r = SIGNAL.exit_signal(SIG, BROKEN)
    assert r is not None and r.code == "setup_broken"
    assert SIGNAL.exit_signal(SIG, FLAT) is None
    assert SIGNAL.exit_signal(SIG, (99.0, 99.2, 97.0, 98.0)) is None    # a close AT the bottom is not below it
    assert CONFIRMED.exit_signal(SIG, BROKEN) is None


# ----------------------------------------------------------------- settings
def test_settings_hold_every_rule_and_match_the_ctest_runs():
    """The values stored with a session: the rules of CTest__01 (the autorun's defaults) and of CTest__02."""
    d = AutorunRules()
    c, s = CONFIRMED.settings(), SIGNAL.settings()
    for k in ("confirm_bars", "hammer", "engulfing", "hammer_wick_body", "hammer_upper_body",
              "stop_atr_mult", "stop_atr_interval", "ratio", "max_fee_share", "trend_filter", "break_exit"):
        assert c[k] == getattr(d, k), k
    assert c["entry"] == d.entry == "confirm" and c["detector"] == d.strategy == "rline_retest"
    assert (s["entry"], s["trend_filter"], s["break_exit"], s["ratio"]) == ("signal", "above", True, 2.5)
    for k in ("stop_atr_mult", "stop_atr_interval", "max_fee_share", "atr_period", "detector_params"):
        assert s[k] == c[k], k
    AutorunRules.from_dict({k: s[k] for k in ("entry", "trend_filter", "break_exit", "ratio", "stop_atr_mult", "max_fee_share")})


def test_the_registry_reads_the_files_once():
    assert trading_strategies() is trading_strategies()
    assert plugin.PLUGIN_PACKAGE == "app.strategies.plugins"
