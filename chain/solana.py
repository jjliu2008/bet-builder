"""Write and check SHA-256 route fingerprints as Solana devnet Memo transactions.

No custom program is needed: the built-in Memo program stores a short UTF-8 string in the
transaction. Only the fingerprint goes onchain. The full route stays in the backend store.

Memo format:  better:v1:<route_id>:<sha256>
where route_id is the first 24 hex chars of the sha256, matching backend/receipts.py.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Optional

import requests
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
DEFAULT_RPC = "https://api.devnet.solana.com"
CONFIRM_TIMEOUT_S = 40
HTTP_TIMEOUT_S = 20


class SolanaConfigError(RuntimeError):
    """Raised when the signing key or RPC URL is missing or not allowed."""


def _rpc_url() -> str:
    url = os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC).strip()
    # Devnet only: refuse anything that looks like mainnet so real funds can never be involved.
    if "devnet" not in url and "localhost" not in url and "127.0.0.1" not in url:
        raise SolanaConfigError("SOLANA_RPC_URL must be a devnet or local endpoint")
    return url


def _keypair() -> Keypair:
    raw = os.environ.get("SOLANA_KEYPAIR")
    if not raw and os.environ.get("SOLANA_KEYPAIR_PATH"):
        with open(os.path.expanduser(os.environ["SOLANA_KEYPAIR_PATH"])) as f:
            raw = f.read()
    if not raw:
        raise SolanaConfigError("Set SOLANA_KEYPAIR or SOLANA_KEYPAIR_PATH to a devnet keypair")
    try:
        return Keypair.from_bytes(bytes(json.loads(raw)))
    except Exception as e:
        raise SolanaConfigError(f"Could not read the signing keypair ({type(e).__name__})") from None


def _rpc(method: str, params: list):
    r = requests.post(_rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      timeout=HTTP_TIMEOUT_S)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC {method}: {body['error'].get('message', body['error'])}")
    return body["result"]


def memo_for(hash_hex: str) -> str:
    return f"better:v1:{hash_hex[:24]}:{hash_hex}"


def explorer_url(signature: str) -> str:
    return f"https://explorer.solana.com/tx/{signature}?cluster=devnet"


def anchor_hash(hash_hex: str) -> dict:
    """Send a Memo transaction recording `hash_hex` and wait for it to be confirmed.

    Returns {"signature", "explorer_url"}. Raises on any failure; the caller marks the receipt pending.
    """
    if len(hash_hex) != 64 or any(c not in "0123456789abcdef" for c in hash_hex):
        raise ValueError("hash must be a 64-char lowercase hex SHA-256")
    kp = _keypair()
    blockhash = _rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    ix = Instruction(MEMO_PROGRAM, memo_for(hash_hex).encode(), [AccountMeta(kp.pubkey(), True, True)])
    msg = Message.new_with_blockhash([ix], kp.pubkey(), Hash.from_string(blockhash))
    tx = Transaction([kp], msg, Hash.from_string(blockhash))
    sig = _rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                   {"encoding": "base64", "preflightCommitment": "confirmed"}])
    deadline = time.time() + CONFIRM_TIMEOUT_S
    while time.time() < deadline:
        st = _rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("err"):
            raise RuntimeError(f"Transaction failed onchain: {st['err']}")
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return {"signature": sig, "explorer_url": explorer_url(sig)}
        time.sleep(1.5)
    raise TimeoutError("Transaction was sent but not confirmed in time")


def verify_hash(signature: str, hash_hex: str) -> bool:
    """True if the confirmed transaction `signature` carries a Memo recording `hash_hex`."""
    tx: Optional[dict] = _rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                             "maxSupportedTransactionVersion": 0}])
    if not tx or (tx.get("meta") or {}).get("err"):
        return False
    want = memo_for(hash_hex)
    for ix in tx["transaction"]["message"]["instructions"]:
        if ix.get("programId") == str(MEMO_PROGRAM) and ix.get("parsed") == want:
            return True
    return False
