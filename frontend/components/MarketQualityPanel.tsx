import type { MarketQuality } from "@/lib/types";

const STATUS: Record<string, { cls: string; icon: string; label: string }> = {
  clear: { cls: "pill-good", icon: "✓", label: "Clear" },
  caution: { cls: "pill-warn", icon: "!", label: "Caution" },
  review: { cls: "pill-warn", icon: "!", label: "Review" },
  unreliable: { cls: "pill-bad", icon: "✕", label: "Unreliable" },
};

/** Advisory market-quality assessment (AEGIS). Shown only when the backend returns it. */
export default function MarketQualityPanel({ mq }: { mq: MarketQuality }) {
  const s = STATUS[mq.status] ?? { cls: "", icon: "?", label: mq.status };
  const score = Math.max(0, Math.min(100, mq.risk_score));
  const barColor = score >= 60 ? "var(--bad)" : score >= 30 ? "var(--warn)" : "var(--good)";
  return (
    <div className="leg space-y-3" aria-label="Market quality">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Market quality</span>
          <span className={`pill ${s.cls}`}>
            <span aria-hidden>{s.icon}</span> {s.label}
          </span>
        </div>
        <span className="muted text-xs">Advisory · visible books only</span>
      </div>
      <div>
        <div className="mb-1 flex justify-between text-xs">
          <span className="muted">Risk score</span>
          <span className="num">{score}/100</span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full" style={{ background: "var(--line)" }} role="meter" aria-valuenow={score} aria-valuemin={0} aria-valuemax={100} aria-label="Risk score">
          <div className="h-full rounded-full" style={{ width: `${score}%`, background: barColor }} />
        </div>
      </div>
      {mq.findings.length > 0 && (
        <ul className="space-y-1 text-sm">
          {mq.findings.map((f, i) => (
            <li key={i} className="flex gap-2">
              <span className="num muted w-10 shrink-0 text-xs leading-5">{f.score}</span>
              <span>
                {f.message}
                {f.venue && <span className="muted"> · {f.venue}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
