"""Route receipts: POST /receipt and GET /verify/{route_id}.

A receipt is a SHA-256 fingerprint of the canonical JSON of {request, route}. It is kept in
memory and, when an onchain module is available, anchored on Solana devnet.

The onchain part is optional. The optional `chain` package (repo root) may provide:
    router                                  extra FastAPI routes, mounted by backend.app
    anchor_hash(hash: str) -> dict          writes the fingerprint and returns {"signature", "explorer_url"}
    verify_hash(signature: str, hash: str) -> bool   True if that transaction records that hash
Without it, or if anchoring fails, a receipt is still issued with status "pending". It has a
hash but no signature or explorer link, so the frontend shows "receipt pending" and the route
itself is unaffected.

Receipts live in process memory: they are lost on restart.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Dict, Optional

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
        route = Route.model_validate(raw_route).model_dump(mode="json")
        request = RouteRequest.model_validate(raw_req).model_dump(mode="json") if raw_req is not None else None
    except ValidationError as e:
        raise HTTPException(422, f"Not a valid route receipt body: {e.errors()[0].get('msg', 'invalid')}")
    return request, route


def make_router(chain: Optional[ModuleType] = None, store: Optional[Dict[str, dict]] = None) -> APIRouter:
    """Build the receipt routes.

    Args:
        chain: the optional onchain module (see the module docstring), or None.
        store: the receipt store, {route_id: record}. A new in-memory dict by default.
    """
    router = APIRouter()
    receipts: Dict[str, dict] = {} if store is None else store
    anchor = getattr(chain, "anchor_hash", None) if chain else None
    check = getattr(chain, "verify_hash", None) if chain else None

    @router.post("/receipt")
    async def post_receipt(body: Any = Body(...)) -> dict:
        """Fingerprint a route and try to anchor it.

        Returns:
            {route_id, hash, signature, explorer_url, status, created_at, error}. `status` is
            "confirmed" (anchored onchain) or "pending" (no chain module, or anchoring failed;
            `error` says why).
        """
        request, route = _parse_body(body)
        h = fingerprint(request, route)
        route_id = h[:24]
        if route_id in receipts:
            return receipts[route_id]["receipt"]
        receipt = {"route_id": route_id, "hash": h, "signature": None, "explorer_url": None,
                   "status": "pending", "created_at": iso(datetime.now(timezone.utc)), "error": None}
        if anchor is None:
            receipt["error"] = "Onchain receipts are not configured on this server"
        else:
            try:
                res = await run_in_threadpool(anchor, h) or {}
                receipt.update(signature=res.get("signature"), explorer_url=res.get("explorer_url"),
                               status="confirmed" if res.get("signature") else "pending")
            except Exception as e:  # never let a chain failure break the route flow
                receipt["error"] = f"Anchoring failed: {type(e).__name__}: {e}"
        receipts[route_id] = {"request": request, "route": route, "receipt": receipt}
        return receipt

    @router.get("/verify/{route_id}")
    async def verify(route_id: str) -> dict:
        """Re-check a stored receipt.

        Returns:
            {route_id, status, hash, signature, explorer_url, detail}. `status` is "verified"
            (the stored route hashes to the receipt hash, and the chain confirms it), "mismatch"
            (either check fails), or "pending" (the hash is intact, but nothing is onchain to
            check against). Returns 404 if the receipt is unknown, e.g. after a restart.
        """
        rec = receipts.get(route_id)
        if rec is None:
            raise HTTPException(404, f"No receipt {route_id} on this server (receipts reset on restart)")
        r = rec["receipt"]
        out = {"route_id": route_id, "hash": r["hash"], "signature": r["signature"],
               "explorer_url": r["explorer_url"]}
        if fingerprint(rec["request"], rec["route"]) != r["hash"]:
            return {**out, "status": "mismatch", "detail": "Stored route no longer matches its fingerprint"}
        if not r["signature"]:
            return {**out, "status": "pending", "detail": r.get("error") or "Not anchored onchain yet"}
        if check is None:
            return {**out, "status": "pending", "detail": "Server cannot read the chain to re-verify"}
        try:
            ok = bool(await run_in_threadpool(check, r["signature"], r["hash"]))
        except Exception as e:
            return {**out, "status": "pending", "detail": f"Chain lookup failed: {type(e).__name__}: {e}"}
        return {**out, "status": "verified" if ok else "mismatch",
                "detail": None if ok else "Onchain record does not match this route"}

    return router
