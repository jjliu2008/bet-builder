import type { Route, RouteRequest, VerdictCode } from "@/lib/types";
import { buildLabel, cents, expiryLabel, isStockOutcome, num, pct, usd, venueName } from "@/lib/format";
import GrokExplanation from "./GrokExplanation";
import Warnings from "./Warnings";
import SettlementChart from "./SettlementChart";
import SaveVerify from "./SaveVerify";
import MarketQualityPanel from "./MarketQualityPanel";

function describe(r: RouteRequest): string {
  const e = expiryLabel(r.expiry);
  if (r.type === "above") return `${r.asset} ${isStockOutcome(r.asset) ? "above" : "at or above"} ${usd(r.lo ?? r.hi ?? 0, 0)} at ${e}`;
  if (r.type === "below") return `${r.asset} ${isStockOutcome(r.asset) ? "at or below" : "below"} ${usd(r.hi ?? r.lo ?? 0, 0)} at ${e}`;
  return `${r.asset} between ${usd(r.lo ?? 0, 0)} and ${usd(r.hi ?? 0, 0)} at ${e}`;
}

const VERDICT_STYLE: Record<VerdictCode, { cls: string; label: string }> = {
  options_higher: { cls: "pill-good", label: "Options price it higher" },
  in_line: { cls: "pill-accent", label: "In line with options" },
  options_lower: { cls: "pill-warn", label: "Options price it lower" },
  unavailable: { cls: "", label: "No options comparison" },
};

function Verdict({ route }: { route: Route }) {
  const v = VERDICT_STYLE[route.verdict_code] ?? VERDICT_STYLE.unavailable;
  return (
    <span className={`pill ${v.cls}`} title={route.verdict_text}>
      {v.label}
    </span>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="num text-xl font-semibold">{value}</div>
      {sub && <div className="muted text-xs">{sub}</div>}
    </div>
  );
}

export default function RouteCard({ route, request }: { route: Route; request: RouteRequest }) {
  const size = route.filled_size ?? route.size ?? request.size;
  const cash = (route.cash_outlay ?? route.total_cost) * size;
  const finding = [...(route.market_quality?.findings ?? [])].sort((a, b) => b.score - a.score)[0];
  const netCost = route.total_cost * size;
  const hasStale = (route.stale_warnings ?? []).length > 0;
  const partial = route.filled_size !== undefined && route.size !== undefined && route.filled_size < route.size;
  // Equity demo books use DEMO- contract ids; anything that is not live BTC data is a simulation.
  const simulated = route.legs.some((l) => l.contract.startsWith("DEMO-"));

  return (
    <section className={`card space-y-5 ${simulated ? "" : "card-glow"}`} style={simulated ? { borderStyle: "dashed", borderColor: "var(--warn)" } : undefined}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="label">Recommended route · {buildLabel(route.build)}</div>
          <h2 className="text-lg font-semibold">{describe(request)}</h2>
        </div>
        <div className="flex flex-wrap gap-2">
          {simulated && <span className="pill pill-warn">◇ Simulated data</span>}
          {partial && <span className="pill pill-bad">Partial fill</span>}
          {hasStale && <span className="pill pill-bad">Stale quote</span>}
          <Verdict route={route} />
        </div>
      </div>

      <Stat label="What you pay upfront" value={usd(cash)} sub={`Includes fees for ${num(size)} of ${num(request.size)} requested contracts`} />
      {finding && <p className="note note-warn"><strong>Quote to check{finding.venue ? ` · ${venueName(finding.venue)}` : ""}</strong><br />{finding.message}</p>}
      <SettlementChart key={route.grok_snapshot ?? JSON.stringify(request)} route={route} request={request} />
      <section className="space-y-2">
        <h3 className="font-semibold">Does using more than one market save money?</h3>
        <p className="muted text-sm">Compare the cost for the same number of contracts after fees and any base payout.</p>
        <div className="grid gap-3 sm:grid-cols-3">
          <Stat label="Selected route" value={usd(netCost)} />
          <Stat label="Best price on one exchange" value={!partial && route.best_single_venue_cost != null ? usd(route.best_single_venue_cost * size) : "No matching quote"} />
          <Stat label="You save" value={!partial && route.best_single_venue_cost != null && route.savings != null ? usd(route.savings * size) : "No comparison"} />
        </div>
        {!partial && route.savings === 0 && <p className="text-sm">One market already offers the best quoted price.</p>}
      </section>
      <details>
        <summary className="cursor-pointer font-semibold mb-2">Which contracts and fees make up this price?</summary>
        <ul className="space-y-2">
          {route.legs.map((l, i) => (
            <li key={i} className="leg">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className={`pill ${l.side === "YES" ? "pill-good" : "pill-bad"}`}>{l.side}</span>
                  <span className="font-medium" style={{ color: `var(--${l.venue})` }}>
                    {venueName(l.venue)}
                    {simulated && <span className="muted font-normal"> (simulated book)</span>}
                  </span>
                  <span className="muted text-xs break-all">{l.contract}</span>
                </div>
                {l.link && !simulated && (
                  <a href={l.link} target="_blank" rel="noreferrer" className="link text-sm">
                    Open market ↗
                  </a>
                )}
              </div>
              <div className="num mt-2 grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm">
                <span>Avg price {cents(l.price)}</span>
                <span>Fee {cents(l.fee)}/ct</span>
                <span>Buy {num(l.size ?? size)}</span>
                <span className="muted">Available {num(l.size_available)}</span>
              </div>
            </li>
          ))}
        </ul>
        <p className="muted text-xs mt-2">Quoted at {new Date(route.timestamp).toLocaleString()}. Options market estimate: {route.fair_value == null ? "unavailable" : pct(route.fair_value)}.</p>
      </details>

      <div className="space-y-1" aria-label="Options comparison">
        <p className={`text-sm ${route.verdict_code === "unavailable" ? "muted" : ""}`}>{route.verdict_text}</p>
        {route.fair_note && <p className="muted text-xs">ⓘ {route.fair_note}</p>}
      </div>

      {route.market_quality && <MarketQualityPanel mq={route.market_quality} />}
      <Warnings route={route} />
      {route.grok_snapshot && <GrokExplanation key={route.grok_snapshot} snapshot={route.grok_snapshot} />}
      {simulated ? (
        <p className="note note-warn">
          Illustrative books, not live venue prices. Solana receipts are only written for live BTC routes.
        </p>
      ) : (
        <SaveVerify key={route.grok_snapshot ?? route.timestamp} route={route} request={request} />
      )}

      <p className="muted text-xs">
        This is a quote. Prices can change, and building a route does not place an order.
      </p>
    </section>
  );
}
