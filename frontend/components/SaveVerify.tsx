"use client";

import { useState } from "react";
import { ApiError, postReceipt } from "@/lib/api";
import type { Receipt, Route, RouteRequest } from "@/lib/types";

type State =
  | { s: "idle" }
  | { s: "saving" }
  | { s: "done"; receipt: Receipt }
  | { s: "pending"; reason: string; receipt?: Receipt };

/** Receipt failures never block the recommendation: the route stays visible, marked "receipt pending". */
export default function SaveVerify({ route, request }: { route: Route; request: RouteRequest }) {
  const [state, setState] = useState<State>({ s: "idle" });
  const [copied, setCopied] = useState(false);

  async function save() {
    setState({ s: "saving" });
    try {
      const receipt = await postReceipt(route, request);
      // A receipt without an onchain transaction is "pending": the fingerprint is saved but not on Solana.
      if (receipt.status === "confirmed" && receipt.explorer_url) setState({ s: "done", receipt });
      else setState({ s: "pending", reason: receipt.error ?? "not anchored onchain yet", receipt });
    } catch (e) {
      setState({ s: "pending", reason: e instanceof ApiError ? e.message : "Unknown error" });
    }
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }

  if (state.s === "done") {
    const { receipt } = state;
    const share = `${window.location.origin}/verify/${encodeURIComponent(receipt.route_id)}`;
    return (
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-3">
          <a href={receipt.explorer_url ?? undefined} target="_blank" rel="noreferrer" className="pill pill-good">
            ✓ Verified on Solana ↗
          </a>
          <a href={`/verify/${encodeURIComponent(receipt.route_id)}`} className="link text-sm">
            Re-verify
          </a>
          <button type="button" onClick={() => copy(share)} className="link text-sm">
            {copied ? "Link copied" : "Copy share link"}
          </button>
        </div>
        <p className="muted text-xs">Devnet receipt. Only a fingerprint of this route is recorded onchain.</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={save} disabled={state.s === "saving"} className="btn-outline">
          {state.s === "saving" ? "Saving…" : state.s === "pending" ? "Retry receipt" : "Save & verify"}
        </button>
        {state.s === "pending" && <span className="pill pill-warn">Receipt pending</span>}
        {state.s === "pending" && state.receipt && (
          <a href={`/verify/${encodeURIComponent(state.receipt.route_id)}`} className="link text-sm">
            Check receipt
          </a>
        )}
      </div>
      {state.s === "pending" && (
        <p className="muted text-xs">
          Your route is still valid. The Solana receipt could not be written yet ({state.reason}).
          {state.receipt && <> Fingerprint <span className="break-all">{state.receipt.hash.slice(0, 16)}…</span> saved.</>}
        </p>
      )}
    </div>
  );
}
