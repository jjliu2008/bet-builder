"""bet-builder engine: route pricing, fair value and stale detection for crypto prediction markets.

Typical use from the API layer:

    from engine import Book, RouteRequest, price_route, build_ladder
    route = price_route(RouteRequest(expiry="2026-11-20T22:00", type="above", lo=68000), books)
"""
from .books import Book, Fill, Level, cost_to_buy, expiry_key
from .fairvalue import (Surface, build_surface, fair_above, fair_below, fair_for_request,
                        fair_range, get_surface, make_fair_fn, verdict)
from .fees import order_fee, taker_fee
from .routes import (Leg, Route, RouteConfig, RouteError, RouteRequest, StrikeNotFound,
                     build_ladder, price_route, tile_buckets)

__all__ = [
    "Book", "Fill", "Level", "cost_to_buy", "expiry_key", "order_fee", "taker_fee",
    "Leg", "Route", "RouteConfig", "RouteError", "RouteRequest", "StrikeNotFound",
    "build_ladder", "price_route", "tile_buckets",
    "Surface", "build_surface", "fair_above", "fair_below", "fair_for_request", "fair_range",
    "get_surface", "make_fair_fn", "verdict",
]
