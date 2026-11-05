"""The trading strategies of the Automatic and Manual session modes, one file per strategy.

To add a strategy, add a file here that defines `STRATEGY = YourStrategy()` (a subclass of
`app.strategies.plugin.TradingStrategy`). Nothing else is edited: the registry in `plugin.py`
finds the file by itself. A file whose name starts with "_" is a shared helper, not a strategy.

A released version's rules are never changed: copy the file (or subclass it) under a new
`version` instead, so the trades stored with the old one stay comparable.
"""
