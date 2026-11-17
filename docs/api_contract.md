# API contract

Served by the FastAPI backend (Person 2), with shapes produced by the `engine` package.
Every endpoint uses public venue data, so no API keys are needed.

**Units.** All prices are dollars per contract (0 to 1). A contract pays $1 if it wins.
Costs are per contract and include fees unless noted otherwise. Times are ISO 8601 UTC.
Expiries use minute precision, for example `2026-11-20T22:00`.

## Changelog

This section describes the fields as currently served by `integration` / `master`.

- **2026-11-17: neutral verdict labels.**
  - `/route` no longer returns `verdict` (`"good deal"` / `"fair"` / `"overpriced"` / `"n/a (too close to expiry)"`). It returns three fields instead:
    - `verdict_code`: `"options_higher"` | `"in_line"` | `"options_lower"` | `"unavailable"`
    - `verdict_text`: one line, e.g. `"Options market prices this 6¢ higher than the best venue price"`
    - `fair_note`: a caveat string, or null when there is no comparison
  - Old-to-new mapping: good deal → `options_higher`, fair → `in_line`, overpriced → `options_lower`, n/a or null → `unavailable`.
- **2026-11-17: persistent onchain receipts.**
  - Receipts are anchored on Solana devnet (Memo program) when a signer is configured, and stored in SQLite or Postgres, so they survive restarts.
  - Added `POST /verify` {request, route, signature} for verifying without the server's database, and `GET /anchors` for hourly snapshot Merkle roots.
- **2026-11-17: receipts and health.**
  - Added `POST /receipt` and `GET /verify/{route_id}` (see below).
  - Receipt fields: `route_id`, `hash` (SHA-256 of `{request, route}`), `signature`, `explorer_url`, `status` (`"confirmed"` | `"pending"`), `created_at`, `error`.
  - Verify fields: `route_id`, `status` (`"verified"` | `"mismatch"` | `"pending"`), `hash`, `signature`, `explorer_url`, `detail`.
  - Added `GET /health`.
  - CORS origins come from the backend's `ALLOWED_ORIGINS`.
- **2026-11-16: near-expiry cutoff.**
  - Under 2 hours to expiry, `/route` returns `fair_value: null` and `verdict_code: "unavailable"`, and `/ladder` rows return `fair: null`.
  - The snapshot log format was added for the logger and replay.
- **Current verdict fields:**
  - `verdict_code` is `"options_higher"` when `fair_value` ≥ `total_cost` + 2¢, `"options_lower"` when `fair_value` ≤ `total_cost` − 2¢, and `"in_line"` otherwise.
  - It is `"unavailable"` under 2 hours to expiry or when no fair value is available.
  - `verdict_text` explains the code in one line.
  - `fair_note` is set whenever a comparison was made.
- **`/route` fields:**
  - Contract fields: `legs[]` {`venue`, `contract`, `side`, `price`, `fee`, `size_available`, `link`}, `total_cost`, `max_payout`, `best_single_venue_cost`, `savings`, `fair_value`, `verdict_code`, `verdict_text`, `fair_note`, `stale_warnings`, `mixed_settlement_index`, `timestamp`.
  - Engine additions: `legs[]` {`size`, `fee_total`, `strike_lo`, `strike_hi`}, `build`, `cash_outlay`, `guaranteed_payout`, `size`, `filled_size`, `alternatives[]` {`build`, `venues`, `total_cost`, `cash_outlay`, `filled_size`, `complete`}, `warnings`.
- **`/ladder` row fields:**
  - `strike`
  - `gemini` / `kalshi`: {`bid`, `ask`, `bid_size`, `ask_size`, `updated`} or null
  - `fair`
  - `stale` {`gemini`, `kalshi`}
  - `stale_reasons` {`gemini`, `kalshi`}

## GET /health

This endpoint always returns HTTP 200 while the process is up. Read `status` for data health:
- `"starting"` until the first poll completes.
- `"ok"` while every source is fresh.
- `"degraded"` once any source is older than 3 poll intervals.

Other fields are `mode` (`"live"` or `"replay"`), `replay`, `poll_interval_s`, `last_poll`, `polls`, `last_poll_duration_s`, `loop_errors`, `venues` {gemini, kalshi, deribit}, `expiries`, `match`, and `snapshot_log`.

