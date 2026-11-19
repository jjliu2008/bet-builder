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
      <header className="mb-10 grid gap-8 lg:grid-cols-[1fr_auto] lg:items-end">
        <div className="space-y-4">
          <span className="pill pill-accent">BTC prediction markets · Gemini × Kalshi</span>
          <h1 className="text-4xl font-bold leading-[1.05] tracking-tight sm:text-5xl">
            Say the bet.
            <br />
            <span className="gradient-text">We build it cheaper.</span>
          </h1>
          <p className="muted max-w-2xl text-lg">
            Pick a BTC outcome. We assemble it from the cheapest mix of Gemini and Kalshi contracts, check it against Deribit options, flag risky
            quotes, and anchor a receipt on Solana.
          </p>
          <div className="flex flex-wrap gap-3 text-sm">
            <Link href="/maker" className="btn-outline">
              Open the maker desk →
            </Link>
            <Link href="/ladder" className="nav-link">
              Strike ladder
            </Link>
          </div>
        </div>
        <ol className="grid gap-2 text-sm sm:grid-cols-3 lg:w-[420px] lg:grid-cols-1">
          {[
            ["1", "Route", "Single contract, YES/NO spread or bucket tiling, across venues"],
            ["2", "Price", "Walks real depth, adds taker fees, compares with options fair value"],
            ["3", "Prove", "SHA-256 fingerprint written to Solana devnet, verifiable by anyone"],
          ].map(([n, t, d]) => (
            <li key={n} className="stat-tile flex gap-3 p-3">
              <span className="num grid h-6 w-6 shrink-0 place-items-center rounded-md text-xs font-bold" style={{ background: "color-mix(in srgb, var(--accent) 18%, transparent)", color: "var(--accent)" }}>
                {n}
              </span>
              <span>
                <span className="font-semibold">{t}</span>
                <span className="muted block text-xs">{d}</span>
              </span>
            </li>
          ))}
        </ol>
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
