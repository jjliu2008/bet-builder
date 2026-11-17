from backend.receipt_store import SqliteReceiptStore, receipt_store_from_env


def test_roundtrip_and_persistence(tmp_path):
    path = str(tmp_path / "r.db")
    s = SqliteReceiptStore(path)
    assert "a" not in s and s.get("a") is None
    s["a"] = {"receipt": {"hash": "h"}, "route": {"x": 1}}
    assert "a" in s and s["a"]["receipt"]["hash"] == "h"
    again = SqliteReceiptStore(path)  # a new process would open the same file
    assert again["a"]["route"] == {"x": 1} and len(again) == 1


def test_env_switch(monkeypatch, tmp_path):
    monkeypatch.delenv("RECEIPTS_DB", raising=False)
    assert receipt_store_from_env() is None
    monkeypatch.setenv("RECEIPTS_DB", str(tmp_path / "x.db"))
    assert isinstance(receipt_store_from_env(), SqliteReceiptStore)
