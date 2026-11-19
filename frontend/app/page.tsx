import Link from "next/link";
import AssetBuilder from "@/components/AssetBuilder";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:py-12">
      <header className="mb-8">
        <h1 className="text-3xl sm:text-4xl font-bold tracking-tight">Bet-ter Crypto</h1>
        <p className="muted mt-2 max-w-2xl">
          Say the BTC bet you want. We build it from the cheapest mix of Gemini and Kalshi contracts, compare it with options-implied fair value, and flag
          risky quotes.
        </p>
        <Link href="/ladder" className="link text-sm mt-2 inline-block">View strike ladder →</Link>
      </header>

      <AssetBuilder />
    </main>
  );
}
