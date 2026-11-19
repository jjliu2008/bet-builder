import Link from "next/link";
import AssetBuilder from "@/components/AssetBuilder";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:py-12">
      <header className="mb-10 grid gap-8 lg:grid-cols-[1fr_auto] lg:items-end">
        <div className="space-y-4">
          <span className="pill pill-accent">Prediction markets · Gemini × Kalshi · Solana receipts</span>
          <h1 className="text-4xl font-bold leading-[1.05] tracking-tight sm:text-5xl">
            Where will Bitcoin finish?
            <br />
            <span className="gradient-text">Compare the cost.</span>
          </h1>
          <p className="muted max-w-2xl text-lg">
            Pick a price range and a date. See what it would cost on Gemini and Kalshi, how much you could receive, and where the quotes may be unreliable. You can also look up prices for stocks and other crypto.
          </p>
          <div className="flex flex-wrap gap-3 text-sm">
            <Link href="/maker" className="btn-outline">
              Open the maker desk →
            </Link>
            <Link href="/ladder" className="nav-link">
              Strike ladder
            </Link>
          </div>
        </div>
        <ol className="grid gap-2 text-sm sm:grid-cols-3 lg:w-[420px] lg:grid-cols-1">
          {[
            ["1", "Route", "Find contracts that cover your outcome"],
            ["2", "Price", "Compare available quantities and prices, including fees"],
            ["3", "Prove", "Share a receipt so anyone can check the saved quote"],
          ].map(([n, t, d]) => (
            <li key={n} className="stat-tile flex gap-3 p-3">
              <span className="num grid h-6 w-6 shrink-0 place-items-center rounded-md text-xs font-bold" style={{ background: "color-mix(in srgb, var(--accent) 18%, transparent)", color: "var(--accent)" }}>
                {n}
              </span>
              <span>
                <span className="font-semibold">{t}</span>
                <span className="muted block text-xs">{d}</span>
              </span>
            </li>
          ))}
        </ol>
      </header>

      <AssetBuilder />
    </main>
  );
}
