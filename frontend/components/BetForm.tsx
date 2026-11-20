"use client";

import { useEffect, useState } from "react";
import type { BetType, LadderRow, RouteRequest } from "@/lib/types";
import { expiryLabel, isStockOutcome } from "@/lib/format";

import { getLadder } from "@/lib/api";

interface Props {
  asset: string;
  expiries: string[];
  loadingExpiries: boolean;
  busy: boolean;
  onSubmit: (req: RouteRequest) => void;
}

const TYPES: { id: BetType; label: string }[] = [
  { id: "above", label: "Above" },
  { id: "below", label: "Below" },
  { id: "between", label: "Between" },
];

export default function BetForm({ asset, expiries, loadingExpiries, busy, onSubmit }: Props) {
  const [expiry, setExpiry] = useState("");
  const [preferredType, setType] = useState<BetType>("between");
  const [manualLo, setLo] = useState<string | null>(null);
  const [manualHi, setHi] = useState<string | null>(null);
  const [ladder, setLadder] = useState<{ expiry: string; rows: LadderRow[] } | null>(null);
  const [size, setSize] = useState("10");
  const [error, setError] = useState<string | null>(null);

  const chosen = expiries.includes(expiry) ? expiry : expiries[0] || "";

  useEffect(() => {
    if (!chosen) return;
    let active = true;
    getLadder(chosen, asset).then(rows => {
      if (active) setLadder({ expiry: chosen, rows: [...rows].sort((a, b) => a.strike - b.strike) });
    }).catch(() => {
      if (active) setLadder({ expiry: chosen, rows: [] });
    });
    return () => { active = false; };
  }, [chosen, asset]);

  const rows = ladder?.expiry === chosen ? ladder.rows : [];
  const type = rows.length === 1 && preferredType === "between" ? "above" : preferredType;
  const availableTypes = rows.length === 1 ? TYPES.filter(t => t.id !== "between") : TYPES;
  function hasDepth(row: LadderRow, side: "bid" | "ask") {
    return (["gemini", "kalshi", "polymarket"] as const).some(venue => {
      const q = row[venue];
      const depth = (side === "ask" ? q?.ask_size : q?.bid_size) ?? 0;
      return q && !row.stale[venue] && q[side] !== null && depth > 0 && depth >= Number(size);
    });
  }
  const bids = rows.filter(row => hasDepth(row, "bid"));
  const asks = rows.filter(row => hasDepth(row, "ask"));
  // The threshold nearest a 50-cent YES quote approximates the center for this expiry.
  let center = rows[Math.floor(rows.length / 2)]?.strike ?? 0;
  let smallestGap = Infinity;
  for (const row of rows) {
    const quotes = (["gemini", "kalshi", "polymarket"] as const).flatMap(venue => {
      const q = row[venue];
      return !row.stale[venue] && q && q.bid !== null && q.ask !== null && q.bid <= q.ask
        ? [(q.bid + q.ask) / 2] : [];
    });
    const price = quotes.length ? quotes.reduce((sum, q) => sum + q, 0) / quotes.length : row.fair;
    if (price == null || price < 0 || price > 1) continue;
    const gap = Math.abs(price - 0.5);
    if (gap < smallestGap) {
      smallestGap = gap;
      center = row.strike;
    }
  }
  const nearest = (items: LadderRow[]) => items.reduce<LadderRow | undefined>((best, row) =>
    !best || Math.abs(row.strike - center) < Math.abs(best.strike - center) ? row : best, undefined);
  const ceiling = Number(manualHi ?? bids.at(-1)?.strike);
  const eligibleLower = asks.filter(row => row.strike < ceiling);
  const lower = type === "between"
    ? eligibleLower.filter(row => row.strike < center).at(-1) ?? nearest(eligibleLower)
    : nearest(asks);
  const floor = Number(manualLo ?? lower?.strike);
  const eligibleUpper = bids.filter(row => row.strike > floor);
  const upper = type === "between"
    ? eligibleUpper.find(row => row.strike > center) ?? nearest(eligibleUpper)
    : nearest(bids);
  const lo = manualLo ?? String(lower?.strike ?? "");
  const hi = manualHi ?? String(upper?.strike ?? "");

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const loN = lo === "" ? null : Number(lo);
    const hiN = hi === "" ? null : Number(hi);
    const sizeN = Number(size);
    if (!chosen) return setError("Pick an expiry.");
    if (!(sizeN > 0)) return setError("Size must be greater than 0.");
    if (type === "above" && (loN === null || !(loN > 0))) return setError("Enter the strike price.");
    if (type === "below" && (hiN === null || !(hiN > 0))) return setError("Enter the strike price.");
    if (type === "between") {
      if (loN === null || hiN === null || !(loN > 0)) return setError("Enter both range boundaries.");
      if (loN >= hiN) return setError("The lower boundary must be below the upper boundary.");
    }
    setError(null);
    onSubmit({
      asset,
      expiry: chosen,
      type,
      lo: type === "below" ? null : loN,
      hi: type === "above" ? null : hiN,
      size: sizeN,
    });
  }

  return (
    <form onSubmit={submit} className="card space-y-5">
      <div>
        <h2 className="text-lg font-semibold">Build your bet</h2>
        <p className="muted text-sm">Pick the outcome. We find the cheapest way to buy it.</p>
      </div>

      <label className="block">
        <span className="label">End date and time</span>
        <select className="input" value={chosen} onChange={(e) => { setExpiry(e.target.value); setLo(null); setHi(null); setError(null); }} disabled={loadingExpiries || !expiries.length}>
          {loadingExpiries && <option>Loading expiries…</option>}
          {!loadingExpiries && !expiries.length && <option>No expiries available</option>}
          {expiries.map((x) => (
            <option key={x} value={x}>
              {expiryLabel(x)}
            </option>
          ))}
        </select>
      </label>

      <div>
        <span className="label">{asset} finishes</span>
        <div className="grid grid-cols-3 gap-2" role="radiogroup">
          {availableTypes.map((t) => (
            <button
              type="button"
              key={t.id}
              role="radio"
              aria-checked={type === t.id}
              onClick={() => setType(t.id)}
              className={`seg ${type === t.id ? "seg-on" : ""}`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {type === "above" && (
          <label className="block col-span-2">
            <span className="label">{isStockOutcome(asset) ? "Above ($)" : "At or above ($)"}</span>
            <input className="input" list="listed-strikes" inputMode="decimal" value={lo} onChange={(e) => setLo(e.target.value)} />
          </label>
        )}
        {type === "below" && (
          <label className="block col-span-2">
            <span className="label">{isStockOutcome(asset) ? "At or below ($)" : "Below ($)"}</span>
            <input className="input" list="listed-strikes" inputMode="decimal" value={hi} onChange={(e) => setHi(e.target.value)} />
          </label>
        )}
        {type === "between" && (
          <>
            <label className="block">
              <span className="label">{isStockOutcome(asset) ? "Above ($)" : "From ($)"}</span>
              <input className="input" list="listed-strikes" inputMode="decimal" value={lo} onChange={(e) => setLo(e.target.value)} />
            </label>
            <label className="block">
              <span className="label">{isStockOutcome(asset) ? "Up to and including ($)" : "Up to, but below ($)"}</span>
              <input className="input" list="listed-strikes" inputMode="decimal" value={hi} onChange={(e) => setHi(e.target.value)} />
            </label>
          </>
        )}
      </div>

      {rows.length > 0 && <div className="flex flex-wrap gap-2 text-xs" aria-label="Examples from this market">
        {availableTypes.map(example => <button key={example.id} type="button" className="link"
          onClick={() => { setType(example.id); setLo(null); setHi(null); setError(null); }}>
          Try {example.label.toLowerCase()} near the market center
        </button>)}
      </div>}
      <label className="block">
        <span className="label">Size (contracts)</span>
        <input className="input" inputMode="decimal" value={size} onChange={(e) => setSize(e.target.value)} />
      </label>

      <datalist id="listed-strikes">{rows.map(row => <option key={row.strike} value={row.strike} />)}</datalist>
      {error && <p role="alert" className="text-sm text-bad">{error}</p>}

      <button type="submit" disabled={busy || loadingExpiries || !chosen} className="btn w-full">
        {busy ? "Building route…" : "Build route"}
      </button>
    </form>
  );
}
