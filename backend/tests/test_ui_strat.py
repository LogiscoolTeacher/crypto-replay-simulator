"""Browser test of an Automatic strategy session with Playwright (skipped when Playwright is not installed).

Runs the real FastAPI app in a thread with the 15-minute fake exchange and one scripted signal, then
presses Start in headless Chromium: the strategy trades, the picture of the trade is taken, the
session ends, and the buttons that trade by hand stay off the whole time."""
from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone

import pytest

pw = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from app import main  # noqa: E402
from app.strategies.plugins._rline_retest import RLineRetest  # noqa: E402
from tests.fakes import DAY, H, LISTING, FineFakeClient, quarter_price  # noqa: E402

START = LISTING + 300 * DAY          # 2018-05-10 00:00 UTC
END = START + 6 * DAY
NOW = START + 10 * DAY + 1
SYMBOL = "XRPUSDT"                   # a coin no other test module downloads
D3 = START + 3 * DAY + 6 * H         # the fake's wave turns up 8 candles later: a bullish engulfing confirms


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture(scope="module")
def server_url():
    main.store.client = FineFakeClient(NOW)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="module")
def page(server_url):
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 860})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.errors = errors  # type: ignore[attr-defined]
        yield page
        browser.close()


def api(page, method: str, path: str, body: dict | None = None) -> dict:
    """A request from inside the page (same origin as the app)."""
    return page.evaluate(
        """async ([method, path, body]) => {
             const r = await fetch(path, { method, headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
             return { status: r.status, json: await r.json().catch(() => null) };
           }""",
        [method, path, body],
    )


def test_start_runs_the_session_and_the_manual_buttons_stay_off(page, server_url, monkeypatch):
    fake = [{"bar": 0, "bar_ms": D3, "zone_top": quarter_price(D3) + 0.5, "zone_bottom": quarter_price(D3) - 2.0,
             "entry_level": quarter_price(D3) + 0.5, "near": False, "rules": ["A"], "touches": 2, "touch_ms": [],
             "breakout_ms": D3 - 5 * H, "psych": None}]
    monkeypatch.setattr(RLineRetest, "find_signals", lambda self, candles: fake)
    page.goto(server_url + "/")
    page.wait_for_function("window.__app && window.__app.state.intervals.length > 0")
    r = api(page, "POST", "/api/strategy/sessions", {"symbol": SYMBOL, "start": iso(START), "end": iso(END), "run_mode": "automatic",
                                                     "strategy": "rline_retest_confirmed", "max_trades": 1, "pictures": True})
    assert r["status"] == 201, r
    sid = r["json"]["id"]
    try:
        for _ in range(100):
            if api(page, "GET", f"/api/day/sessions/{sid}")["json"]["status"] == "ready":
                break
            time.sleep(0.1)
        page.evaluate(f"localStorage.setItem('replay.lastSession', '{sid}')")
        page.goto(server_url + "/")                     # the page opens the session it was told was the last one
        page.wait_for_function(f"window.__app && window.__app.state.session && window.__app.state.session.id === '{sid}' && !!window.__app.state.session.day")
        page.wait_for_selector("#strat-card:not(.hidden)")
        assert page.inner_text("#btn-strat-run") == "Start" and page.inner_text("#strat-status") == "not started"
        for button in ("#btn-step", "#btn-play", "#btn-day-new", "#btn-day-skip", "#btn-day-signal"):
            assert page.is_disabled(button), button
        assert "Automatic session" in page.inner_text("#day-gate")

        page.click("#btn-strat-run")
        page.wait_for_function("document.getElementById('btn-strat-run').textContent === 'Finished'", timeout=60_000)
        st = api(page, "GET", f"/api/day/sessions/{sid}")["json"]
        b = st["day"]["session"]
        assert b["run"]["done"] and b["ended"] and b["trades_taken"] == 1
        assert page.inner_text("#strat-status") == "ended" and b["end_flag"] in page.inner_text("#strat-end")
        (tr,) = api(page, "GET", f"/api/day/sessions/{sid}/trades")["json"]["trades"]
        assert tr["status"] == "closed" and tr["has_image"] is True and tr["run_mode"] == "automatic"
        assert not any(d["has_image"] for d in api(page, "GET", f"/api/day/sessions/{sid}/days")["json"]["days"])   # trade pictures only
        for button in ("#btn-step", "#btn-play", "#btn-day-new", "#btn-strat-run", "#btn-strat-stop"):
            assert page.is_disabled(button), button
        assert not page.errors, page.errors
    finally:
        api(page, "DELETE", f"/api/sessions/{sid}")
