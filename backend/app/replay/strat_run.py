"""Automatic mode: a strategy session's plug-in trades the replay by itself (the Start button).

The run plays the replay with the same engine calls the buttons make, and every decision only reads
candles that have closed by then. It knows nothing about any particular strategy: everything that
is a trading rule is asked from the session's plug-in (`strategies/plugin.py`), in this order:

1. `find_signals` over the strategy's timeframe (once per run), `locate` for the 15-minute candle of
   each signal: the strategy's entry points.
2. `pre_filter` for the next signal, as of its candle. Turned down: logged with the strategy's
   reason, no jump.
3. The replay **jumps** to the signal's candle (`DayEngine.jump_to`); the strategy's `zone` is drawn.
4. `entry` for that candle (n = 0) and, while the strategy says "wait", for every following candle
   (n = 1, 2, ...): enter at this close, wait, or cancel (logged with the strategy's reason).
5. `levels` gives the stop and the target, the day engine sizes the trade from the session's fixed
   risk, `accept` is the strategy's last word (e.g. a fee filter). Then the buy.
6. The trade runs until the stop or the target (the engine's normal exits); after every candle the
   strategy's `exit_signal` may close it at that close. Then the next signal.

Signals the run passes while a window or a trade is running are logged too (`waiting` /
`trade_open`), so the signal log (`day_signal_log`) holds every signal of the period.

**End.** The session's limits end it (`StrategySessions.settle`, see strat.py): the trade limit, the
replay's end date, or Stop. When no signal is left the run jumps to the replay's last candle, which
is the end date: `ENDED_BY_TIMEOUT`. After the end the run only finishes the pictures and is "done".

**Pictures** (the session's "screenshots" setting; trade pictures only, never day pictures). The
browser takes them (day.js), so `advance` returns with `need: "trade_picture"` whenever a closed
trade's picture is due (24:00 of its exit day reached) and still missing, and never steps on while
it is: a picture that fails holds the run instead of being skipped. After the session's end the
replay moves on to 24:00 of the last exit's day for the last picture (at the replay's last candle
it is due at once, see `DayEngine._picture_due`). With screenshots off nothing waits.

**Zones.** A signal's zone is never merged into a nearby one, and a trade's zone is deleted once
the trade has closed (the trade and the signal log keep its edges), so the chart shows only the
current signal's zone. A strategy without a zone gets the band from its stop to its entry price:
the day engine stores a band with every trade.

The run's own state (`day_sessions.run_state`, JSON) is small: the phase, the signal being decided
on and the trade being held (both as an index into the strategy's signal list, which is computed
again after a restart: the plug-ins are deterministic), and the last signal handled.
"""
from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

from .. import config
from ..indicators import core
from ..strategies.patterns import Candle
from ..strategies.plugin import CANCEL, ENTER, WAIT, Decision, Levels, PlannedTrade, Reason, TradingStrategy, Verdict, get_trading_strategy
from .day_engine import BASE_INTERVAL, EXIT_REASON_LABELS, NOTE_MAX_CHARS, DayEngine, local_str
from .engine import WARMUP_BARS, Conflict, _iso, _now_iso
from .strat import AUTOMATIC, StrategySessions

IDLE, WAITING, TRADE, FINISHING, DONE = "idle", "waiting", "trade", "finishing", "done"
MAX_STEPS_PER_CALL = 400   # one advance call never runs longer than this many 15m steps (the UI stays fresh)
NEED_TRADE_PICTURE = "trade_picture"


class EngineMarket:
    """What a strategy may read at a decision (`plugin.Market`): only candles closed by the close
    of the 15-minute candle opening at `cursor_ms`."""

    def __init__(self, day: DayEngine, s: dict, cursor_ms: int | None = None) -> None:
        self._day = day
        self._s = dict(s, cursor_ms=s["cursor_ms"] if cursor_ms is None else cursor_ms)
        self._close = self._s["cursor_ms"] + day._base_ms

    def candles(self, interval: str, bars: int) -> pd.DataFrame:
        iv = config.interval_ms(interval)
        return self._day.candles.get_candles(self._s["symbol"], interval, self._close - int(bars) * iv, self._close - iv)

    def atr(self, interval: str, period: int = 14) -> float | None:
        """ATR of the last closed `interval` candle (Wilder, like the app's ATR)."""
        df = self.candles(interval, period * 10 + 1)
        if len(df) <= period:
            return None
        v = float(core.atr(df["high"], df["low"], df["close"], period).iloc[-1])
        return v if math.isfinite(v) else None

    def daily_trend(self) -> int | None:
        return self._day._daily_ema(self._s)["ema_above"]


