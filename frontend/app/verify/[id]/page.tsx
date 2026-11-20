"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { isStockOutcome, usd, expiryLabel, venueName } from "@/lib/format";
import { ApiError, getVerify } from "@/lib/api";
import type { Verification } from "@/lib/types";

export default function VerifyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const routeId = decodeURIComponent(id);
  const [data, setData] = useState<Verification | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getVerify(routeId)
      .then(setData)
      .catch((e) => setErr(e instanceof ApiError ? e.message : "Unknown error"));
  }, [routeId]);

  const quote = data?.quote;
  const req = quote?.request;
  const ok = data?.status === "verified";
  const pending = data?.status === "pending";
  return (
    <main className="mx-auto w-full max-w-xl px-4 py-12 space-y-4">
      <Link href="/" className="link text-sm">← Back to bet builder</Link>
      <div className="card space-y-3">
        <div className="label">Route receipt</div>
        <p className="muted text-xs break-all">{routeId}</p>
        {!data && !err && <p className="muted animate-pulse">Checking Solana…</p>}
        {err && <p role="alert" className="note note-bad">Couldn&apos;t verify: {err}</p>}
        {data && (
          <>
            <span className={`pill ${ok ? "pill-good" : pending ? "pill-warn" : "pill-bad"} text-base`}>
              {ok ? "✓ Verified" : pending ? "Receipt pending" : "✗ Mismatch"}
            </span>
            <p className="text-sm">
              {ok
                ? "The stored route matches the fingerprint recorded on Solana devnet."
                : pending
                  ? "The saved quote matches its fingerprint. Onchain confirmation is still pending."
                  : "The stored route does not match its fingerprint. It may have been changed."}
            </p>
            {quote && <section className="leg space-y-1">
              <h2 className="font-semibold">Saved quote</h2>
              {req && <p>{req.asset} {req.type === "between" ? `${isStockOutcome(req.asset) ? "above" : "from"} ${usd(req.lo!)} to ${isStockOutcome(req.asset) ? "and including" : "below"} ${usd(req.hi!)}` : req.type === "above" ? `${isStockOutcome(req.asset) ? "above" : "at or above"} ${usd(req.lo ?? req.hi!)}` : `${isStockOutcome(req.asset) ? "at or below" : "below"} ${usd(req.hi ?? req.lo!)}`} at {expiryLabel(req.expiry)}</p>}
              {quote.timestamp && <p>Quoted {new Date(quote.timestamp).toUTCString()}</p>}
              <p>{quote.venues.map(venueName).join(" + ")}</p>
              {quote.cash_outlay != null && quote.filled_size != null && <p>{usd(quote.cash_outlay * quote.filled_size)} upfront for {quote.filled_size} contracts</p>}
            </section>}
            {data.detail && <p className="muted text-xs">{data.detail}</p>}
            <p className="muted text-xs break-all">SHA-256 {data.hash}</p>
            {data.explorer_url && (
              <a href={data.explorer_url} target="_blank" rel="noreferrer" className="link text-sm">
                View transaction on Solana Explorer ↗
              </a>
            )}
          </>
        )}
      </div>
    </main>
  );
}
