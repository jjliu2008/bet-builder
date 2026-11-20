"""Route receipts: POST /receipt and GET /verify/{route_id}.

A receipt is a SHA-256 fingerprint of the canonical JSON of {request, route}. It is kept in the
receipt store (backend.receipt_store: SQLite or Postgres) and, when the `chain` module has a
signer, anchored on Solana devnet as a Memo transaction.

The onchain part is optional. The optional `chain` package (repo root) may provide:
    router                                  extra FastAPI routes, mounted by backend.app
    anchor_hash(hash: str) -> dict          writes the fingerprint and returns {"signature", "explorer_url"}
    verify_hash(signature: str, hash: str) -> bool   True if that transaction records that hash
Without it, or if anchoring fails, a receipt is still issued with status "pending". It has a
hash but no signature or explorer link, so the frontend shows "receipt pending" and the route
itself is unaffected.

POST /verify checks {request, route, signature} straight against the chain, so a route can be
verified even if this server's database is gone: the proof lives onchain.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import ModuleType
from collections.abc import MutableMapping
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, HTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError

from engine import Route, RouteRequest
from engine.books import iso


def load_chain() -> Optional[ModuleType]:
    """Import the optional `chain` package. Returns None if it isn't installed or fails to import."""
    try:
        import chain  # type: ignore[import-not-found]
    except Exception:
        return None
    return chain


