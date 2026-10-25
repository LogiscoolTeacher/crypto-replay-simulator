"""FastAPI application for the replay trading simulator.

Scope so far (steps 1-3): catalogues, data downloads, raw candle reads, the
compute endpoint, and replay sessions with stepping, trading and a view
endpoint that never reveals candles beyond the session cursor.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool

from . import config
from .data.candle_store import CandleStore
from .indicators.registry import INDICATORS, get_indicator, validate_params
from .replay import day_engine as day_rules
from .replay.day_engine import DayEngine
from .replay.day_store import DayStore
from .replay import engine as engine_rules
from .replay.engine import Conflict, Invalid, NotFound, ReplayEngine
from .replay import reasons as checklists
from .replay.payload import build_payload
from .replay.session_store import SessionStore
from .strategies.builtin import STRATEGIES, get_strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Replay Trading", version="0.4.0")
store = CandleStore(config.DB_PATH)
sessions = SessionStore(config.DB_PATH)
engine = ReplayEngine(store, sessions)
day = DayEngine(engine, DayStore(config.DB_PATH))  # day-trading mode on top of the shared sessions and data


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Like FastAPI's default, minus the echoed input (which may be inf/NaN and not JSON-serialisable)."""
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(NotFound)
async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(Invalid)
async def _invalid(_: Request, exc: Invalid) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(Conflict)
async def _conflict(_: Request, exc: Conflict) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=409)


# ----------------------------------------------------------------- job registry
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_prepare_progress: dict[str, dict] = {}
_preparing: set[str] = set()
_preparing_lock = threading.Lock()


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _run_download(job_id: str, symbol: str, interval: str, start_ms: int, end_ms: int | None) -> None:
    _set_job(job_id, status="running")

    def progress(candles: int, requests_made: int) -> None:
        _set_job(job_id, candles=candles, requests=requests_made)

    try:
        added = store.ensure_range(symbol, interval, start_ms, end_ms, progress=progress)
        _set_job(job_id, status="done", candles=added, finished_at=_now_iso())
    except Exception as exc:  # report, never crash the server
        log.exception("Download job %s failed", job_id)
        _set_job(job_id, status="error", error=str(exc), finished_at=_now_iso())


def _start_prepare(session_id: str, background: BackgroundTasks) -> bool:
    """Queue a prepare for the session unless one is already running. Returns True if queued."""
    with _preparing_lock:
        if session_id in _preparing:
            return False
        _preparing.add(session_id)
    background.add_task(_run_prepare, session_id)
    return True


def _run_prepare(session_id: str) -> None:
    def progress(candles: int, requests_made: int) -> None:
        _prepare_progress[session_id] = {"candles": candles, "requests": requests_made}

    try:
        with sessions.tx() as conn:
            row = sessions.get_session(conn, session_id)
        prepare = day.prepare if row is not None and (row.get("mode") or "swing") == "day" else engine.prepare
        prepare(session_id, progress=progress)
    except Exception:  # the session row already carries status=error and the message
        log.exception("Preparing session %s failed", session_id)
    finally:
        _prepare_progress.pop(session_id, None)
        with _preparing_lock:
            _preparing.discard(session_id)


