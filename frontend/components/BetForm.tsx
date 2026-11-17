"use client";

import { useState } from "react";
import type { BetType, RouteRequest } from "@/lib/types";
import { expiryLabel } from "@/lib/format";

interface Props {
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

export default function BetForm({ expiries, loadingExpiries, busy, onSubmit }: Props) {
  const [expiry, setExpiry] = useState("");
  const [type, setType] = useState<BetType>("between");
  const [lo, setLo] = useState("66000");
  const [hi, setHi] = useState("68000");
  const [size, setSize] = useState("10");
  const [error, setError] = useState<string | null>(null);

  const chosen = expiry || expiries[0] || "";

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
      asset: "BTC",
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
        <span className="label">Asset</span>
        <select className="input" disabled>
          <option>BTC (Bitcoin)</option>
        </select>
      </label>

      <label className="block">
        <span className="label">Expiry</span>
        <select className="input" value={chosen} onChange={(e) => setExpiry(e.target.value)} disabled={loadingExpiries || !expiries.length}>
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
        <span className="label">BTC finishes</span>
        <div className="grid grid-cols-3 gap-2" role="radiogroup">
          {TYPES.map((t) => (
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
            <span className="label">At or above ($)</span>
            <input className="input" inputMode="decimal" value={lo} onChange={(e) => setLo(e.target.value)} />
          </label>
        )}
        {type === "below" && (
          <label className="block col-span-2">
            <span className="label">Below ($)</span>
            <input className="input" inputMode="decimal" value={hi} onChange={(e) => setHi(e.target.value)} />
          </label>
        )}
        {type === "between" && (
          <>
            <label className="block">
              <span className="label">From ($)</span>
              <input className="input" inputMode="decimal" value={lo} onChange={(e) => setLo(e.target.value)} />
            </label>
            <label className="block">
              <span className="label">To, exclusive ($)</span>
              <input className="input" inputMode="decimal" value={hi} onChange={(e) => setHi(e.target.value)} />
            </label>
          </>
        )}
      </div>

      <label className="block">
        <span className="label">Size (contracts = $ paid out if you win)</span>
        <input className="input" inputMode="decimal" value={size} onChange={(e) => setSize(e.target.value)} />
      </label>

      {error && <p role="alert" className="text-sm text-bad">{error}</p>}

      <button type="submit" disabled={busy || loadingExpiries} className="btn w-full">
        {busy ? "Building route…" : "Build route"}
      </button>
    </form>
  );
}
