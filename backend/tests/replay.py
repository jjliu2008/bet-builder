"""Serve recorded venue responses to the ingest code, with no network.

`fixtures/recorded.json` is written by `scripts/record_ingest_fixtures.py`:
    {"meta": {"recorded_at": ISO, "spot": float, "targets": [expiry keys]}, "responses": {key: json}}
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

from engine.books import to_utc

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "recorded.json")


def key_for(url: httpx.URL) -> str:
    """Return a stable key for a request: host, path, and the query sorted by name."""
    return f"{url.host}{url.path}?{urlencode(sorted(url.params.multi_items()))}"


def load(path: str = FIXTURE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Replay:
    """An httpx mock that answers from recorded responses and counts what was asked.

    Attributes:
        hits: Counter of request keys.
        fail: an optional `fn(request) -> httpx.Response | None`. A returned response is sent
            instead of the recording; raise inside it to simulate a connection error.
    """

    def __init__(self, data: Optional[dict] = None):
        self.data = data or load()
        self.meta = self.data["meta"]
        self.responses = self.data["responses"]
        self.hits: Counter = Counter()
        self.fail: Optional[Callable[[httpx.Request], Optional[httpx.Response]]] = None

    @property
    def now(self) -> datetime:
        return to_utc(self.meta["recorded_at"])

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = key_for(request.url)
        self.hits[key] += 1
        if self.fail is not None:
            r = self.fail(request)
            if r is not None:
                return r
        if key not in self.responses:
            return httpx.Response(404, json={"error": f"not recorded: {key}"})
        return httpx.Response(200, json=self.responses[key])

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def count(self, fragment: str) -> int:
        """Return how many requests had `fragment` in their key."""
        return sum(n for k, n in self.hits.items() if fragment in k)
