// Field names follow docs/api_contract.md. Optional engine additions are marked optional.

export type BetType = "above" | "below" | "between";
export type Side = "YES" | "NO";
export type VerdictCode = "options_higher" | "in_line" | "options_lower" | "unavailable";

export interface RouteRequest {
  asset: string;
  expiry: string;
  type: BetType;
  lo: number | null;
  hi: number | null;
  size: number;
}

export interface Leg {
  venue: string;
  contract: string;
  side: Side;
  price: number;
  fee: number;
  size_available: number;
  link: string;
  size?: number;
  fee_total?: number;
  strike_lo?: number | null;
  strike_hi?: number | null;
}

export interface Alternative {
  build: string;
  venues: string[];
  total_cost: number;
  cash_outlay: number;
  filled_size: number;
  complete: boolean;
}

export interface Route {
  grok_snapshot?: string | null;
  legs: Leg[];
  total_cost: number;
  max_payout: number;
  best_single_venue_cost: number | null;
  savings: number | null;
  fair_value: number | null;
  /** Where the options market prices this outcome relative to our cost. */
  verdict_code: VerdictCode;
  /** One-line explanation, e.g. "Options market prices this 6¢ higher than the best venue price". */
  verdict_text: string;
  /** Caveat about options-implied odds; null when there is no comparison. */
  fair_note: string | null;
  stale_warnings: string[];
  mixed_settlement_index: boolean;
  timestamp: string;
  /** Advisory market-quality assessment (PR #7). Absent on older backends. */
  market_quality?: MarketQuality | null;
  build?: string;
  cash_outlay?: number;
  guaranteed_payout?: number;
  size?: number;
  filled_size?: number;
  alternatives?: Alternative[];
  warnings?: string[];
}

export interface VenueQuote {
  bid: number | null;
  ask: number | null;
  bid_size: number;
  ask_size: number;
  updated: string;
}

export interface LadderRow {
  strike: number;
  gemini: VenueQuote | null;
  kalshi: VenueQuote | null;
  fair: number | null;
  stale: { gemini: boolean; kalshi: boolean };
  stale_reasons?: { gemini: string | null; kalshi: string | null };
}

export interface Health {
  status: string;
  [k: string]: unknown;
}

// Receipts: POST /receipt and GET /verify/{route_id}, see docs/api_contract.md.
export interface Receipt {
  route_id: string;
  hash: string;
  signature: string | null;
  explorer_url: string | null;
  /** "confirmed" = anchored on Solana devnet; "pending" = fingerprint saved, not onchain yet. */
  status: "confirmed" | "pending";
  created_at?: string;
  error?: string | null;
}

export interface Verification {
  route_id: string;
  status: "verified" | "mismatch" | "pending";
  hash: string;
  signature: string | null;
  explorer_url: string | null;
  detail?: string | null;
}

export interface MarketQualityFinding {
  code: string;
  message: string;
  score: number;
  venue?: string | null;
  contract?: string | null;
}

export interface MarketQuality {
  status: "clear" | "caution" | "review" | "unreliable" | string;
  risk_score: number;
  advisory_only?: boolean;
  evaluated_at?: string;
  methodology?: string;
  unavailable_signals?: string[];
  findings: MarketQualityFinding[];
}
