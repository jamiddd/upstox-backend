from __future__ import annotations

from app.services.upstox_portfolio_feed_client import UpstoxPortfolioFeedClient


def _client() -> tuple[UpstoxPortfolioFeedClient, list[dict[str, object]]]:
    updates: list[dict[str, object]] = []
    client = UpstoxPortfolioFeedClient(
        upstox=None,  # type: ignore[arg-type]
        token_store=None,  # type: ignore[arg-type]
        on_order_update=updates.append,
    )
    return client, updates


def test_dispatches_order_update_messages() -> None:
    client, updates = _client()

    client._on_message('{"update_type": "order", "order_id": "O1", "status": "complete"}')

    assert updates == [{"update_type": "order", "order_id": "O1", "status": "complete"}]


def test_ignores_non_order_update_types() -> None:
    client, updates = _client()

    client._on_message('{"update_type": "holding", "value": 1}')

    assert updates == []


def test_handles_binary_frames() -> None:
    client, updates = _client()

    client._on_message(b'{"update_type": "order", "order_id": "O1", "status": "open"}')

    assert updates == [{"update_type": "order", "order_id": "O1", "status": "open"}]


def test_ignores_malformed_json() -> None:
    client, updates = _client()

    client._on_message("not json")

    assert updates == []
