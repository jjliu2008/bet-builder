"""Persistent receipts: restart survival, POST /verify without a stored receipt, startup retry."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair

import chain
from backend.app import create_app
from backend.receipt_store import ReceiptStore
from backend.receipts import fingerprint, retry_pending
from backend.tests.test_app import EXP_KEY, Env
from chain.tests.fake_rpc import FakeLedger

REQ = {"asset": "BTC", "expiry": EXP_KEY, "type": "above", "lo": 68000, "hi": None, "size": 10}


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.setenv("SOLANA_KEYPAIR", str(Keypair()))
    led = FakeLedger()
    chain.set_rpc(led)
    return led


def app_on(db, env=None):
    env = env or Env()
    return TestClient(create_app(state=env.state, chain=chain, receipt_store=ReceiptStore(db_path=str(db)))), env


def test_store_mapping_and_queries(tmp_path):
    s = ReceiptStore(db_path=str(tmp_path / "r.db"))
    assert s.backend == "sqlite" and len(s) == 0 and "x" not in s and s.get("x") is None
    s["a"] = {"request": None, "route": {"k": 1}, "receipt": {"status": "pending", "signature": None}}
    s["b"] = {"request": None, "route": {"k": 2}, "receipt": {"status": "confirmed", "signature": "S"}}
    assert "a" in s and s["a"]["route"] == {"k": 1} and len(s) == 2 and set(s) == {"a", "b"}
    assert s.pending_ids() == ["a"]
    s["a"] = {**s["a"], "receipt": {"status": "confirmed", "signature": "T"}}       # upsert
    assert s.pending_ids() == [] and len(s) == 2
    del s["b"]
    assert "b" not in s
    s.save_anchor("2026111720", "ab" * 32, 10, "SIG", "url")
    s.save_anchor("2026111721", "cd" * 32, 12, None, None)
    assert [a["hour"] for a in s.anchors()] == ["2026111721", "2026111720"] and s.has_anchor("2026111720")


def test_lazy_connect_creates_no_file(tmp_path):
    p = tmp_path / "sub" / "r.db"
    ReceiptStore(db_path=str(p))
    assert not p.exists()


def test_postgres_selected_by_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    s = ReceiptStore()
    assert s.backend == "postgres" and s._ph == "%s"               # no connection until first use


def test_receipt_survives_restart(tmp_path, ledger):
    db = tmp_path / "receipts.db"
    c1, env = app_on(db)
    route = c1.post("/route", json=REQ).json()
    rec = c1.post("/receipt", json={"request": REQ, "route": route}).json()
    assert rec["status"] == "confirmed"
    c1.app.state.receipts.close()

    c2, _ = app_on(db, env)                                          # a "restart": new store, same file
    v = c2.get(f"/verify/{rec['route_id']}").json()
    assert v["status"] == "verified" and v["signature"] == rec["signature"]
    again = c2.post("/receipt", json={"request": REQ, "route": route}).json()
    assert again["signature"] == rec["signature"]                    # no second transaction
    assert len(ledger.txs) == 1


def test_post_verify_without_stored_receipt(tmp_path, ledger):
    issuer, env = app_on(tmp_path / "issuer.db")                     # the server that wrote the receipt
    route = issuer.post("/route", json=REQ).json()
    sig = issuer.post("/receipt", json={"request": REQ, "route": route}).json()["signature"]
    c, _ = app_on(tmp_path / "empty.db", env)                         # a server with an empty database
    assert c.get(f"/verify/{fingerprint(None, {})[:24]}").status_code == 404
    body = {"request": REQ, "route": route, "signature": sig}
    v = c.post("/verify", json=body).json()
    assert v["status"] == "verified" and v["explorer_url"].endswith("?cluster=devnet")
    tampered = {**body, "route": {**route, "total_cost": 0.01}}
    assert c.post("/verify", json=tampered).json()["status"] == "mismatch"
    assert c.post("/verify", json={"route": route}).status_code == 422


def test_post_verify_chain_unreachable_is_pending(tmp_path):
    c, _ = app_on(tmp_path / "a.db")                                  # conftest blocks the RPC
    route = c.post("/route", json=REQ).json()
    v = c.post("/verify", json={"request": REQ, "route": route, "signature": "abc"}).json()
    assert v["status"] == "pending" and "network disabled" in v["detail"]


def test_pending_receipts_retried(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    c, env = app_on(db)                                               # no signer yet
    route = c.post("/route", json=REQ).json()
    rec = c.post("/receipt", json={"request": REQ, "route": route}).json()
    assert rec["status"] == "pending" and "no signer configured" in rec["error"]

    monkeypatch.setenv("SOLANA_KEYPAIR", str(Keypair()))
    chain.set_rpc(FakeLedger())
    store = ReceiptStore(db_path=str(db))
    assert asyncio.run(retry_pending(store, chain)) == 1
    assert store[rec["route_id"]]["receipt"]["status"] == "confirmed"


def test_startup_retry_runs_in_background(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    c, env = app_on(db)
    route = c.post("/route", json=REQ).json()
    rid = c.post("/receipt", json={"request": REQ, "route": route}).json()["route_id"]
    monkeypatch.setenv("SOLANA_KEYPAIR", str(Keypair()))
    chain.set_rpc(FakeLedger())
    store = ReceiptStore(db_path=str(db))
    with TestClient(create_app(state=env.state, chain=chain, receipt_store=store)) as c2:
        for _ in range(50):
            if store[rid]["receipt"]["status"] == "confirmed":
                break
            time.sleep(0.05)
        assert c2.get(f"/verify/{rid}").json()["status"] == "verified"


def test_retry_skips_without_signer(tmp_path):
    store = ReceiptStore(db_path=str(tmp_path / "r.db"))
    store["x"] = {"request": None, "route": {}, "receipt": {"status": "pending", "signature": None, "hash": "0" * 64}}
    assert asyncio.run(retry_pending(store, chain)) == 0


def test_savings_summary_counts_distinct_comparable_live_routes(tmp_path):
    from backend.receipt_store import ReceiptStore
    store = ReceiptStore(str(tmp_path / "stats.db"), database_url="")
    for key, mode, amount in [("a", "live", 2), ("a", "live", 2), ("b", "replay", 10),
                              ("c", "live", None), ("d", "live", 4)]:
        store.record_route(key, "2026-11-19", mode, amount, {})
    assert store.savings_summary() == {"observations": 2, "average_estimated_savings_usd": 3,
                                       "largest_estimated_savings_usd": 4}
