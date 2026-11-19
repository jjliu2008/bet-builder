import type { Route } from "@/lib/types";
import { buildLabel, cents, num, venueName } from "@/lib/format";

export default function Alternatives({ route }: { route: Route }) {
  const alts = route.alternatives ?? [];
  if (alts.length < 2) return null;
  const sorted = [...alts].sort((a, b) => Number(b.complete) - Number(a.complete) || a.total_cost - b.total_cost);
  return (
    <details className="card">
      <summary className="cursor-pointer font-semibold mb-3">Compare other ways to build this route</summary>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="muted text-left">
            <tr>
              <th className="py-1 pr-3 font-medium">Build</th>
              <th className="py-1 pr-3 font-medium">Venues</th>
              <th className="py-1 pr-3 font-medium text-right">Cost / $1</th>
              <th className="py-1 font-medium text-right">Fillable</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((a, i) => {
              const chosen = a.build === route.build && Math.abs(a.total_cost - route.total_cost) < 1e-9;
              return (
                <tr key={i} className={`border-t border-line ${chosen ? "text-good font-medium" : ""}`}>
                  <td className="py-2 pr-3">
                    {buildLabel(a.build)} {chosen && <span className="pill pill-good ml-1">chosen</span>}
                  </td>
                  <td className="py-2 pr-3">{a.venues.map(venueName).join(" + ")}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{cents(a.total_cost)}</td>
                  <td className="py-2 text-right tabular-nums">
                    {a.complete ? "Full" : `Partial (${num(a.filled_size)})`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}
