"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ASSET_GROUPS } from "@/lib/assets";
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
    title = "Couldn’t reach the price service";
    body = "Try again in a moment to get a fresh quote.";
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
        <h2 className="text-lg font-semibold">Choose a routed asset</h2>
        <div className="grid gap-4 sm:grid-cols-3">
          {ASSET_GROUPS.map((g) => (
            <div key={g.title} className="stat-tile space-y-3 p-4">
              <h3 className="label">{g.title}</h3>
              <div className="flex flex-wrap gap-2">
                {g.items.map(([value,label]) => (
                  <button key={value} type="button" aria-pressed={asset === value}
                    onClick={() => setAsset(value)} title={label}
                    className={`seg ${asset === value ? "seg-on" : ""}`}>
                    {value}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
      <Builder key={asset} symbol={asset} asset={asset.replace(/x$/, "")} />
    </>
  );
}

function Builder({ asset, symbol }: { asset: string; symbol: string }) {
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
    const timer = window.setInterval(loadExpiries, 60_000);
    return () => window.clearInterval(timer);
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
      <TokenQuote symbol={symbol} expiries={expiries} pending={loadingExp || !!expErr} />
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
            <div className="card muted text-sm">Choose an outcome and press <strong>Build route</strong> to compare available prices.</div>
          )}
          {result.s === "loading" && (
            <div className="card animate-pulse space-y-3" aria-busy="true">
              <div className="h-5 w-1/2 rounded bg-line" />
              <div className="h-16 rounded bg-line" />
              <div className="h-24 rounded bg-line" />
              <p className="muted text-sm">Checking prices and how many contracts are available…</p>
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