class StrategyRun:
    """The Automatic run of one strategy session (one run per session, kept in day_sessions.run_state)."""

    def __init__(self, strat: StrategySessions) -> None:
        self.strat = strat
        self.day = strat.day
        self.store = strat.store
        self.candles = strat.day.candles
        self._signals: dict[str, tuple[tuple, list[dict]]] = {}   # per session: (what they were computed for, the entries)

    # ------------------------------------------------------------ public
    def advance(self, session_id: str, max_steps: int = MAX_STEPS_PER_CALL) -> dict:
        """Start the run, or run on: until a trade picture is needed, something happened the screen
        should show (a jump, a buy, an exit), the run is done, or `max_steps` candles were stepped.
        Returns the session state with `events` and `run` (the session block's run status plus
        `need`: "trade_picture" or None). Pausing is simply not calling this."""
        events: list[dict] = []
        with self.day._lock:
            ds = self.strat._row(session_id)
            if ds["run_mode"] != AUTOMATIC:
                raise Conflict("Only an Automatic session runs by itself.")
            s = self.day._ready(session_id)
            strategy = self._strategy(ds)
            run = json.loads(ds["run_state"]) if ds.get("run_state") else None
            if run is None:
                run = {"phase": IDLE, "signal": None, "n": 0, "trade": None, "seen_ms": s["cursor_ms"],
                       "started_at": _now_iso(), "start_ms": s["cursor_ms"]}
                self._save(session_id, run)
            pictures = bool(ds.get("pictures"))
            st = self.day.state(session_id)
            steps = 0
            while run["phase"] != DONE and steps < max_steps:
                if st["day"].get("picture_due"):   # never step past a missing trade picture
                    break
                if st["day"]["session"]["ended"] and run["phase"] != FINISHING:
                    st = self._session_ended(session_id, run, strategy, st)
                    self._save(session_id, run)
                    continue
                phase = run["phase"]
                if phase == IDLE:
                    st, stop, stepped = self._idle(session_id, run, strategy, st, events, pictures)
                elif phase == WAITING:
                    st, stop, stepped = self._waiting(session_id, run, strategy, st, events)
                elif phase == TRADE:
                    st, stop, stepped = self._trade(session_id, run, strategy, st, events)
                else:   # finishing: only the last trade pictures are still to come
                    st, stop, stepped = self._finishing(session_id, run, st, events, pictures)
                steps += stepped
                self._save(session_id, run)
                if stop:
                    break
        st = self.day.state(session_id)
        st["events"] = events
        need = NEED_TRADE_PICTURE if run["phase"] != DONE and st["day"].get("picture_due") else None
        st["run"] = {**st["day"]["session"]["run"], "need": need}
        return st

    def signals(self, session_id: str) -> list[dict]:
        """The signal log of the session, oldest first, as the API hands it out."""
        self.strat._row(session_id)
        with self.store.tx() as conn:
            rows = self.store.list_signals(conn, session_id)
        for r in rows:
            r["outcome_label"] = self.strat.outcome_label(r["outcome"], r.get("outcome_text")) if r["outcome"] else None
            for k in ("signal_ms", "bar_ms", "confirm_ms"):
                r[k.replace("_ms", "_iso")] = _iso(r[k]) if r[k] is not None else None
            r["details"] = json.loads(r["details"]) if r.get("details") else None
            r["what_ifs"] = json.loads(r["what_ifs"]) if r.get("what_ifs") else None
        return rows

    # ------------------------------------------------------------ phases
    # Each returns (state, stop, stepped): stop = hand the state back to the screen now.
    def _idle(self, sid: str, run: dict, strategy: TradingStrategy, st: dict, events: list, pictures: bool) -> tuple[dict, bool, int]:
        s = self.day._ready(sid)
        upcoming = self._upcoming(s, strategy, run)
        nxt = upcoming[0] if upcoming else None
        if nxt is not None:
            verdict = self._verdict(strategy, "pre_filter", strategy.pre_filter(nxt["sig"], EngineMarket(self.day, s, nxt["cursor_ms"])))
            if not verdict.ok:   # logged without a jump: nothing to watch for it
                with self.store.tx() as conn:
                    self.store.add_signal(conn, session_id=sid, **self._signal_fields(s, strategy, nxt),
                                          outcome=verdict.code or "filtered", outcome_text=verdict.text or None,
                                          details=self._details(verdict.data), created_at=_now_iso(), updated_at=_now_iso())
                run["seen_ms"] = nxt["cursor_ms"]
                return st, False, 0
        target = nxt["cursor_ms"] if nxt is not None else s["data_end_ms"]
        if pictures:
            # a closed trade's picture becomes due (its 24:00) before the jump lands: step there first
            with self.store.tx() as conn:
                due = self.store.due_pictures(conn, sid)
            if any(self.day._review_end(t) <= target + self.day._base_ms for t in due):
                st, stop = self._step(sid, st, events)
                return st, stop, 1
        if nxt is None:   # the time limit of an Automatic session is its end date: settle ends it there
            st = self.day.jump_to(sid, s["data_end_ms"], "No signal is left before the end of the replay: on to its last candle.")
            events.extend(st.pop("events", []))
            return st, False, 0   # the loop goes on: the session has just ended there
        sig = nxt["sig"]
        st = self.day.jump_to(sid, nxt["cursor_ms"], f"{strategy.name}: {strategy.describe_signal(sig)}.")
        evs = st.pop("events", [])
        zone = self._zone_of(strategy, sig)
        z = self.day._signal_zone(sid, {"zone_top": zone[0], "zone_bottom": zone[1]}, merge=False) if zone else None
        for ev in evs:
            if ev["kind"] == "jump":
                ev.update({"strategy": strategy.key, "zone_id": z["id"] if z else None})
        events.extend(evs)
        s = self.day._ready(sid)
        with self.store.tx() as conn:
            row = self.store.add_signal(conn, session_id=sid, **self._signal_fields(s, strategy, nxt), outcome=None,
                                        created_at=_now_iso(), updated_at=_now_iso())
        run["signal"] = {"log_id": row["id"], "index": nxt["index"], "bar_ms": nxt["bar_ms"], "cursor_ms": nxt["cursor_ms"]}
        run["n"] = 0
        run["seen_ms"] = nxt["cursor_ms"]
        run["phase"] = WAITING
        st, _ = self._decide(sid, run, strategy, st, events)
        return st, True, 0

    def _waiting(self, sid: str, run: dict, strategy: TradingStrategy, st: dict, events: list) -> tuple[dict, bool, int]:
        st, stop = self._step(sid, st, events)
        run["n"] += 1
        if st["day"]["session"]["ended"]:   # the replay's last candle: an ended session decides nothing
            return st, True, 1
        st, bought = self._decide(sid, run, strategy, st, events)
        return st, stop or bought, 1

    def _trade(self, sid: str, run: dict, strategy: TradingStrategy, st: dict, events: list) -> tuple[dict, bool, int]:
        if st["day"]["trade"] is None:   # closed (stop or target): look for the next signal
            return self._closed(sid, run), False, 0
        st, stop = self._step(sid, st, events)
        if st["day"]["trade"] is not None:
            s = self.day._ready(sid)
            cur = self._candle(s, s["cursor_ms"])
            held = self._entry_of(s, strategy, run, run["trade"])
            reason = strategy.exit_signal(held["sig"], cur) if cur is not None else None
            if reason is not None:   # the strategy wants out: at this candle's close
                if not isinstance(reason, Reason):
                    raise Conflict(f"{strategy.label}: exit_signal() must return a Reason or None.")
                known = reason.code in EXIT_REASON_LABELS and reason.code != "other"
                note = f"Automatic ({strategy.label}): {reason.text or reason.code}"[:NOTE_MAX_CHARS]
                st = self.day.sell(sid, reason.code if known else "other", note)
                events.extend(st.pop("events", []))
                stop = True
        if st["day"]["trade"] is None:
            st = self._closed(sid, run)
        return st, stop, 1

    def _closed(self, sid: str, run: dict) -> dict:
        """The run's trade has closed: its zone is deleted, the "end the day or keep trading" choice
        of a hand-traded day is skipped, and the run looks for the next signal."""
        held = run.get("trade") or {}
        run["trade"] = None
        if held.get("zone_id") is not None:
            with self.store.tx() as conn:
                z = self.store.get_zone(conn, sid, held["zone_id"])
                if z is not None and z.get("source") == "signal":
                    self.store.delete_zone(conn, sid, z["id"])
        run["phase"] = IDLE
        return self.day.keep_trading(sid)

    def _session_ended(self, sid: str, run: dict, strategy: TradingStrategy, st: dict) -> dict:
        """A limit or Stop ended the session: a signal still being decided on is closed in the log,
        a trade the end closed is let go, the signals passed since the last look are logged (the
        log holds every signal up to the end), and only the pictures are left to do."""
        if run["signal"]:
            if st["finished"]:
                self._close_signal(sid, run, "end_of_data", "The replay ended during the entry window.")
            else:
                self._close_signal(sid, run, "session_ended", "The session ended during the entry window.")
        if run.get("trade"):
            st = self._closed(sid, run)
        self._upcoming(self.day._ready(sid), strategy, run)
        run["phase"] = FINISHING
        return st

    def _finishing(self, sid: str, run: dict, st: dict, events: list, pictures: bool) -> tuple[dict, bool, int]:
        with self.store.tx() as conn:
            due = self.store.due_pictures(conn, sid) if pictures else []
        if not due or st["finished"]:   # at the last candle every missing picture is due at once (the loop asks for it)
            run["phase"] = DONE
            return st, True, 0
        st, stop = self._step(sid, st, events)   # on to the last trade's 24:00
        return st, stop, 1

    # ------------------------------------------------------------- steps
    def _step(self, sid: str, st: dict, events: list) -> tuple[dict, bool]:
        """One 15m step. Returns (state, stop): stop after an exit and where a trade picture became due."""
        st = self.day.step(sid, 1)
        evs = st.pop("events", [])
        events.extend(evs)
        return st, any(e["kind"] in ("exit", "picture") for e in evs)

    def _decide(self, sid: str, run: dict, strategy: TradingStrategy, st: dict, events: list) -> tuple[dict, bool]:
        """Ask the strategy about the candle that just closed (candle `n` of the signal). Returns (state, bought)."""
        s = self.day._ready(sid)
        entry = self._entry_of(s, strategy, run, run["signal"])
        cur = self._candle(s, s["cursor_ms"])
        prev = self._candle(s, s["cursor_ms"] - self.day._base_ms)
        d = strategy.entry(entry["sig"], run["n"], cur, prev)
        if not isinstance(d, Decision) or d.action not in (ENTER, WAIT, CANCEL):
            raise Conflict(f"{strategy.label}: entry() must return a Decision (enter, wait or cancel).")
        if d.action == ENTER:
            return self._enter(sid, run, strategy, st, entry, d, events)
        if d.action == CANCEL:
            self._close_signal(sid, run, d.code or "cancelled", d.text, confirm_ms=s["cursor_ms"])
        return st, False

    def _enter(self, sid: str, run: dict, strategy: TradingStrategy, st: dict, entry: dict, d: Decision, events: list) -> tuple[dict, bool]:
        """The strategy enters at this close: its stop and target, the engine's size, its last word, the buy."""
        s = self.day._ready(sid)
        sig = entry["sig"]
        price = st["price"]   # the close of the cursor's candle: where the engine fills
        f: dict[str, Any] = {"confirm_ms": s["cursor_ms"], "pattern": d.code or None}
        lv = strategy.levels(sig, price, EngineMarket(self.day, s))
        if lv is None:
            self._close_signal(sid, run, "invalid", "No valid trade (the strategy gave no stop and target).", **f)
            return st, False
        if not isinstance(lv, Levels):
            raise Conflict(f"{strategy.label}: levels() must return Levels or None.")
        f.update(atr=lv.atr, entry=price, stop=lv.stop, take_profit=lv.take_profit,
                 stop_dist_pct=(price - lv.stop) / price * 100.0 if price else None)
        # the trade's band: the strategy's zone (back if crossing 00:00 removed the unused one), else stop to entry
        top, bottom = self._zone_of(strategy, sig) or (price, lv.stop)
        if not top > bottom:
            self._close_signal(sid, run, "invalid", "No valid trade (the stop is not below the entry).", **f)
            return st, False
        zone = self.day._signal_zone(sid, {"zone_top": top, "zone_bottom": bottom}, merge=False)
        p = self.day.plan(sid, zone["id"], stop=lv.stop, take_profit=lv.take_profit)
        if p["errors"] or p["blocked"] or p["take_profit"] is None:
            self._close_signal(sid, run, "invalid", "No valid trade: " + (" ".join(p["errors"]) or p["blocked"] or "no target."), **f)
            return st, False
        verdict = self._verdict(strategy, "accept", strategy.accept(PlannedTrade(
            entry=p["entry_price"], stop=p["stop_used"], take_profit=p["take_profit"],
            risk_amount=p["risk_amount"], buy_fee=p["buy_fee"], fee_rate=p["fee_rate"])))
        f.update({k: verdict.data[k] for k in ("est_fees", "fee_share") if k in verdict.data})
        f["details"] = self._details({"decision": d.code, "levels": lv.kind, **verdict.data})
        if not verdict.ok:
            self._close_signal(sid, run, verdict.code or "rejected", verdict.text, **f)
            return st, False
        base = self.day._base_ms
        note = (f"Automatic ({strategy.label}): {d.text or d.code or 'entry'} Entry at {local_str(s['cursor_ms'] + base)}, "
                f"signal at {local_str(entry['cursor_ms'] + base)}. {lv.text}").strip()[:NOTE_MAX_CHARS]
        res = self.day.buy(sid, zone["id"], take_profit=lv.take_profit, stop=lv.stop, notes=note)
        st, trade = res["state"], res["trade"]
        run["trade"] = {"id": trade["id"], "zone_id": zone["id"], "index": entry["index"], "bar_ms": entry["bar_ms"],
                        "cursor_ms": entry["cursor_ms"]}
        self._close_signal(sid, run, "traded", "", trade_id=trade["id"], **f)
        run["phase"] = TRADE
        events.append({"kind": "strategy_buy", "n": st["day"]["session"]["trades_taken"], "code": d.code, "text": d.text, "trade": trade})
        return st, True

    def _close_signal(self, sid: str, run: dict, outcome: str, text: str = "", **f: Any) -> None:
        with self.store.tx() as conn:
            self.store.update_signal(conn, run["signal"]["log_id"], outcome=outcome, outcome_text=text or None,
                                     updated_at=_now_iso(), **f)
        run["signal"] = None
        run["n"] = 0
        if run["phase"] == WAITING:
            run["phase"] = IDLE

    # ----------------------------------------------------------- signals
    def _entries(self, s: dict, strategy: TradingStrategy, run: dict) -> list[dict]:
        """The strategy's signals over the session's candles (warm-up included), computed once per
        run: `find_signals` is one causal pass, so the signals after the cursor never depend on
        later candles. Each entry: the plug-in's own dict (`sig`), its candle and its place in the list."""
        key = (strategy.key, strategy.version, s["start_ms"], s["data_end_ms"], run["started_at"])
        hit = self._signals.get(s["id"])
        if hit is None or hit[0] != key:
            iv = config.interval_ms(strategy.timeframe)
            df = self.candles.get_candles(s["symbol"], strategy.timeframe, s["start_ms"] - WARMUP_BARS * iv, s["data_end_ms"])
            found = strategy.find_signals(df)
            if not all(isinstance(sig, dict) and isinstance(sig.get("bar_ms"), int) for sig in found):
                raise Conflict(f"{strategy.label}: every signal of find_signals() must be a dict with a whole-number bar_ms.")
            hit = (key, [{"sig": sig, "bar_ms": sig["bar_ms"], "index": i} for i, sig in enumerate(found)])
            self._signals[s["id"]] = hit
        return hit[1]

    def _entry_of(self, s: dict, strategy: TradingStrategy, run: dict, ref: dict) -> dict:
        """The signal the run is deciding on or holding a trade of, found again in the strategy's list."""
        entries = self._entries(s, strategy, run)
        e = entries[ref["index"]] if 0 <= ref["index"] < len(entries) else None
        if e is None or e["bar_ms"] != ref["bar_ms"]:
            raise Conflict(f"{strategy.label} no longer finds the signal this run was working on: reset the session.")
        e.setdefault("cursor_ms", ref.get("cursor_ms"))
        return e

    def _upcoming(self, s: dict, strategy: TradingStrategy, run: dict) -> list[dict]:
        """The signals after the cursor, in order, each with `cursor_ms`: the 15m candle the strategy
        places it in (`locate`). The ones between the last signal the run handled and the cursor
        were passed during a window or a trade: they are logged here (`waiting` / `trade_open`)."""
        iv = config.interval_ms(strategy.timeframe)
        base = self.day._base_ms
        passed, upcoming = [], []
        for e in self._entries(s, strategy, run):
            if e["bar_ms"] + iv <= run["seen_ms"]:
                continue
            if "cursor_ms" not in e:
                inside = self.candles.get_candles(s["symbol"], BASE_INTERVAL, e["bar_ms"], e["bar_ms"] + iv - base)
                e["cursor_ms"] = strategy.locate(e["sig"], inside)
            c = e["cursor_ms"]
            if c is None or c <= run["seen_ms"] or c > s["data_end_ms"]:
                continue
            (passed if c <= s["cursor_ms"] else upcoming).append(e)
        if passed:
            with self.store.tx() as conn:
                trades = self.store.list_trades(conn, s["id"])
                for e in passed:
                    c = e["cursor_ms"]
                    busy = any(t["entry_ms"] <= c and (t["exit_ms"] is None or c <= t["exit_ms"]) for t in trades)
                    self.store.add_signal(conn, session_id=s["id"], **self._signal_fields(s, strategy, e),
                                          outcome="trade_open" if busy else "waiting", created_at=_now_iso(), updated_at=_now_iso())
            run["seen_ms"] = max(e["cursor_ms"] for e in passed)
        return upcoming

    def _signal_fields(self, s: dict, strategy: TradingStrategy, e: dict) -> dict:
        """The log columns every signal gets. A strategy without a zone has no band to log: the
        band columns then hold the close of the signal's candle."""
        sig = e["sig"]
        zone = self._zone_of(strategy, sig)
        if zone is None:
            cur = self._candle(s, e["cursor_ms"])
            zone = (cur[3], cur[3]) if cur is not None else (0.0, 0.0)
        touches = sig.get("touches")
        return {"signal_ms": e["cursor_ms"], "bar_ms": e["bar_ms"], "zone_top": zone[0], "zone_bottom": zone[1],
                "entry_level": float(sig.get("entry_level", zone[0])), "rules": ",".join(str(r) for r in sig.get("rules") or []),
                "touches": int(touches) if isinstance(touches, (int, float)) and not isinstance(touches, bool) else None,
                "ema_above": self.day._daily_ema(dict(s, cursor_ms=e["cursor_ms"]))["ema_above"], "strategy_key": strategy.key}

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _strategy(ds: dict) -> TradingStrategy:
        """The session's plug-in, as long as it is still the one the session was set up with: a
        result must come from the rules stored with it."""
        try:
            st = get_trading_strategy(ds["strategy_key"])
        except ValueError:
            raise Conflict(f"The strategy {ds['strategy_name']!r} is not registered any more: this session cannot run.") from None
        now = json.loads(json.dumps(st.settings()))
        if st.version != ds["strategy_version"] or now != json.loads(ds["strategy_settings"] or "{}"):
            raise Conflict(f"{ds['strategy_name']} has changed since this session was set up (it was version "
                           f"{ds['strategy_version']}, the plug-in is version {st.version}): start a new session.")
        return st

    @staticmethod
    def _verdict(strategy: TradingStrategy, method: str, v: Any) -> Verdict:
        if not isinstance(v, Verdict):
            raise Conflict(f"{strategy.label}: {method}() must return a Verdict.")
        return v

    @staticmethod
    def _zone_of(strategy: TradingStrategy, sig: dict) -> tuple[float, float] | None:
        z = strategy.zone(sig)
        if z is None:
            return None
        top, bottom = float(z[0]), float(z[1])
        if not (math.isfinite(top) and math.isfinite(bottom) and top > bottom > 0):
            raise Conflict(f"{strategy.label}: zone() must return (top, bottom) with top above bottom.")
        return top, bottom

    @staticmethod
    def _details(data: dict) -> str | None:
        """What the strategy's filters reported, as JSON (numbers of other libraries become plain ones)."""
        return json.dumps(data, default=lambda o: o.item() if hasattr(o, "item") else str(o)) if data else None

    def _candle(self, s: dict, open_ms: int) -> Candle | None:
        df = self.candles.get_candles(s["symbol"], BASE_INTERVAL, open_ms, open_ms)
        if len(df) != 1:
            return None
        r = df.iloc[0]
        return float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])

    def _save(self, session_id: str, run: dict) -> None:
        with self.store.tx() as conn:
            self.store.update_day_session(conn, session_id, run_state=json.dumps(run), updated_at=_now_iso())
