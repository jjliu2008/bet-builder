import type { Route, RouteRequest } from "@/lib/types";
import { buildLabel, cents, expiryLabel, num, pct, usd, venueName } from "@/lib/format";
import Warnings from "./Warnings";
import SaveVerify from "./SaveVerify";

function describe(r: RouteRequest): string {
  const e = expiryLabel(r.expiry);
  if (r.type === "above") return `BTC at or above ${usd(r.lo ?? r.hi ?? 0, 0)} at ${e}`;
  if (r.type === "below") return `BTC below ${usd(r.hi ?? r.lo ?? 0, 0)} at ${e}`;
  return `BTC between ${usd(r.lo ?? 0, 0)} and ${usd(r.hi ?? 0, 0)} at ${e}`;
}

function Verdict({ route }: { route: Route }) {
  const v = route.verdict;
  if (route.fair_value === null || !v)
    return <span className="pill">Fair value unavailable</span>;
  if (v.startsWith("n/a")) return <span className="pill">Fair value n/a (too close to expiry)</span>;
  const cls = v === "good deal" ? "pill-good" : v === "overpriced" ? "pill-bad" : "pill-warn";
  return <span className={`pill ${cls}`}>{v[0].toUpperCase() + v.slice(1)}</span>;
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
      {sub && <div className="muted text-xs">{sub}</div>}
    </div>
  );
}

export default function RouteCard({ route, request }: { route: Route; request: RouteRequest }) {
  const size = route.filled_size ?? route.size ?? request.size;
  const cash = (route.cash_outlay ?? route.total_cost) * size;
  const guaranteed = (route.guaranteed_payout ?? 0) * size;
  const minPayout = guaranteed;
  const maxPayout = guaranteed + route.max_payout * size;
  const netCost = route.total_cost * size;
  const hasStale = (route.stale_warnings ?? []).length > 0;
  const partial = route.filled_size !== undefined && route.size !== undefined && route.filled_size < route.size;

  return (
    <section className="card space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="label">Recommended route · {buildLabel(route.build)}</div>
          <h2 className="text-lg font-semibold">{describe(request)}</h2>
        </div>
        <div className="flex gap-2">
          {partial && <span className="pill pill-bad">Partial fill</span>}
          {hasStale && <span className="pill pill-bad">Stale quote</span>}
          <Verdict route={route} />
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <Stat label="Upfront cash" value={usd(cash)} sub={partial ? `${num(size)} of ${num(route.size ?? size)} requested, incl. fees` : `${num(size)} contracts, incl. fees`} />
        <Stat label="Net risk" value={usd(netCost)} sub={guaranteed > 0 ? `after ${usd(guaranteed, 0)} guaranteed` : "max loss"} />
        <Stat
          label={guaranteed > 0 ? "Payout in range" : "Payout if you win"}
          value={usd(maxPayout)}
          sub={guaranteed > 0 ? `${usd(minPayout)} outside range` : "$0 otherwise"}
        />
        <Stat
          label="Savings"
          value={route.savings === null ? "n/a" : usd(route.savings * size)}
          sub={route.savings === null ? "no single-venue fill" : "vs. best single venue"}
        />
      </div>

      <div>
        <h3 className="font-semibold mb-2">Contracts to buy</h3>
        <ul className="space-y-2">
          {route.legs.map((l, i) => (
            <li key={i} className="leg">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className={`pill ${l.side === "YES" ? "pill-good" : "pill-bad"}`}>{l.side}</span>
                  <span className="font-medium">{venueName(l.venue)}</span>
                  <span className="muted text-xs break-all">{l.contract}</span>
                </div>
                {l.link && (
                  <a href={l.link} target="_blank" rel="noreferrer" className="link text-sm">
                    Open market ↗
                  </a>
                )}
              </div>
              <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm tabular-nums">
                <span>Avg price {cents(l.price)}</span>
                <span>Fee {cents(l.fee)}/ct</span>
                <span>Buy {num(l.size ?? size)}</span>
                <span className="muted">Depth {num(l.size_available)}</span>
              </div>
            </li>
          ))}
        </ul>
        {route.legs.length === 2 && route.guaranteed_payout ? (
          <p className="muted text-xs mt-2">
            This two-leg spread pays $2 inside the range and $1 outside it, so effective cost is cash minus the guaranteed $1.
          </p>
        ) : null}
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 border-t border-line pt-4">
        <Stat label="Effective cost" value={cents(route.total_cost)} sub="per $1 of payout" />
        <Stat label="Options fair value" value={route.fair_value === null ? "n/a" : pct(route.fair_value)} sub="Deribit-implied" />
        <Stat
          label="Best single venue"
          value={route.best_single_venue_cost === null ? "n/a" : cents(route.best_single_venue_cost)}
        />
        <Stat label="Quoted at" value={new Date(route.timestamp).toLocaleTimeString()} />
      </div>

      <Warnings route={route} />
      <SaveVerify route={route} request={request} />

      <p className="muted text-xs">
        Analysis only. Fair value is a risk-neutral options estimate, not a guarantee of profit. Nothing is traded for you.
      </p>
    </section>
  );
}
