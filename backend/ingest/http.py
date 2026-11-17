"""Shared HTTP layer for the venue adapters: a token-bucket rate limiter and a counting client."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

UA = {"User-Agent": "bet-builder/0.1"}
RETRY_STATUSES = (502, 503, 504)


class RateLimiter:
    """A token bucket shared by every request to one venue.

    Args:
        rate: sustained requests per second.
        burst: bucket capacity, i.e. how many requests may go out back to back. Default 1,
            which spaces requests evenly at 1/rate seconds.
        clock, sleep: injectable for tests.
    """

    def __init__(self, rate: float, burst: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.burst = max(1.0, float(burst))
        self._clock, self._sleep = clock, sleep
        self._tokens = self.burst
        self._at = clock()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until one request may be sent. Waiters are served in order."""
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
                self._at = now
                if self._tokens >= 1.0 - 1e-9:               # tolerance: float error must not cause a re-wait
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return
                await self._sleep((1.0 - self._tokens) / self.rate)

    def penalize(self, seconds: float) -> None:
        """After a 429: send nothing for `seconds` and empty the bucket."""
        self._blocked_until = max(self._blocked_until, self._clock() + max(0.0, seconds))
        self._tokens = 0.0


@dataclass
class ClientStats:
    """Counters since process start. `requests` counts every attempt, including retries."""
    requests: int = 0
    http_429: int = 0
    errors: int = 0


class VenueClient:
    """An async JSON GET client for one venue: rate limited, concurrency capped, retried, and counted.

    Args:
        name: the venue label, e.g. "kalshi".
        base_url: prepended to every path.
        rps: requests per second for the token bucket.
        concurrency: the most requests in flight at once.
        retries: attempts per call. 429s, 502/503/504, timeouts, and connection errors are retried.
        transport: an httpx transport, e.g. `httpx.MockTransport` in tests.
        sleep: injectable for tests.
    """

    def __init__(self, name: str, base_url: str, rps: float, concurrency: int = 4, retries: int = 4,
                 timeout: float = 15.0, transport: Optional[httpx.AsyncBaseTransport] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 limiter: Optional[RateLimiter] = None):
        self.name = name
        self.retries = retries
        self.stats = ClientStats()
        self.limiter = limiter or RateLimiter(rps, sleep=sleep)
        self._sleep = sleep
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(base_url=base_url, headers=UA, timeout=timeout, transport=transport)

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET `path` and return the decoded JSON. Raises the last error once retries run out."""
        err: Exception = RuntimeError("no attempts made")
        for attempt in range(self.retries):
            backoff = 0.5 * 2 ** attempt
            async with self._sem:
                await self.limiter.acquire()
                self.stats.requests += 1
                try:
                    r = await self._client.get(path, params=params)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    self.stats.errors += 1
                    err, r = e, None
            if r is None:
                await self._sleep(backoff)
                continue
            if r.status_code == 429:
                self.stats.http_429 += 1
                err = httpx.HTTPStatusError(f"429 Too Many Requests for {r.url}", request=r.request, response=r)
                wait = _retry_after(r)
                self.limiter.penalize(2 * backoff if wait is None else wait)   # every caller waits
                continue
            if r.status_code in RETRY_STATUSES:
                self.stats.errors += 1
                err = httpx.HTTPStatusError(f"{r.status_code} for {r.url}", request=r.request, response=r)
                await self._sleep(backoff)
                continue
            if r.is_error:
                self.stats.errors += 1
                r.raise_for_status()
            return r.json()
        raise err

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(r: httpx.Response) -> Optional[float]:
    try:
        return min(float(r.headers["retry-after"]), 30.0)
    except (KeyError, ValueError):
        return None