# -------------------------------------------------------------------- helpers
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date_ms(value: str, name: str) -> int:
    """Accept YYYY-MM-DD or full ISO 8601; naive values are treated as UTC."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be an ISO date like 2025-01-31, got {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _check(symbol: str, interval: str) -> str:
    try:
        symbol = config.validate_symbol(symbol)
        config.interval_ms(interval)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return symbol


# -------------------------------------------------------------------- schemas
class DownloadRequest(BaseModel):
    symbol: str = Field(examples=["BTCUSDT"])
    interval: str = Field(examples=["1h"])
    start: str = Field(description="ISO date, inclusive, UTC", examples=["2024-01-01"])
    end: str | None = Field(default=None, description="ISO date, inclusive, UTC; default = now")


class IndicatorRequest(BaseModel):
    key: str = Field(examples=["ema"])
    params: dict[str, Any] = Field(default_factory=dict)


class StrategyRequest(BaseModel):
    key: str = Field(examples=["ema_cross"])
    params: dict[str, Any] = Field(default_factory=dict)


class ComputeRequest(BaseModel):
    symbol: str = Field(examples=["BTCUSDT"])
    interval: str = Field(examples=["1h"])
    start: str | None = Field(default=None, description="Anchor: indicators are computed from this bar on (ISO, UTC)")
    end: str | None = Field(default=None, description="Cursor: last bar included (ISO, UTC); default = latest stored")
    limit: int = Field(default=500, ge=1, le=20_000, description="Return the last N bars of the window")
    indicators: list[IndicatorRequest] = Field(default_factory=list)
    strategy: StrategyRequest | None = None


class SessionCreate(BaseModel):
    symbol: str = Field(examples=["BTCUSDT"])
    start: str = Field(description="Replay start (ISO, UTC)", examples=["2025-01-01"])
    end: str | None = Field(default=None, description="Replay end (ISO, UTC); default = latest closed candle")
    base_interval: str = Field(default="1h", description="Finest timeframe of the session")
    start_balance: float = Field(default=config.DEFAULT_START_BALANCE, gt=0, le=1e12, allow_inf_nan=False)
    fee_rate: float = Field(default=config.DEFAULT_FEE_RATE, ge=0, lt=0.1, allow_inf_nan=False)
    name: str | None = Field(default=None, max_length=80)


class ViewRequest(BaseModel):
    interval: str | None = Field(default=None, description="View timeframe; default = base interval")
    limit: int = Field(default=500, ge=1, le=20_000)
    indicators: list[IndicatorRequest] = Field(default_factory=list)
    strategy: StrategyRequest | None = None
    auto_fib: int | None = Field(default=None, ge=2, le=2000, description="Lookback in candles for an automatic retracement")
    fvg_min_pct: float | None = Field(default=None, ge=0, le=10, allow_inf_nan=False, description="Draw fair value gaps at least this percent of price high")
    daily_ema: bool = Field(default=False, description="Also return the daily EMA 200 mapped onto the bars")
    sr_tol: float | None = Field(default=None, gt=0, le=10, allow_inf_nan=False, description="Swings within this percent form one level (next support/resistance); default 1.5")
    rsi_panes: list[str] = Field(default_factory=list, max_length=engine_rules.RSI_PANES_MAX, description="Timeframes whose RSI(14) is returned as extra panes, one value per bar on screen")


class ContextRequest(BaseModel):
    years: int = Field(default=2, ge=1, le=5, description="Years of Bitcoin daily candles up to the replay day")
    tol_pct: float = Field(default=1.5, gt=0, le=10, allow_inf_nan=False, description="Swings within this percent form one level")
    indicators: list[IndicatorRequest] = Field(default_factory=list)


class StepRequest(BaseModel):
    interval: str | None = Field(default=None, description="Step size; default = base interval")
    n: int = Field(default=1, ge=1, le=100_000)
    view: ViewRequest | None = Field(default=None, description="If given, the response also carries this view")


class BuyRequest(BaseModel):
    amount: float = Field(gt=0, le=1e12, allow_inf_nan=False, description="Quote currency to spend")
    interval: str | None = Field(default=None, description="Timeframe whose RSI(14) is recorded; default = base")
    whole: bool = Field(default=False, description="Round the quantity to a whole number of coins (up, or down if cash is short)")
    coins: int | None = Field(default=None, ge=1, le=10**12, description="Buy exactly this many whole coins; `amount` is then ignored")
    sr_tol: float | None = Field(default=None, gt=0, le=10, allow_inf_nan=False, description="Swings within this percent form one level (next support/resistance); default 1.5")
    reasons: list[str] = Field(default_factory=list, max_length=checklists.MAX_REASONS, description="Ticked keys of the buy checklist (GET /api/reasons)")
    note: str | None = Field(default=None, max_length=checklists.NOTE_MAX_CHARS, description="Free text for the journal row")
    tp_pct: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False, description="Take profit, percent above the entry price")
    sl_pct: float | None = Field(default=None, gt=0, lt=100, allow_inf_nan=False, description="Stop loss, percent below the entry price")


class ArmRequest(BaseModel):
    armed: StrictBool = Field(description="True: sell automatically when a candle reaches the target or the stop; False: off")


class FibCreate(BaseModel):
    interval: str | None = Field(default=None, description="Timeframe the fib was drawn on; default = base")
    t1_ms: int = Field(description="First anchor (100 %) time, ms since epoch")
    p1: float = Field(gt=0, allow_inf_nan=False)
    t2_ms: int = Field(description="Second anchor (0 %) time, ms since epoch")
    p2: float = Field(gt=0, allow_inf_nan=False)


class SnapshotImage(BaseModel):
    image: str = Field(max_length=12_000_000, description="PNG data URL of the chart")
    extra: dict[str, Any] | None = Field(default=None, description="UI-side readings to store with the row")


class SellRequest(BaseModel):
    bucket_id: int
    interval: str | None = Field(default=None, description="Timeframe whose RSI(14) is recorded; default = base")
    sr_tol: float | None = Field(default=None, gt=0, le=10, allow_inf_nan=False, description="Swings within this percent form one level (next support/resistance); default 1.5")
    part: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False, description="Share of what is left in the bucket to sell: 0.25, 0.5 or 1 (everything)")
    whole: bool = Field(default=False, description="Round the part down to whole coins (refused when that leaves none)")
    reasons: list[str] = Field(default_factory=list, max_length=checklists.MAX_REASONS, description="Ticked keys of the sell checklist (GET /api/reasons)")
    note: str | None = Field(default=None, max_length=checklists.NOTE_MAX_CHARS, description="Free text for the journal row")


class DaySessionCreate(BaseModel):
    symbol: str = Field(examples=["BTCUSDT"])
    start: str = Field(description="Replay start date (ISO, UTC); the first trading day is the start date (00:00 to 24:00 UTC)", examples=["2025-01-01"])
    end: str | None = Field(default=None, description="Replay end (ISO, UTC); default = latest closed candle")
    start_balance: float = Field(default=day_rules.DAY_START_BALANCE, gt=0, le=1e12, allow_inf_nan=False, description="Starting capital; the risk per trade is 0.1 % of it")
    name: str | None = Field(default=None, max_length=80)


class ZoneCreate(BaseModel):
    interval: str = Field(description="Chart the zone was drawn on: 1h or 4h", examples=["1h"])
    p1: float = Field(gt=0, allow_inf_nan=False, description="One edge of the zone (price); the order does not matter")
    p2: float = Field(gt=0, allow_inf_nan=False, description="The other edge")


class ZoneUpdate(BaseModel):
    p1: float = Field(gt=0, allow_inf_nan=False)
    p2: float = Field(gt=0, allow_inf_nan=False)


class DayPlanRequest(BaseModel):
    zone_id: int | None = Field(default=None, description="The support zone the entry is based on")
    stop: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="Stop-loss price; default = the middle of the zone")
    take_profit: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="Take-profit price (this or a ratio is required for the buy)")
    ratio: float | None = Field(default=None, gt=0, le=day_rules.RATIO_MAX, allow_inf_nan=False,
                                description="Reward-to-risk ratio; without a take-profit the target is entry + ratio x (entry - stop)")


class DayBuyRequest(DayPlanRequest):
    notes: str | None = Field(default=None, max_length=day_rules.NOTE_MAX_CHARS, description="Free text saved with the trade")
    confidence: int = Field(ge=0, le=10, strict=True, description="How confident you are in this trade, 0 to 10 (set once at the buy)")


class DaySellRequest(BaseModel):
    reason: str = Field(description="Exit reason key (state.day.exit_reasons)")
    exits: Literal["A", "C", "both"] = Field(default="both", description="Which exit to close; the trade closes once A and C are both closed")
    note: str | None = Field(default=None, max_length=day_rules.NOTE_MAX_CHARS, description="Free text; required for the reason 'other'")


class DayReviewRequest(BaseModel):
    stop_tight: bool = Field(strict=True, description="The stop was too tight")
    over_target: bool = Field(strict=True, description="The price later went over the target (out too early)")
    stop_saved: bool = Field(strict=True, description="The stop saved me")
    stop_moved_up: bool = Field(strict=True, description="I moved the stop too far up")


class DayMoveRequest(BaseModel):
    stop: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="New stop-loss price (below the current price)")
    take_profit: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="New take-profit price (above the current price)")


class DayGotoRequest(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="A later trading day (YYYY-MM-DD) to jump to, at 00:00")


class DayNextSignalRequest(BaseModel):
    strategy: str = Field(default="rline_retest", description="Jump strategy key (see /api/day/strategies)")
    params: dict[str, Any] | None = Field(default=None, description="Strategy parameters; defaults where left out")


class DayStepRequest(BaseModel):
    n: int = Field(default=1, ge=1, le=100_000, description="15-minute candles to advance (stops at an exit)")
    view: ViewRequest | None = Field(default=None, description="If given, the response also carries this view")


class DayImage(BaseModel):
    image: str = Field(max_length=12_000_000, description="PNG data URL of the chart at the exit")


class DayNotes(BaseModel):
    notes: str | None = Field(default=None, max_length=day_rules.DAY_NOTES_MAX_CHARS, description="Free text for the trading day")


def _validate_view(req: ViewRequest) -> None:
    """Reject bad indicator/strategy keys or parameters before any state changes."""
    try:
        for i in req.indicators:
            validate_params(get_indicator(i.key).params, i.params)
        if req.strategy:
            validate_params(get_strategy(req.strategy.key).params, req.strategy.params)
    except ValueError as exc:
        raise Invalid(str(exc)) from None


def _view(session_id: str, req: ViewRequest) -> dict:
    return engine.view(
        session_id,
        req.interval,
        req.limit,
        [(i.key, i.params) for i in req.indicators],
        (req.strategy.key, req.strategy.params) if req.strategy else None,
        req.auto_fib,
        req.fvg_min_pct,
        req.daily_ema,
        req.sr_tol,
        rsi_panes=req.rsi_panes,
    )


# ------------------------------------------------------------------- endpoints
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "time": _now_iso()}


@app.get("/api/coins")
def coins() -> list[dict]:
    return config.COINS


@app.get("/api/intervals")
def intervals() -> list[dict]:
    return [{"name": name, "ms": ms} for name, ms in config.INTERVALS_MS.items()]


@app.get("/api/indicators")
def indicators() -> list[dict]:
    return [spec.describe() for spec in INDICATORS.values()]


@app.get("/api/strategies")
def strategies() -> list[dict]:
    return [spec.describe() for spec in STRATEGIES.values()]


@app.get("/api/reasons")
def reasons() -> dict:
    """The buy and sell checklists (keys, labels, version) ticked when a trade is logged."""
    return checklists.describe()


@app.get("/api/stats/reasons")
def reason_stats(symbol: str | None = Query(default=None, max_length=20, description="Only sessions of this coin, e.g. BTCUSDT")) -> dict:
    """Per buy reason the closed trades with and without it, per sell reason the sells with it:
    count and average P&L percent, over every session (or one coin)."""
    return engine.reason_stats(symbol.upper() if symbol else None)


@app.get("/api/data/status")
def data_status() -> list[dict]:
    rows = store.stats()
    for r in rows:
        r["first"] = datetime.fromtimestamp(r["first_ms"] / 1000, tz=timezone.utc).isoformat()
        r["last"] = datetime.fromtimestamp(r["last_ms"] / 1000, tz=timezone.utc).isoformat()
    return rows


@app.post("/api/data/download", status_code=202)
def start_download(req: DownloadRequest, background: BackgroundTasks) -> dict:
    symbol = _check(req.symbol, req.interval)
    start_ms = _parse_date_ms(req.start, "start")
    end_ms = _parse_date_ms(req.end, "end") if req.end else None
    if end_ms is not None and end_ms < start_ms:
        raise HTTPException(422, "end must not be before start")
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id, "symbol": symbol, "interval": req.interval, "status": "queued",
            "candles": 0, "requests": 0, "started_at": _now_iso(),
        }
    background.add_task(_run_download, job_id, symbol, req.interval, start_ms, end_ms)
    with _jobs_lock:
        return dict(_jobs[job_id])


@app.get("/api/data/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
        job = None if job is None else dict(job)  # copy: the download thread keeps mutating the original
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/api/candles")
def candles(
    symbol: str,
    interval: str,
    start: str | None = Query(default=None, description="ISO date, inclusive"),
    end: str | None = Query(default=None, description="ISO date, inclusive"),
    limit: int | None = Query(default=None, ge=1, le=50_000),
) -> dict:
    symbol = _check(symbol, interval)
    start_ms = _parse_date_ms(start, "start") if start else None
    end_ms = _parse_date_ms(end, "end") if end else None
    df = store.get_candles(symbol, interval, start_ms, end_ms, limit)
    return {"symbol": symbol, "interval": interval, "count": int(len(df)), "candles": df.to_dict(orient="records")}


@app.post("/api/compute")
def compute(req: ComputeRequest) -> dict:
    """Candles plus indicator values and strategy signals for an arbitrary window.

    Indicators are computed over every stored bar from `start` (anchor) to
    `end` (cursor); only the last `limit` bars are returned. This endpoint
    has no replay protection; the UI uses the session view endpoint instead.
    """
    symbol = _check(req.symbol, req.interval)
    start_ms = _parse_date_ms(req.start, "start") if req.start else None
    end_ms = _parse_date_ms(req.end, "end") if req.end else None
    if start_ms is not None and end_ms is not None and end_ms < start_ms:
        raise HTTPException(422, "end must not be before start")
    df = store.get_candles(symbol, req.interval, start_ms, end_ms)
    try:
        payload = build_payload(
            df,
            [(i.key, i.params) for i in req.indicators],
            (req.strategy.key, req.strategy.params) if req.strategy else None,
            req.limit,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"symbol": symbol, "interval": req.interval, **payload}


# ------------------------------------------------------------------- sessions
@app.get("/api/sessions")
def list_sessions() -> list[dict]:
    return engine.list_sessions()


@app.post("/api/sessions", status_code=201)
def create_session(req: SessionCreate, background: BackgroundTasks) -> dict:
    s = engine.create_session(
        symbol=req.symbol,
        start_ms=_parse_date_ms(req.start, "start"),
        end_ms=_parse_date_ms(req.end, "end") if req.end else None,
        base_interval=req.base_interval,
        start_balance=req.start_balance,
        fee_rate=req.fee_rate,
        name=req.name,
    )
    _start_prepare(s["id"], background)
    return s


@app.get("/api/sessions/{session_id}")
def session_state(session_id: str) -> dict:
    st = engine.state(session_id)
    st["prepare_progress"] = _prepare_progress.get(session_id)
    return st


@app.post("/api/sessions/{session_id}/prepare")
def prepare_session(session_id: str, background: BackgroundTasks) -> dict:
    """Retry downloading data for a session that ended in an error."""
    st = engine.state(session_id)
    if st["status"] == "ready":
        return st
    if _start_prepare(session_id, background):
        st["status"] = "preparing"
        st["error"] = None
    return st


@app.delete("/api/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> None:
    engine.delete_session(session_id)


@app.post("/api/sessions/{session_id}/step")
def step(session_id: str, req: StepRequest) -> dict:
    if req.view is not None:
        _validate_view(req.view)  # a bad view must not advance the cursor
    st = engine.step(session_id, req.interval, req.n)
    if req.view is not None:
        st["view"] = _view(session_id, req.view)
    return st


@app.post("/api/sessions/{session_id}/reset")
def reset(session_id: str) -> dict:
    return engine.reset(session_id)


@app.post("/api/sessions/{session_id}/buy")
def buy(session_id: str, req: BuyRequest) -> dict:
    bucket = engine.buy(session_id, req.amount, req.interval, req.whole, req.coins, req.sr_tol, req.reasons, req.note, req.tp_pct, req.sl_pct)
    return {"bucket": bucket, "state": engine.state(session_id)}


@app.post("/api/sessions/{session_id}/sell")
def sell(session_id: str, req: SellRequest) -> dict:
    bucket = engine.sell(session_id, req.bucket_id, req.interval, req.sr_tol, req.part, req.whole, req.reasons, req.note)
    return {"bucket": bucket, "state": engine.state(session_id)}


@app.post("/api/sessions/{session_id}/buckets/{bucket_id}/arm")
def arm(session_id: str, bucket_id: int, req: ArmRequest) -> dict:
    """Switch the automatic sell at the bucket's target or stop on or off."""
    bucket = engine.arm(session_id, bucket_id, req.armed)
    return {"bucket": bucket, "state": engine.state(session_id)}


