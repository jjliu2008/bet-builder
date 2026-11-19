// Market-maker analytics computed from /ladder rows (no extra backend calls).
// Every row is the threshold contract "BTC >= strike at expiry"; prices are $ per $1 payout.

import type { LadderRow, VenueQuote } from "./types";

export type Venue = "gemini" | "kalshi";
export const VENUES: Venue[] = ["gemini", "kalshi"];

/** Taker fee per contract, same formula as engine/fees.py: 0.07 * P * (1 - P). */
export const TAKER_RATE = 0.07;
export const feePer = (price: number, rate: number) => rate * price * (1 - price);

export interface Touch {
  px: number;
  size: number;
  venue: Venue;
}

export interface MakerRow {
  strike: number;
  quotes: Record<Venue, VenueQuote | null>;
  stale: Record<Venue, boolean>;
  fair: number | null;
  bestBid: Touch | null;
  bestAsk: Touch | null;
  /** Consolidated spread: best ask anywhere minus best bid anywhere. Negative = crossed. */
  spread: number | null;
  mid: number | null;
  venueSpread: Record<Venue, number | null>;
  venueMid: Record<Venue, number | null>;
  /** Consolidated mid minus options fair value. Positive = venues price it richer than options. */
  midVsFair: number | null;
  /** Best bid on one venue is strictly above the best ask on the other. */
  crossed: boolean;
  /** Best bid on one venue equals the best ask on the other. */
  locked: boolean;
  /** Buy the ask, sell the bid, both as taker: $ per contract after fees (before settlement-index risk). */
  crossNet: number | null;
}

function touch(q: VenueQuote | null, side: "bid" | "ask", venue: Venue): Touch | null {
  if (!q) return null;
  const px = q[side];
  if (px === null || px === undefined) return null;
  return { px, size: side === "bid" ? q.bid_size : q.ask_size, venue };
}

export function toMakerRow(r: LadderRow): MakerRow {
  const quotes: Record<Venue, VenueQuote | null> = { gemini: r.gemini, kalshi: r.kalshi };
  const bids = VENUES.map((v) => touch(quotes[v], "bid", v)).filter((t): t is Touch => !!t);
  const asks = VENUES.map((v) => touch(quotes[v], "ask", v)).filter((t): t is Touch => !!t);
  const bestBid = bids.length ? bids.reduce((a, b) => (b.px > a.px ? b : a)) : null;
  const bestAsk = asks.length ? asks.reduce((a, b) => (b.px < a.px ? b : a)) : null;
  const spread = bestBid && bestAsk ? bestAsk.px - bestBid.px : null;
  const mid = bestBid && bestAsk ? (bestAsk.px + bestBid.px) / 2 : null;

  const venueSpread = {} as Record<Venue, number | null>;
  const venueMid = {} as Record<Venue, number | null>;
  for (const v of VENUES) {
    const q = quotes[v];
    const ok = q && q.bid !== null && q.ask !== null;
    venueSpread[v] = ok ? (q.ask as number) - (q.bid as number) : null;
    venueMid[v] = ok ? ((q.ask as number) + (q.bid as number)) / 2 : null;
  }

  const twoVenue = !!(bestBid && bestAsk && bestBid.venue !== bestAsk.venue);
  const crossed = twoVenue && bestBid!.px > bestAsk!.px + 1e-9;
  const locked = twoVenue && Math.abs(bestBid!.px - bestAsk!.px) <= 1e-9;
  const crossNet = crossed && bestBid && bestAsk
    ? bestBid.px - bestAsk.px - feePer(bestAsk.px, TAKER_RATE) - feePer(bestBid.px, TAKER_RATE)
    : null;

  return {
    strike: r.strike,
    quotes,
    stale: r.stale ?? { gemini: false, kalshi: false },
    fair: r.fair,
    bestBid,
    bestAsk,
    spread,
    mid,
    venueSpread,
    venueMid,
    midVsFair: mid !== null && r.fair !== null ? mid - r.fair : null,
    crossed,
    locked,
    crossNet,
  };
}

const median = (xs: number[]) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

export interface DeskSummary {
  strikes: number;
  bothVenues: number;
  medianSpread: number | null;
  avgVenueSpread: Record<Venue, number | null>;
  /** How often each venue holds the best bid or best ask (ties count for both). */
  touchShare: Record<Venue, number>;
  crossedCount: number;
  lockedCount: number;
  /** Crossed strikes that still pay after taker fees on both legs. */
  crossProfitable: number;
  bestCrossNet: number | null;
  topDepth: Record<Venue, number>;
  maxMispricing: MakerRow | null;
}

