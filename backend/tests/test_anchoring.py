"""Hourly snapshot anchoring: Merkle roots, the once-per-hour trigger, failure isolation, GET /anchors."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from solders.keypair import Keypair

import chain
from backend.app import create_app
from backend.logger import LogSettings, SnapshotLogger, hour_lines, merkle_root
from backend.receipt_store import ReceiptStore
from backend.tests.test_app import Env
from chain.tests.fake_rpc import FakeLedger

sha = lambda b: hashlib.sha256(b).digest()
T = datetime(2026, 11, 17, 20, 15, tzinfo=timezone.utc)


def test_merkle_root_known_inputs():
    assert merkle_root([]) == (None, 0)
    assert merkle_root([b"a"]) == (sha(b"a").hex(), 1)
    assert merkle_root([b"a", b"b"]) == (sha(sha(b"a") + sha(b"b")).hex(), 2)
    # odd count: the last node is duplicated
    ab, cc = sha(sha(b"a") + sha(b"b")), sha(sha(b"c") + sha(b"c"))
    assert merkle_root([b"a", b"b", b"c"]) == (sha(ab + cc).hex(), 3)
    cd = sha(sha(b"c") + sha(b"d"))
    assert merkle_root([b"a", b"b", b"c", b"d"])[0] == sha(ab + cd).hex()
    # five leaves: level sizes 5 -> 3 -> 2 -> 1
    ee = sha(sha(b"e") + sha(b"e"))
    l2 = [sha(ab + cd), sha(ee + ee)]
    assert merkle_root([b"a", b"b", b"c", b"d", b"e"]) == (sha(l2[0] + l2[1]).hex(), 5)
    assert hashlib.sha256(b"a").hexdigest() == "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"


def write_log(dirpath, lines_by_ts):
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, f"{T:%Y-%m-%d}.jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for ts in lines_by_ts:
            f.write(json.dumps({"type": "spot", "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "price": 1}) + "\n")
    return path


def test_hour_lines_selects_one_hour(tmp_path):
    ts = [T.replace(hour=19, minute=59), T.replace(minute=0), T.replace(minute=59, second=59), T.replace(hour=21, minute=0)]
    path = write_log(str(tmp_path), ts)
    got = list(hour_lines(path, T))
    assert len(got) == 2 and all(b'"2026-11-17T20:' in l for l in got)


def fake_chain():
    calls = []
    return SimpleNamespace(calls=calls, signer_pubkey=lambda: "PUB",
                           anchor_root=lambda label, root, count: calls.append((label, root, count)) or
                           {"signature": f"SIG{label}", "explorer_url": f"https://explorer.solana.com/tx/SIG{label}?cluster=devnet"})


def test_anchor_hour_saves_row(tmp_path):
    d = str(tmp_path / "snap")
    path = write_log(d, [T.replace(minute=m) for m in (1, 2, 3)])
    store = ReceiptStore(db_path=str(tmp_path / "r.db"))
    c = fake_chain()
    lg = SnapshotLogger(LogSettings(dir=d), background=False, chain=c, anchor_store=store)
    a = lg.anchor_hour(T)
    root, n = merkle_root(hour_lines(path, T))
    assert a["hour"] == "2026111720" and a["root"] == root and n == 3 and c.calls == [("2026111720", root, 3)]
    assert store.anchors()[0]["signature"] == "SIG2026111720"
    assert lg.anchor_hour(T + timedelta(hours=3)) is None              # no lines that hour: nothing saved


def test_trigger_once_per_hour_and_after_restart(tmp_path):
    d = str(tmp_path / "snap")
    write_log(d, [T.replace(minute=m) for m in (1, 2)])
    store = ReceiptStore(db_path=str(tmp_path / "r.db"))
    c = fake_chain()
    lg = SnapshotLogger(LogSettings(dir=d), background=False, chain=c, anchor_store=store)
    lg._maybe_anchor(T + timedelta(hours=1))                           # first write at 21:xx anchors 20:00
    lg._maybe_anchor(T + timedelta(hours=1, minutes=5))                # same hour again: nothing new
    lg.wait_anchors()
    assert [x[0] for x in c.calls] == ["2026111720"]
    lg2 = SnapshotLogger(LogSettings(dir=d), background=False, chain=c, anchor_store=store)   # restart
    lg2._maybe_anchor(T + timedelta(hours=1, minutes=10))
    lg2.wait_anchors()
    assert len(c.calls) == 1                                           # already in the store


def test_skipped_without_signer_or_chain(tmp_path):
    d = str(tmp_path / "snap")
    write_log(d, [T])
    store = ReceiptStore(db_path=str(tmp_path / "r.db"))
    for c in (None, chain):                                            # real chain: conftest clears the signer
        lg = SnapshotLogger(LogSettings(dir=d), background=False, chain=c, anchor_store=store)
        assert lg.anchoring_enabled() is False
        lg._maybe_anchor(T + timedelta(hours=1))
        lg.wait_anchors()
    assert store.anchors() == []


def test_anchor_failure_never_breaks_logging(tmp_path):
    d = str(tmp_path / "snap")
    write_log(d, [T])
    store = ReceiptStore(db_path=str(tmp_path / "r.db"))

    def boom(*a):
        raise RuntimeError("devnet down")

    lg = SnapshotLogger(LogSettings(dir=d), background=False, anchor_store=store,
                        chain=SimpleNamespace(signer_pubkey=lambda: "PUB", anchor_root=boom))
    a = lg.anchor_hour(T)
    assert a["signature"] is None and lg.anchor_errors == 1
    assert store.anchors()[0]["root"] == a["root"]
    broken = SnapshotLogger(LogSettings(dir=d), background=False, chain=fake_chain(), anchor_store=object())
    broken._maybe_anchor(T + timedelta(hours=1))                       # store without has_anchor: swallowed
    assert broken.anchor_errors == 1


def test_real_chain_anchor_root_with_mocked_rpc(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLANA_KEYPAIR", str(Keypair()))
    ledger = FakeLedger()
    chain.set_rpc(ledger)
    d = str(tmp_path / "snap")
    write_log(d, [T.replace(minute=m) for m in range(5)])
    store = ReceiptStore(db_path=str(tmp_path / "r.db"))
    a = SnapshotLogger(LogSettings(dir=d), background=False, chain=chain, anchor_store=store).anchor_hour(T)
    assert ledger.txs[a["signature"]] == f"better:v1:snap:2026111720:{a['root']}:5"


def test_get_anchors_endpoint(tmp_path):
    store = ReceiptStore(db_path=str(tmp_path / "r.db"))
    for h in range(50):
        store.save_anchor(f"20261117{h % 24:02d}" if h < 24 else f"20261118{(h - 24) % 24:02d}" if h < 48 else f"20261119{h - 48:02d}",
                          "ab" * 32, h, None, None)
    c = TestClient(create_app(state=Env().state, receipt_store=store))
    got = c.get("/anchors").json()
    assert len(got) == 48 and got[0]["hour"] == "2026111901" and set(got[0]) == {
        "hour", "root", "count", "signature", "explorer_url", "created_at"}
