"use client";

import { useEffect, useState } from "react";
import { getTokenQuote } from "@/lib/api";
import { expiryLabel, usd } from "@/lib/format";

export default function TokenQuote({ symbol, expiries, pending }: { symbol: string; expiries: string[]; pending: boolean }) {
  const [quote, setQuote] = useState<Awaited<ReturnType<typeof getTokenQuote>>>();
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    getTokenQuote(symbol).then(value => {
      if (active) setQuote(value);
    }).catch(() => {
      if (active) setError(true);
    });
    return () => { active = false; };
  }, [symbol, attempt]);

  function refresh() {
    setError(false);
    setQuote(undefined);
    setAttempt(value => value + 1);
  }

  return (
    <section className="card mb-6 grid gap-5 sm:grid-cols-2" aria-live="polite">
      <div className="space-y-2"><div className="flex items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{symbol} · USD</h2>

      </div>
      {quote ? <>
        <p className="num text-3xl font-semibold">{usd(quote.price_usd)}</p>
        <p className="muted text-sm">{quote.source} · {new Date(quote.traded_at).toUTCString()}</p>
        {quote.issuer_url && <a className="link text-sm" href={quote.issuer_url} target="_blank" rel="noreferrer">Issuer product details ↗</a>}
      </> : error ? <p className="note note-bad">The last trade is unavailable.</p> : (
        <div className="space-y-2" aria-busy="true">
          <div className="h-9 w-40 animate-pulse rounded bg-line" />
          <div className="h-4 w-64 animate-pulse rounded bg-line" />
        </div>
      )}

      <button type="button" className="btn-outline" disabled={!quote && !error} onClick={refresh}>Refresh price</button></div>
      <div className="stat-tile space-y-1 p-4"><p className="label">Listed end times</p><p className="num text-2xl font-semibold">{pending ? "—" : expiries.length}</p>{!pending && expiries[0] && <p className="muted text-sm">Next close · {expiryLabel(expiries[0])}</p>}{symbol.endsWith("x") && <p className="text-sm">Routes settle on the underlying {symbol.slice(0,-1)} stock close.</p>}</div>
    </section>
  );
}
