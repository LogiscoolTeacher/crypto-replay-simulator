"""Browser tests of the strategy sessions with Playwright (skipped when Playwright is not installed).

They run the real FastAPI app in a thread with the 15-minute fake exchange and one scripted signal.
Automatic: Start is pressed in headless Chromium, the strategy trades, the picture of the trade is
taken, the session ends, and the buttons that trade by hand stay off the whole time.
Manual: Start goes to the entry point and shows the proposal; Take buys it, the trader steps to the
exit and the run finishes by itself; Skip passes on it."""
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


def open_session(page, server_url, monkeypatch, run_mode: str, pictures: bool) -> str:
    """A strategy session with one scripted signal (trade limit 1), opened in the page."""
    fake = [{"bar": 0, "bar_ms": D3, "zone_top": quarter_price(D3) + 0.5, "zone_bottom": quarter_price(D3) - 2.0,
             "entry_level": quarter_price(D3) + 0.5, "near": False, "rules": ["A"], "touches": 2, "touch_ms": [],
             "breakout_ms": D3 - 5 * H, "psych": None}]
    monkeypatch.setattr(RLineRetest, "find_signals", lambda self, candles: fake)
    page.goto(server_url + "/")
    page.wait_for_function("window.__app && window.__app.state.intervals.length > 0")
    r = api(page, "POST", "/api/strategy/sessions", {"symbol": SYMBOL, "start": iso(START), "end": iso(END), "run_mode": run_mode,
                                                     "strategy": "rline_retest_confirmed", "max_trades": 1, "pictures": pictures})
    assert r["status"] == 201, r
    sid = r["json"]["id"]
    for _ in range(100):
        if api(page, "GET", f"/api/day/sessions/{sid}")["json"]["status"] == "ready":
            break
        time.sleep(0.1)
    page.evaluate(f"localStorage.setItem('replay.lastSession', '{sid}')")
    page.goto(server_url + "/")                     # the page opens the session it was told was the last one
    page.wait_for_function(f"window.__app && window.__app.state.session && window.__app.state.session.id === '{sid}' && !!window.__app.state.session.day")
    page.wait_for_selector("#strat-card:not(.hidden)")
    return sid


def test_start_runs_the_session_and_the_manual_buttons_stay_off(page, server_url, monkeypatch):
    sid = open_session(page, server_url, monkeypatch, "automatic", pictures=True)
    try:
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


STEP_UNTIL_CLOSED = """async () => {
    for (let i = 0; i < 600 && window.__app.state.session.day.trade; i++) await window.__app.step(1);
}"""


def test_manual_take_the_proposal_hold_the_trade_and_the_run_finishes_by_itself(page, server_url, monkeypatch):
    sid = open_session(page, server_url, monkeypatch, "manual", pictures=True)
    try:
        assert page.inner_text("#btn-strat-run") == "Start" and page.is_hidden("#strat-proposal")
        for button in ("#btn-step", "#btn-play", "#btn-step-back", "#btn-day-new", "#btn-day-skip", "#btn-day-signal"):
            assert page.is_disabled(button), button
        assert "press Start" in page.inner_text("#day-gate")

        page.click("#btn-strat-run")                    # the replay goes to the entry point and stops with the proposal
        page.wait_for_selector("#strat-proposal:not(.hidden)", timeout=60_000)
        page.wait_for_function("document.getElementById('btn-strat-run').textContent === 'Take or skip'")
        st = api(page, "GET", f"/api/day/sessions/{sid}")["json"]
        p = st["day"]["session"]["run"]["proposal"]
        assert st["cursor_ms"] == p["cursor_ms"] > D3 and st["day"]["trade"] is None
        assert page.inner_text("#strat-status") == "proposal" and "Proposed trade" in page.inner_text("#sp-title")
        for button in ("#btn-sp-take", "#btn-step", "#btn-play", "#btn-strat-run", "#btn-day-new"):
            assert page.is_disabled(button), button     # Take waits for the confidence; the replay waits for the answer
        page.click("#sp-conf button[data-conf='7']")
        page.fill("#sp-notes", "From the browser test.")
        page.click("#btn-sp-take")
        page.wait_for_selector("#day-trade:not(.hidden)")
        assert page.is_hidden("#strat-proposal") and page.inner_text("#btn-strat-run") == "In a trade"
        assert not page.is_disabled("#btn-step") and not page.is_disabled("#btn-play") and not page.is_disabled("#btn-day-sell")
        assert page.is_disabled("#btn-step-back") and page.is_disabled("#btn-day-new") and page.is_hidden("#dt-ac")

        # the trader steps until the trade has closed; then the run goes on by itself: limit reached, picture, done
        page.evaluate(STEP_UNTIL_CLOSED)
        page.wait_for_function("document.getElementById('btn-strat-run').textContent === 'Finished'", timeout=60_000)
        b = api(page, "GET", f"/api/day/sessions/{sid}")["json"]["day"]["session"]
        assert b["run"]["done"] and b["ended"] and b["trades_taken"] == 1 and b["end_flag"] in page.inner_text("#strat-end")
        (tr,) = api(page, "GET", f"/api/day/sessions/{sid}/trades")["json"]["trades"]
        assert (tr["status"], tr["run_mode"], tr["confidence"], tr["entry_price"]) == ("closed", "manual", 7, p["entry"])
        assert tr["has_image"] is True and tr["notes"].startswith("From the browser test. | Manual (")
        assert not any(d["has_image"] for d in api(page, "GET", f"/api/day/sessions/{sid}/days")["json"]["days"])   # trade pictures only
        assert page.is_hidden("#day-exit-choice") and page.is_disabled("#btn-step")
        assert not page.errors, page.errors
    finally:
        api(page, "DELETE", f"/api/sessions/{sid}")


