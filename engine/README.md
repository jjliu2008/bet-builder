# engine/

This is the pricing core of bet-builder. It is a pure Python package that the FastAPI backend imports. It does no I/O, except for:
- `dev_fetch.py`, which holds dev and test fetchers;
- `fairvalue.fetch_deribit_rows`, the default Deribit fetcher, which you can override.

| module | what it does |
|---|---|
| `fees.py` | Taker fee per order, rounded up to the cent (with a flag to disable rounding). |
| `books.py` | `Level`, `Book` (normalized YES book with outcome bounds [lo, hi)), and `cost_to_buy` (walks the levels). |
| `routes.py` | `RouteRequest`, then `price_route`, then `Route`. Also `build_ladder` for GET /ladder. |
| `fairvalue.py` | Deribit to SVI per expiry, interpolated in time, giving `fair_above` / `fair_below` / `fair_range`, and `verdict`. |
| `stale.py` | `stale_flags` / `make_stale_fn` (rule-based, with reasons) and the `History` store. |
| `dev_fetch.py` | Public Gemini, Kalshi, and Deribit fetchers plus expiry matching. These are for tests and the demo only; the production ingest is in backend/. |

```python
from engine import RouteRequest, price_route, build_ladder, get_surface, make_fair_fn, fair_above
from engine.stale import make_stale_fn, stale_flags

surface = get_surface("BTC")                         # cached 60 s
fair = fair_above("BTC", expiry, strikes, surface)   # {K: P(S_T >= K)}
route = price_route(RouteRequest(expiry=expiry, type="between", lo=64000, hi=66000, size=10),
                    books, fair_fn=make_fair_fn(surface),
                    stale_fn=make_stale_fn(books, expiry, fair, quote_history, spot_history))
ladder = build_ladder(books, "BTC", expiry, fair, stale_flags(books, expiry, fair, quote_history, spot_history))
```

Units:
- Prices and costs are in **$ per contract**, and a contract pays $1.
- Strikes are in $.
- Sizes are in contracts.
- Times are aware UTC datetimes.
- Expiry keys are minute-precision strings, `2026-11-20T22:00`.

## The math

### Fees

Both venues charge the same taker fee on each order:

```
fee = ceil_to_cent(0.07 × contracts × P × (1 − P))
```

- **Rounding is per order.** When an order sweeps several levels, the exact fees are summed first and rounded once.
- **Small orders pay a lot.** For example, 1 contract at 0.24 pays 2¢ (exact 1.28¢), which is 8% of the price.
- **The flag.** `RouteConfig(round_fees=False)` turns rounding off for per-contract comparisons.
- **Splitting.** Because P(1−P) rises with P, cheaper levels also have cheaper marginal fees. A greedy cheapest-first split is therefore optimal, apart from rounding. To cover rounding, `buy_across` also prices each single-venue fill and keeps the cheapest complete option.

### Normalized books

Each book holds YES bids and asks. **Buying NO costs 1 − YES bid** and fills against the YES bid depth. Kalshi publishes YES bids and NO bids, so its YES ask is 1 − the best NO bid. Outcome bounds:

| contract | YES pays when | normalization |
|---|---|---|
| Gemini `HI68000`, Kalshi `KXBTCD … T67999.99` | S ≥ 68000 | Kalshi floor + 0.01 |
| Kalshi `KXBTC … B65250` (between) | 65000 ≤ S < 65500 | cap + 0.01 |
| Kalshi `KXBTC … T57000` (less) | S < 57000 | — |
| Kalshi `KXBTC … T80999.99` (greater) | S ≥ 81000 | floor + 0.01 |

### Builds

- **above K**: buy YES at K. **below K**: buy NO at K. Either order can be split across Gemini and Kalshi when one book is too thin. A Kalshi bucket tiling of [K, ∞) or (−∞, K) is also priced, but it rarely wins because each bucket pays its own rounded fee.
- **between [a, b)**: the engine prices two kinds of build.
  1. **Bucket tiling.** Kalshi buckets that exactly tile [a, b), with no gaps or overlaps. Cost = Σ YES cost of each bucket.
  2. **Two-leg build: YES(≥a) + NO(≥b).** Each leg comes from whichever venue is cheapest, and each leg can be split.

     | where S_T lands | YES(≥a) pays | NO(≥b) pays | total |
     |---|---|---|---|
     | S < a | 0 | 1 | **$1** |
     | a ≤ S < b | 1 | 1 | **$2** |
     | S ≥ b | 1 | 0 | **$1** |

     $1 is guaranteed, and the range itself is worth one extra $1. So the **effective cost = cost(YES a) + cost(NO b) − 1**.

     The route reports this as `total_cost`. It also reports `cash_outlay`, which is the real money you pay, and `guaranteed_payout = 1`.
