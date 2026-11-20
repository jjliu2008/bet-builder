export const usd = (n: number, digits = 2) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });

export const cents = (n: number) => `${(n * 100).toFixed(1)}¢`;
export const pct = (n: number) => `${(n * 100).toFixed(1)}%`;
export const num = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 2 });

export function expiryLabel(iso: string): string {
  const d = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  if (isNaN(d.getTime())) return iso;
  return (
    d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) +
    " UTC"
  );
}

export const venueName = (v: string) => (v ? v[0].toUpperCase() + v.slice(1) : v);

export const buildLabel = (b?: string) =>
  b === "single" ? "Single contract" : b === "two_leg" ? "Two-leg spread" : b === "buckets" ? "Native buckets" : (b ?? "Route");

export const isStockOutcome = (symbol: string) => ["AAPL", "NVDA", "MSFT", "TSLA"].includes(symbol);
