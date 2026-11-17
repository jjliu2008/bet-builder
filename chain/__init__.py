"""Onchain receipts on Solana devnet via the Memo program.

backend/receipts.py imports this package (if present) and calls:
    anchor_hash(hash) -> {"signature", "explorer_url"}    memo "better:v1:<hash>"
    verify_hash(signature, hash) -> bool                  True iff that tx's memo is "better:v1:<hash>"
backend/logger.py calls:
    anchor_root(label, root, count) -> {"signature", "explorer_url"}   memo "better:v1:snap:<label>:<root>:<count>"

Configuration (environment, read at call time):
    SOLANA_RPC_URL        JSON-RPC endpoint. Default https://api.devnet.solana.com
    SOLANA_KEYPAIR        signer secret: base58 (64-byte keypair) or a JSON byte array
    SOLANA_KEYPAIR_PATH   path to a JSON byte-array keypair file (solana-keygen format)
Importing never needs a signer; anchoring without one raises NoSignerError, which the backend
records on the receipt (status "pending"). Verification needs no signer.

Transactions are built with `solders` and sent over plain JSON-RPC (`requests`) so every call
has a hard 15 s budget and the RPC can be mocked in tests via `set_rpc`.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Callable, Optional

import requests

MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
DEFAULT_RPC_URL = "https://api.devnet.solana.com"
PREFIX = "better:v1:"
TIMEOUT_S = 15.0
MAX_MEMO_BYTES = 500

__all__ = ["anchor_hash", "verify_hash", "anchor_root", "memo_for_hash", "memo_for_root",
           "explorer_url", "load_signer", "signer_pubkey", "set_rpc", "ChainError", "NoSignerError",
           "MEMO_PROGRAM_ID"]


class ChainError(RuntimeError):
    """An RPC or confirmation failure."""


class NoSignerError(ChainError):
    """No SOLANA_KEYPAIR / SOLANA_KEYPAIR_PATH is configured."""


# ---- memos ------------------------------------------------------------------------------
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def memo_for_hash(h: str) -> str:
    """Return the receipt memo 'better:v1:<hash>' for a 64-char lowercase hex SHA-256."""
    h = h.lower()
    if not _HEX64.match(h):
        raise ValueError("hash must be 64 lowercase hex characters (SHA-256)")
    return PREFIX + h


def memo_for_root(label: str, root: str, count: int) -> str:
    """Return the snapshot memo 'better:v1:snap:<label>:<root>:<count>'."""
    if not re.match(r"^[A-Za-z0-9_-]{1,32}$", label):
        raise ValueError("label must be 1-32 chars of [A-Za-z0-9_-]")
    return f"{PREFIX}snap:{label}:{memo_for_hash(root)[len(PREFIX):]}:{int(count)}"


def explorer_url(signature: str) -> str:
    """Return the Solana Explorer devnet link for a transaction signature."""
    return f"https://explorer.solana.com/tx/{signature}?cluster=devnet"


# ---- RPC --------------------------------------------------------------------------------
def _http_rpc(method: str, params: list, timeout: float) -> Any:
    url = os.environ.get("SOLANA_RPC_URL") or DEFAULT_RPC_URL
    try:
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          timeout=timeout, headers={"User-Agent": "bet-builder/0.1"})
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ChainError(f"{method} failed: {type(e).__name__}: {e}") from e
    if body.get("error"):
        raise ChainError(f"{method} error: {body['error'].get('message', body['error'])}")
    return body.get("result")


_rpc: Callable[[str, list, float], Any] = _http_rpc


def set_rpc(fn: Optional[Callable[[str, list, float], Any]]) -> None:
    """Replace the JSON-RPC transport, fn(method, params, timeout) -> result (tests). None restores HTTP."""
    global _rpc
    _rpc = fn or _http_rpc


# ---- signer -----------------------------------------------------------------------------
def load_signer():
    """Return a solders Keypair from SOLANA_KEYPAIR or SOLANA_KEYPAIR_PATH.

    Raises:
        NoSignerError: neither is set.
        ValueError: the value is not a valid keypair.
    """
    from solders.keypair import Keypair
    raw = (os.environ.get("SOLANA_KEYPAIR") or "").strip()
    path = (os.environ.get("SOLANA_KEYPAIR_PATH") or "").strip()
    if not raw and path:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            raw = f.read().strip()
    if not raw:
        raise NoSignerError("no signer configured: set SOLANA_KEYPAIR (base58 secret or JSON byte array) "
                            "or SOLANA_KEYPAIR_PATH")
    if raw.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(raw)))
    return Keypair.from_base58_string(raw)


def signer_pubkey() -> Optional[str]:
    """Return the configured signer's public key (base58), or None if there is no signer."""
    try:
        return str(load_signer().pubkey())
    except (NoSignerError, ValueError, OSError):
        return None


