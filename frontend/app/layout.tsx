import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import SiteHeader from "@/components/SiteHeader";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Bet-ter Crypto",
  description: "Build the BTC prediction bet you want from the cheapest mix of Gemini and Kalshi contracts, and read the market like a maker.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <SiteHeader />
        <div className="flex-1">{children}</div>
        <footer className="border-t border-line">
          <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-5 text-xs muted">
            <span>Live public data from Gemini Predictions, Kalshi and Deribit. Analysis only, nothing is traded for you.</span>
            <span>Route receipts anchored on Solana devnet · UBC Hackathon 2026</span>
          </div>
        </footer>
      </body>
    </html>
  );
}
