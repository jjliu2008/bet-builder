import type { Route } from "@/lib/types";

export default function Warnings({ route }: { route: Route }) {
  const items: { kind: "bad" | "warn"; text: string }[] = [];
  for (const s of route.stale_warnings ?? []) items.push({ kind: "bad", text: `Stale quote: ${s}` });
  for (const w of route.warnings ?? []) items.push({ kind: "warn", text: w });
  if (route.mixed_settlement_index)
    items.push({
      kind: "warn",
      text: "Legs settle on different indexes (Gemini's own index vs. Kalshi's CF Benchmarks BRTI). Outcomes can differ near the boundary.",
    });
  if (route.filled_size !== undefined && route.size !== undefined && route.filled_size < route.size)
    items.push({ kind: "bad", text: `Only ${route.filled_size} of ${route.size} contracts can be filled from visible depth.` });
  if (!items.length) return null;
  return (
    <ul className="space-y-2" aria-label="Warnings">
      {items.map((i, k) => (
        <li key={k} className={i.kind === "bad" ? "note note-bad" : "note note-warn"}>
          {i.text}
        </li>
      ))}
    </ul>
  );
}
