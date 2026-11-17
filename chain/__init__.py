"""Onchain route receipts on Solana devnet (plan section 5.8).

backend/receipts.py imports this package if it is installed and uses:
    anchor_hash(hash)               write the fingerprint as a Memo transaction
    verify_hash(signature, hash)    check that transaction records that fingerprint

Configuration (environment only, never committed):
    SOLANA_KEYPAIR        the devnet signing key as a JSON array of 64 ints, or
    SOLANA_KEYPAIR_PATH   path to a solana-keygen style JSON file
    SOLANA_RPC_URL        default https://api.devnet.solana.com (devnet and localhost only)
"""
from .solana import SolanaConfigError, anchor_hash, memo_for, verify_hash

__all__ = ["anchor_hash", "verify_hash", "memo_for", "SolanaConfigError"]
