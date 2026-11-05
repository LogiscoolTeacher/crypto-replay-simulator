"""The example strategy of README.md ("Adding a strategy") is real code: this test takes it from
the README, registers it and lets an Automatic session trade with it, so the docs cannot go stale."""
from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from app.strategies import plugin
from tests.fakes import DAY, H
from tests.test_day import START
from tests.test_strat_run import run_all, setup

README = Path(__file__).resolve().parents[2] / "README.md"


def readme_strategy() -> plugin.TradingStrategy:
    text = README.read_text(encoding="utf-8")
    part = text.split("### Adding a strategy", 1)[1]
    code = re.search(r"```python\n(.*?)```", part, re.S).group(1)
    mod = types.ModuleType("readme_strategy")
    # the file lives in strategies/plugins/, so it imports the interface relatively
    exec(code.replace("from ..plugin import", "from app.strategies.plugin import"), mod.__dict__)   # noqa: S102
    return mod.STRATEGY


def test_the_readme_example_strategy_trades(tmp_path: Path, monkeypatch):
    st = readme_strategy()
    assert isinstance(st, plugin.TradingStrategy) and (st.key, st.timeframe) == ("morning_buy", "15m")
    monkeypatch.setattr(plugin, "_registry", {**plugin.trading_strategies(), st.key: st})
    day, strat, runner, sid = setup(tmp_path / "replay.db", monkeypatch, None, overrides={}, strategy=st.key, max_trades=1, end=START + 2 * DAY)
    run_all(day, runner, sid)
    trades = day.trades(sid)["trades"]
    assert len(trades) == 1 and trades[0]["status"] == "closed" and all(t["strategy_name"] == "Morning buy" and t["strategy_version"] == "1" for t in trades)
    for t in trades:
        assert (t["entry_ms"] - START) % DAY == 6 * H                     # the 06:00 candle, filled at its close
        assert t["stop_used"] == pytest.approx(t["entry_price"] * 0.99) and t["take_profit"] == pytest.approx(t["entry_price"] * 1.02)
