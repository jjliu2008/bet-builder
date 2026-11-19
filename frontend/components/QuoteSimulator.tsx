"use client";

import { useState } from "react";
import { cents, venueName } from "@/lib/format";
import { simulateQuote, type Anchor, type MakerRow, type Placement, type VenuePlacement } from "@/lib/maker";

const FEES = [
  { rate: 0, label: "No maker fee" },
  { rate: 0.0175, label: "0.0175·P(1−P)" },
  { rate: 0.07, label: "0.07·P(1−P)" },
];

const usd0 = (n: number) => n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const signedC = (v: number) => `${v >= 0 ? "+" : "−"}${cents(Math.abs(v))}`;

function placementText(p: Placement, gap: number | null, queue: number | null): { text: string; cls: string } {
  switch (p) {
    case "crosses":
      return { text: "Would trade now (taker)", cls: "pill-bad" };
    case "improves":
      return { text: gap === null ? "Only quote on this side" : `New best, ${cents(gap)} better`, cls: "pill-good" };
    case "joins":
      return { text: `Joins best, ${Math.round(queue ?? 0).toLocaleString()} ahead`, cls: "pill-accent" };
    case "behind":
      return { text: `Behind best by ${cents(Math.abs(gap ?? 0))}`, cls: "" };
    default:
      return { text: "Not listed", cls: "" };
  }
}

function VenueRow({ p }: { p: VenuePlacement }) {
  const b = placementText(p.bid, p.bidGap, p.bidQueue);
  const a = placementText(p.ask, p.askGap, p.askQueue);
  return (
    <div className="leg space-y-1.5">
      <div className="text-sm font-medium" style={{ color: `var(--${p.venue})` }}>
        {venueName(p.venue)}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="muted w-8">Bid</span>
        <span className={`pill ${b.cls}`}>{b.text}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="muted w-8">Ask</span>
        <span className={`pill ${a.cls}`}>{a.text}</span>
      </div>
    </div>
  );
}

export default function QuoteSimulator({ row, rows, onSelect }: { row: MakerRow | null; rows: MakerRow[]; onSelect: (s: number) => void }) {
  const [anchor, setAnchor] = useState<Anchor>("fair");
  const [half, setHalf] = useState(2); // cents
  const [skew, setSkew] = useState(0); // cents
  const [makerRate, setMakerRate] = useState(0);

  const effectiveAnchor: Anchor = anchor === "fair" && row?.fair === null ? "mid" : anchor;
  const q = row ? simulateQuote(row, { anchor: effectiveAnchor, halfWidth: half / 100, skew: skew / 100, makerRate }) : null;
  const crosses = q?.placements.some((p) => p.bid === "crosses" || p.ask === "crosses");

  return (
    <section className="card card-glow space-y-5" aria-label="Quote simulator">
      <div>
        <h2 className="font-semibold">Quote simulator</h2>
        <p className="muted text-xs">Post a two-sided quote around a reference price and see where it would rest in each venue&apos;s book.</p>
      </div>

      <label className="block">
        <span className="label">Strike (BTC ≥)</span>
        <select className="input" value={row?.strike ?? ""} onChange={(e) => onSelect(Number(e.target.value))}>
          {rows.map((r) => (
            <option key={r.strike} value={r.strike}>
              {usd0(r.strike)}
            </option>
          ))}
        </select>
      </label>

      <div>
        <span className="label">Anchor quotes on</span>
        <div className="grid grid-cols-2 gap-2" role="radiogroup">
          {(["fair", "mid"] as Anchor[]).map((a) => (
            <button
              key={a}
              type="button"
              role="radio"
              aria-checked={anchor === a}
              onClick={() => setAnchor(a)}
              disabled={a === "fair" && row?.fair === null}
              className={`seg ${anchor === a ? "seg-on" : ""} disabled:opacity-40`}
            >
              {a === "fair" ? "Options fair value" : "Book mid"}
            </button>
          ))}
        </div>
        {anchor === "fair" && row?.fair === null && <p className="muted mt-1 text-xs">No fair value for this strike, using book mid.</p>}
      </div>

      <label className="block">
        <span className="label flex justify-between">
          <span>Half-width</span>
          <span className="num normal-case tracking-normal">±{half.toFixed(1)}¢</span>
        </span>
        <input type="range" min={0.5} max={10} step={0.5} value={half} onChange={(e) => setHalf(Number(e.target.value))} className="w-full accent-[var(--accent)]" />
      </label>

      <label className="block">
        <span className="label flex justify-between">
          <span>Inventory skew</span>
          <span className="num normal-case tracking-normal">{skew > 0 ? "+" : ""}{skew.toFixed(1)}¢</span>
        </span>
        <input type="range" min={-5} max={5} step={0.5} value={skew} onChange={(e) => setSkew(Number(e.target.value))} className="w-full accent-[var(--accent)]" />
        <span className="muted text-xs">Shift both sides. Negative leans you toward selling (to shed a long position).</span>
      </label>

      <div>
        <span className="label">Maker fee</span>
        <div className="grid grid-cols-3 gap-2">
          {FEES.map((f) => (
            <button key={f.rate} type="button" onClick={() => setMakerRate(f.rate)} className={`seg text-xs ${makerRate === f.rate ? "seg-on" : ""}`}>
              {f.label}
            </button>
          ))}
        </div>
        <span className="muted text-xs">Maker fees differ by venue and market. Pick the one that applies to you.</span>
      </div>

      {!row && <p className="muted text-sm">Pick a strike from the chart or the book.</p>}
      {row && !q && <p className="muted text-sm">No reference price for this strike yet.</p>}

      {q && (
        <div className="space-y-4 border-t border-line pt-4">
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="stat-tile p-3">
              <div className="label">Your bid</div>
              <div className="num text-xl font-semibold" style={{ color: "var(--good)" }}>
                {cents(q.bid)}
              </div>
            </div>
            <div className="stat-tile p-3">
              <div className="label">{effectiveAnchor === "fair" ? "Fair" : "Mid"}</div>
              <div className="num text-xl font-semibold">{cents(q.reference)}</div>
            </div>
            <div className="stat-tile p-3">
              <div className="label">Your ask</div>
              <div className="num text-xl font-semibold" style={{ color: "var(--bad)" }}>
                {cents(q.ask)}
              </div>
            </div>
          </div>

          <div className="space-y-2">
            {q.placements.map((p) => (
              <VenueRow key={p.venue} p={p} />
            ))}
          </div>

          <dl className="grid grid-cols-3 gap-2 text-xs">
            <div>
              <dt className="muted">Edge if bid fills</dt>
              <dd className="num text-sm font-semibold">{signedC(q.edgeIfBidFilled)}</dd>
            </div>
            <div>
              <dt className="muted">Edge if ask fills</dt>
              <dd className="num text-sm font-semibold">{signedC(q.edgeIfAskFilled)}</dd>
            </div>
            <div>
              <dt className="muted">Round trip</dt>
              <dd className="num text-sm font-semibold">{signedC(q.roundTrip)}</dd>
            </div>
          </dl>

          {crosses && (
            <p className="note note-warn">
              Part of this quote would trade immediately against a resting order, so it takes liquidity instead of providing it. Widen the
              quote or move the skew.
            </p>
          )}
          <p className="muted text-xs">
            Edge is per contract against the {effectiveAnchor === "fair" ? "options fair value" : "book mid"}, after the maker fee you picked.
            One filled side leaves you holding BTC price risk until the other side fills or you hedge.
          </p>
        </div>
      )}
    </section>
  );
}