def test_manual_skip_goes_on_to_the_next_entry_point_by_itself(page, server_url, monkeypatch):
    sid = open_session(page, server_url, monkeypatch, "manual", pictures=False)
    try:
        page.click("#btn-strat-run")
        page.wait_for_selector("#strat-proposal:not(.hidden)", timeout=60_000)
        page.wait_for_function("!document.getElementById('btn-sp-skip').disabled")
        page.click("#btn-sp-skip")                      # no other signal: the run goes to the replay's last candle and ends
        page.wait_for_function("document.getElementById('btn-strat-run').textContent === 'Finished'", timeout=60_000)
        st = api(page, "GET", f"/api/day/sessions/{sid}")["json"]
        b = st["day"]["session"]
        assert st["finished"] and (b["end_flag"], b["trades_taken"]) == ("ENDED_BY_TIMEOUT", 0) and page.is_hidden("#strat-proposal")
        (row,) = api(page, "GET", f"/api/strategy/sessions/{sid}/signals")["json"]
        assert row["outcome"] == "skipped"
        assert not page.errors, page.errors
    finally:
        api(page, "DELETE", f"/api/sessions/{sid}")


def test_report_tab_follows_the_open_session_and_exports_csv(page, server_url, monkeypatch):
    sid = open_session(page, server_url, monkeypatch, "automatic", pictures=False)
    try:
        for _ in range(50):                              # the strategy trades the one signal; the end of the replay closes it
            if api(page, "POST", f"/api/strategy/sessions/{sid}/run", {})["json"]["run"]["phase"] == "done":
                break
        page.reload()
        page.wait_for_function(f"window.__app && window.__app.state.session && window.__app.state.session.id === '{sid}' && !!window.__app.state.session.day")
        page.click("#tab-report")
        page.wait_for_function("document.getElementById('rp-count').textContent.startsWith('1 trade in 1 session')")
        assert page.inner_text("#rp-sessions-btn") == "Sessions: open session"
        assert "ENDED_BY_TIMEOUT" in page.inner_text("#rp-session-list") and "Automatic" in page.inner_text("#rp-session-list")
        assert page.locator("#rp-trade-list tr").count() == 1 and page.locator("#rp-tiles .tile").count() == 9
        page.click("#rp-end .chip[data-v='non_timeout']")   # it ended by the time limit (the end of the replay)
        page.wait_for_function("document.getElementById('rp-count').textContent.startsWith('0 trades in 0 sessions')")
        assert page.is_visible("#rp-sessions-empty")
        page.click("#rp-end .chip[data-v='all']")
        page.wait_for_function("document.getElementById('rp-count').textContent.startsWith('1 trade')")
        href = page.get_attribute("#btn-rp-csv", "href")
        assert href == f"/api/report.csv?sessions={sid}"
        csv_text = page.evaluate("async (u) => (await fetch(u)).text()", href)
        assert csv_text.splitlines()[0].startswith("trade_id,session_id,session_name") and len(csv_text.strip().splitlines()) == 2
        assert not page.errors, page.errors
    finally:
        api(page, "DELETE", f"/api/sessions/{sid}")
