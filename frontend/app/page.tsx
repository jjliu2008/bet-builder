import Link from "next/link";
import AssetBuilder from "@/components/AssetBuilder";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:py-12">
      <header className="mb-10 grid gap-8 lg:grid-cols-[1fr_auto] lg:items-end">
        <div className="space-y-4">
          <span className="pill pill-accent">Prediction markets · Gemini × Kalshi · Solana receipts</span>
          <h1 className="text-4xl font-bold leading-[1.05] tracking-tight sm:text-5xl">
            Say the bet.
            <br />
            <span className="gradient-text">We build it cheaper.</span>
          </h1>
          <p className="muted max-w-2xl text-lg">
            Pick an outcome, like BTC finishing between two prices. We assemble it from the cheapest mix of Gemini and Kalshi contracts, check
            it against Deribit options, flag risky quotes, and anchor a receipt on Solana. Stocks and other crypto show live reference prices.
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
            ["1", "Route", "Single contract, YES/NO spread or bucket tiling, across venues"],
            ["2", "Price", "Walks real depth, adds taker fees, compares with options fair value"],
            ["3", "Prove", "SHA-256 fingerprint written to Solana devnet, verifiable by anyone"],
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
