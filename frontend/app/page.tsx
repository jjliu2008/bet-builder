import Link from "next/link";
import AssetBuilder from "@/components/AssetBuilder";

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:py-12">
      <header className="mb-7 space-y-3">
        <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">Prediction route builder</h1>
        <p className="muted max-w-2xl">Choose an asset and a closing price outcome to compare contract prices, available size and market risk.</p>
        <nav className="flex gap-5 text-sm"><Link href="/maker" className="link">Maker desk</Link><Link href="/ladder" className="link">Strike ladder</Link></nav>
      </header>

      <AssetBuilder />
    </main>
  );
}