@app.get("/api/sessions/{session_id}/performance")
def performance(session_id: str) -> dict:
    """Numbers, equity curve with drawdown, trades and monthly returns of the session."""
    return engine.performance(session_id)


@app.get("/api/stats/performance")
def performance_all() -> dict:
    """The same over every session together (realized P&L added up trade by trade)."""
    return engine.performance_all()


@app.get("/api/sessions/{session_id}/trades")
def trades(session_id: str) -> dict:
    return engine.trades(session_id)


@app.get("/api/sessions/{session_id}/snapshots")
def list_snapshots(session_id: str) -> list[dict]:
    return engine.snapshots(session_id)


@app.get("/api/sessions/{session_id}/snapshots.csv")
def snapshots_csv(session_id: str) -> PlainTextResponse:
    body = engine.snapshots_csv(session_id)
    return PlainTextResponse(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="journal_{session_id}.csv"'})


@app.post("/api/sessions/{session_id}/snapshots/{bucket_id}/{side}/image")
def attach_snapshot_image(session_id: str, bucket_id: int, side: str, req: SnapshotImage) -> dict:
    return engine.attach_snapshot_image(session_id, bucket_id, side, req.image, req.extra)


@app.get("/api/sessions/{session_id}/snapshots/{snap_id}/image")
def snapshot_image(session_id: str, snap_id: int) -> FileResponse:
    return FileResponse(engine.snapshot_image_path(session_id, snap_id), media_type="image/png")


@app.post("/api/sessions/{session_id}/context")
def btc_context(session_id: str, req: ContextRequest) -> dict:
    """Bitcoin daily candles of the last years known at the cursor, with support/resistance levels."""
    return engine.btc_context(session_id, req.years, req.tol_pct, [(i.key, i.params) for i in req.indicators])


@app.get("/api/sessions/{session_id}/fibs")
def list_fibs(session_id: str) -> list[dict]:
    return engine.fibs(session_id)


@app.post("/api/sessions/{session_id}/fibs", status_code=201)
def add_fib(session_id: str, req: FibCreate) -> dict:
    return engine.add_fib(session_id, req.interval, req.t1_ms, req.p1, req.t2_ms, req.p2)


@app.delete("/api/sessions/{session_id}/fibs/{fib_id}", status_code=204)
def delete_fib(session_id: str, fib_id: int) -> None:
    engine.delete_fib(session_id, fib_id)


@app.post("/api/sessions/{session_id}/view")
def view(session_id: str, req: ViewRequest) -> dict:
    return _view(session_id, req)


# ---------------------------------------------------------------- day trading
@app.post("/api/day/sessions", status_code=201)
def day_create_session(req: DaySessionCreate, background: BackgroundTasks) -> dict:
    """A day-trading session: 15-minute base, fee 0.1 %, risk 1 % of the starting capital."""
    s = day.create_session(
        symbol=req.symbol,
        start_ms=_parse_date_ms(req.start, "start"),
        end_ms=_parse_date_ms(req.end, "end") if req.end else None,
        start_balance=req.start_balance,
        name=req.name,
    )
    _start_prepare(s["id"], background)
    return s


@app.get("/api/day/sessions/{session_id}")
def day_state(session_id: str) -> dict:
    st = day.state(session_id)
    st["prepare_progress"] = _prepare_progress.get(session_id)
    return st


@app.post("/api/day/sessions/{session_id}/prepare")
def day_prepare(session_id: str, background: BackgroundTasks) -> dict:
    """Retry downloading data for a day-trading session that ended in an error."""
    st = day.state(session_id)
    if st["status"] == "ready":
        return st
    if _start_prepare(session_id, background):
        st["status"] = "preparing"
        st["error"] = None
    return st


@app.get("/api/day/sessions/{session_id}/zones")
def day_zones(session_id: str) -> list[dict]:
    return day.zones(session_id)


@app.post("/api/day/sessions/{session_id}/zones", status_code=201)
def day_add_zone(session_id: str, req: ZoneCreate) -> dict:
    return day.add_zone(session_id, req.interval, req.p1, req.p2)


@app.put("/api/day/sessions/{session_id}/zones/{zone_id}")
def day_update_zone(session_id: str, zone_id: int, req: ZoneUpdate) -> dict:
    return day.update_zone(session_id, zone_id, req.p1, req.p2)


@app.delete("/api/day/sessions/{session_id}/zones/{zone_id}", status_code=204)
def day_delete_zone(session_id: str, zone_id: int) -> None:
    day.delete_zone(session_id, zone_id)


@app.post("/api/day/sessions/{session_id}/plan")
def day_plan(session_id: str, req: DayPlanRequest) -> dict:
    """Preview of a trade: entry, suggested stop, size, risk, reward-to-risk, rule readings, errors and warnings."""
    return day.plan(session_id, req.zone_id, req.stop, req.take_profit, req.ratio)


@app.post("/api/day/sessions/{session_id}/buy")
def day_buy(session_id: str, req: DayBuyRequest) -> dict:
    return day.buy(session_id, req.zone_id, req.take_profit, req.stop, req.notes, req.ratio, req.confidence)


@app.post("/api/day/sessions/{session_id}/sell")
def day_sell(session_id: str, req: DaySellRequest) -> dict:
    """Early exit at the close of the current candle."""
    return day.sell(session_id, req.reason, req.note, req.exits)


@app.patch("/api/day/sessions/{session_id}/trade")
def day_move_trade(session_id: str, req: DayMoveRequest) -> dict:
    """Move the stop and / or the take-profit of the open trade; every move is saved with the trade."""
    return day.move_trade(session_id, req.stop, req.take_profit)


@app.post("/api/day/sessions/{session_id}/step")
def day_step(session_id: str, req: DayStepRequest) -> dict:
    if req.view is not None:
        _validate_view(req.view)  # a bad view must not advance the cursor
    st = day.step(session_id, req.n)
    if req.view is not None:
        st["view"] = _view(session_id, req.view)
    return st


class DayStepBackRequest(BaseModel):
    view: ViewRequest | None = Field(default=None, description="If given, the response also carries this view")


@app.post("/api/day/sessions/{session_id}/step-back")
def day_step_back(session_id: str, req: DayStepBackRequest) -> dict:
    """One 15-minute candle back, never before the latest trade action or jump."""
    if req.view is not None:
        _validate_view(req.view)
    st = day.step_back(session_id)
    if req.view is not None:
        st["view"] = _view(session_id, req.view)
    return st


@app.post("/api/day/sessions/{session_id}/end-day")
def day_end_day(session_id: str) -> dict:
    """After an exit: jump to 24:00 of the exit's trading day = 00:00 of the next (the day is left, the review is asked there)."""
    return day.end_day(session_id)


@app.post("/api/day/sessions/{session_id}/keep-trading")
def day_keep_trading(session_id: str) -> dict:
    """After an exit: go on from the exit candle; the review waits for 24:00."""
    return day.keep_trading(session_id)


@app.post("/api/day/sessions/{session_id}/skip")
def day_skip(session_id: str) -> dict:
    """Jump to the next day's 00:00 (no open trade)."""
    return day.skip_day(session_id)


@app.post("/api/day/sessions/{session_id}/goto")
def day_goto(session_id: str, req: DayGotoRequest) -> dict:
    """Jump forward to 00:00 of a later trading day (no open trade, never backwards)."""
    return day.goto_day(session_id, req.date)


@app.post("/api/day/sessions/{session_id}/next-ema-day")
def day_next_ema_day(session_id: str) -> dict:
    """Jump forward to 00:00 of the next trading day whose daily close is above the EMA 200 (no open trade)."""
    return day.next_ema_day(session_id)


@app.get("/api/day/strategies")
def day_strategies() -> list[dict]:
    """The strategies the "Next signal" button can jump with, with their parameters."""
    return day.signal_strategies()


@app.post("/api/day/sessions/{session_id}/next-signal")
def day_next_signal(session_id: str, req: DayNextSignalRequest) -> dict:
    """Jump forward to the strategy's next entry signal (no open trade); its zone is added to the session's zones."""
    return day.next_signal(session_id, req.strategy, req.params)


@app.post("/api/day/sessions/{session_id}/continue")
def day_continue(session_id: str) -> dict:
    """After an exit: jump to the next 00:00 (the same jump as end-day since the day starts at 00:00)."""
    return day.resume(session_id)


@app.post("/api/day/sessions/{session_id}/reset")
def day_reset(session_id: str) -> dict:
    return day.reset(session_id)


@app.get("/api/day/sessions/{session_id}/trades")
def day_trades(session_id: str) -> dict:
    return day.trades(session_id)


@app.post("/api/day/sessions/{session_id}/trades/{trade_id}/image")
def day_trade_image_upload(session_id: str, trade_id: int, req: DayImage) -> dict:
    return day.attach_image(session_id, trade_id, req.image)


@app.get("/api/day/sessions/{session_id}/trades/{trade_id}/image")
def day_trade_image(session_id: str, trade_id: int) -> FileResponse:
    return FileResponse(day.image_path(session_id, trade_id), media_type="image/png")


@app.post("/api/day/sessions/{session_id}/trades/{trade_id}/review")
def day_trade_review(session_id: str, trade_id: int, req: DayReviewRequest) -> dict:
    """Answer (or change) the four review marks of a closed trade; the replay waits for them after every exit."""
    return day.review(session_id, trade_id, req.model_dump())


@app.get("/api/day/sessions/{session_id}/auto-trades")
def day_auto_trades(session_id: str) -> dict:
    """The automatic-exit twins: every manual trade's copy with the original stop and target, closed
    only when one of them is hit (overnight and across days), compared to the manual trade."""
    return day.auto_trades(session_id)


@app.get("/api/day/sessions/{session_id}/days")
def day_days(session_id: str) -> dict:
    """The daily journal: one row per trading day (trend check, how it was left, its trade, picture, notes)."""
    return day.days(session_id)


@app.get("/api/day/sessions/{session_id}/conditions")
def day_conditions(session_id: str, scope: str = Query(default="session", pattern="^(session|all)$")) -> dict:
    """Closed trades with and without each entry condition (count, win rate, average R, P&L)."""
    return day.conditions(session_id, scope)


@app.get("/api/day/sessions/{session_id}/conditions.csv")
def day_conditions_csv(session_id: str, scope: str = Query(default="session", pattern="^(session|all)$")) -> PlainTextResponse:
    body = day.conditions_csv(session_id, scope)
    return PlainTextResponse(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="conditions_{scope}_{session_id}.csv"'})


@app.get("/api/day/sessions/{session_id}/days.csv")
def day_days_csv(session_id: str) -> PlainTextResponse:
    body = day.days_csv(session_id)
    return PlainTextResponse(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="days_{session_id}.csv"'})


@app.put("/api/day/sessions/{session_id}/days/{day_date}/notes")
def day_notes(session_id: str, day_date: str, req: DayNotes) -> dict:
    return day.set_day_notes(session_id, day_date, req.notes)


@app.post("/api/day/sessions/{session_id}/days/{day_date}/image")
def day_image_upload(session_id: str, day_date: str, req: DayImage) -> dict:
    """The end-of-day picture of a trading day (taken by the frontend before the replay leaves the day)."""
    return day.attach_day_image(session_id, day_date, req.image)


@app.get("/api/day/sessions/{session_id}/days/{day_date}/image")
def day_image(session_id: str, day_date: str) -> FileResponse:
    return FileResponse(day.day_image_path(session_id, day_date), media_type="image/png")


# ------------------------------------------------------------------- frontend
if (config.FRONTEND_DIR / "index.html").exists():
    app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(config.FRONTEND_DIR / "index.html")

    @app.middleware("http")
    async def revalidate_frontend(request, call_next):
        """Browsers otherwise keep module scripts from heuristic caching after an update; ETags keep this cheap."""
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response
