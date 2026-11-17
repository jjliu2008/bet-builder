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

Receipts (`POST /receipt`, `GET /verify/{id}`) live in `backend/receipts.py`. If an optional `chain`
package with `anchor_hash` / `verify_hash` (and optionally `router`) is importable, receipts are
anchored on Solana devnet. Otherwise they are issued with status `"pending"`.
