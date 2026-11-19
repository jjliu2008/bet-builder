from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from backend import xstocks


@pytest.mark.parametrize("symbol", ["AAPLx", "NVDAx"])
def test_reads_only_requested_token_pair(monkeypatch, symbol):
    pair = f"{symbol}/USD"
    fetch = Mock(return_value=Mock(json=lambda: {"error": [], "result": {
        pair: [["123.50", "1", 1700000000]], "last": "cursor"}}))
    monkeypatch.setattr(xstocks.requests, "get", fetch)
    result = xstocks.last_trade(symbol)
    assert result["price_usd"] == 123.5 and result["traded_at"].timestamp() == 1700000000
    assert fetch.call_args.kwargs["params"]["asset_class"] == "tokenized_asset"
    assert fetch.call_args.kwargs["params"]["pair"] == pair


def test_wrong_market_cannot_supply_a_fallback_price(monkeypatch):
    monkeypatch.setattr(xstocks.requests, "get", Mock(return_value=Mock(json=lambda: {
        "error": [], "result": {"AAPL/USD": [["1", "1", 1700000000]]}})))
    with pytest.raises(HTTPException) as error:
        xstocks.last_trade("AAPLx")
    assert error.value.status_code == 503
