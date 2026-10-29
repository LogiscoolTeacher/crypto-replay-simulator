"""Browser smoke test of the UI with Playwright (skipped when Playwright is not installed).

Runs the real FastAPI app in a thread with the fake exchange wired in, drives the
page in headless Chromium, and fails on any JavaScript error.
"""
from __future__ import annotations

import math
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402

from app import main  # noqa: E402
from tests.fakes import H, LISTING, FakeClient  # noqa: E402

NOW = LISTING + 400 * H + 1


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture(scope="module")
def server_url():
    main.store.client = FakeClient(NOW)
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
        # Browsers log every failed HTTP response; a 422 the test provokes on purpose is not a bug.
        page.on(
            "console",
            lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "status of 422" not in m.text else None,
        )
        page.errors = errors  # type: ignore[attr-defined]
        yield page
        browser.close()


def confirm_reasons(page, ticks=(), note=None, side=None, title=None):
    """The reason checklist that opens on Buy and on every sell button: tick, note, Confirm."""
    page.wait_for_selector("#dlg-reasons[open]")
    if side:
        assert side in page.get_attribute("#dlg-reasons", "class")
        assert page.locator("#reasons-list input").count() == 11
    if title:
        assert page.inner_text("#reasons-title").startswith(title), page.inner_text("#reasons-title")
    for k in ticks:
        page.check(f"#reasons-list input[value='{k}']")
    if note is not None:
        page.fill("#reasons-note", note)
    page.click("#btn-confirm-reasons")
    page.wait_for_selector("#dlg-reasons:not([open])", state="attached")


