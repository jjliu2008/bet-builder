import type { Health, LadderRow, Receipt, Route, RouteRequest, Verification } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  nearest?: number[];
  constructor(message: string, status: number, nearest?: number[]) {
    super(message);
    this.status = status;
    this.nearest = nearest;
  }
  /** True when the backend is up but has no data yet (HTTP 503). */
  get unavailable() {
    return this.status === 503;
  }
  /** True when the server could not be reached at all. */
  get offline() {
    return this.status === 0;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(`Cannot reach the backend at ${API_URL}`, 0);
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    let nearest: number[] | undefined;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg?: string }) => d.msg).join("; ");
      if (Array.isArray(body.nearest)) nearest = body.nearest;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status, nearest);
  }
  return res.json() as Promise<T>;
}

export const getExpiries = (asset = "BTC") => request<string[]>(`/expiries?asset=${asset}`);
export const getLadder = (expiry: string, asset = "BTC") =>
  request<LadderRow[]>(`/ladder?asset=${asset}&expiry=${encodeURIComponent(expiry)}`);
export const getHealth = () => request<Health>("/health");
export const postRoute = (body: RouteRequest) =>
  request<Route>("/route", { method: "POST", body: JSON.stringify(body) });

// Receipts: POST /receipt and GET /verify/{route_id} (plan section 5.8).
export const postReceipt = (route: Route, req: RouteRequest) =>
  request<Receipt>("/receipt", { method: "POST", body: JSON.stringify({ request: req, route }) });
export const getVerify = (routeId: string) => request<Verification>(`/verify/${encodeURIComponent(routeId)}`);

export const getTokenQuote = (symbol: string) =>
  request<{ price_usd: number; traded_at: string; source: string; issuer_url?: string }>(
    `/${symbol.endsWith("x") ? "rwas" : "spot"}/${encodeURIComponent(symbol)}`
  );

export const explainRoute = (snapshot: string, topic: string) =>
  request<{ answer: string; model: string; timestamp: string }>("/grok/explain", {
    method: "POST", body: JSON.stringify({ snapshot, topic }),
  });
