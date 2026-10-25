"""Browser test of day-trading mode with Playwright (skipped when Playwright is not installed).

Runs the real FastAPI app in a thread with the 15-minute fake exchange, sets the session up
through the API (buy, exit, end the day) and drives the review in headless Chromium.
"""
from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone

import pytest

pw = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from app import main  # noqa: E402
from tests.fakes import DAY, LISTING, Q, FineFakeClient, quarter_candle  # noqa: E402

START = LISTING + 300 * DAY          # 2018-05-10 00:00 UTC
END = START + 6 * DAY
NOW = START + 10 * DAY + 1
FIRST_CURSOR = START                 # the first 15m candle of the replay (00:00-00:15 UTC)
SYMBOL = "ADAUSDT"   # a coin no other test module downloads
PRICE = quarter_candle(FIRST_CURSOR)[3]
# the next candle hits the stop of the test trade
OVERRIDES = {FIRST_CURSOR + Q: (PRICE, PRICE + 1.0, PRICE - 3.0, PRICE - 1.0)}


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture(scope="module")
def server_url():
    main.store.client = FineFakeClient(NOW, OVERRIDES)
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


def day_session_with_due_review(page) -> str:
    """A day session whose trade #1 closed at its stop and whose review is due (the day was ended)."""
    r = api(page, "POST", "/api/day/sessions", {"symbol": SYMBOL, "start": iso(START), "end": iso(END), "start_balance": 100_000})
    assert r["status"] == 201, r
    sid = r["json"]["id"]
    for _ in range(100):
        if api(page, "GET", f"/api/day/sessions/{sid}")["json"]["status"] == "ready":
            break
        time.sleep(0.1)
    base = f"/api/day/sessions/{sid}"
    z = api(page, "POST", f"{base}/zones", {"interval": "1h", "p1": PRICE + 0.5, "p2": PRICE - 0.5})["json"]
    assert api(page, "POST", f"{base}/buy", {"zone_id": z["id"], "stop": PRICE - 2.0, "take_profit": PRICE + 4.0, "confidence": 5})["status"] == 200
    assert api(page, "POST", f"{base}/step", {"n": 1})["json"]["events"][0]["kind"] == "exit"
    st = api(page, "POST", f"{base}/end-day")["json"]
    assert st["day"]["review_pending"] is not None
    return sid


def test_review_survives_a_refused_step(page, server_url):
    """A step while the review is due must not lose the review form: it used to resync from the swing
    endpoint (no day block), which hid the form and its Save button."""
    page.goto(server_url + "/")
    page.wait_for_function("window.__app && window.__app.state.intervals.length > 0")
    sid = day_session_with_due_review(page)
    page.evaluate(f"window.__app.loadSession('{sid}')")
    page.wait_for_selector("#day-review:not(.hidden)")
    assert page.is_disabled("#btn-step") and page.is_disabled("#btn-play")   # Step and Play wait for the answers
    page.keyboard.press("ArrowRight")                                         # the key is refused in the browser
    page.evaluate("window.__app.step(1)")                                     # and so is a direct call
    page.wait_for_timeout(300)
    assert page.is_visible("#day-review") and page.is_enabled("#btn-dr-save")
    page.click("#btn-dr-save")
    page.wait_for_selector("#day-review.hidden", state="attached")
    st = api(page, "GET", f"/api/day/sessions/{sid}")["json"]
    assert st["day"]["review_pending"] is None
    assert page.is_enabled("#btn-step")
    assert not page.errors, page.errors


def test_indicator_pane_can_be_minimized(page, server_url):
    """The -/+ button of an indicator pane (here the day-mode RSI pane) shrinks it to a thin strip with
    its lines hidden, is remembered in the browser over a reload, and brings the pane back."""
    page.goto(server_url + "/")
    page.wait_for_function("window.__app && window.__app.state.intervals.length > 0")
    sid = page.evaluate("window.__app.state.sessions.find(s => s.mode === 'day').id")
    page.evaluate(f"window.__app.loadSession('{sid}')")
    page.wait_for_selector(".pane-fold[data-key='rsi']")
    page.click(".pane-fold[data-key='rsi']")
    assert page.inner_text(".pane-fold[data-key='rsi']").startswith("+ RSI 14")
    assert page.evaluate("window.__app.chart.chart.panes()[1].getHeight()") < 40
    assert page.evaluate("window.__app.chart.rsiLines.every(r => r.series.options().visible === false)")
    assert page.evaluate("JSON.parse(localStorage.getItem('replay.ui.minPanes'))") == ["rsi"]
    page.reload()
    page.wait_for_function("window.__app && window.__app.state.intervals.length > 0")
    page.evaluate(f"window.__app.loadSession('{sid}')")
    page.wait_for_selector(".pane-fold.min[data-key='rsi']")
    page.click(".pane-fold[data-key='rsi']")
    assert page.inner_text(".pane-fold[data-key='rsi']") == "−"
    assert page.evaluate("window.__app.chart.chart.panes()[1].getHeight()") > 100
    assert page.evaluate("JSON.parse(localStorage.getItem('replay.ui.minPanes'))") == []
    assert not page.errors, page.errors
