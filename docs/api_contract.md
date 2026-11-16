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
- `verdict`: `"good deal"`, `"fair"`, `"overpriced"`, `"n/a (too close to expiry)"`, or `null` (no fair value available). The default band is ±2¢ of `fair_value`.
- **Near-expiry cutoff:** under 2 hours to expiry (`RouteConfig.min_fair_hours`), `fair_value` is `null` and `verdict` is `"n/a (too close to expiry)"`. In `/ladder`, every row's `fair` is `null` in the same window.
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

## Snapshot log format (logger → stale replay)

The logger (Person 2) writes **JSON lines**, one record per line, UTF-8. Blank lines and lines starting with `#` are ignored.
`python scripts/replay_stale.py LOG.jsonl` replays a log through the stale detector and prints every flag with its reason.
`engine.stale.replay(records)` does the same from code.

A full sample is in `docs/samples/snapshots_sample.jsonl`. In it, the Gemini 68000 quote freezes while Kalshi and BTC move, so a flag is raised at 15:05 and cleared at 15:07.

Each record has `type` and `ts`, which is ISO 8601 UTC. Records that share the same `ts` form one snapshot: the detector runs once after all of them are applied.

**`book`** (the default if `type` is missing). Write one record per contract per poll. `engine.stale.book_to_record(book, ts)` writes exactly this shape.

```json
{"type":"book","ts":"2026-11-18T15:00:00.000Z","venue":"gemini","contract_id":"GEMI-BTC2611202200-HI68000",
 "asset":"BTC","expiry":"2026-11-20T22:00","kind":"threshold","strike":68000.0,"lo":68000.0,"hi":null,
 "yes_bids":[[0.29,250.0]],"yes_asks":[[0.31,250.0]],
 "settlement_index":"Gemini BTC index (LS-LRRR-XBTUSD)","link":"https://www.gemini.com/predictions/btc-price-on-november-20"}
```

| field | type | notes |
|---|---|---|
| `venue` | string | `"gemini"` or `"kalshi"` |
| `contract_id` | string | venue symbol; history is keyed by this |
| `asset` | string | `"BTC"` |
| `expiry` | string | minute-precision UTC key |
| `kind` | string | `"threshold"` (YES pays if S ≥ strike) or `"bucket"` (lo ≤ S < hi) |
| `strike`, `lo`, `hi` | number or null | in $. Kalshi strikes are already normalized: 67999.99 → 68000 |
| `yes_bids` | [[price, size], ...] | in $/contract and contracts, best first. For Kalshi, these are the YES bids |
| `yes_asks` | [[price, size], ...] | in $/contract and contracts, best first. For Kalshi, YES ask = 1 − NO bid |
| `settlement_index`, `link` | string | optional |

**`spot`**: one record per poll. This feeds rule (a), "BTC moved > 30 bp".

```json
{"type":"spot","ts":"2026-11-18T15:00:00.000Z","asset":"BTC","price":65000}
```

**`fair`**: optional. Write it whenever the ladder's fair values are computed. It feeds rule (b), "the other venue agrees with fair". Without `fair` records, only rule (a) can fire.

```json
{"type":"fair","ts":"2026-11-18T15:06:00.000Z","asset":"BTC","expiry":"2026-11-20T22:00","strike":68000,"p_above":0.215}
```

Rule (a) needs at least `frozen_window_s` (default 5 min) of history, so poll at least once a minute.
`python scripts/live_demo.py --loops N --interval 60 --record LOG.jsonl` writes `book` and `spot` records in this format.
