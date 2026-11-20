# Supporting market-quality stage

The route API screens crossed books before pricing. After pricing it assesses the
selected fills using visible depth, comparable quotes and available history.
The route card shows structured findings. Older clients can read the warning
summary.

The original risk foundation is reworked for market evidence. Reused primitives
provide validated scores, exact decimal rounding and decision thresholds.
Wallet rules, access restrictions and customer messages are removed. Source and
revision are recorded in `engine/market_risk_core/SOURCE.md`.

## Response

`POST /route` additionally returns `market_quality`:

```json
{
  "status": "caution",
  "risk_score": 35,
  "advisory_only": true,
  "evaluated_at": "2026-11-17T12:00:00Z",
  "methodology": "visible_book_rules_v2",
  "unavailable_signals": [
    "market_trained_anomaly_model", "market_threat_memory", "semantic_triage"
  ],
  "findings": [{
    "code": "WIDE_SPREAD",
    "message": "Visible spread is 10.0 cents.",
    "score": 35,
    "venue": "gemini",
    "contract": "example-contract"
  }]
}
```

The optional model field defaults to null when parsing older route records.
Findings carry venue and contract when book-specific, otherwise those fields are
null. Any findings also produce a readable advisory in `warnings`.

## Evidence and scoring

| Evidence | Score |
| --- | ---: |
| Quote older than 60 seconds, up to 120 seconds | 35 |
| Quote older than 120 seconds | 75 |
| Timestamp more than 5 seconds in the future | 55 |
| Expired contract or crossed book | 100 |
| Spread at least 10 cents | 35 |
| Missing bid or ask, or missing settlement label | 55 |
| Selected books have differing settlement labels | 35 |
| Requested size is only partly fillable | 55 |
| Existing stale detector raises a warning | 55 |
| No selected book evidence | 55 |

The highest finding score determines the route score. Reused deterministic
fusion assigns all weight to observed rules. Unavailable model components have
zero weight. Bands are clear (0–34), caution (35–54), review (55–74), and
unreliable (75–100). These are provisional engineering thresholds, not calibrated
probabilities. Multiple correlated findings do not inflate the score.

Quote age uses `Book.updated`, whose semantics depend on the venue adapter.
Equal settlement labels do not prove identical settlement rules. A clear result
means none of these visible-book checks fired; it does not guarantee execution,
profitability, safety or absence of manipulation. No wallet-trained anomaly model
or threat memory is presented as a market-trained detector.

## Local validation

From the repository root, with requirements installed:

```bash
python -m pytest engine/tests/test_market_quality.py -q
python -m pytest -q
```

Tests cover quote-age boundaries, future timestamps, expiry, crossed/wide and
one-sided books, partial depth, missing settlement evidence, multi-venue routing,
stale findings, score boundaries, invalid numeric scores, unchanged route prices
and API serialization. Tests use prepared data and do not submit trades or
anchor transactions onchain.


### Route screening and equity scenarios

The API removes crossed books before comparing routes. If all matching books are
crossed it returns 422. The assessment of the remaining fills stays advisory.
The engine also reports a 5-cent average depth premium, a 15-cent midpoint
difference between fresh books with identical settlement terms, and unusual
midpoint movement against at least five distinct observations in the preceding
ten minutes. A move must reach both 10 cents and four median absolute deviations.
These thresholds are provisional.

AAPL and NVDA use fixed synthetic stock-price outcome books. The demo clock is
November 19, 2026 at 15:00 UTC, with settlement November 20 at 21:00 UTC.
Apple strikes are $220/$230 and NVIDIA strikes are $170/$180. Use the standard
expiries, ladder and route endpoints with the ticker as the asset. BTC uses its
existing data source. Stock scenarios have illustrative fees, no independent
options benchmark and no live venue listing or tokenized-share ownership.

For live non-BTC routes the same quote checks run on the selected contracts.
The score includes a continuous component capped at 25 points, using the larger of
2.5 points per cent of spread and 25 points per minute of quote age. More severe
findings take precedence. The UI displays the widest spread and oldest observation
alongside the score. This measures execution conditions, not outcome probability.

ETH and SOL use listed Gemini and Kalshi thresholds. Equity routes use open
Polymarket stock-close thresholds and actual YES and NO ask depth. For those
contracts the lower threshold is exclusive and the upper threshold inclusive.
The tokenized equity tabs show a token spot reference alongside a separately
labeled route on the underlying stock. Available dates follow each venue's
catalog. Only ETH and BTC currently have a Deribit options comparison.
