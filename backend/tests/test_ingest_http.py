"""Offline tests for the rate limiter and the counting, retrying venue client."""
import asyncio

import httpx
import pytest

from backend.ingest.http import RateLimiter, VenueClient


class FakeTime:
    """A virtual clock: sleeping advances it instantly."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    async def sleep(self, d):
        self.sleeps.append(d)
        self.t += d


def client_for(handler, ft, rps=1000.0, retries=4):
    return VenueClient("kalshi", "https://venue.test", rps, retries=retries,
                       transport=httpx.MockTransport(handler), sleep=ft.sleep,
                       limiter=RateLimiter(rps, clock=ft.clock, sleep=ft.sleep))


def run(coro):
    return asyncio.run(coro)


# ---- token bucket ------------------------------------------------------------------------
def test_limiter_spaces_requests_at_the_rate():
    ft = FakeTime()
    lim = RateLimiter(10, clock=ft.clock, sleep=ft.sleep)

    async def go():
        stamps = []
        for _ in range(21):
            await lim.acquire()
            stamps.append(ft.t)
        return stamps

    stamps = run(go())
    assert stamps[0] == 0.0
    assert stamps[-1] == pytest.approx(2.0)                      # 20 more requests at 10 per second
    assert all(b - a == pytest.approx(0.1) for a, b in zip(stamps, stamps[1:]))


def test_limiter_burst_then_steady():
    ft = FakeTime()
    lim = RateLimiter(10, burst=5, clock=ft.clock, sleep=ft.sleep)

    async def go():
        stamps = []
        for _ in range(7):
            await lim.acquire()
            stamps.append(round(ft.t, 6))
        return stamps

    assert run(go()) == [0, 0, 0, 0, 0, 0.1, 0.2]


def test_limiter_is_shared_by_concurrent_callers():
    ft = FakeTime()
    lim = RateLimiter(5, clock=ft.clock, sleep=ft.sleep)

    async def go():
        await asyncio.gather(*(lim.acquire() for _ in range(11)))

    run(go())
    assert ft.t == pytest.approx(2.0)                            # 11 callers, one bucket


def test_limiter_penalize_blocks_everyone():
    ft = FakeTime()
    lim = RateLimiter(100, clock=ft.clock, sleep=ft.sleep)

    async def go():
        await lim.acquire()
        lim.penalize(3.0)
        await lim.acquire()

    run(go())
    assert ft.t >= 3.0


def test_limiter_rejects_bad_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)


# ---- client ------------------------------------------------------------------------------
def test_get_counts_requests():
    ft = FakeTime()
    c = client_for(lambda r: httpx.Response(200, json={"path": r.url.path, "q": r.url.params["a"]}), ft)
    assert run(c.get("/x", {"a": "1"})) == {"path": "/x", "q": "1"}
    assert (c.stats.requests, c.stats.http_429, c.stats.errors) == (1, 0, 0)


def test_429_is_counted_retried_and_honors_retry_after():
    ft, calls = FakeTime(), []

    def handler(r):
        calls.append(ft.t)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    c = client_for(handler, ft)
    assert run(c.get("/x")) == {"ok": True}
    assert (c.stats.requests, c.stats.http_429) == (3, 2)
    assert calls[1] - calls[0] >= 2.0 and calls[2] - calls[1] >= 2.0


def test_429_without_retry_after_backs_off_exponentially():
    ft, calls = FakeTime(), []

    def handler(r):
        calls.append(ft.t)
        return httpx.Response(429) if len(calls) < 4 else httpx.Response(200, json={})

    c = client_for(handler, ft)
    run(c.get("/x"))
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert gaps == sorted(gaps) and gaps[0] >= 1.0 and gaps[2] >= 4.0


def test_429_gives_up_after_retries():
    ft = FakeTime()
    c = client_for(lambda r: httpx.Response(429, headers={"Retry-After": "0"}), ft, retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        run(c.get("/x"))
    assert (c.stats.requests, c.stats.http_429) == (3, 3)


def test_5xx_gateway_errors_and_timeouts_are_retried():
    ft, n = FakeTime(), {"i": 0}

    def handler(r):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(503)
        if n["i"] == 2:
            raise httpx.ConnectTimeout("slow", request=r)
        return httpx.Response(200, json={"n": n["i"]})

    c = client_for(handler, ft)
    assert run(c.get("/x")) == {"n": 3}
    assert (c.stats.requests, c.stats.errors, c.stats.http_429) == (3, 2, 0)


def test_other_http_errors_raise_at_once():
    ft = FakeTime()
    c = client_for(lambda r: httpx.Response(404), ft)
    with pytest.raises(httpx.HTTPStatusError):
        run(c.get("/x"))
    assert (c.stats.requests, c.stats.errors) == (1, 1)


def test_concurrency_cap():
    ft, live, peak = FakeTime(), {"n": 0}, {"n": 0}

    async def handler(r):
        live["n"] += 1
        peak["n"] = max(peak["n"], live["n"])
        await asyncio.sleep(0)
        live["n"] -= 1
        return httpx.Response(200, json={})

    async def go():
        c = VenueClient("kalshi", "https://venue.test", 1e6, concurrency=3,
                        transport=httpx.MockTransport(handler))
        await asyncio.gather(*(c.get("/x") for _ in range(20)))
        return c.stats.requests

    assert run(go()) == 20
    assert 1 <= peak["n"] <= 3
