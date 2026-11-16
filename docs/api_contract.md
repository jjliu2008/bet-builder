# API contract

Served by the FastAPI backend (Person 2), with shapes produced by the `engine` package.
Every endpoint uses public venue data, so no API keys are needed.

**Units.** All prices are dollars per contract (0 to 1). A contract pays $1 if it wins.
Costs are per contract and include fees unless noted otherwise. Times are ISO 8601 UTC.
Expiries use minute precision, for example `2026-11-20T22:00`.

## GET /expiries?asset=BTC

Returns the expiries listed on both Gemini and Kalshi, sorted ascending.

```json
["2026-11-16T22:00", "2026-11-17T22:00", "2026-11-20T22:00"]
```

## GET /ladder?asset=BTC&expiry=2026-11-20T22:00

Returns one row per strike K. Each row covers the threshold contract "BTC ≥ K at expiry".
A venue's field is `null` when that venue doesn't list the strike.
Produced by `engine.build_ladder(...)`.

```json
[{ "strike": 68000,
   "gemini": {"bid":0.21,"ask":0.24,"bid_size":250,"ask_size":250,"updated":"ISO"},
   "kalshi": {"bid":0.26,"ask":0.27,"bid_size":900,"ask_size":400,"updated":"ISO"},
   "fair": 0.23, "stale": {"gemini": false, "kalshi": false} }]
```

Engine additions, which are optional for the frontend to read:
- `stale_reasons`: `{"gemini": null | "reason string", "kalshi": ...}`

## POST /route

Request body (`engine.RouteRequest`):

```json
{"asset": "BTC", "expiry": "2026-11-20T22:00", "type": "above" | "below" | "between",
 "lo": 66000, "hi": 68000, "size": 10}
```

- `above`: strike K = `lo`, or `hi` if `lo` is null.
- `below`: strike K = `hi`, or `lo` if `hi` is null.
- `between`: the range [lo, hi), with lo < hi.
- `size`: number of contracts (= $ of max payout). Defaults to 1.

Response (`engine.Route`):

```json
{ "legs": [{"venue":"gemini","contract":"GEMI-...","side":"YES","price":0.24,"fee":0.0128,"size_available":250,"link":"..."}],
  "total_cost": 0.31, "max_payout": 1.0, "best_single_venue_cost": 0.36, "savings": 0.05,
  "fair_value": 0.27, "verdict": "overpriced", "stale_warnings": [], "mixed_settlement_index": true, "timestamp": "ISO" }
```

Field semantics:
- `legs[].price`: the average fill price for this leg, in $/contract, before fees.
- `legs[].fee`: the taker fee per contract for this leg. The fee is computed per order and rounded up to the next cent, then divided by the leg size.
- `legs[].size_available`: the total visible depth on that side of that book, in contracts.
- `total_cost`: the **effective** cost per $1 of payout in the requested outcome, including fees.
  A two-leg range build, YES(≥a) + NO(≥b), pays $2 inside the range and $1 outside it.
  Its effective cost is therefore the cash outlay minus the guaranteed $1.
- `max_payout`: always 1.0, the per-contract payout on top of anything guaranteed.
- `best_single_venue_cost`: the cheapest effective cost using only one venue. It is `null` if no single venue can fill the order.
- `savings`: `best_single_venue_cost - total_cost`. It is `null` when the former is null.
- `fair_value`: the Deribit-implied probability of the outcome. It is `null` if fair value is unavailable.
- `verdict`: `"good deal"`, `"fair"`, `"overpriced"`, or `null`. The default band is ±2¢ of `fair_value`.
- `mixed_settlement_index`: true when the legs settle on different venues' indexes. Gemini uses its own index; Kalshi uses CF Benchmarks BRTI, 60-second average.

Engine additions, which are optional:
- `legs[].size`: the contracts bought on this leg.
- `legs[].fee_total`: the $ fee for the leg's order.
- `legs[].strike_lo`, `legs[].strike_hi`: the leg's outcome bounds. Null means unbounded.
- `build`: `"single"`, `"two_leg"`, or `"buckets"`.
- `cash_outlay`: the $ per contract actually paid, including fees.
- `guaranteed_payout`: $1 for a two-leg build, otherwise 0.
- `size`, `filled_size`: the requested size versus the size that the visible books can fill.
- `alternatives`: a summary of every candidate build that was considered.
- `warnings`: human-readable notes, for example partial fills.

Errors: an unlisted strike raises `engine.StrikeNotFound`, with a `.nearest` list of valid strikes. The server should map it to HTTP 422.

```json
{"detail": "Strike 68100 is not listed for BTC 2026-11-20T22:00. Nearest listed strikes: 68000, 68500", "nearest": [68000, 68500]}
```