# ---- send / confirm ---------------------------------------------------------------------
def _send_memo(memo: str) -> dict:
    data = memo.encode("utf-8")
    if len(data) > MAX_MEMO_BYTES:
        raise ValueError(f"memo is {len(data)} bytes; max {MAX_MEMO_BYTES}")
    kp = load_signer()                                       # raises NoSignerError first, no RPC
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    deadline = time.monotonic() + TIMEOUT_S
    left = lambda: max(deadline - time.monotonic(), 0.5)
    bh = _rpc("getLatestBlockhash", [{"commitment": "confirmed"}], left())
    blockhash = Hash.from_string(bh["value"]["blockhash"])
    ix = Instruction(Pubkey.from_string(MEMO_PROGRAM_ID), data,
                     [AccountMeta(kp.pubkey(), is_signer=True, is_writable=False)])
    tx = Transaction([kp], Message.new_with_blockhash([ix], kp.pubkey(), blockhash), blockhash)
    sig = _rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode("ascii"),
                                   {"encoding": "base64", "preflightCommitment": "confirmed"}], left())
    while True:                                              # wait for confirmation within the budget
        st = (_rpc("getSignatureStatuses", [[sig]], left()) or {}).get("value", [None])[0]
        if st:
            if st.get("err"):
                raise ChainError(f"transaction {sig} failed: {st['err']}")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return {"signature": sig, "explorer_url": explorer_url(sig)}
        if time.monotonic() >= deadline:
            raise ChainError(f"transaction {sig} not confirmed within {TIMEOUT_S:g}s")
        time.sleep(0.5)


def anchor_hash(hash: str) -> dict:
    """Write memo 'better:v1:<hash>' to Solana devnet.

    Args:
        hash: SHA-256 hex digest of the receipt (64 chars).

    Returns:
        {"signature": base58 tx signature, "explorer_url": devnet explorer link}.

    Raises:
        NoSignerError: no signer configured. ChainError: RPC failure or not confirmed within 15 s.
    """
    return _send_memo(memo_for_hash(hash))


def anchor_root(label: str, root: str, count: int) -> dict:
    """Write memo 'better:v1:snap:<label>:<root>:<count>' (hourly snapshot Merkle root).

    Args:
        label: e.g. '2026111720' (UTC hour, YYYYMMDDHH).
        root: SHA-256 Merkle root, 64 hex chars.
        count: number of log lines covered.

    Returns:
        {"signature", "explorer_url"}. Raises like anchor_hash.
    """
    return _send_memo(memo_for_root(label, root, count))


# ---- verify -----------------------------------------------------------------------------
_LOG_MEMO = re.compile(r'^Program log: Memo \(len \d+\): "(.*)"$')


def _memos(tx: dict) -> list:
    out = []
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    for ix in msg.get("instructions", []):
        if ix.get("programId") == MEMO_PROGRAM_ID or ix.get("program") == "spl-memo":
            if isinstance(ix.get("parsed"), str):
                out.append(ix["parsed"])
    for line in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        m = _LOG_MEMO.match(line)
        if m:
            out.append(m.group(1))
    return out


def verify_hash(signature: str, hash: str) -> bool:
    """Return True only if transaction `signature` succeeded and carries memo 'better:v1:<hash>'.

    Args:
        signature: base58 transaction signature.
        hash: expected SHA-256 hex digest.

    Raises:
        ChainError: the RPC could not be reached (so the caller can report "pending", not "mismatch").
    """
    try:
        want = memo_for_hash(hash)
    except ValueError:
        return False
    tx = _rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                                             "maxSupportedTransactionVersion": 0}], TIMEOUT_S)
    if not tx or ((tx.get("meta") or {}).get("err")):
        return False
    return want in _memos(tx)
