# frontend/

**Owner: Person 3.** Next.js (App Router, TypeScript, Tailwind) UI for the bet builder.

Pages: `/` bet builder and route card, `/ladder` strike ladder, `/verify/[id]` Solana receipt check.
Field names follow `docs/api_contract.md`; types live in `lib/types.ts` and the API client in `lib/api.ts`.

## Run

```bash
cd frontend
cp .env.example .env.local     # set NEXT_PUBLIC_API_URL to the FastAPI server
npm install
npm run dev                    # http://localhost:3000
npm run lint && npm run build
```

Backend: `uvicorn backend.app:app` from the repo root (port 8000 by default).

## Deploy

Deploy the `frontend/` directory (for example on Vercel with root directory `frontend`) and set
`NEXT_PUBLIC_API_URL` to the public backend URL. On the backend, set `ALLOWED_ORIGINS` to the
deployed frontend origin. Never put secrets in `NEXT_PUBLIC_*` variables.

## Receipts

`POST /receipt` and `GET /verify/{route_id}` are not yet in the API contract. The client assumes
`/receipt` takes `{request, route}` and returns `{route_id, signature, explorer_url}`, and `/verify`
returns `{status: "verified" | "mismatch", explorer_url}`. Change `lib/api.ts` if the backend differs.
