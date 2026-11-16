# bet-builder

**Say the BTC bet you want. We build it from the cheapest mix of Gemini Predictions and Kalshi contracts, show the Deribit options-implied fair value, and flag stale quotes.**

UBC 2026 Hackathon project.

## Repo layout

```
engine/            Python package (engine lead): fees, normalized books, route pricing,
                   fair value from Deribit, stale-quote detector
  tests/           pytest suite (offline, fixture books)
backend/           FastAPI server, ingest, contract matcher, logger (Person 2)
frontend/          Next.js UI (Person 3)
reference/         prior research code for reuse (not imported)
scripts/
  live_demo.py     pulls live public books and prints routes, fair values, stale flags
docs/
  api_contract.md  endpoint shapes shared by engine, backend and frontend
```

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pytest
python scripts/live_demo.py      # needs internet; all endpoints are public, no keys
```

## Architecture overview

1. **Ingest.** Pull public books from Gemini Predictions and Kalshi, and the option surface from Deribit.
2. **Normalize.** Every contract becomes a `Book` of YES bids and asks in $/contract, with its outcome bounds [lo, hi).
3. **Route.** `engine.price_route` enumerates every way to build the requested payoff:
   - a single contract
   - a two-leg YES/NO spread
   - a tiling of Kalshi range buckets

   It walks the books, adds per-order taker fees, and returns the cheapest build. It splits across venues when one book is too thin.
4. **Fair value.** `engine.fairvalue` fits an SVI smile per Deribit expiry and interpolates total variance to the bet's expiry. It then reads P(S_T ≥ K) off the call-price slope.
5. **Stale flags.** `engine.stale` flags quotes that disagree with the other venue and either:
   - didn't move while BTC did, or
   - disagree with fair while the other venue agrees.

See `engine/README.md` for the math and known limits.

## Hackathon submission checklist

- [ ] Public repo with setup instructions (this README)
- [ ] Working deployed demo
- [ ] 2–3 min demo video
- [ ] README architecture overview
- [ ] Track + bounties selected on Devpost
