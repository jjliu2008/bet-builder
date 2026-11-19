"use client";

import { useEffect, useState } from "react";
import { getTokenQuote } from "@/lib/api";
import { usd } from "@/lib/format";

export default function TokenQuote({ symbol }: { symbol: string }) {
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
    <section className="card max-w-xl space-y-3" aria-live="polite">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{symbol} · USD</h2>
        <span className="pill">Price only</span>
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
      <p className="muted text-sm">Reference price as of the displayed time. Choose Bitcoin to price a prediction route.</p>
      <button type="button" className="btn-outline" disabled={!quote && !error} onClick={refresh}>Refresh price</button>
    </section>
  );
}
