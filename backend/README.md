# backend/

**Owner: Person 2.** Ingest, contract matcher, quote logger, and the FastAPI server.

The server imports the engine package (`from engine import price_route, fair_above, ...`).
See `docs/api_contract.md` for the endpoint shapes and `engine/README.md` for the math.

## Run (from the repo root)

```bash
pip install -r requirements.txt

# Live mode: polls Gemini, Kalshi, and Deribit (public endpoints, no keys)
ALLOWED_ORIGINS=http://localhost:3000 uvicorn backend.app:app --port 8000

# Mock mode: replays the recorded demo fixture. No venue calls; the clock is the recorded time
REPLAY_FILE=backend/fixtures/demo/snapshots.jsonl.gz REPLAY_DERIBIT_FILE=backend/fixtures/demo/deribit.jsonl.gz ALLOWED_ORIGINS=http://localhost:3000 uvicorn backend.app:app --port 8000
```

On Windows PowerShell, set variables first, e.g. `$env:ALLOWED_ORIGINS="http://localhost:3000"`.

Key environment variables (the full list is in the `backend/app.py` docstring):

| variable | default | meaning |
|---|---|---|
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins. This is the name `frontend/README.md` uses. |
| `POLL_INTERVAL_S` | 30 | Seconds between polls. |
| `N_EXPIRIES` | 3 | Nearest matched expiries to poll. |
| `MIN_EXPIRY_HOURS` | 0 | Skip expiries closer than this many hours. |
| `REPLAY_FILE`, `REPLAY_DERIBIT_FILE`, `REPLAY_SPEED` | | Mock/replay mode. |
| `LOG_DIR`, `LOG_ENABLED`, … | | Snapshot logger (`backend/logger.py`). |

## Onchain receipts

`POST /receipt`, `GET /verify/{id}`, `POST /verify` and `GET /anchors` are documented in `docs/api_contract.md`.
- Receipts are SHA-256 fingerprints of `{request, route}`, written to Solana devnet as Memo program transactions by `chain/`.
- They are stored in `backend/receipt_store.py` (SQLite or Postgres).
- Without a signer, receipts are issued with status `"pending"`, and routes work normally.

| variable | default | meaning |
|---|---|---|
| `SOLANA_KEYPAIR` | (none) | Signer secret: base58 (64-byte keypair) or a JSON byte array. Use this on hosts. |
| `SOLANA_KEYPAIR_PATH` | (none) | Path to a JSON keypair file instead, e.g. `~/.config/bet-builder/devnet.json`. |
| `SOLANA_RPC_URL` | `https://api.devnet.solana.com` | Solana JSON-RPC endpoint. |
| `RECEIPTS_DB` | `data/receipts.db` | SQLite file for receipts and hourly anchors. |
| `DATABASE_URL` | (none) | Postgres URL. If set, it is used instead of SQLite. |

Create and fund a devnet key with `python scripts/new_keypair.py`. It saves the key outside the repo, prints the base58 secret to paste into `SOLANA_KEYPAIR`, and tries an airdrop. If the airdrop is rate-limited, use https://faucet.solana.com with the printed public key. Each receipt costs about 0.000005 devnet SOL.

**Deploy note.** Most hosts (Render, Railway, Fly, Heroku-style dynos) wipe local files on every redeploy, so a default SQLite file would lose its receipts. In production, do one of these:
- Put `RECEIPTS_DB` on a persistent disk or volume, e.g. `RECEIPTS_DB=/var/data/receipts.db`.
- Set `DATABASE_URL` to a free hosted Postgres (Neon, Supabase, Render Postgres).

Even if the database is lost, any receipt can still be checked with `POST /verify` using the route and its signature, because the proof is onchain.
