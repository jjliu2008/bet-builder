"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useExpiries } from "@/lib/useExpiries";
import { ApiError, getLadder } from "@/lib/api";
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
  const [asset, setAsset] = useState("BTC");
  return <><label className="mx-auto mt-4 block w-full max-w-5xl px-4">Outcome market
    <select className="input" value={asset} onChange={e => setAsset(e.target.value)}>
      {["BTC","ETH","SOL","AAPL","NVDA","MSFT","TSLA"].map(a => <option key={a}>{a}</option>)}
    </select></label><AssetLadder key={asset} asset={asset} /></>;
}

function AssetLadder({asset}: {asset: string}) {
  const {expiries, expiry, setExpiry, loading, error: dateError, refresh} = useExpiries(asset);
  const [rows, setRows] = useState<LadderRow[] | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [onlyBoth, setOnlyBoth] = useState(false);
  const shown = rows ? (onlyBoth ? rows.filter((r) => r.gemini && r.kalshi) : rows) : null;

  const sequence = useRef(0);
  const load = useCallback(async (exp: string) => {
    const request = ++sequence.current;
    setRows(null);
    setErr(null);
    try {
      const data = await getLadder(exp, asset);
      if (sequence.current === request) setRows(data);
    } catch (e) {
      if (sequence.current !== request) return;
      setErr(e instanceof ApiError ? e : new ApiError("Unknown error", 0));
    }
  }, [asset]);

  const invalidate = useCallback(() => { sequence.current++; }, []);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (expiry) load(expiry);
    return invalidate;
  }, [expiry, load, invalidate]);

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:py-12 space-y-6">
      <header className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">
          Strike <span className="gradient-text">ladder</span>
        </h1>
        <p className="muted max-w-2xl">
          Listed {asset} thresholds show the current bid and ask. Fair value appears when an independent options benchmark is available. Green marks
          the cheaper ask when both venues list a strike. For spreads, crossed markets and quoting, open the{" "}
          <Link href="/maker" className="link">
            maker desk
          </Link>
          .
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-4">
        <select className="input w-auto" value={expiry} onChange={(e) => setExpiry(e.target.value)} aria-label="Expiry">
          {expiries.map((x) => (
            <option key={x} value={x}>{expiryLabel(x)}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={onlyBoth} onChange={(e) => setOnlyBoth(e.target.checked)} className="accent-[var(--accent)]" />
          Listed on both venues only
        </label>
      </div>

      {dateError && <p role="alert" className="note note-bad">{dateError.message} <button className="link" onClick={refresh}>Retry end times</button></p>}
      {!expiry && !loading && !dateError && <p className="note">No listed end times for this asset right now.</p>}
      {expiry && err && (
        <div role="alert" className="note note-bad">
          {err.message} {expiry && <button className="link ml-2" onClick={() => load(expiry)}>Retry</button>}
        </div>
      )}
      {(loading || (expiry && !rows && !err)) && <div className="card animate-pulse h-48" aria-busy="true" />}
      {expiry && shown && shown.length === 0 && (
        <div className="card muted">{onlyBoth ? "No strike is listed on both venues for this expiry. Untick the filter to see all." : "No strikes listed for this expiry."}</div>
      )}
      {expiry && shown && shown.length > 0 && (
        <div className="card overflow-x-auto p-0 sm:p-0">
          <table className="num w-full text-sm">
            <thead className="table-head">
              <tr>
                <th>Strike</th>
                <th style={{ color: "var(--gemini)" }}>Gemini</th>
                <th style={{ color: "var(--kalshi)" }}>Kalshi</th>
                {shown.some(r => r.polymarket) && <th>Polymarket</th>}
                <th className="text-right">Fair</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const g = r.gemini?.ask ?? null;
                const k = r.kalshi?.ask ?? null;
                // Only highlight when there is a real cross-venue comparison.
                const cheapest = g !== null && k !== null ? (g < k ? "gemini" : k < g ? "kalshi" : null) : null;
                return (
                  <tr key={r.strike} className="row-hover border-t border-line">
                    <td className="p-3 font-medium">{usd(r.strike, 0)}</td>
                    <td className="p-3"><Quote q={r.gemini} stale={r.stale.gemini} reason={r.stale_reasons?.gemini} best={cheapest === "gemini"} /></td>
                    <td className="p-3"><Quote q={r.kalshi} stale={r.stale.kalshi} reason={r.stale_reasons?.kalshi} best={cheapest === "kalshi"} /></td>
                    {shown.some(row => row.polymarket) && <td className="p-3"><Quote q={r.polymarket ?? null} stale={!!r.stale.polymarket} best={false} /></td>}
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
