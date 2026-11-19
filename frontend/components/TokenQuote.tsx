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
    <section className="card space-y-3" aria-live="polite">
      <h2 className="text-lg font-semibold">{symbol} tokenized equity</h2>
      {quote ? <>
        <p className="text-2xl tabular-nums">{usd(quote.price_usd)}</p>
        <p>Last trade on {quote.source} · {new Date(quote.traded_at).toUTCString()}</p>
        <a className="link" href={quote.issuer_url}>Issuer product details</a>
      </> : <p>{error ? "The last trade is unavailable." : "Fetching the last trade…"}</p>}
      <p className="muted text-sm">Spot market reference. Prediction contract pricing requires a separately listed outcome market.</p>
      <button type="button" className="btn-outline" disabled={!quote && !error} onClick={refresh}>Refresh price</button>
    </section>
  );
}