export function summarize(rows: MakerRow[]): DeskSummary {
  const both = rows.filter((r) => r.quotes.gemini && r.quotes.kalshi);
  const touchShare: Record<Venue, number> = { gemini: 0, kalshi: 0 };
  let touches = 0;
  for (const r of both) {
    for (const side of ["bid", "ask"] as const) {
      const best = side === "bid" ? r.bestBid : r.bestAsk;
      if (!best) continue;
      touches++;
      for (const v of VENUES) if (r.quotes[v]?.[side] === best.px) touchShare[v]++;
    }
  }
  if (touches) for (const v of VENUES) touchShare[v] /= touches;

  const topDepth: Record<Venue, number> = { gemini: 0, kalshi: 0 };
  for (const r of rows) for (const v of VENUES) topDepth[v] += (r.quotes[v]?.bid_size ?? 0) + (r.quotes[v]?.ask_size ?? 0);

  const withFair = rows.filter((r) => r.midVsFair !== null);
  const maxMispricing = withFair.length
    ? withFair.reduce((a, b) => (Math.abs(b.midVsFair as number) > Math.abs(a.midVsFair as number) ? b : a))
    : null;

  return {
    strikes: rows.length,
    bothVenues: both.length,
    medianSpread: median(rows.map((r) => r.spread).filter((x): x is number => x !== null)),
    avgVenueSpread: {
      gemini: mean(rows.map((r) => r.venueSpread.gemini).filter((x): x is number => x !== null)),
      kalshi: mean(rows.map((r) => r.venueSpread.kalshi).filter((x): x is number => x !== null)),
    },
    touchShare,
    crossedCount: rows.filter((r) => r.crossed).length,
    lockedCount: rows.filter((r) => r.locked).length,
    crossProfitable: rows.filter((r) => r.crossed && (r.crossNet ?? 0) > 0).length,
    bestCrossNet: rows.some((r) => r.crossed)
      ? Math.max(...rows.filter((r) => r.crossed).map((r) => r.crossNet as number))
      : null,
    topDepth,
    maxMispricing,
  };
}

// ---- Quote simulator ------------------------------------------------------

export type Anchor = "fair" | "mid";

export interface QuoteInput {
  anchor: Anchor;
  /** Half-width of the quote around the anchor, in $ (0.02 = 2¢). */
  halfWidth: number;
  /** Shift both sides, in $. Positive leans long (higher bid and ask) to shed short inventory. */
  skew: number;
  /** Maker fee coefficient c in c * P * (1 - P). 0 = no maker fee. */
  makerRate: number;
}

export type Placement = "crosses" | "improves" | "joins" | "behind" | "no-book";

export interface VenuePlacement {
  venue: Venue;
  bid: Placement;
  ask: Placement;
  /** Distance to that venue's best on the same side, in $ (positive = we are better). */
  bidGap: number | null;
  askGap: number | null;
  /** Size resting at the price we would join, when joining. */
  bidQueue: number | null;
  askQueue: number | null;
}

export interface QuoteResult {
  reference: number;
  bid: number;
  ask: number;
  width: number;
  edgeIfBidFilled: number;
  edgeIfAskFilled: number;
  roundTrip: number;
  placements: VenuePlacement[];
}

const clampPx = (p: number) => Math.min(0.99, Math.max(0.01, p));
const toCent = (p: number, mode: "floor" | "ceil") => (mode === "floor" ? Math.floor(p * 100 + 1e-9) : Math.ceil(p * 100 - 1e-9)) / 100;

function place(ours: number, side: "bid" | "ask", q: VenueQuote | null): { p: Placement; gap: number | null; queue: number | null } {
  if (!q) return { p: "no-book", gap: null, queue: null };
  const sameBest = side === "bid" ? q.bid : q.ask;
  const otherBest = side === "bid" ? q.ask : q.bid;
  // A bid at or above the venue's ask (or an ask at or below its bid) would trade immediately as a taker.
  if (otherBest !== null && (side === "bid" ? ours >= otherBest : ours <= otherBest)) return { p: "crosses", gap: null, queue: null };
  if (sameBest === null) return { p: "improves", gap: null, queue: null };
  const gap = side === "bid" ? ours - sameBest : sameBest - ours;
  if (Math.abs(gap) < 1e-9) return { p: "joins", gap: 0, queue: side === "bid" ? q.bid_size : q.ask_size };
  return { p: gap > 0 ? "improves" : "behind", gap, queue: null };
}

/** Returns null when the anchor price is unavailable for this strike. */
export function simulateQuote(row: MakerRow, input: QuoteInput): QuoteResult | null {
  const ref = input.anchor === "fair" ? row.fair : row.mid;
  if (ref === null || ref === undefined) return null;
  const bid = clampPx(toCent(ref - input.halfWidth + input.skew, "floor"));
  let ask = clampPx(toCent(ref + input.halfWidth + input.skew, "ceil"));
  if (ask <= bid) ask = clampPx(bid + 0.01);

  const placements: VenuePlacement[] = VENUES.map((v) => {
    const b = place(bid, "bid", row.quotes[v]);
    const a = place(ask, "ask", row.quotes[v]);
    return { venue: v, bid: b.p, ask: a.p, bidGap: b.gap, askGap: a.gap, bidQueue: b.queue, askQueue: a.queue };
  });

  const bidFee = feePer(bid, input.makerRate);
  const askFee = feePer(ask, input.makerRate);
  return {
    reference: ref,
    bid,
    ask,
    width: ask - bid,
    edgeIfBidFilled: ref - bid - bidFee,
    edgeIfAskFilled: ask - ref - askFee,
    roundTrip: ask - bid - bidFee - askFee,
    placements,
  };
}
