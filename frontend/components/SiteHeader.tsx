"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getHealth } from "@/lib/api";
import type { Health } from "@/lib/types";

const NAV = [
  { href: "/", label: "Bet builder" },
  { href: "/maker", label: "Maker desk" },
  { href: "/ladder", label: "Strike ladder" },
];

type Feed = { state: "loading" | "ok" | "starting" | "degraded" | "offline"; venues: Record<string, boolean | null> };

function useFeed(): Feed {
  const [feed, setFeed] = useState<Feed>({ state: "loading", venues: {} });
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const h: Health = await getHealth();
        const venues: Record<string, boolean | null> = {};
        const raw = (h.venues ?? {}) as Record<string, { ok?: boolean | null }>;
        for (const [k, v] of Object.entries(raw)) venues[k] = v?.ok ?? null;
        const state = h.status === "ok" ? "ok" : h.status === "starting" ? "starting" : "degraded";
        if (alive) setFeed({ state, venues });
      } catch {
        if (alive) setFeed({ state: "offline", venues: {} });
      }
    };
    load();
    const id = setInterval(load, 30_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);
  return feed;
}

const STATE_LABEL: Record<Feed["state"], { text: string; color: string }> = {
  loading: { text: "Connecting", color: "var(--ink-2)" },
  ok: { text: "Live", color: "var(--good)" },
  starting: { text: "Warming up", color: "var(--warn)" },
  degraded: { text: "Degraded", color: "var(--warn)" },
  offline: { text: "Offline", color: "var(--bad)" },
};

export default function SiteHeader() {
  const path = usePathname();
  const feed = useFeed();
  const s = STATE_LABEL[feed.state];
  const venueTitle = Object.entries(feed.venues)
    .map(([k, ok]) => `${k}: ${ok === null ? "pending" : ok ? "ok" : "down"}`)
    .join(" · ");

  return (
    <header className="sticky top-0 z-20 border-b border-line backdrop-blur-md" style={{ background: "color-mix(in srgb, var(--bg) 75%, transparent)" }}>
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <span
            aria-hidden
            className="grid h-7 w-7 place-items-center rounded-lg text-sm font-bold"
            style={{ background: "linear-gradient(135deg, var(--accent), var(--accent-2))", color: "var(--accent-ink)" }}
          >
            B
          </span>
          <span>
            Bet-ter <span className="gradient-text">Crypto</span>
          </span>
        </Link>

        <nav className="order-3 flex w-full gap-1 overflow-x-auto sm:order-none sm:w-auto" aria-label="Main">
          {NAV.map((n) => {
            const on = n.href === "/" ? path === "/" : path.startsWith(n.href);
            return (
              <Link key={n.href} href={n.href} className={`nav-link ${on ? "nav-link-on" : ""}`} aria-current={on ? "page" : undefined}>
                {n.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3 text-xs">
          <span className="pill" title={venueTitle || undefined} aria-live="polite">
            <span className={`dot ${feed.state === "ok" ? "live-dot" : ""}`} style={{ background: s.color }} />
            <span style={{ color: s.color }}>{s.text}</span>
            <span className="muted hidden xl:inline">· Gemini · Kalshi · Deribit</span>
          </span>
          <span className="pill pill-accent hidden lg:inline-flex" title="Receipts are written to Solana devnet">
            ◎ Solana devnet
          </span>
        </div>
      </div>
    </header>
  );
}