def test_ui_end_to_end(page, server_url, tmp_path: Path):
    page.goto(server_url + "/")
    page.wait_for_function("window.__app && window.__app.state.intervals.length > 0")
    # With no sessions the dialog opens by itself; otherwise (other tests left sessions behind) open it.
    page.evaluate("if (!document.getElementById('dlg-session').open) document.getElementById('btn-new-session').click()")
    page.wait_for_selector("#dlg-session[open]")

    page.select_option("#f-symbol", "BTCUSDT")
    page.select_option("#f-base", "1h")
    page.fill("#f-start", _day(LISTING + 96 * H))
    page.fill("#f-end", _day(LISTING + 336 * H))
    page.fill("#f-name", "UI smoke")
    page.click("#btn-create-session")

    page.wait_for_function(
        "window.__app.state.session && window.__app.state.session.name === 'UI smoke' && window.__app.state.session.status === 'ready'",
        timeout=15000,
    )
    page.wait_for_selector("#chart-overlay.hidden", state="attached")
    page.wait_for_function("document.querySelectorAll('#chart canvas').length > 0")
    page.wait_for_function("window.__app.chart.payload && window.__app.chart.payload.count > 0")
    assert "1h" in page.inner_text("#legend")
    # The chart keeps its row even while the start notice is shown or hidden (a hidden grid item drops out).
    assert page.locator("#chart").bounding_box()["height"] > 300
    page.evaluate("document.getElementById('session-note').classList.add('hidden')")
    assert page.locator("#chart").bounding_box()["height"] > 300
    page.evaluate("document.getElementById('session-note').classList.remove('hidden')")
    assert page.inner_text("#cursor-time").startswith(_day(LISTING + 96 * H))
    assert page.inner_text("#acc-cash") == "10,000.00"
    assert page.inner_text("#indicator-count") == "2"  # default EMA 20 / 50
    # The session starts 4 days after the fake listing: the warm-up notice above the chart says so.
    assert page.is_visible("#session-note")
    assert page.inner_text("#session-note").startswith("BTCUSDT has only 4 daily candles before the start")

    # Step one candle.
    before = page.inner_text("#cursor-time")
    page.click("#btn-step")
    page.wait_for_function("arg => document.getElementById('cursor-time').textContent !== arg", arg=before)
    assert page.inner_text("#cursor-time").endswith("01:00 UTC")

    # Timeframes finer than the base are disabled; switch to 4h.
    assert page.is_disabled("button.tf[data-interval='15m']")
    page.click("button.tf[data-interval='4h']")
    page.wait_for_function("document.getElementById('legend').textContent.includes('· 4h ·')")
    assert page.get_attribute("button.tf[data-interval='4h']", "aria-checked") == "true"
    assert "forming" in page.inner_text("#legend")  # 2 of 4 hours seen: partial candle flagged

    # Add an RSI indicator: a second pane appears.
    page.click("#btn-indicators")
    page.select_option("#indicator-key", "rsi")
    page.click("#btn-add-indicator")
    page.wait_for_function("window.__app.chart.chart.panes().length === 2")
    assert page.inner_text("#indicator-count") == "3"
    page.wait_for_function("document.getElementById('legend').textContent.includes('RSI (')")
    page.click("#btn-close-indicators")

    # Play for a moment, then pause.
    before = page.inner_text("#cursor-time")
    page.click("#btn-play")
    page.wait_for_function("document.getElementById('status-badge').textContent === 'playing'")
    page.wait_for_function("arg => document.getElementById('cursor-time').textContent !== arg", arg=before)
    page.click("#btn-play")
    page.wait_for_function("document.getElementById('status-badge').textContent === 'paused'")

    # Keyboard: right arrow steps.
    before = page.inner_text("#cursor-time")
    page.keyboard.press("ArrowRight")
    page.wait_for_function("arg => document.getElementById('cursor-time').textContent !== arg", arg=before)
    # Keyboard zoom: up narrows, down widens, Space restores the default width; the right edge stays.
    # The chart applies a range on the next frame, so wait for the applied width after each key.
    def wait_bars(expected):
        page.wait_for_function("e => Math.abs(window.__app.chart.visibleBars() - e) < 1e-6 * e", arg=expected)
    assert page.evaluate("document.activeElement === document.body")  # mouse-clicked buttons drop focus
    page.keyboard.press("Space")
    page.wait_for_function("window.__app.chart.targetRange === null")
    w0 = page.evaluate("window.__app.chart.visibleBars()")
    edge = page.evaluate("window.__app.chart.chart.timeScale().getVisibleLogicalRange().to")
    page.keyboard.press("ArrowUp")
    wait_bars(w0 / 1.25)
    assert page.evaluate("window.__app.chart.chart.timeScale().getVisibleLogicalRange().to") == pytest.approx(edge)
    page.keyboard.press("ArrowDown"); page.keyboard.press("ArrowDown")  # back-to-back presses must both count
    wait_bars(w0 * 1.25)
    page.keyboard.press("Space")
    wait_bars(w0)
    for _ in range(40): page.keyboard.press("ArrowUp")
    wait_bars(10)
    page.keyboard.press("Space")
    wait_bars(w0)
    # P toggles playback.
    page.keyboard.press("p")
    page.wait_for_function("window.__app.state.playing === true")
    page.keyboard.press("P")
    page.wait_for_function("window.__app.state.playing === false")
    # The footer has no zoom buttons any more: the keys above are the zoom.
    assert page.locator("#btn-zoom-in").count() == 0

    # ---------------------------------------------------------------- trading
    page.click("button.tf[data-interval='1h']")
    page.wait_for_function("document.getElementById('legend').textContent.includes('· 1h ·')")

    # Strategy picker: lights follow the last closed candle; its indicators join the chart.
    page.select_option("#strategy-key", "ema_cross")
    page.wait_for_function("window.__app.chart.payload && window.__app.chart.payload.strategy && window.__app.chart.payload.strategy.key === 'ema_cross'")
    assert "EMA Crossover" in page.inner_text("#strategy-note")
    lamp_on = page.evaluate("document.getElementById('lamp-enter').classList.contains('on') || document.getElementById('lamp-exit').classList.contains('on')")
    state_last = page.evaluate("(() => { const p = window.__app.chart.payload; return p.strategy.state[p.count - 1]; })()")
    assert lamp_on == (state_last != 0)
    assert page.evaluate("window.__app.chart.indicators.length") == 3  # strategy EMAs duplicate the defaults: not drawn twice
    page.evaluate("window.__app.setStrategy('macd_cross', null)")
    page.wait_for_function("window.__app.chart.indicators.length === 4 && window.__app.chart.chart.panes().length === 3")
    page.evaluate("window.__app.setStrategy('ema_cross', null)")
    page.wait_for_function("window.__app.chart.indicators.length === 3 && window.__app.chart.chart.panes().length === 2")

    # Readings card: RSI of the timeframe on screen, coloured at the 30 line; daily EMA 200.
    assert page.is_hidden("#gauge-buy") and page.is_hidden("#gauge-sell")  # no trade yet
    assert page.inner_text("#gauge-rsi-label") == "RSI 14 (1h)"
    g = page.evaluate("window.__app.chart.payload.gauges")
    txt = page.inner_text("#gauge-rsi")
    if g["rsi"]["value"] is None:
        assert txt == "–"
    else:
        assert float(txt) == pytest.approx(g["rsi"]["value"], abs=0.051)
        assert page.evaluate("document.getElementById('gauge-rsi').classList.contains('up')") == (g["rsi"]["value"] <= 30)
        assert page.evaluate("document.getElementById('gauge-rsi').classList.contains('down')") == (g["rsi"]["value"] > 30)
    assert (page.inner_text("#gauge-ema") == "–") == (g["ema_long"]["value"] is None)
    # Highs & lows card: three windows of daily wicks; above-low on the left, below-high on the right,
    # the marker between them; the tooltip names the dates.
    ex = g["extremes"]
    assert ex["complete"] and ex["all"]["bars"] >= ex["1y"]["bars"] >= ex["6m"]["bars"] >= ex["3m"]["bars"] > 0
    assert page.locator("#extremes .extreme").count() == 4
    for key in ("all", "1y", "6m", "3m"):
        w = ex[key]
        row = page.locator(f"#extremes .extreme[data-key='{key}']")
        assert row.locator(".ex-low").inner_text() == f"{w['vs_low_pct']:+.2f}%"
        assert row.locator(".ex-high").inner_text() == f"{w['vs_high_pct']:+.2f}%"
        left = row.locator(".ex-marker").evaluate("e => parseFloat(e.style.left)")
        assert left == pytest.approx(w["position"] * 100, abs=0.01) and 0 <= left <= 100
        assert left == row.locator(".ex-fill").evaluate("e => parseFloat(e.style.width)")
        assert w["high_iso"][:10] in row.locator(".ex-bar").get_attribute("title")
    assert "since the listing" in page.locator("#extremes .extreme[data-key='all'] .ex-label").get_attribute("title")
    # Next resistance / support rows: distance, touches and last touch, or a dash with the reason.
    lv = g["levels"]
    assert lv["tol_pct"] == 1.5 and lv["days"] == 365
    for key, el_id in (("resistance", "gauge-res"), ("support", "gauge-sup")):
        l = lv[key]
        txt = page.inner_text(f"#{el_id}")
        if l is None:
            assert txt == "–" and "touches" in page.get_attribute(f"#{el_id}", "title")
        else:
            days = (page.evaluate("window.__app.state.session.cursor_ms") - l["last_ms"]) // 86400000
            assert txt == ("inside" if l["inside"] else f"{l['pct']:+.2f}%") + f" · {l['touches']}× · " + ("today" if days <= 0 else f"{days}d ago")
            assert l["last_iso"][:10] in page.get_attribute(f"#{el_id}", "title") and "zone" in page.get_attribute(f"#{el_id}", "title")
            assert all(d in page.get_attribute(f"#{el_id}", "title") for d in l["touch_dates"])
    assert page.evaluate("document.querySelector('#extremes [data-key=all] .ex-low').classList.contains('up')") == (ex["all"]["vs_low_pct"] > 0)
    page.click("button.tf[data-interval='4h']")
    page.wait_for_function("document.getElementById('gauge-rsi-label').textContent === 'RSI 14 (4h)'")
    page.click("button.tf[data-interval='1h']")
    page.wait_for_function("document.getElementById('gauge-rsi-label').textContent === 'RSI 14 (1h)'")

    # Fibonacci: two snapped clicks draw one, it is selected, listed and deletable; auto fib rides on the view.
    # (Clicks stay in the upper part: the RSI pane below the price pane takes the lower half here.)
    box = page.locator("#chart").bounding_box()
    page.keyboard.press("f")
    page.wait_for_selector("#draw-hint:not(.hidden)")
    page.mouse.click(box["x"] + box["width"] * 0.40, box["y"] + box["height"] * 0.18)
    page.wait_for_function("window.__app.state.fibDraft !== null")
    page.mouse.click(box["x"] + box["width"] * 0.62, box["y"] + box["height"] * 0.36)
    try:
        page.wait_for_function("window.__app.state.fibs.length === 1", timeout=5000)
    except Exception as exc:  # pragma: no cover - diagnostics for a flaky canvas click
        info = page.evaluate("JSON.stringify({drawing: window.__app.state.drawing, draft: window.__app.state.fibDraft, toast: document.getElementById('toast').textContent, hint: document.getElementById('draw-hint').textContent, panes: window.__app.chart.chart.panes().map(p => p.getHeight()), count: window.__app.chart.payload.count})")
        raise AssertionError(f"second fib click did not create a fib: {info}") from exc
    fib = page.evaluate("window.__app.state.fibs[0]")
    assert fib["levels"][0]["price"] == fib["p2"] and fib["t2_ms"] <= page.evaluate("window.__app.state.session.cursor_ms")
    assert fib["interval"] == "1h" and fib["p1"] != fib["p2"]
    assert page.inner_text("#fib-count") == "1" and page.is_hidden("#draw-hint")
    assert page.evaluate("window.__app.state.selectedFib") == fib["id"]
    assert page.evaluate("window.__app.chart.fibs.length") == 1
    page.click("#btn-fib")
    page.wait_for_selector("#fib-popover:not(.hidden)")
    assert page.locator("#fib-list li").count() == 1
    page.check("#fib-auto")
    page.wait_for_function("window.__app.chart.payload.auto_fib && window.__app.chart.payload.auto_fib.lookback === 100")
    page.fill("#fib-lookback", "30")
    page.dispatch_event("#fib-lookback", "change")
    page.wait_for_function("window.__app.chart.payload.auto_fib && window.__app.chart.payload.auto_fib.lookback === 30")
    auto = page.evaluate("window.__app.chart.payload.auto_fib")
    assert auto["bars"] == 30 and auto["levels"][0]["price"] == auto["p2"]
    page.uncheck("#fib-auto")
    page.wait_for_function("window.__app.chart.payload.auto_fib === null")
    page.click("#btn-close-fib")
    page.wait_for_selector("#fib-popover.hidden", state="attached")
    page.keyboard.press("Delete")  # removes the selected fib
    page.wait_for_function("window.__app.state.fibs.length === 0")
    assert page.inner_text("#fib-count") == "0"
    # Esc cancels a half-drawn fib.
    page.keyboard.press("f")
    page.mouse.click(box["x"] + box["width"] * 0.40, box["y"] + box["height"] * 0.18)
    page.wait_for_function("window.__app.state.fibDraft !== null")
    page.keyboard.press("Escape")
    assert page.evaluate("window.__app.state.drawing") is False and page.is_hidden("#draw-hint")

    # Bollinger: %B reading in the side panel; O toggles the bands on the chart via the indicator list.
    bb = page.evaluate("window.__app.chart.payload.gauges.bb")
    assert (page.inner_text("#gauge-bb") == "–") == (bb["pctb"] is None)
    if bb["pctb"] is not None:
        assert float(page.inner_text("#gauge-bb")) == pytest.approx(bb["pctb"], abs=0.006)
        assert page.inner_text("#gauge-bbw").endswith("%")
    n_ind = page.evaluate("window.__app.indicators.instances.length")
    page.keyboard.press("o")
    page.wait_for_function(f"window.__app.indicators.instances.length === {n_ind + 1} && window.__app.chart.indicators.some(i => i.instance.key === 'bbands')")
    page.wait_for_function("document.getElementById('legend').textContent.includes('BB (')")
    page.keyboard.press("o")
    page.wait_for_function(f"window.__app.indicators.instances.length === {n_ind} && !window.__app.chart.indicators.some(i => i.instance.key === 'bbands')")

    # S: daily EMA 200 line on the intraday chart; the reading row follows the gauges.
    assert page.evaluate("window.__app.chart.dailyEma") is None
    page.keyboard.press("s")
    page.wait_for_function("window.__app.state.dailyEma === true && window.__app.chart.payload.daily_ema !== null")
    assert page.is_checked("#daily-ema")
    em = page.evaluate("window.__app.chart.payload.gauges.ema_long")
    assert (page.inner_text("#gauge-ema") == "–") == (em["value"] is None)
    assert page.locator("#gauge-sma").count() == 0
    page.keyboard.press("s")
    page.wait_for_function("window.__app.state.dailyEma === false && window.__app.chart.dailyEma === null")
    # L: tubes at the next support / resistance and dashed lines at the 6-month high and low, on by default.
    g = page.evaluate("window.__app.chart.payload.gauges")
    lv = g["levels"]
    expected = sum(1 for k in ("resistance", "support") if lv[k])
    assert page.evaluate("window.__app.state.nearLevels") is True and page.is_checked("#near-levels")
    zones = page.evaluate("window.__app.chart.zones.zones")
    assert len(zones) == expected
    for z in zones:
        assert z["low"] == lv[z["kind"]]["low"] and z["high"] == lv[z["kind"]]["high"]
        assert re.fullmatch(r"[RS] \d+ touch(es)?( · \d+ breaks?)? · ([-+]\d+\.\d\d% to the edge|price inside)", z["label"]), z["label"]
    titles = page.evaluate("window.__app.chart.extremeLines.map(l => l.options().title)")
    w = g["extremes"]["6m"]
    assert titles == [f"6M high {w['vs_high_pct']:+.2f}%", f"6M low {w['vs_low_pct']:+.2f}%"]
    assert page.evaluate("window.__app.chart.extremeLines.map(l => l.options().price)") == [w["high"], w["low"]]
    page.keyboard.press("l")
    page.wait_for_function("window.__app.state.nearLevels === false && window.__app.chart.zones.zones.length === 0 && window.__app.chart.extremeLines.length === 0")
    assert not page.is_checked("#near-levels")
    page.keyboard.press("l")
    page.wait_for_function(f"window.__app.state.nearLevels === true && window.__app.chart.zones.zones.length === {expected} && window.__app.chart.extremeLines.length === 2")

    # D held: daily candles of the last year; the chosen timeframe and zoom come back on release.
    iv = page.evaluate("window.__app.state.interval")
    assert iv != "1D"
    page.wait_for_function("window.__app.chart.targetRange === null")
    r0 = page.evaluate("window.__app.chart.currentRange()")
    page.keyboard.down("d")
    page.wait_for_function("window.__app.state.peek === 'year' && window.__app.chart.payload.interval === '1D'")
    assert page.get_attribute("#btn-peek", "aria-pressed") == "true"
    assert page.get_attribute("button.tf[data-interval='1D']", "aria-checked") == "true"
    assert page.evaluate("window.__app.state.interval") == iv  # the step size is untouched
    n_day = page.evaluate("window.__app.chart.payload.count")
    assert 0 < n_day < 365  # the fake exchange lists in 2017-07: fewer than a year of days exist
    page.wait_for_function("window.__app.chart.targetRange === null")
    rng = page.evaluate("window.__app.chart.chart.timeScale().getVisibleLogicalRange()")
    assert rng["from"] <= 0 and rng["to"] >= n_day - 1  # every day is on screen (the library keeps at least 10 slots)
    assert "1D" in page.inner_text("#legend") and not page.locator("#draw-hint").evaluate("e => e.classList.contains('hidden')")
    # While the peek is held the price-line labels (bucket lines, 6-month lines) are hidden so the
    # latest candles stay visible; they come back on release.
    assert page.evaluate("window.__app.chart.quiet && [...window.__app.chart._titles.keys()].every(l => l.options().title === '')")
    page.keyboard.down("d")  # auto-repeat while held must not restart the peek
    page.keyboard.up("d")
    page.wait_for_function(f"window.__app.state.peek === null && window.__app.chart.payload.interval === '{iv}'")
    page.wait_for_function("r => { const c = window.__app.chart.chart.timeScale().getVisibleLogicalRange(); return c && Math.abs(c.from - r.from) < 1e-6 && Math.abs(c.to - r.to) < 1e-6; }", arg=r0)
    assert page.get_attribute("#btn-peek", "aria-pressed") == "false"
    page.wait_for_function("!window.__app.chart.quiet && window.__app.chart.bucketLines.every(b => b.line.options().title.startsWith('#')) && window.__app.chart.extremeLines.every(l => l.options().title.startsWith('6M'))")
    assert page.get_attribute(f"button.tf[data-interval='{iv}']", "aria-checked") == "true"
    assert page.locator("#draw-hint").evaluate("e => e.classList.contains('hidden')")
    # The 1Y button held with the mouse does the same.
    page.hover("#btn-peek")
    page.mouse.down()
    page.wait_for_function("window.__app.state.peek === 'year' && window.__app.chart.payload.interval === '1D'")
    page.mouse.up()
    page.wait_for_function(f"window.__app.state.peek === null && window.__app.chart.payload.interval === '{iv}'")
    assert page.evaluate("document.activeElement === document.body")  # the held button did not take focus

    # Y held: Bitcoin daily candles of two years with the level lines; release restores the view.
    page.keyboard.down("y")
    page.wait_for_function("window.__app.state.peek === 'btc' && window.__app.chart.payload.symbol === 'BTCUSDT' && window.__app.chart.payload.interval === '1D' && 'levels' in window.__app.chart.payload")
    assert page.get_attribute("#btn-peek-btc", "aria-pressed") == "true"
    lv = page.evaluate("window.__app.chart.payload.levels")
    assert page.evaluate("window.__app.chart.levelLines.length") == len(lv["resistance"]) + len(lv["support"]) <= 6
    assert "Bitcoin" in page.inner_text("#draw-hint")
    page.keyboard.down("d")  # a second peek while one is held is ignored
    page.keyboard.up("d")
    assert page.evaluate("window.__app.state.peek") == "btc"
    page.keyboard.up("y")
    page.wait_for_function(f"window.__app.state.peek === null && window.__app.chart.payload.interval === '{iv}' && window.__app.chart.levelLines.length === 0")
    page.wait_for_function("r => { const c = window.__app.chart.chart.timeScale().getVisibleLogicalRange(); return c && Math.abs(c.from - r.from) < 1e-6 && Math.abs(c.to - r.to) < 1e-6; }", arg=r0)
    assert page.get_attribute("#btn-peek-btc", "aria-pressed") == "false"

    # H shows the shortcut list; Esc closes it.
    page.keyboard.press("h")
    page.wait_for_selector("#help-popover:not(.hidden)")
    assert page.locator("#help-popover kbd").count() >= 10
    page.keyboard.press("Escape")
    page.wait_for_selector("#help-popover.hidden", state="attached")

    # Fair value gaps: G toggles, the view then carries the gap list; the min size is adjustable.
    assert page.evaluate("window.__app.chart.payload.fvg") is None
    page.keyboard.press("g")
    page.wait_for_function("Array.isArray(window.__app.chart.payload.fvg)")
    assert page.get_attribute("#btn-fvg", "aria-pressed") == "true"
    gaps = page.evaluate("window.__app.chart.payload.fvg")
    assert all(g["top"] > g["bottom"] for g in gaps)
    assert page.inner_text("#fvg-count") == str(sum(1 for g in gaps if not g["filled"]))
    page.click("#btn-fvg")
    page.wait_for_selector("#fvg-popover:not(.hidden)")
    assert "on screen" in page.inner_text("#fvg-summary")
    page.fill("#fvg-min", "5")
    page.dispatch_event("#fvg-min", "change")
    page.wait_for_function("window.__app.state.fvg.minPct === 5")
    page.wait_for_function("Array.isArray(window.__app.chart.payload.fvg) && window.__app.chart.payload.fvg.every(g => g.size_pct >= 5)")
    page.click("#btn-close-fvg")
    page.keyboard.press("g")
    page.wait_for_function("window.__app.state.fvg.on === false")
    assert page.inner_text("#fvg-count") == "0" and page.get_attribute("#btn-fvg", "aria-pressed") == "false"

    # Whole coins: the estimate shows the rounded order; off again for the ordinary buy below.
    page.click("#buy-presets button[data-pct='15']")
    assert page.input_value("#buy-amount") == "1500.00"
    page.click("#btn-whole")
    assert page.get_attribute("#btn-whole", "aria-pressed") == "true" and page.evaluate("window.__app.state.wholeCoins") is True
    page.wait_for_selector("#buy-coins-wrap:not(.hidden)")
    price = page.evaluate("window.__app.state.session.price")
    coins = math.floor(1500 * 0.999 / price)
    assert page.input_value("#buy-coins") == str(coins)  # the money was rounded to whole coins
    assert page.input_value("#buy-amount") == f"{coins * price / 0.999:.2f}"
    assert page.inner_text("#buy-estimate").startswith(f"{coins} whole BTC")
    page.fill("#buy-coins", "3")  # typing coins rewrites the USDT field
    assert page.input_value("#buy-amount") == f"{3 * price / 0.999:.2f}" and page.inner_text("#buy-estimate").startswith("3 whole BTC")
    page.fill("#buy-amount", "250"); page.press("#buy-amount", "Tab")  # a typed amount rounds on leaving the field
    assert page.input_value("#buy-coins") == str(math.floor(250 * 0.999 / price)) and float(page.input_value("#buy-amount")) <= 250
    page.click("#btn-whole")  # off again: the buys below check the exact amounts
    page.wait_for_selector("#buy-coins-wrap.hidden", state="attached")
    assert page.get_attribute("#btn-whole", "aria-pressed") == "false" and "whole" not in page.inner_text("#buy-estimate")

    # Buy with a preset, then a typed amount.
    page.click("#buy-presets button[data-pct='10']")
    assert "fee 1.00" in page.inner_text("#buy-estimate")
    page.click("#btn-buy")
    # The reason checklist opens first; Esc cancels without buying, Confirm sends the order.
    page.wait_for_selector("#dlg-reasons[open]")
    assert page.inner_text("#reasons-version") == "Checklist v1"
    page.keyboard.press("Escape")
    page.wait_for_selector("#dlg-reasons:not([open])", state="attached")
    page.wait_for_timeout(300)
    assert page.locator("#bucket-list .bucket").count() == 0
    page.click("#btn-buy")
    confirm_reasons(page, ("at_support", "rsi_low"), "bounce off the tube", side="buy", title="Buy 1,000.00 USDT of BTC @ ")
    page.wait_for_function("document.querySelectorAll('#bucket-list .bucket').length === 1")
    assert page.inner_text("#acc-cash") == "9,000.00"
    # Journal: the buy got a row with the readings and a chart picture.
    page.wait_for_function("window.__app.state.journal.length === 1 && window.__app.state.journal[0].has_image")
    j = page.evaluate("window.__app.state.journal[0]")
    assert j["side"] == "buy" and j["bucket_seq"] == 1 and j["interval"] == "1h" and j["strategy"] == "ema_cross"
    assert j["quote"] == 1000 and "ema" in j["indicators"] and isinstance(j["light_enter"], bool)
    assert j["reasons"] == ["at_support", "rsi_low"] and j["reason_note"] == "bounce off the tube" and j["checklist"] == "v1"
    assert j["ath_pct"] <= 0 <= j["atl_pct"] and len(j["ath_date"]) == 10 and j["high_3m"] >= j["low_3m"]
    assert j["high_6m"] >= j["high_3m"] and j["low_6m"] <= j["low_3m"] and len(j["high_6m_date"]) == 10
    assert "next_support_pct" in j and "next_resistance_touches" in j and "next_resistance_breaks" in j and "next_support_low" in j
    assert page.inner_text("#journal-count") == "1"
    # Age column: measured on the replay clock, zero right after the buy.
    assert page.locator("#bucket-table thead th").count() == 17
    # High / Low since the buy: percent of the buy price, price and time on hover.
    ob = page.evaluate("window.__app.state.session.buckets[0]")
    assert ob["held_high"] >= ob["entry_price"] >= ob["held_low"] and ob["held_high_pct"] >= 0 >= ob["held_low_pct"]
    assert page.inner_text("#bucket-list .bucket:nth-child(1) td:nth-child(12)") == f"{ob['held_high_pct']:+.2f}%"
    assert "Highest high" in page.get_attribute("#bucket-list .bucket:nth-child(1) td:nth-child(12)", "title")
    assert page.inner_text("#bucket-list .bucket:nth-child(1) td:nth-child(13)") == f"{ob['held_low_pct']:+.2f}%"
    rsi_txt = page.inner_text("#bucket-list .bucket:nth-child(1) td.rsi")
    assert rsi_txt == "–" or 0 <= float(rsi_txt) <= 100
    assert page.evaluate("window.__app.state.session.buckets[0].buy_rsi_interval") == "1h"
    assert page.inner_text("#bucket-list .bucket:nth-child(1) td.age") == "<1h"
    assert page.evaluate("[0, 3599e3, 3600e3, 5 * 3600e3, 86400e3, 86400e3 + 7200e3, 10 * 86400e3 + 7200e3, -1].map(window.__app.ageText)") == ["<1h", "<1h", "1h", "5h", "1d", "1d 2h", "10d", ""]
    page.fill("#buy-amount", "250")
    page.press("#buy-amount", "Enter")
    confirm_reasons(page)  # nothing ticked is allowed
    page.wait_for_function("document.querySelectorAll('#bucket-list .bucket').length === 2")
    assert page.inner_text("#bucket-count") == "2"
    assert page.evaluate("window.__app.chart.bucketLines.length") == 2
    page.wait_for_function("window.__app.chart.markerList.filter(m => m.text && m.text.startsWith('B')).length === 2")  # markers follow the trades refresh

    # Over-spending is rejected with a message, not silently.
    page.fill("#buy-amount", "999999")
    page.click("#btn-buy")
    confirm_reasons(page, ("breakout",))
    page.wait_for_function("document.getElementById('toast').textContent.includes('exceeds cash')")
    assert page.inner_text("#bucket-count") == "2"

    # Select a bucket (highlighted line), step, then sell it.
    page.click("#bucket-list .bucket:nth-child(2)")
    assert page.evaluate("window.__app.state.selectedBucket") == page.evaluate("window.__app.state.session.buckets[1].id")
    assert "selected" in page.get_attribute("#bucket-list .bucket:nth-child(2)", "class")
    page.click("#btn-step")
    page.wait_for_function("window.__app.state.session.buckets[0].unrealized_pnl !== null")
    # Sell a quarter first: the bucket stays with three quarters, History gets row #1.1.
    assert page.locator("#bucket-list .bucket:nth-child(1) .sells .sell").count() == 3
    qty_before = page.evaluate("window.__app.state.session.buckets[0].qty")
    page.click("#bucket-list .bucket:nth-child(1) .sell[data-part='0.25']")
    confirm_reasons(page, ("trim",), side="sell", title="Sell 25 % of bucket #1: ")
    page.wait_for_function("document.querySelectorAll('#history-list .bucket').length === 1")
    assert page.locator("#bucket-list .bucket").count() == 2
    assert page.evaluate("window.__app.state.session.buckets[0].qty") == pytest.approx(qty_before * 0.75)
    assert page.inner_text("#bucket-list .bucket:nth-child(1) td.seq") == "#1 ·1"
    assert page.inner_text("#history-list .bucket td.seq") == "#1.1" and "25 %" in page.get_attribute("#history-list .bucket td.seq", "title")
    page.wait_for_function("document.getElementById('toast').textContent.includes('25 % of bucket #1 (part 1)')")
    # Then the rest: the bucket closes as part 2.
    page.click("#bucket-list .bucket:nth-child(1) .sell[data-part='1']")
    confirm_reasons(page, ("profit_target", "at_resistance"), "done", title="Sell all of bucket #1: ")
    page.wait_for_function("document.querySelectorAll('#bucket-list .bucket').length === 1")
    page.wait_for_function("document.querySelectorAll('#history-list .bucket').length === 2")
    assert page.inner_text("#history-count") == "2"
    assert sorted(page.locator("#history-list .bucket td.seq").all_inner_texts()) == ["#1.1", "#1.2"]
    # Journal: the sell row carries the result and the buy readings; the picture opens in a dialog.
    page.wait_for_function("window.__app.state.journal.length === 4 && window.__app.state.journal.every(r => r.has_image)")
    sell_row = page.evaluate("window.__app.state.journal.find(r => r.side === 'sell')")
    assert sell_row["bucket_seq"] == 1 and sell_row["at_buy"]["quote"] == 1000 and "realized_pnl" in sell_row
    page.click("#tab-journal")
    page.wait_for_selector("#panel-journal:not(.hidden)")
    assert page.locator("#journal-list tr.journal").count() == 4  # two buys, the quarter and the rest of #1
    assert page.locator("#journal-table thead th").count() == 20
    assert sorted(page.locator("#journal-list tr.journal.sell td.seq").all_inner_texts()) == ["#1.1", "#1.2"]
    reasons = page.locator("#journal-list tr.journal td.reasons").all_inner_texts()
    assert sorted(reasons) == sorted(["At support, RSI low (<30)", "–", "Trim", "At resistance, Profit target reached"])  # checklist order
    assert "bounce off the tube" in " ".join(page.get_attribute(f"#journal-list tr.journal:nth-child({i + 1}) td.reasons", "title") for i in range(4))
    ath_cell = page.locator("#journal-list tr.journal:first-child td:nth-child(11)")
    assert re.fullmatch(r"[-+]\d+\.\d\d%", ath_cell.inner_text()) and ath_cell.get_attribute("title").startswith("1 year ")
    assert " · 6 months " in ath_cell.get_attribute("title")
    page.wait_for_function("[...document.querySelectorAll('#journal-list img.thumb')].every(i => i.complete && i.naturalWidth > 100)")
    page.click("#journal-list tr.journal.sell img.thumb")
    page.wait_for_selector("#dlg-snapshot[open]")
    assert page.inner_text("#snap-title").startswith("Sell #1.")
    assert "Part of the bucket" in page.inner_text("#snap-readings")
    assert "Reasons ticked" in page.inner_text("#snap-readings") and "Checklist" in page.inner_text("#snap-readings")
    assert page.locator("#snap-readings tr").count() > 20 and "at the buy" in page.inner_text("#snap-readings").lower()
    page.wait_for_function("document.getElementById('snap-img').naturalWidth > 100")
    page.click("#btn-close-snapshot")
    page.wait_for_selector("#dlg-snapshot:not([open])", state="attached")
    page.click("#journal-tools .chip[data-side='sell']")
    assert page.locator("#journal-list tr.journal").count() == 2
    page.click("#journal-tools .chip[data-side='all']")
    csv_text = page.evaluate("fetch(document.getElementById('btn-journal-csv').href).then(r => r.text())")
    assert len(csv_text.splitlines()) == 5 and "at_buy_price" in csv_text.splitlines()[0] and ",part," in csv_text.splitlines()[0]
    assert ",reasons," in csv_text.splitlines()[0] and "at_resistance | profit_target" in csv_text
    # Statistics: the Reasons view over all sessions, counts next to every average, small groups flagged.
    page.click("#tab-stats")
    page.wait_for_selector("#panel-stats:not(.hidden)")
    # The test database is shared with the API tests, so the table is checked against the server's numbers.
    page.wait_for_function("window.__app.state.stats && window.__app.state.stats.trades >= 1")
    st = page.evaluate("window.__app.state.stats")
    buy = {r["key"]: r for r in st["buy"]}
    sell = {r["key"]: r for r in st["sell"]}
    assert st["checklist"] == "v1" and st["sells"] >= 2 and buy["at_support"]["ticked"]["count"] >= 1 and sell["trim"]["ticked"]["count"] >= 1
    assert page.inner_text("#stats-summary").startswith(f"{st['trades']} closed trade{'' if st['trades'] == 1 else 's'} · {st['sells']} sells · average ")
    assert "groups under 10 are too few to judge" in page.inner_text("#stats-summary")
    assert page.locator("#stats-buy tr").count() == 11 and page.locator("#stats-sell tr").count() == 11
    row = page.locator("#stats-buy tr[data-key='at_support']")
    assert row.locator("td.count").all_inner_texts() == [str(buy["at_support"]["ticked"]["count"]), str(buy["at_support"]["unticked"]["count"])]
    avg = row.locator("td.avg").nth(0).inner_text()
    assert re.fullmatch(r"[-+]\d+\.\d\d%(too few to judge)?", avg, re.I) and avg.lower().endswith("too few to judge") == buy["at_support"]["ticked"]["few"]
    assert row.locator("td.avg").nth(1).inner_text().startswith("–") == (buy["at_support"]["unticked"]["count"] == 0)
    assert page.locator("#stats-buy tr[data-key='breakout'] td.count").all_inner_texts() == [str(buy["breakout"]["ticked"]["count"]), str(buy["breakout"]["unticked"]["count"])]
    assert page.locator("#stats-sell tr[data-key='trim'] td.count").inner_text() == str(sell["trim"]["ticked"]["count"])
    assert page.locator("#stats-sell tr[data-key='profit_target'] td.count").inner_text() == str(sell["profit_target"]["ticked"]["count"])
    few_buy = sum(int(g["few"]) for r in st["buy"] for g in (r["ticked"], r["unticked"]))
    few_sell = sum(int(r["ticked"]["few"]) for r in st["sell"])
    assert page.locator("#stats-buy .tag.few").count() == few_buy and page.locator("#stats-sell .tag.few").count() == few_sell
    assert [o.strip() for o in page.locator("#stats-symbol option").all_inner_texts()] == ["All coins", "BTC"]
    page.select_option("#stats-symbol", "BTCUSDT")
    page.wait_for_function("window.__app.state.stats.symbol === 'BTCUSDT'")
    assert page.locator("#stats-sell tr[data-key='trim'] td.count").inner_text() == str(sell["trim"]["ticked"]["count"])
    page.select_option("#stats-symbol", "")
    page.wait_for_function("window.__app.state.stats.symbol === null")
    page.click("#tab-open")
    page.wait_for_selector("#panel-open:not(.hidden)")
    # The history lives in its own tab under the chart.
    assert page.is_hidden("#panel-history")
    page.click("#tab-history")
    page.wait_for_selector("#panel-history:not(.hidden)")
    assert page.is_hidden("#panel-open")
    assert "→" not in page.inner_text("#history-list") and page.locator("#history-list tr").count() == 2  # the two parts of #1
    assert page.locator("#history-table thead th").count() == 23
    hb = page.evaluate("window.__app.state.closedBuckets[0]")
    assert hb["held_high"] >= hb["exit_price"] and hb["held_low"] <= hb["entry_price"]
    assert page.inner_text("#history-list .bucket:first-child td:nth-child(21)") == f"{hb['held_high_pct']:+.2f}%"
    assert page.inner_text("#history-list .bucket:first-child td:nth-child(23)") == f"{hb['held_low_pct']:+.2f}%"
    # EMA 200 change columns: sign colour follows the value, the hover text names both ends.
    closed = page.evaluate("window.__app.state.closedBuckets[0]")
    for col, key in ((10, "ema200_change_pct"), (11, "ema200_dist_change")):
        cell = page.locator(f"#history-list .bucket:first-child td:nth-child({col})")
        v = closed[key]
        if v is None:
            assert cell.inner_text() == "–"
        else:
            assert cell.inner_text() == (f"{v:+.2f}%" if key.endswith("_pct") else f"{v:+.2f} pp")
            assert cell.get_attribute("class") == f"num {'up' if v > 0 else 'down' if v < 0 else ''}"
        assert "at buy" in cell.get_attribute("title") and "at sell" in cell.get_attribute("title")
    assert page.locator("#history-list .bucket:nth-child(1) td.rsi").count() == 2
    assert page.inner_text("#history-list .bucket:nth-child(1) td.held") == "1h"  # bought, one 1h step, sold
    page.click("#tab-open")
    page.wait_for_selector("#panel-open:not(.hidden)")
    assert sorted(page.evaluate("window.__app.chart.markerList.filter(m => m.text && m.text.startsWith('S')).map(m => m.text)")) == ["S1.1", "S1.2"]
    assert page.evaluate("window.__app.chart.bucketLines.length") == 1
    assert "#2" in page.inner_text("#bucket-list")  # the remaining bucket keeps its number
    # "Now" template row: current price against the last buy (#2) and the last sell (#1).
    now = page.inner_text("#bucket-now")
    assert re.search(r"vs (last buy|high since buy) #2", now) and re.search(r"vs (last sell|high since sell) #1", now)
    assert "Buy…" in now
    # The same two percentages sit in the Readings card.
    assert page.is_visible("#gauge-buy") and page.is_visible("#gauge-sell")
    assert "#2" in page.inner_text("#gauge-buy-label") and "#1" in page.inner_text("#gauge-sell-label")
    ref_pct = page.evaluate("window.__app.state.session.reference.last_buy.pct")
    assert page.inner_text("#gauge-buy") == f"{ref_pct:+.2f}%"
    assert page.locator("#bucket-now .pnl").count() == 2
    assert page.inner_text("#bucket-list .bucket:nth-child(1) td.age") == "1h"  # one 1h step since the buy

    # ------------------------------------------------------- review regressions
    # Invalid indicator parameters are rejected client-side; nothing is added.
    page.click("#btn-indicators")
    page.select_option("#indicator-key", "ema")
    page.fill("#indicator-params input[name='period']", "0")
    page.click("#btn-add-indicator")
    page.wait_for_function("!document.getElementById('indicator-error').classList.contains('hidden')")
    assert "at least 1" in page.inner_text("#indicator-error")
    assert page.inner_text("#indicator-count") == "3"
    # A combination only the server can reject (MACD fast >= slow) rolls back after the 422.
    page.select_option("#indicator-key", "macd")
    page.fill("#indicator-params input[name='fast']", "30")
    page.fill("#indicator-params input[name='slow']", "26")
    page.click("#btn-add-indicator")
    page.wait_for_function("document.getElementById('indicator-error').textContent.includes('fast')")
    assert page.inner_text("#indicator-count") == "3"
    assert page.evaluate("window.__app.chart.indicators.length") == 3
    page.click("#btn-close-indicators")

    # Strategy parameters the server rejects roll back to the previous values.
    page.click("#btn-strategy-params")
    page.fill("#strategy-params input[name='fast']", "60")
    page.dispatch_event("#strategy-params input[name='fast']", "change")
    page.wait_for_function("!document.getElementById('strategy-error').classList.contains('hidden')")
    assert page.evaluate("window.__app.state.strategy.params.fast") == 20
    assert page.input_value("#strategy-params input[name='fast']") == "20"

    # A double submit creates one bucket, not two.
    n_before = page.evaluate("document.querySelectorAll('#bucket-list .bucket').length")
    page.fill("#buy-amount", "100")
    page.press("#buy-amount", "Enter")
    page.wait_for_selector("#dlg-reasons[open]")
    page.evaluate("window.__app.confirmReasons(); window.__app.confirmReasons()")
    page.wait_for_function(f"document.querySelectorAll('#bucket-list .bucket').length === {n_before + 1}")
    page.wait_for_timeout(400)
    assert page.evaluate("document.querySelectorAll('#bucket-list .bucket').length") == n_before + 1

    # With buckets open, the D peek hides their line labels and brings them back on release.
    page.evaluate("document.activeElement && document.activeElement.blur()")  # hotkeys are off inside inputs
    page.keyboard.down("d")
    page.wait_for_function("window.__app.state.peek === 'year' && window.__app.chart.payload.interval === '1D'")
    page.wait_for_function("window.__app.chart.targetRange === null")
    assert page.evaluate("window.__app.chart.quiet && window.__app.chart.bucketLines.length > 0 && window.__app.chart.bucketLines.every(b => b.line.options().title === '')")
    page.keyboard.up("d")
    page.wait_for_function("window.__app.state.peek === null && !window.__app.chart.quiet && window.__app.chart.bucketLines.length > 0 && window.__app.chart.bucketLines.every(b => b.line.options().title.startsWith('#'))")

    # Target and stop: hatched preview while hovering Buy, stored with the bucket, dotted lines for
    # the selected bucket, the Auto switch, and the automatic sell inside a step.
    assert page.input_value("#buy-tp") == "20" and page.input_value("#buy-sl") == "10"
    page.fill("#buy-tp", "1"); page.dispatch_event("#buy-tp", "change")
    page.fill("#buy-sl", "5"); page.dispatch_event("#buy-sl", "change")
    page.fill("#buy-amount", "100")
    page.hover("#btn-buy")
    page.wait_for_function("window.__app.chart.plan.plan !== null")
    plan = page.evaluate("window.__app.chart.plan.plan")
    price = page.evaluate("window.__app.state.session.price")
    assert plan["tp"] == pytest.approx(price * 1.01) and plan["sl"] == pytest.approx(price * 0.95) and plan["tpPct"] == 1 and plan["slPct"] == 5
    assert plan["time"] == page.evaluate("window.__app.chart.payload.time[window.__app.chart.payload.count - 1]")
    page.hover("#acc-cash")
    page.wait_for_function("window.__app.chart.plan.plan === null")
    n_before = page.evaluate("document.querySelectorAll('#bucket-list .bucket').length")
    page.click("#btn-buy")
    page.wait_for_selector("#dlg-reasons[open]")
    assert page.inner_text("#reasons-title").endswith("· target +1 % · stop −5 %")
    confirm_reasons(page)
    page.wait_for_function(f"document.querySelectorAll('#bucket-list .bucket').length === {n_before + 1}")
    b = page.evaluate("window.__app.state.session.buckets[window.__app.state.session.buckets.length - 1]")
    assert b["tp_pct"] == 1 and b["sl_pct"] == 5 and b["tp_price"] == pytest.approx(b["entry_price"] * 1.01) and b["armed_ms"] is None
    row = page.locator(f"#bucket-list .bucket[data-id='{b['id']}']")
    assert row.locator("td.tp").inner_text() == "+1.0%" and row.locator("td.sl").inner_text() == "−5.0%"
    assert row.locator("td.tp").get_attribute("title").startswith("Target ") and " from here" in row.locator("td.tp").get_attribute("title")
    assert row.locator(".arm").inner_text() == "Off" and row.locator(".arm").get_attribute("aria-pressed") == "false"
    row.click()
    page.wait_for_function("window.__app.chart.planLines.length === 2")
    page.wait_for_function("window.__app.state.journal.some(r => r.side === 'buy' && r.tp_pct === 1 && r.sl_pct === 5)")
    row.locator(".arm").click()
    page.wait_for_function(f"window.__app.state.session.buckets.find(x => x.id === {b['id']}).armed_ms !== null")
    page.wait_for_function("document.getElementById('toast').textContent.includes('Auto on for bucket')")
    row = page.locator(f"#bucket-list .bucket[data-id='{b['id']}']")
    assert row.locator(".arm").inner_text() == "On" and row.locator(".arm").get_attribute("aria-pressed") == "true"
    # The fake price rises 1 per hour (or drops a third once a cycle): a 1 % target or a 5 % stop
    # fires within a few hourly steps; the step reports the sale, the bucket closes in History.
    for _ in range(8):
        page.click("#btn-step")
        page.wait_for_function("!window.__app.state.busy")
        if page.evaluate(f"!window.__app.state.session.buckets.some(x => x.id === {b['id']})"):
            break
    assert page.evaluate(f"!window.__app.state.session.buckets.some(x => x.id === {b['id']})")
    page.wait_for_function("document.getElementById('toast').textContent.startsWith('Auto: ')")
    page.wait_for_function(f"window.__app.state.closedBuckets.some(x => x.id === {b['id']})")
    closed = page.evaluate(f"window.__app.state.closedBuckets.find(x => x.id === {b['id']})")
    assert closed["trigger"] in ("target", "stop") and closed["armed_ms"] is not None
    if closed["trigger"] == "target":
        assert closed["exit_price"] == pytest.approx(b["tp_price"]) or closed["exit_price"] > b["tp_price"]
    page.wait_for_function(f"window.__app.state.journal.some(r => r.side === 'sell' && r.bucket_seq === {b['seq']} && r.trigger && r.has_image)")
    j = page.evaluate(f"window.__app.state.journal.find(r => r.side === 'sell' && r.bucket_seq === {b['seq']})")
    assert j["trigger"] == closed["trigger"] and j["armed_at"] and j["reasons"] == [] and j[f"{closed['trigger']}_hit"] is True
    page.click("#tab-history")
    page.wait_for_selector("#panel-history:not(.hidden)")
    hrow = page.locator(f"#history-list .bucket[data-id='{b['id']}']")
    assert hrow.locator("td.exit").inner_text() == closed["trigger"] and "Auto was on since" in hrow.locator("td.exit").get_attribute("title")
    assert hrow.locator("td.tp").inner_text() == "+1.0%"
    page.click("#tab-journal")
    page.wait_for_selector("#panel-journal:not(.hidden)")
    assert f"Auto: {closed['trigger']} reached" in page.locator("#journal-list tr.journal td.reasons").all_inner_texts()
    # Performance: tiles, the three canvases and the monthly grid, for this session and for all;
    # the bottom panel grows while the tab is open and the numbers match the server's report.
    page.click("#tab-perf")
    page.wait_for_selector("#panel-perf:not(.hidden)")
    page.wait_for_function("window.__app.state.perf && window.__app.state.perf.scope === 'session'")
    assert "tall" in page.get_attribute(".center", "class")
    rep = page.evaluate("window.__app.state.perf")
    sm = rep["summary"]
    assert sm["trades"] == len(rep["trades"]) >= 3 and rep["curve"]["kind"] == "equity" and len(rep["curve"]["time"]) == rep["curve"]["points"]
    assert page.locator("#perf-tiles .tile").count() == 12
    assert page.locator("#perf-tiles .tile[data-key='trades'] .v").inner_text() == str(sm["trades"])
    assert page.locator("#perf-tiles .tile[data-key='trades'] .s").inner_text().startswith(f"{sm['winners']} won · {sm['losers']} lost")
    assert page.locator("#perf-tiles .tile[data-key='net-profit'] .v").inner_text() == f"{sm['net_pnl']:+,.2f}"
    assert page.locator("#perf-tiles .tile[data-key='max-drawdown'] .v").inner_text() == f"−{sm['max_drawdown_pct']:.2f}%"
    assert page.locator("#perf-tiles .tile[data-key='best-trade'] .s").inner_text().startswith(sm["best"]["label"])
    for cid in ("perf-equity", "perf-bars", "perf-donut"):
        box = page.locator(f"#{cid} canvas").bounding_box()
        assert box["width"] > 150 and box["height"] > 150, (cid, box)
        assert page.evaluate(f"document.querySelector('#{cid} canvas').width") > 150
    assert page.locator("#perf-months table.months td.up, #perf-months table.months td.down").count() >= 1
    assert page.locator("#perf-months table.months th").first.inner_text() == ""
    # Hovering the bars shows a tooltip with the trade; leaving hides it.
    bars = page.locator("#perf-bars canvas").bounding_box()
    page.locator("#perf-bars canvas").hover(position={"x": 60, "y": bars["height"] / 2})
    page.wait_for_selector("#perf-bars .perf-tip:not(.hidden)")
    assert "Trade" in page.inner_text("#perf-bars .perf-tip") and "P&L" in page.inner_text("#perf-bars .perf-tip")
    eq = page.locator("#perf-equity canvas").bounding_box()
    page.locator("#perf-equity canvas").hover(position={"x": eq["width"] * 0.6, "y": eq["height"] / 2})
    page.wait_for_selector("#perf-equity .perf-tip:not(.hidden)")
    assert "Equity" in page.inner_text("#perf-equity .perf-tip") and "Below peak" in page.inner_text("#perf-equity .perf-tip")
    page.locator("#perf-equity-title").hover()
    page.wait_for_selector("#perf-equity .perf-tip.hidden", state="attached")
    # All sessions: the same numbers or more (the test database holds the API tests' sessions too).
    page.click("#perf-tools .chip[data-scope='all']")
    page.wait_for_function("window.__app.state.perf && window.__app.state.perf.scope === 'all'")
    allrep = page.evaluate("window.__app.state.perf")
    assert allrep["summary"]["trades"] >= sm["trades"] and allrep["curve"]["kind"] == "cumulative" and allrep["sessions"] >= 1
    assert page.locator("#perf-tiles .tile[data-key='equity-all-sessions']").count() == 1
    assert "trade by trade" in page.inner_text("#perf-equity-title").lower()
    page.click("#perf-tools .chip[data-scope='session']")
    page.wait_for_function("window.__app.state.perf && window.__app.state.perf.scope === 'session'")
    # A step refreshes the report while the tab is open.
    pts = rep["curve"]["points"]
    page.click("#btn-step")
    page.wait_for_function(f"window.__app.state.perf.curve.points === {pts + 1}")
    page.click("#tab-open")
    page.wait_for_selector("#panel-open:not(.hidden)")
    assert "tall" not in page.get_attribute(".center", "class")
    page.fill("#buy-tp", "20"); page.dispatch_event("#buy-tp", "change")
    page.fill("#buy-sl", "10"); page.dispatch_event("#buy-sl", "change")

    # Selling everything keeps the last sold bucket as a greyed reference row plus the "Now" row.
    while page.locator("#bucket-list .bucket").count() > 0:
        n = page.locator("#bucket-list .bucket").count()
        page.click("#bucket-list .bucket:nth-child(1) .sell[data-part='1']")
        confirm_reasons(page, ("other",), "clearing out")
        page.wait_for_function(f"document.querySelectorAll('#bucket-list .bucket').length === {n - 1}")
    page.wait_for_selector("#bucket-list tr.ref")
    assert page.locator("#bucket-list tr").count() == 1 and "sold" in page.inner_text("#bucket-list tr.ref")
    assert page.is_hidden("#bucket-empty") and page.is_visible("#bucket-now")
    assert page.inner_text("#bucket-count") == "0"
    assert page.inner_text("#history-count") == page.evaluate("String(window.__app.state.closedBuckets.length)")

    # Sorting: click a History header to sort, click again to flip; the RSI filter hides non-matching rows.
    page.click("#tab-history")
    page.wait_for_selector("#panel-history:not(.hidden)")
    seqs = lambda: page.evaluate("[...document.querySelectorAll('#history-list .bucket td.seq')].map(td => td.textContent)")
    page.click("#history-table thead th[data-key='realized_pnl']")
    assert page.get_attribute("#history-table thead th[data-key='realized_pnl']", "aria-sort") == "ascending"
    pnls = page.evaluate("[...document.querySelectorAll('#history-list .bucket')].map(tr => window.__app.state.closedBuckets.find(b => '#' + b.seq + (b.part ? '.' + b.part : '') === tr.querySelector('td.seq').textContent).realized_pnl)")
    assert pnls == sorted(pnls)
    asc = seqs()
    page.click("#history-table thead th[data-key='realized_pnl']")
    assert page.get_attribute("#history-table thead th[data-key='realized_pnl']", "aria-sort") == "descending"
    if len(set(pnls)) == len(pnls):
        assert seqs() == asc[::-1]
    page.click("#history-table thead th[data-key='seq']")
    assert seqs() == sorted(seqs(), key=lambda t: float(t[1:]))  # #1.1 before #1.2
    n_all = len(seqs())
    n_below = page.evaluate("window.__app.state.closedBuckets.filter(b => b.buy_rsi !== null && b.buy_rsi < 30).length")
    page.click("#history-tools .chip[data-f='below']")
    assert len(seqs()) == n_below and page.get_attribute("#history-tools .chip[data-f='below']", "aria-pressed") == "true"
    if n_below == 0:
        assert "hidden" in page.inner_text("#history-list tr.filtered")
    page.click("#history-tools .chip[data-f='above']")
    n_above = page.evaluate("window.__app.state.closedBuckets.filter(b => b.buy_rsi !== null && b.buy_rsi > 30).length")
    assert len(seqs()) == n_above
    page.click("#history-tools .chip[data-f='all']")
    assert len(seqs()) == n_all
    page.click("#tab-open")

    # Space on a focused button activates the button, not the zoom reset.
    page.keyboard.press("ArrowUp")
    page.wait_for_function("window.__app.chart.targetRange === null")
    zoomed = page.evaluate("window.__app.chart.visibleBars()")
    page.focus("#btn-reset")
    page.keyboard.press("Space")  # confirm() is auto-dismissed by Playwright: reset does not run
    page.wait_for_timeout(200)
    assert page.evaluate("window.__app.chart.visibleBars()") == pytest.approx(zoomed)
    assert page.evaluate("window.__app.state.playing") is False

    shot = Path(os.environ.get("REPLAY_UI_SHOT", tmp_path / "ui.png"))
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))

    assert page.errors == [], page.errors
