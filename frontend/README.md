# frontend/

**Owner: Person 3.** Next.js (App Router, TypeScript, Tailwind) UI for the bet builder.

Pages: `/` bet builder and route card, `/ladder` strike ladder, `/verify/[id]` Solana receipt check.
Field names follow `docs/api_contract.md`; types live in `lib/types.ts` and the API client in `lib/api.ts`.

## Run

```bash
cd frontend
cp .env.example .env.local     # NEXT_PUBLIC_API_URL = the FastAPI server (default http://localhost:8000)
npm install
npm run dev                    # http://localhost:3000
npm run lint && npm run build
```

Backend: `uvicorn backend.app:app` from the repo root (port 8000 by default). The backend allows
CORS from the origins in **`ALLOWED_ORIGINS`** (comma-separated; default `*`). Set it to this
frontend's origin, e.g. `ALLOWED_ORIGINS=http://localhost:3000`.

## Deploy

Deploy the `frontend/` directory (for example on Vercel with root directory `frontend`) and set
`NEXT_PUBLIC_API_URL` to the public backend URL. On the backend, set `ALLOWED_ORIGINS` to the
deployed frontend origin. Never put secrets in `NEXT_PUBLIC_*` variables.

## Receipts

`POST /receipt` and `GET /verify/{route_id}` are in `docs/api_contract.md`.

- `/receipt` takes `{request, route}` (a bare route also works). It returns
  `{route_id, hash, signature, explorer_url, status}`.
- `status` is `"confirmed"` once the fingerprint is on Solana devnet. Otherwise it is `"pending"`,
  for example when the server has no chain module or devnet keypair. In that case the UI shows
  "Receipt pending", and the route stays usable.
- `/verify/{route_id}` returns `{status: "verified" | "mismatch" | "pending", hash, signature, explorer_url, detail}`.

Route cards can request a Grok explanation of a server-priced snapshot. Set
`XAI_API_KEY` on the FastAPI service. Optional `XAI_MODEL` defaults to
`grok-4.3`. Only an explicit question calls the provider. The service keeps
the most recent 256 snapshots per process and allows 12 explanations per minute
per process. A restart expires the snapshots. Use one worker for this deployment.