def fingerprint(request: Optional[dict], route: dict) -> str:
    """Return the SHA-256 hex digest of the canonical JSON (sorted keys, no spaces) of {request, route}."""
    blob = json.dumps({"request": request, "route": route}, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse_body(body: Any) -> tuple:
    """Accept {request, route} (the frontend's shape) or a bare Route. Return (request dict or None, route dict)."""
    if not isinstance(body, dict):
        raise HTTPException(422, "Body must be a JSON object: {request, route} or a route")
    raw_route, raw_req = (body["route"], body.get("request")) if "route" in body else (body, None)
    try:
        route = Route.model_validate(raw_route).model_dump(mode="json", exclude={"grok_snapshot"})
        if "market_quality" not in raw_route:
            route.pop("market_quality", None)
        elif isinstance(raw_route.get("market_quality"), dict):
            for metric in ("observed_spread_cents", "oldest_quote_seconds"):
                if metric not in raw_route["market_quality"]:
                    route["market_quality"].pop(metric, None)
        request = RouteRequest.model_validate(raw_req).model_dump(mode="json") if raw_req is not None else None
    except ValidationError as e:
        raise HTTPException(422, f"Not a valid route receipt body: {e.errors()[0].get('msg', 'invalid')}")
    return request, route


async def try_anchor(receipt: dict, anchor: Optional[Callable[[str], dict]]) -> dict:
    """Try to anchor `receipt["hash"]` onchain, updating the receipt in place (and returning it).

    The chain call runs in a threadpool so it never blocks the event loop. On success
    status becomes "confirmed" with signature + explorer_url; on any failure it stays "pending"
    and `error` says why.
    """
    if anchor is None:
        receipt["error"] = "Onchain receipts are not configured on this server"
        return receipt
    try:
        res = await run_in_threadpool(anchor, receipt["hash"]) or {}
        receipt.update(signature=res.get("signature"), explorer_url=res.get("explorer_url"),
                       status="confirmed" if res.get("signature") else "pending",
                       error=None if res.get("signature") else "Anchor returned no signature")
    except Exception as e:  # never let a chain failure break the route flow
        receipt["error"] = f"Anchoring failed: {type(e).__name__}: {e}"
    return receipt


async def retry_pending(store: MutableMapping, chain: Optional[ModuleType]) -> int:
    """Retry anchoring every stored receipt that is still pending with no signature.

    Runs at startup in the background. Skips quietly if there is no chain module or no signer.
    Returns the number of receipts that became confirmed.
    """
    anchor = getattr(chain, "anchor_hash", None) if chain else None
    has_signer = getattr(chain, "signer_pubkey", None) if chain else None
    if anchor is None or (has_signer is not None and has_signer() is None):
        return 0
    ids = store.pending_ids() if hasattr(store, "pending_ids") else \
        [k for k, v in list(store.items()) if v["receipt"]["status"] == "pending" and not v["receipt"]["signature"]]
    confirmed = 0
    for route_id in ids:
        rec = store.get(route_id)
        if rec is None:
            continue
        await try_anchor(rec["receipt"], anchor)
        store[route_id] = rec
        confirmed += rec["receipt"]["status"] == "confirmed"
    return confirmed


def make_router(chain: Optional[ModuleType] = None, store: Optional[MutableMapping] = None) -> APIRouter:
    """Build the receipt routes.

    Args:
        chain: the optional onchain module (see the module docstring), or None.
        store: the receipt store, {route_id: record} (e.g. backend.receipt_store.ReceiptStore).
            A new in-memory dict by default.
    """
    router = APIRouter()
    receipts: MutableMapping = {} if store is None else store
    anchor = getattr(chain, "anchor_hash", None) if chain else None
    check = getattr(chain, "verify_hash", None) if chain else None

    @router.post("/receipt")
    async def post_receipt(body: Any = Body(...)) -> dict:
        """Fingerprint a route and try to anchor it.

        Posting the same route again returns the stored receipt; if that receipt is still
        pending, anchoring is retried first.

        Returns:
            {route_id, hash, signature, explorer_url, status, created_at, error}. `status` is
            "confirmed" (anchored onchain) or "pending" (no chain module, or anchoring failed;
            `error` says why).
        """
        request, route = _parse_body(body)
        h = fingerprint(request, route)
        route_id = h[:24]
        rec = receipts.get(route_id)
        if rec is None:
            rec = {"request": request, "route": route, "receipt": {
                "route_id": route_id, "hash": h, "signature": None, "explorer_url": None,
                "status": "pending", "created_at": iso(datetime.now(timezone.utc)), "error": None}}
        elif rec["receipt"]["status"] == "confirmed":
            return rec["receipt"]
        await try_anchor(rec["receipt"], anchor)
        receipts[route_id] = rec
        return rec["receipt"]

    @router.get("/verify/{route_id}")
    async def verify(route_id: str, include_quote: bool = False) -> dict:
        """Re-check a stored receipt.

        Returns:
            {route_id, status, hash, signature, explorer_url, detail}. `status` is "verified"
            (the stored route hashes to the receipt hash, and the chain confirms it), "mismatch"
            (either check fails), or "pending" (the hash is intact, but nothing is onchain to
            check against). Returns 404 if this server has no such receipt; use POST /verify with
            the route and signature to verify without the server's database.
        """
        rec = receipts.get(route_id)
        if rec is None:
            raise HTTPException(404, f"No receipt {route_id} on this server. "
                                     "POST /verify with {request, route, signature} to check it onchain.")
        r = rec["receipt"]
        out = {"route_id": route_id, "hash": r["hash"], "signature": r["signature"],
               "explorer_url": r["explorer_url"]}
        if fingerprint(rec["request"], rec["route"]) != r["hash"]:
            return {**out, "status": "mismatch", "detail": "Stored route no longer matches its fingerprint"}
        if include_quote:
            route = rec["route"]
            out["quote"] = {"request": rec.get("request"), "timestamp": route.get("timestamp"),
                            "venues": sorted({leg["venue"] for leg in route.get("legs", [])}),
                            "filled_size": route.get("filled_size"), "cash_outlay": route.get("cash_outlay")}
        if not r["signature"]:
            return {**out, "status": "pending", "detail": r.get("error") or "Not anchored onchain yet"}
        return {**out, **(await _check(r["signature"], r["hash"]))}

    @router.post("/verify")
    async def verify_body(body: Any = Body(...)) -> dict:
        """Verify a route against the chain without the server's receipt database.

        Body: {request, route, signature} (request may be null; it must be exactly what was
        receipted). The fingerprint is recomputed and checked against the memo of transaction
        `signature`.

        Returns:
            {route_id, status, hash, signature, explorer_url, detail} with status "verified",
            "mismatch", or "pending" (the chain could not be read).
        """
        if not isinstance(body, dict) or not body.get("signature") or "route" not in body:
            raise HTTPException(422, "Body must be {request, route, signature}")
        request, route = _parse_body({"request": body.get("request"), "route": body["route"]})
        h, sig = fingerprint(request, route), str(body["signature"])
        explorer = getattr(chain, "explorer_url", None) if chain else None
        out = {"route_id": h[:24], "hash": h, "signature": sig,
               "explorer_url": explorer(sig) if explorer else None}
        return {**out, **(await _check(sig, h))}

    async def _check(sig: str, h: str) -> dict:
        if check is None:
            return {"status": "pending", "detail": "Server cannot read the chain to verify"}
        try:
            ok = bool(await run_in_threadpool(check, sig, h))
        except Exception as e:
            return {"status": "pending", "detail": f"Chain lookup failed: {type(e).__name__}: {e}"}
        return {"status": "verified" if ok else "mismatch",
                "detail": None if ok else "Onchain record does not match this route"}

    return router
