"use client";

import { useState } from "react";
import { num, usd } from "@/lib/format";
import type { Route, RouteRequest } from "@/lib/types";

export default function SettlementChart({ route, request }: { route: Route; request: RouteRequest }) {
  const bounds = request.type === "between" ? [request.lo!, request.hi!] :
    [request.type === "below" ? request.hi ?? request.lo! : request.lo ?? request.hi!];
  const first = bounds[0], last = bounds[bounds.length - 1];
  const padding = Math.max((last - first) / 2, last * .01, .01);
  const min = Math.max(0, first - padding), max = last + padding;
  const [price, setPrice] = useState((first + last) / 2);
  const wins = (p: number) => request.type === "above" ? p >= first :
    request.type === "below" ? p < first : p >= first && p < last;
  const size = route.filled_size ?? request.size;
  const payout = size * ((route.guaranteed_payout ?? 0) + (wins(price) ? route.max_payout : 0));
  const net = payout - size * (route.cash_outlay ?? route.total_cost);
  const x = (p: number) => 100 * (p - min) / (max - min);
  const y = (p: number) => wins(p) ? 12 : 54;
  const line = 'M0 ' + y(min) + bounds.map(b => ' H' + x(b) + ' V' + y(b)).join('') + ' H100';
  return <section className="leg space-y-3">
    <h3 className="font-semibold">What if {request.asset} finishes at {usd(price)}?</h3>
    {route.mixed_settlement_index && <p className="note note-warn">This chart assumes both markets use the same final price. Their price sources differ, so the actual payout can differ.</p>}
    <svg viewBox="0 0 100 66" preserveAspectRatio="none" className="h-20 w-full" role="img" aria-label="Payout rises or falls at the marked strike prices">
      {bounds.map(b => <line key={b} x1={x(b)} x2={x(b)} y1="0" y2="66" stroke="currentColor" opacity=".3" strokeDasharray="2 2" vectorEffect="non-scaling-stroke" />)}
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="3" vectorEffect="non-scaling-stroke" />
      <line x1={x(price)} x2={x(price)} y1="0" y2="66" stroke="currentColor" vectorEffect="non-scaling-stroke" />
    </svg>
    <input aria-label="Final asset price" type="range" min={min} max={max} step="any" value={price} onChange={e => setPrice(Number(e.target.value))} className="w-full" />
    <div className="flex flex-wrap items-center gap-2 text-xs"><span>Try an exact boundary</span>{bounds.map(b => <button type="button" key={b} className="btn-outline" onClick={() => setPrice(b)}>{usd(b)}</button>)}</div>
    <p className="text-sm">{request.type === "between" ? `The extra payout starts at ${usd(first)} and ends just below ${usd(last)}.` : request.type === "above" ? `The payout starts at ${usd(first)}, including that price.` : `The payout applies below ${usd(first)}, excluding that price.`}</p>
    <p aria-live="polite">For {num(size)} priced contracts, you receive <strong>{usd(payout)}</strong>. After fees, your {net < 0 ? "loss" : "gain"} is <strong>{usd(Math.abs(net))}</strong>.</p>
  </section>;
}
