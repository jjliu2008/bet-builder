"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
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

export default function Home() {
  const [expiries, setExpiries] = useState<string[]>([]);
  const [loadingExp, setLoadingExp] = useState(true);
  const [expErr, setExpErr] = useState<ApiError | null>(null);
  const [result, setResult] = useState<Result>({ s: "idle" });
  const last = useRef<RouteRequest | null>(null);

  async function loadExpiries() {
    setLoadingExp(true);
    setExpErr(null);
    try {
      setExpiries(await getExpiries("BTC"));
    } catch (e) {
      setExpErr(e instanceof ApiError ? e : new ApiError("Unknown error", 0));
    } finally {
      setLoadingExp(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadExpiries();
  }, []);

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
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:py-12">
      <header className="mb-8">
        <h1 className="text-3xl sm:text-4xl font-bold tracking-tight">Bet-ter Crypto</h1>
        <p className="muted mt-2 max-w-2xl">
          Say the BTC bet you want. We build it from the cheapest mix of Gemini and Kalshi contracts, compare it with options-implied fair value, and flag
          risky quotes.
        </p>
        <Link href="/ladder" className="link text-sm mt-2 inline-block">View strike ladder →</Link>
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
        <BetForm expiries={expiries} loadingExpiries={loadingExp} busy={result.s === "loading"} onSubmit={build} />

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
    </main>
  );
}
