"""Repo-wide test isolation: no test may reach Solana, a real signer, or a real database."""
import pytest


@pytest.fixture(autouse=True)
def _offline_chain(monkeypatch, tmp_path):
    for var in ("SOLANA_KEYPAIR", "SOLANA_KEYPAIR_PATH", "SOLANA_RPC_URL", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RECEIPTS_DB", str(tmp_path / "receipts.db"))
    try:
        import chain
    except ImportError:
        yield
        return

    def blocked(method, params, timeout):
        raise chain.ChainError(f"network disabled in tests ({method})")

    chain.set_rpc(blocked)
    yield
    chain.set_rpc(None)
