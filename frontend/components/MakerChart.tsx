"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { MakerRow } from "@/lib/maker";
import { cents } from "@/lib/format";

const H = 300;
const PAD = { l: 44, r: 16, t: 14, b: 34 };

const SERIES = [
  { key: "gemini", label: "Gemini mid", color: "var(--gemini)", dash: undefined },
  { key: "kalshi", label: "Kalshi mid", color: "var(--kalshi)", dash: undefined },
  { key: "fair", label: "Options fair (Deribit)", color: "var(--fair)", dash: "5 4" },
] as const;

type Key = (typeof SERIES)[number]["key"];

const valueOf = (r: MakerRow, k: Key) => (k === "fair" ? r.fair : r.venueMid[k]);
const kfmt = (s: number) => `$${(s / 1000).toFixed(s % 1000 ? 1 : 0)}k`;

export default function MakerChart({ rows, selected, onSelect }: { rows: MakerRow[]; selected: number | null; onSelect: (strike: number) => void }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(720);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(300, Math.round(e.contentRect.width))));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  const { x, y, xTicks, paths, band } = useMemo(() => {
    const strikes = rows.map((r) => r.strike);
    const lo = Math.min(...strikes);
    const hi = Math.max(...strikes);
    const span = hi - lo || 1;
    const x = (s: number) => PAD.l + ((s - lo) / span) * (w - PAD.l - PAD.r);
    const y = (p: number) => PAD.t + (1 - p) * (H - PAD.t - PAD.b);
    const n = Math.min(6, rows.length);
    const xTicks = Array.from({ length: n }, (_, i) => rows[Math.round((i * (rows.length - 1)) / Math.max(1, n - 1))]?.strike).filter(
      (v, i, a): v is number => v !== undefined && a.indexOf(v) === i,
    );

    const line = (k: Key) => {
      let d = "";
      let pen = false;
      for (const r of rows) {
        const v = valueOf(r, k);
        if (v === null) {
          pen = false;
          continue;
        }
        d += `${pen ? "L" : "M"}${x(r.strike).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      }
      return d;
    };
    const paths = Object.fromEntries(SERIES.map((s) => [s.key, line(s.key)])) as Record<Key, string>;

    const withBand = rows.filter((r) => r.bestBid && r.bestAsk);
    const band = withBand.length
      ? `M${withBand.map((r) => `${x(r.strike).toFixed(1)},${y(r.bestAsk!.px).toFixed(1)}`).join("L")}` +
        `L${[...withBand].reverse().map((r) => `${x(r.strike).toFixed(1)},${y(r.bestBid!.px).toFixed(1)}`).join("L")}Z`
      : "";
    return { x, y, xTicks, paths, band };
  }, [rows, w]);

  if (rows.length < 2) return <div className="muted text-sm">Not enough strikes to chart.</div>;

  const hv = hover !== null ? rows.find((r) => r.strike === hover) ?? null : null;
  const nearest = (px: number) => rows.reduce((a, b) => (Math.abs(x(b.strike) - px) < Math.abs(x(a.strike) - px) ? b : a)).strike;
  const tipLeft = hv ? Math.min(Math.max(x(hv.strike) + 12, 0), w - 200) : 0;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs" aria-label="Legend">
        {SERIES.map((s) => (
          <span key={s.key} className="flex items-center gap-2">
            <svg width="22" height="8" aria-hidden>
              <line x1="1" x2="21" y1="4" y2="4" stroke={s.color} strokeWidth="2" strokeDasharray={s.dash} strokeLinecap="round" />
            </svg>
            {s.label}
          </span>
        ))}
        <span className="flex items-center gap-2">
          <span className="inline-block h-3 w-5 rounded-sm" style={{ background: "color-mix(in srgb, var(--accent) 16%, transparent)" }} />
          Best bid–ask across venues
        </span>
      </div>

      <div ref={wrap} className="relative">
        <svg
          width={w}
          height={H}
          role="img"
          aria-label="Probability of BTC finishing at or above each strike, by venue mid price and options fair value"
          onMouseMove={(e) => {
            const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
            setHover(nearest(e.clientX - r.left));
          }}
          onMouseLeave={() => setHover(null)}
          onClick={() => hover !== null && onSelect(hover)}
          className="cursor-crosshair select-none"
        >
          {[0, 0.25, 0.5, 0.75, 1].map((t) => (
            <g key={t}>
              <line x1={PAD.l} x2={w - PAD.r} y1={y(t)} y2={y(t)} stroke="var(--line)" strokeWidth="1" />
              <text x={PAD.l - 8} y={y(t) + 4} textAnchor="end" fontSize="11" fill="var(--ink-2)">
                {Math.round(t * 100)}¢
              </text>
            </g>
          ))}
          {xTicks.map((s) => (
            <text key={s} x={x(s)} y={H - 10} textAnchor="middle" fontSize="11" fill="var(--ink-2)">
              {kfmt(s)}
            </text>
          ))}

          {band && <path d={band} fill="color-mix(in srgb, var(--accent) 16%, transparent)" />}

          {selected !== null && rows.some((r) => r.strike === selected) && (
            <line x1={x(selected)} x2={x(selected)} y1={PAD.t} y2={H - PAD.b} stroke="var(--accent)" strokeWidth="1.5" />
          )}

          {SERIES.map((s) => (
            <path key={s.key} d={paths[s.key]} fill="none" stroke={s.color} strokeWidth="2" strokeDasharray={s.dash} strokeLinejoin="round" strokeLinecap="round" />
          ))}

          {rows
            .filter((r) => r.crossed)
            .map((r) => (
              <circle key={`x${r.strike}`} cx={x(r.strike)} cy={y(r.mid ?? 0.5)} r="6" fill="none" stroke="var(--bad)" strokeWidth="2">
                <title>Crossed market at {kfmt(r.strike)}</title>
              </circle>
            ))}

          {hv && (
            <g pointerEvents="none">
              <line x1={x(hv.strike)} x2={x(hv.strike)} y1={PAD.t} y2={H - PAD.b} stroke="var(--ink-2)" strokeDasharray="2 3" />
              {SERIES.map((s) => {
                const v = valueOf(hv, s.key);
                return v === null ? null : (
                  <circle key={s.key} cx={x(hv.strike)} cy={y(v)} r="4.5" fill={s.color} stroke="var(--panel)" strokeWidth="2" />
                );
              })}
            </g>
          )}
        </svg>

        {hv && (
          <div
            className="pointer-events-none absolute top-2 w-48 rounded-lg border border-line p-2.5 text-xs shadow-lg"
            style={{ left: tipLeft, background: "var(--panel-2)" }}
          >
            <div className="mb-1 font-semibold">BTC ≥ {hv.strike.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}</div>
            {SERIES.map((s) => {
              const v = valueOf(hv, s.key);
              return (
                <div key={s.key} className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1.5">
                    <span className="dot" style={{ background: s.color }} />
                    {s.label.replace(" (Deribit)", "")}
                  </span>
                  <span className="num">{v === null ? "–" : cents(v)}</span>
                </div>
              );
            })}
            <div className="mt-1 flex justify-between border-t border-line pt-1">
              <span className="muted">Best spread</span>
              <span className="num">{hv.spread === null ? "–" : cents(hv.spread)}</span>
            </div>
            <div className="muted mt-1">Click to quote this strike</div>
          </div>
        )}
      </div>
    </div>
  );
}
