"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import TokenQuote from "@/components/TokenQuote";
import BetForm from "@/components/BetForm";
import RouteCard from "@/components/RouteCard";
import Alternatives from "@/components/Alternatives";
import { ApiError, getExpiries, postRoute } from "@/lib/api";
import type { Route, RouteRequest } from "@/lib/types";

type Result =
  | { s: "idle" }
  | { s: "loading" }
  | { s: "ok"; route: Route; req: RouteRequest }
  | { s: "error"; err: ApiError };

function ErrorPanel({ err, onRetry }: { err: ApiError; onRetry: () => void }) {
  let title = "Couldn't build a route";
  let body = err.message;
  if (err.offline) {
    title = "Backend unreachable";
    body = `${err.message}. Check that the API is running and NEXT_PUBLIC_API_URL is correct.`;
  } else if (err.unavailable) {
    title = "Market data isn't ready";
    body = `${err.message}. The feed may still be starting or stale. Try again in a moment.`;
  } else if (err.status === 404) {
    title = "Expiry not available";
  } else if (err.status === 422 && /liquid|fill|depth/i.test(err.message)) {
    title = "Not enough liquidity";
  } else if (err.status === 422 && err.nearest) {
    title = "Strike not listed";
  }
  return (
    <div role="alert" className="card border-bad space-y-3">
      <h2 className="font-semibold text-bad">{title}</h2>
      <p className="text-sm">{body}</p>
      <button type="button" onClick={onRetry} className="btn-outline">
        Try again
      </button>
    </div>
  );
}

export default function AssetBuilder() {
  const [asset, setAsset] = useState("BTC");
  return (
    <>
      <section className="card mb-6 space-y-3 py-4 sm:py-4" aria-label="Underlying asset">
        <span className="label">Underlying asset</span>
        <div className="flex flex-wrap gap-x-6 gap-y-3">
          {ASSET_GROUPS.map((g) => (
            <div key={g.title} className="space-y-1.5">
              <div className="flex items-center gap-2 text-xs">
                <span className={`pill ${g.pill}`}>{g.title}</span>
                <span className="muted">{g.hint}</span>
              </div>
              <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={g.title}>
                {g.items.map((it) => (
                  <button
                    key={it.value}
                    type="button"
                    role="radio"
                    aria-checked={asset === it.value}
                    onClick={() => setAsset(it.value)}
                    className={`seg ${asset === it.value ? "seg-on" : ""}`}
                  >
                    {it.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
      {asset.endsWith("x") ? <TokenQuote key={asset} symbol={asset} /> : <Builder key={asset} asset={asset} />}
    </>
  );
}

const ASSET_GROUPS = [
  { title: "Live", hint: "Gemini × Kalshi books, Deribit fair value", pill: "pill-good", items: [{ value: "BTC", label: "Bitcoin" }] },
  {
    title: "Simulation",
    hint: "illustrative stock books",
    pill: "pill-warn",
    items: [
      { value: "AAPL", label: "Apple (AAPL)" },
      { value: "NVDA", label: "NVIDIA (NVDA)" },
    ],
  },
  {
    title: "Price only",
    hint: "tokenized stock spot price",
    pill: "",
    items: [
      { value: "AAPLx", label: "AAPLx" },
      { value: "NVDAx", label: "NVDAx" },
    ],
  },
];

function Builder({ asset }: { asset: string }) {
  const [expiries, setExpiries] = useState<string[]>([]);
  const [loadingExp, setLoadingExp] = useState(true);
  const [expErr, setExpErr] = useState<ApiError | null>(null);
  const [result, setResult] = useState<Result>({ s: "idle" });
  const last = useRef<RouteRequest | null>(null);

  const loadExpiries = useCallback(async () => {
    setLoadingExp(true);
    setExpErr(null);
    try {
      setExpiries(await getExpiries(asset));
    } catch (e) {
      setExpErr(e instanceof ApiError ? e : new ApiError("Unknown error", 0));
    } finally {
      setLoadingExp(false);
    }
  }, [asset]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadExpiries();
  }, [loadExpiries]);

  async function build(req: RouteRequest) {
    last.current = req;
    setResult({ s: "loading" });
    try {
      setResult({ s: "ok", route: await postRoute(req), req });
    } catch (e) {
      setResult({ s: "error", err: e instanceof ApiError ? e : new ApiError("Unknown error", 0) });
    }
  }

  return (
    <>
      <header className="mb-6">
        {asset !== "BTC" &&
          <p className="note note-warn">Stock-price prediction demo using simulated books as of November 19, 2026 at 15:00 UTC. All prices and sizes are illustrative.</p>}
      </header>

      {expErr && (
        <div role="alert" className="note note-bad mb-6 flex flex-wrap items-center justify-between gap-3">
          <span>
            {expErr.unavailable ? "Market data is still loading." : "Could not load expiries."} {expErr.message}
          </span>
          <button type="button" className="link" onClick={loadExpiries}>
            Retry
          </button>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[380px_1fr]">
        <BetForm asset={asset} expiries={expiries} loadingExpiries={loadingExp} busy={result.s === "loading"} onSubmit={build} />

        <div className="space-y-6" aria-live="polite">
          {result.s === "idle" && (
            <div className="card muted text-sm">Choose an outcome and press <strong>Build route</strong> to see the cheapest construction.</div>
          )}
          {result.s === "loading" && (
            <div className="card animate-pulse space-y-3" aria-busy="true">
              <div className="h-5 w-1/2 rounded bg-line" />
              <div className="h-16 rounded bg-line" />
              <div className="h-24 rounded bg-line" />
              <p className="muted text-sm">Fetching live books and walking depth…</p>
            </div>
          )}
          {result.s === "error" && <ErrorPanel err={result.err} onRetry={() => last.current && build(last.current)} />}
          {result.s === "ok" && (
            <>
              {result.route.legs.length === 0 || (result.route.filled_size ?? 1) === 0 ? (
                <div className="card border-bad space-y-2" role="alert">
                  <h2 className="font-semibold text-bad">No liquidity</h2>
                  <p className="text-sm">Visible order books can&apos;t fill any of this bet right now. Try a smaller size or a different strike.</p>
                </div>
              ) : (
                <RouteCard route={result.route} request={result.req} />
              )}
              <Alternatives route={result.route} />
            </>
          )}
        </div>
      </div>
    </>
  );
}