While `status` is `"starting"`, the data endpoints return 503.

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
  "fair_value": 0.27, "verdict_code": "options_lower",
  "verdict_text": "Options market prices this 4¢ lower than the best venue price",
  "fair_note": "Options prices include a premium for crash protection, so treat this as a reference, not the true odds.",
  "stale_warnings": [], "mixed_settlement_index": true, "timestamp": "ISO" }
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
- `verdict_code`: where the options market prices the outcome relative to our cost. The band is 2¢, configurable via `RouteConfig.higher_threshold` / `lower_threshold`.
  - `"options_higher"`: `fair_value` ≥ `total_cost` + 2¢.
  - `"in_line"`: `fair_value` is within 2¢ of `total_cost`.
  - `"options_lower"`: `fair_value` ≤ `total_cost` − 2¢.
  - `"unavailable"`: there is no fair value, or the expiry is too close.
  - The label is deliberately neutral. It doesn't say the venue price is "good" or "bad", because options-implied odds include a crash-protection premium.
- `verdict_text`: a one-line explanation of `verdict_code`, e.g. `"Options market prices this 6¢ higher than the best venue price"`.
- `fair_note`: a caveat string shown with any comparison, or `null` when `verdict_code` is `"unavailable"`.
- **Near-expiry cutoff:** under 2 hours to expiry (`RouteConfig.min_fair_hours`), `fair_value` is `null`, `verdict_code` is `"unavailable"`, and `fair_note` is `null`. In `/ladder`, every row's `fair` is `null` in the same window.
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

## POST /receipt

This endpoint fingerprints a route and anchors the fingerprint on Solana devnet, as a Memo program transaction with memo `better:v1:<hash>`, when the server has a signer configured (`SOLANA_KEYPAIR`).

Body: `{"request": RouteRequest | null, "route": Route}`. A bare `Route` also works, for backward compatibility. An invalid body returns 422. Posting the same route again returns the same receipt; if that receipt is still pending, anchoring is retried first.

The hash is SHA-256 over the canonical JSON (sorted keys, no spaces) of `{"request": ..., "route": ...}`, after both are normalized by the API models.

```json
{"route_id": "3f1c9a…(24 hex)", "hash": "3f1c9a…(64 hex sha256)", "signature": null,
 "explorer_url": null, "status": "pending", "created_at": "2026-11-17T19:02:11Z",
 "error": "Onchain receipts are not configured on this server"}
```

`status` is `"confirmed"` only when an onchain transaction was written. In that case, `signature` and `explorer_url` are set. `"pending"` means the fingerprint is saved but not onchain, and `error` says why. A pending receipt never affects the route.

Receipts are stored in the server's database (SQLite at `RECEIPTS_DB`, or Postgres via `DATABASE_URL`), so they survive restarts. At startup, the server retries any receipts that are still pending, in the background.

## GET /verify/{route_id}

```json
{"route_id": "3f1c9a…", "status": "pending", "hash": "3f1c9a…", "signature": null,
 "explorer_url": null, "detail": "Onchain receipts are not configured on this server"}
```

`status` values:
- `"verified"`: the stored route still hashes to `hash`, and the chain confirms the transaction records it.
- `"mismatch"`: either check fails.
- `"pending"`: the hash is intact, but nothing onchain can be checked against it.

An unknown `route_id` returns 404. In that case, use `POST /verify`.

## POST /verify

This endpoint verifies a route straight against the chain. The proof lives onchain, so it works even if this server's database is gone.

Body: `{"request": RouteRequest | null, "route": Route, "signature": "<tx signature>"}`. Send exactly the request and route that were receipted. The server recomputes the hash and checks that transaction `signature` carries the memo `better:v1:<hash>`. A missing `signature` or `route` returns 422.

```json
{"route_id": "3f1c9a…", "hash": "3f1c9a…", "signature": "5Kd…", "status": "verified",
 "explorer_url": "https://explorer.solana.com/tx/5Kd…?cluster=devnet", "detail": null}
```

`status` is `"verified"` or `"mismatch"`, or `"pending"` if the chain could not be read.

## GET /anchors

This endpoint returns the latest 48 hourly snapshot anchors, newest first. Each one is a SHA-256 Merkle root of one UTC hour of snapshot-log lines, written onchain with memo `better:v1:snap:<YYYYMMDDHH>:<root>:<count>`:

```json
[{"hour": "2026111720", "root": "9a0c…(64 hex)", "count": 4180, "signature": "3xQ…",
  "explorer_url": "https://explorer.solana.com/tx/3xQ…?cluster=devnet", "created_at": "2026-11-17T21:00:04+00:00"}]
```

`signature` and `explorer_url` are null if anchoring failed, for example with no signer configured. The list is empty if the snapshot logger is disabled.

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
