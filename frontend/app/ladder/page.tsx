"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ApiError, getExpiries, getLadder } from "@/lib/api";
import { cents, expiryLabel, pct, usd } from "@/lib/format";
import type { LadderRow, VenueQuote } from "@/lib/types";

function Quote({ q, stale, reason, best }: { q: VenueQuote | null; stale: boolean; reason?: string | null; best: boolean }) {
  if (!q) return <span className="muted">not listed</span>;
  return (
    <span className={best ? "text-good font-semibold" : ""} title={reason ?? undefined}>
      {q.bid === null ? "–" : cents(q.bid)} / {q.ask === null ? "–" : cents(q.ask)}
      {stale && <span className="pill pill-bad ml-2">stale</span>}
    </span>
  );
}

export default function LadderPage() {
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [rows, setRows] = useState<LadderRow[] | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);

  useEffect(() => {
    getExpiries()
      .then((e) => {
        setExpiries(e);
        setExpiry((cur) => cur || e[e.length - 1] || "");
      })
      .catch((e) => setErr(e instanceof ApiError ? e : new ApiError("Unknown error", 0)));
  }, []);

  const load = useCallback(async (exp: string) => {
    setRows(null);
    setErr(null);
    try {
      setRows(await getLadder(exp));
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError("Unknown error", 0));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (expiry) load(expiry);
  }, [expiry, load]);

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:py-12 space-y-6">
      <Link href="/" className="link text-sm">← Back to bet builder</Link>
      <header className="space-y-1">
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight">Strike ladder</h1>
        <p className="muted text-sm">
          Contract &ldquo;BTC ≥ strike&rdquo; on each venue, shown as bid / ask. Green marks the cheapest ask. Fair value is the Deribit-implied probability.
        </p>
      </header>

      <select className="input max-w-xs" value={expiry} onChange={(e) => setExpiry(e.target.value)} aria-label="Expiry">
        {expiries.map((x) => (
          <option key={x} value={x}>{expiryLabel(x)}</option>
        ))}
      </select>

      {err && (
        <div role="alert" className="note note-bad">
          {err.message} {expiry && <button className="link ml-2" onClick={() => load(expiry)}>Retry</button>}
        </div>
      )}
      {!rows && !err && <div className="card animate-pulse h-48" aria-busy="true" />}
      {rows && rows.length === 0 && <div className="card muted">No strikes listed for this expiry.</div>}
      {rows && rows.length > 0 && (
        <div className="card overflow-x-auto p-0 sm:p-0">
          <table className="w-full text-sm tabular-nums">
            <thead className="muted text-left">
              <tr>
                <th className="p-3 font-medium">Strike</th>
                <th className="p-3 font-medium">Gemini</th>
                <th className="p-3 font-medium">Kalshi</th>
                <th className="p-3 font-medium text-right">Fair</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const g = r.gemini?.ask ?? null;
                const k = r.kalshi?.ask ?? null;
                const cheapest = g !== null && k !== null ? (g < k ? "gemini" : k < g ? "kalshi" : null) : g !== null ? "gemini" : k !== null ? "kalshi" : null;
                return (
                  <tr key={r.strike} className="border-t border-line">
                    <td className="p-3 font-medium">{usd(r.strike, 0)}</td>
                    <td className="p-3"><Quote q={r.gemini} stale={r.stale.gemini} reason={r.stale_reasons?.gemini} best={cheapest === "gemini"} /></td>
                    <td className="p-3"><Quote q={r.kalshi} stale={r.stale.kalshi} reason={r.stale_reasons?.kalshi} best={cheapest === "kalshi"} /></td>
                    <td className="p-3 text-right">{r.fair === null ? <span className="muted">n/a</span> : pct(r.fair)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
