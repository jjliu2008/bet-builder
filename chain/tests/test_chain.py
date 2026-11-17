import hashlib
import json

import pytest
from solders.keypair import Keypair

import chain

from .fake_rpc import FakeLedger

H = hashlib.sha256(b"route").hexdigest()


@pytest.fixture
def ledger(monkeypatch):
    kp = Keypair()
    monkeypatch.setenv("SOLANA_KEYPAIR", str(kp))                 # base58 secret
    led = FakeLedger()
    chain.set_rpc(led)
    return led


def test_anchor_and_verify_round_trip(ledger):
    r = chain.anchor_hash(H)
    assert r["explorer_url"] == f"https://explorer.solana.com/tx/{r['signature']}?cluster=devnet"
    assert ledger.txs[r["signature"]] == f"better:v1:{H}"
    assert chain.verify_hash(r["signature"], H) is True
    tampered = hashlib.sha256(b"route, edited").hexdigest()
    assert chain.verify_hash(r["signature"], tampered) is False
    assert chain.verify_hash("1" * 64, H) is False                # unknown transaction


def test_anchor_root_memo(ledger):
    root = hashlib.sha256(b"root").hexdigest()
    r = chain.anchor_root("2026111720", root, 1234)
    assert ledger.txs[r["signature"]] == f"better:v1:snap:2026111720:{root}:1234"


def test_memo_sizes_under_500_bytes():
    assert len(chain.memo_for_hash(H).encode()) < 500
    assert len(chain.memo_for_root("2026111720", H, 10 ** 12).encode()) < 500
    with pytest.raises(ValueError):
        chain.memo_for_hash("not-a-hash")
    with pytest.raises(ValueError):
        chain.memo_for_root("bad label!", H, 1)


def test_no_signer_raises_clearly_without_rpc():
    calls = []
    chain.set_rpc(lambda m, p, t: calls.append(m))
    with pytest.raises(chain.NoSignerError, match="no signer configured"):
        chain.anchor_hash(H)
    assert calls == [] and chain.signer_pubkey() is None


def test_keypair_from_json_array_and_path(monkeypatch, tmp_path):
    kp = Keypair()
    monkeypatch.setenv("SOLANA_KEYPAIR", json.dumps(list(bytes(kp))))
    assert chain.signer_pubkey() == str(kp.pubkey())
    monkeypatch.delenv("SOLANA_KEYPAIR")
    p = tmp_path / "devnet.json"
    p.write_text(json.dumps(list(bytes(kp))))
    monkeypatch.setenv("SOLANA_KEYPAIR_PATH", str(p))
    assert chain.signer_pubkey() == str(kp.pubkey())


def test_rpc_error_and_failed_tx(ledger):
    def broken(m, p, t):
        raise chain.ChainError("devnet down")
    chain.set_rpc(broken)
    with pytest.raises(chain.ChainError, match="devnet down"):
        chain.anchor_hash(H)
    with pytest.raises(chain.ChainError):                         # verify can't tell: caller says "pending"
        chain.verify_hash("x", H)


def test_verify_reads_memo_from_logs():
    log_tx = {"meta": {"err": None, "logMessages": [f'Program log: Memo (len 74): "better:v1:{H}"']},
              "transaction": {"message": {"instructions": []}}}
    chain.set_rpc(lambda m, p, t: log_tx)
    assert chain.verify_hash("sig", H) is True
    chain.set_rpc(lambda m, p, t: {**log_tx, "meta": {**log_tx["meta"], "err": {"InstructionError": 0}}})
    assert chain.verify_hash("sig", H) is False
