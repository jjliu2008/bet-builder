"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import MakerChart from "@/components/MakerChart";
import QuoteSimulator from "@/components/QuoteSimulator";
import { ApiError, getExpiries, getLadder } from "@/lib/api";
import { cents, expiryLabel, pct, venueName } from "@/lib/format";
import { summarize, toMakerRow, type MakerRow, type Touch } from "@/lib/maker";

const signed = (v: number) => `${v >= 0 ? "+" : "−"}${cents(Math.abs(v))}`;
const usd0 = (n: number) => n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

function TouchCell({ t }: { t: Touch | null }) {
  if (!t) return <span className="muted">–</span>;
  return (
    <span className="flex items-center gap-2">
      <span className="num">{cents(t.px)}</span>
      <span className={`pill pill-${t.venue}`}>{venueName(t.venue)}</span>
      <span className="muted num text-xs">×{Math.round(t.size).toLocaleString()}</span>
    </span>
  );
}

function Tile({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="stat-tile">
      <div className="label">{label}</div>
      <div className="num text-2xl font-semibold">{value}</div>
      {sub && <div className="muted mt-1 text-xs">{sub}</div>}
    </div>
  );
}

export default function MakerDesk() {
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [rows, setRows] = useState<MakerRow[] | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [updated, setUpdated] = useState<Date | null>(null);
  const [auto, setAuto] = useState(true);
  const [onlyBoth, setOnlyBoth] = useState(true);

  useEffect(() => {
    getExpiries()
      .then((e) => {
        setExpiries(e);
        setExpiry((cur) => cur || e[e.length - 1] || "");
      })
      .catch((e) => setErr(e instanceof ApiError ? e : new ApiError("Unknown error", 0)));
  }, []);

  const load = useCallback(async (exp: string, quiet = false) => {
    if (!quiet) setRows(null);
    setErr(null);
    try {
      const data = (await getLadder(exp)).map(toMakerRow).sort((a, b) => a.strike - b.strike);
      setRows(data);
      setUpdated(new Date());
      setSelected((cur) => {
        if (cur !== null && data.some((r) => r.strike === cur)) return cur;
        // Default to the two-venue strike whose fair value is closest to 50¢: the most active part of the book.
        const pool = data.filter((r) => r.quotes.gemini && r.quotes.kalshi);
        const pick = (pool.length ? pool : data).reduce<MakerRow | null>(
          (a, b) => (!a || Math.abs((b.fair ?? b.mid ?? 0) - 0.5) < Math.abs((a.fair ?? a.mid ?? 0) - 0.5) ? b : a),
          null,
        );
        return pick?.strike ?? null;
      });
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError("Unknown error", 0));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (expiry) load(expiry);
  }, [expiry, load]);

  useEffect(() => {
    if (!auto || !expiry) return;
    const id = setInterval(() => load(expiry, true), 30_000);
    return () => clearInterval(id);
  }, [auto, expiry, load]);

  const view = useMemo(() => (rows ? (onlyBoth ? rows.filter((r) => r.quotes.gemini && r.quotes.kalshi) : rows) : null), [rows, onlyBoth]);
  const sum = useMemo(() => (view ? summarize(view) : null), [view]);
  const selRow = view?.find((r) => r.strike === selected) ?? rows?.find((r) => r.strike === selected) ?? null;

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-8 sm:py-10">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="max-w-2xl space-y-2">
          <span className="pill pill-accent">Market-maker view</span>
          <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">
            Maker <span className="gradient-text">desk</span>
          </h1>
          <p className="muted">
            One consolidated order book across Gemini and Kalshi, priced against the Deribit options surface. See who sets the best price, where
            spreads are wide, and how a two-sided quote would sit in each venue&apos;s book.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select className="input w-auto" value={expiry} onChange={(e) => setExpiry(e.target.value)} aria-label="Expiry">
            {expiries.map((x) => (
              <option key={x} value={x}>
                {expiryLabel(x)}
              </option>
            ))}
          </select>
          <button type="button" className="btn-outline" onClick={() => expiry && load(expiry, true)}>
            Refresh
          </button>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} className="accent-[var(--accent)]" />
            Auto 30s
          </label>
        </div>
      </header>

      {err && (
        <div role="alert" className="note note-bad flex flex-wrap items-center justify-between gap-3">
          <span>
            {err.unavailable ? "Market data is warming up." : err.offline ? "Backend unreachable." : "Couldn't load the book."} {err.message}
          </span>
          {expiry && (
            <button className="link" onClick={() => load(expiry)}>
              Retry
            </button>
          )}
        </div>
      )}

      {!rows && !err && (
        <div className="grid gap-4 sm:grid-cols-4" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="stat-tile h-24 animate-pulse" />
          ))}
          <div className="card h-80 animate-pulse sm:col-span-4" />
        </div>
      )}

      {view && sum && (
        <>
          <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" aria-label="Desk summary">
            <Tile
              label="Median best spread"
              value={sum.medianSpread === null ? "–" : cents(sum.medianSpread)}
              sub={
                <>
                  Gemini avg {sum.avgVenueSpread.gemini === null ? "–" : cents(sum.avgVenueSpread.gemini)} · Kalshi avg{" "}
                  {sum.avgVenueSpread.kalshi === null ? "–" : cents(sum.avgVenueSpread.kalshi)}
                </>
              }
            />
            <Tile
              label="Who sets the best price"
              value={
                <span className="flex items-baseline gap-3 text-xl">
                  <span style={{ color: "var(--gemini)" }}>G {Math.round(sum.touchShare.gemini * 100)}%</span>
                  <span style={{ color: "var(--kalshi)" }}>K {Math.round(sum.touchShare.kalshi * 100)}%</span>
                </span>
              }
              sub={`Share of best bids and asks, ${sum.bothVenues} strikes on both venues (ties count for both)`}
            />
            <Tile
              label="Crossed across venues"
              value={
                <span className="flex items-baseline gap-2">
                  {sum.crossedCount}
                  <span className="muted text-sm font-normal">+ {sum.lockedCount} locked</span>
                </span>
              }
              sub={
                sum.crossedCount
                  ? `${sum.crossProfitable} still pay after taker fees on both legs (best ${signed(sum.bestCrossNet ?? 0)} per contract)`
                  : "No venue's bid is above the other's ask right now"
              }
            />
            <Tile
              label="Largest gap to options"
              value={sum.maxMispricing ? `${sum.maxMispricing.midVsFair! > 0 ? "+" : "−"}${cents(Math.abs(sum.maxMispricing.midVsFair!))}` : "–"}
              sub={
                sum.maxMispricing
                  ? `At ${usd0(sum.maxMispricing.strike)}: book mid ${cents(sum.maxMispricing.mid!)} vs fair ${pct(sum.maxMispricing.fair!)}`
                  : "No fair value for this expiry (under 2h to expiry or no options data)"
              }
            />
          </section>

          <section className="card">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="font-semibold">Implied probability curve</h2>
                <p className="muted text-xs">Price of &ldquo;BTC ≥ strike&rdquo; on each venue vs the Deribit options-implied value. Red rings mark crossed markets.</p>
              </div>
              <span className="muted text-xs">{updated ? `Updated ${updated.toLocaleTimeString()}` : ""}</span>
            </div>
            <MakerChart rows={view} selected={selected} onSelect={setSelected} />
          </section>

          <div className="grid gap-6 lg:grid-cols-[400px_1fr]">
            <QuoteSimulator row={selRow} rows={view} onSelect={setSelected} />

            <section className="card overflow-hidden p-0 sm:p-0">
              <div className="flex flex-wrap items-center justify-between gap-2 p-5 pb-3">
                <div>
                  <h2 className="font-semibold">Consolidated book</h2>
                  <p className="muted text-xs">Best bid and ask across both venues. Click a row to quote it.</p>
                </div>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={onlyBoth} onChange={(e) => setOnlyBoth(e.target.checked)} className="accent-[var(--accent)]" />
                  Both venues only
                </label>
              </div>
              <div className="max-h-[560px] overflow-auto">
                <table className="w-full text-sm">
                  <thead className="table-head sticky top-0" style={{ background: "var(--panel)" }}>
                    <tr>
                      <th>Strike</th>
                      <th>Best bid</th>
                      <th>Best ask</th>
                      <th className="text-right">Spread</th>
                      <th className="text-right">Fair</th>
                      <th className="text-right">Mid − fair</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view.map((r) => (
                      <tr
                        key={r.strike}
                        onClick={() => setSelected(r.strike)}
                        className={`row-hover cursor-pointer border-t border-line ${r.strike === selected ? "row-on" : ""}`}
                        aria-selected={r.strike === selected}
                      >
                        <td className="num px-3 py-2.5 font-medium">
                          {usd0(r.strike)}
                          {r.crossed && (
                            <span className="pill pill-bad ml-2" title={`Buy the ask and sell the bid as taker: ${signed(r.crossNet ?? 0)} per contract after fees`}>
                              crossed {signed(r.crossNet ?? 0)}
                            </span>
                          )}
                          {r.locked && <span className="pill ml-2">locked</span>}
                          {(r.stale.gemini || r.stale.kalshi) && <span className="pill pill-warn ml-2">stale</span>}
                        </td>
                        <td className="px-3 py-2.5">
                          <TouchCell t={r.bestBid} />
                        </td>
                        <td className="px-3 py-2.5">
                          <TouchCell t={r.bestAsk} />
                        </td>
                        <td className={`num px-3 py-2.5 text-right ${r.spread !== null && r.spread < 0 ? "text-bad" : ""}`}>{r.spread === null ? "–" : cents(r.spread)}</td>
                        <td className="num px-3 py-2.5 text-right">{r.fair === null ? <span className="muted">n/a</span> : cents(r.fair)}</td>
                        <td className="num px-3 py-2.5 text-right">
                          {r.midVsFair === null ? <span className="muted">–</span> : `${r.midVsFair > 0 ? "+" : "−"}${cents(Math.abs(r.midVsFair))}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {view.length === 0 && <p className="muted p-5 text-sm">No strikes are quoted on both venues for this expiry. Untick &ldquo;Both venues only&rdquo;.</p>}
              </div>
            </section>
          </div>

          <p className="muted text-xs">
            Top-of-book only, from public data refreshed every ~30s. Gemini settles on its own BTC index and Kalshi on CF Benchmarks BRTI, so
            &ldquo;crossed&rdquo; prices are not risk-free. Fair value is a risk-neutral options estimate. Analysis only; nothing is traded for you.
          </p>
        </>
      )}
    </main>
  );
}
