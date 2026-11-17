"""Offline tests for the Solana receipt module (no network)."""
import pytest

import chain
from chain import solana

H = "ab" * 32


def test_memo_format():
    assert chain.memo_for(H) == f"better:v1:{H[:24]}:{H}"


def test_anchor_rejects_bad_hash():
    with pytest.raises(ValueError):
        chain.anchor_hash("not-a-hash")


def test_anchor_needs_a_key(monkeypatch):
    monkeypatch.delenv("SOLANA_KEYPAIR", raising=False)
    monkeypatch.delenv("SOLANA_KEYPAIR_PATH", raising=False)
    with pytest.raises(chain.SolanaConfigError):
        chain.anchor_hash(H)


def test_mainnet_rpc_is_refused(monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    with pytest.raises(chain.SolanaConfigError):
        solana._rpc_url()


def test_verify_checks_memo(monkeypatch):
    tx = {"meta": {"err": None}, "transaction": {"message": {"instructions": [
        {"programId": str(solana.MEMO_PROGRAM), "parsed": chain.memo_for(H)}]}}}
    monkeypatch.setattr(solana, "_rpc", lambda *_: tx)
    assert chain.verify_hash("sig", H) is True
    assert chain.verify_hash("sig", "cd" * 32) is False
    monkeypatch.setattr(solana, "_rpc", lambda *_: None)
    assert chain.verify_hash("sig", H) is False