- **Comparisons.** `best_single_venue_cost` is the cheapest *complete* build that uses one venue only. `savings` = that − `total_cost`.
- **Partial fills.** If the book is too thin for the requested size, the route fills what it can and adds a warning.
- **Bad strikes.** An unlisted strike raises `StrikeNotFound` with `.nearest`.

### Fair value: from SVI to a digital

1. Deribit `get_book_summary_by_currency` provides, for each option, `mark_iv` (in %) and `underlying_price` (F).
2. The options are grouped by expiry, which is 08:00 UTC. Only **OTM** options are kept: calls with K ≥ F and puts with K < F.
3. For each option, compute k = ln(K/F) and w = (mark_iv/100)² · τ, with τ in years and 365 days per year.
4. Fit raw SVI, w(k) = a + b(ρ(k−m) + √((k−m)² + σ²)), using bounded least squares from several starting points.
   - Points are weighted by 1/(1 + (k/2σ_ATM)²), so the near-money smile is fitted first. That is where the prediction-market strikes are.
   - Live near-ATM fit error is under 1 vol point.
   - If an expiry has fewer than 5 points, the engine linearly interpolates w(k) instead of fitting.
5. For a target expiry T:
   - **Between two Deribit expiries:** interpolate F and total variance w linearly in τ, at the same k = ln(K/F_T).
   - **Before the first Deribit expiry:** use w_first(k) · τ_T/τ_first.
   - **After the last Deribit expiry:** use w_last(k) · τ_T/τ_last.
6. Compute **P(S_T ≥ K) = −∂C/∂K** of an undiscounted Black call, using each strike's own smile variance. The derivative is a central difference with h = 0.1% of K.
   - Because each strike uses its own smile variance, the skew term (−vega · ∂σ/∂K) is included. It typically adds several cents near the money.
   - The result is clipped to [0, 1] and forced to be non-increasing in K.
7. Derived quantities:
   - below = 1 − above
   - range [a, b) = above(a) − above(b)
8. The fitted surface is cached for 60 s.

`verdict`:
- **"good deal"** if cost ≤ fair − 2¢
- **"overpriced"** if cost ≥ fair + 2¢
- **"fair"** otherwise

Both thresholds are configurable.

### Stale detector

A threshold quote is flagged when **|mid − other venue's mid| > 4¢** and at least one of these holds:

- **(a) Frozen.** Its mid hasn't changed for 5 min, according to the supplied history, while BTC's high-low range over those 5 min was > 30 bp.
  - With no history for the contract, the book's `updated` timestamp is used instead.
  - With under 5 min of history, rule (a) never fires.
- **(b) Off fair.** The other venue's mid is within 2¢ of fair and this one isn't.

Every flag carries a reason string, for example: `Gemini mid 0.300 is 9.0¢ from Kalshi's 0.210; Kalshi is within 2¢ of fair 0.220 but this quote is 8.0¢ away`. All thresholds are in `StaleConfig`. The history is an argument, `{contract_id: [(t, mid)]}`, and the backend logger supplies it. `History` is a minimal in-memory version.

## Known limits

- **Mixed legs settle on different indexes.**
  - Gemini settles on its own BTC index (LS-LRRR-XBTUSD). Kalshi settles on the CF Benchmarks BRTI 60-second average. Deribit fair value uses Deribit's index and futures.
  - A mixed two-leg build can mis-pay if BTC finishes within a few dollars of a strike and the indexes disagree. Routes flag this with `mixed_settlement_index`.
  - Single-venue alternatives are always reported in `alternatives`.
- **Gemini books are thin.** Gemini books are often only 250 contracts deep at the top. Larger sizes walk the book or split to Kalshi.
- **Visible depth only.** Only the top 10 levels per side are fetched, so large sizes may be under-filled.
- **Quotes move between quote and fill.** Routes are snapshots: there is no locking and no atomic multi-leg execution. Leg risk is real, because one leg can fill and the other move. Prices can move between the API call and the click.
- **Fair value near expiry is crude.**
  - The first Deribit expiry is 08:00 UTC. A bet expiring in minutes or hours uses a √τ-scaled smile from that expiry, with no intraday or event vol term structure.
  - This ignores Kalshi's 60-second averaging and index basis. In the final minutes, trust the venues over the model.
- **Simplified pricing.** There is no discounting (prediction contracts settle within days) and no funding or basis adjustment beyond Deribit's per-expiry `underlying_price`.
- **Simple stale rules.** The rules are deliberately simple. A real repricing can trip rule (b) if Deribit is the one that lags.
- **No ranges from one-sided strikes.** Two-leg builds need both strikes listed as thresholds; bucket tiling needs Kalshi buckets on exactly those edges. Gemini strikes are every $500 or $250, and Kalshi buckets are $500 wide (offset by $250 on some expiries).
