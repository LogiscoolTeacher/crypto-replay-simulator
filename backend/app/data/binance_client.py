"""Thin client for Binance's public spot market-data REST API.

Only unauthenticated endpoints are used (no API key). The client:
  * retries transient failures (network errors, 5xx) with exponential backoff,
  * honours 429 / 418 responses and their Retry-After header,
  * watches the X-MBX-USED-WEIGHT-1M header and pauses before the 6000/min cap.
"""
from __future__ import annotations

import logging
import time
from typing import NamedTuple

import requests

log = logging.getLogger(__name__)

BASE_URLS = ("https://api.binance.com", "https://data-api.binance.vision")
MAX_KLINES_PER_REQUEST = 1000
WEIGHT_LIMIT_PER_MINUTE = 6000


class BinanceError(Exception):
    """Raised when Binance returns an error payload or retries are exhausted."""


class Kline(NamedTuple):
    open_time: int  # ms since epoch, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float  # base asset units
    close_time: int  # ms since epoch, UTC; equals open_time + interval - 1
    taker_buy: float | None = None  # base volume bought by takers (market buys); volume delta = 2 x this - volume


class BinanceClient:
    def __init__(
        self,
        base_url: str = BASE_URLS[0],
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_retries: int = 6,
        soft_weight_cap: int = 4800,
        min_spacing_s: float = 0.05,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.soft_weight_cap = soft_weight_cap
        self.min_spacing_s = min_spacing_s
        self._last_request_at = 0.0
        self.used_weight_1m = 0

    # ------------------------------------------------------------------ public
    def server_time_ms(self) -> int:
        data = self._get("/api/v3/time")
        return int(data["serverTime"])

    def klines(
        self,
        symbol: str,
        binance_interval: str,
        start_ms: int,
        end_ms: int | None = None,
        limit: int = MAX_KLINES_PER_REQUEST,
    ) -> list[Kline]:
        """Return up to `limit` candles whose open time lies in [start_ms, end_ms].

        Binance returns the oldest candles first. The last one may still be
        forming; callers decide whether to keep it (see CandleStore).
        """
        if not 1 <= limit <= MAX_KLINES_PER_REQUEST:
            raise ValueError(f"limit must be 1..{MAX_KLINES_PER_REQUEST}, got {limit}")
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": binance_interval,
            "startTime": int(start_ms),
            "limit": limit,
        }
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        raw = self._get("/api/v3/klines", params=params)
        if not isinstance(raw, list):
            raise BinanceError(f"Unexpected klines payload: {raw!r}")
        out: list[Kline] = []
        for row in raw:
            # Row layout documented by Binance: [openTime, o, h, l, c, v, closeTime, quoteVolume,
            # trades, takerBuyBaseVolume, ...]
            out.append(
                Kline(
                    open_time=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time=int(row[6]),
                    taker_buy=float(row[9]) if len(row) > 9 else None,
                )
            )
        return out

    # ----------------------------------------------------------------- private
    def _throttle(self) -> None:
        wait = self.min_spacing_s - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        if self.used_weight_1m >= self.soft_weight_cap:
            # Weight resets at the top of each UTC minute.
            pause = 60 - (time.time() % 60) + 0.5
            log.warning("Binance weight %s near cap; pausing %.1fs", self.used_weight_1m, pause)
            time.sleep(pause)
            self.used_weight_1m = 0

    def _get(self, path: str, params: dict | None = None):
        url = self.base_url + path
        backoff = 1.0
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:  # network-level failure
                last_err = exc
                log.warning("Binance request failed (%s/%s): %s", attempt, self.max_retries, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            finally:
                self._last_request_at = time.monotonic()

            used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
            if used and used.isdigit():
                self.used_weight_1m = int(used)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code in (429, 418):
                retry_after = float(resp.headers.get("Retry-After", "0") or 0)
                pause = max(retry_after, backoff)
                log.warning("Binance rate limit %s; sleeping %.1fs", resp.status_code, pause)
                time.sleep(pause)
                backoff = min(backoff * 2, 60)
                last_err = BinanceError(f"HTTP {resp.status_code} rate limited")
                continue

            if 500 <= resp.status_code < 600:
                last_err = BinanceError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            # 4xx other than rate limiting is a caller error; do not retry.
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text[:200]
            raise BinanceError(f"HTTP {resp.status_code} for {path} {params}: {payload}")

        raise BinanceError(f"Giving up on {path} after {self.max_retries} attempts: {last_err}")
