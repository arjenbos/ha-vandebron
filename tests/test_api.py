import asyncio
import base64
import json
import time
from datetime import date
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.vandebron import api as module
from custom_components.vandebron.api import (
    VandebronApi,
    VandebronApiError,
    VandebronAuthError,
    VandebronTokens,
)


def jwt(user="customer"):
    body = (
        base64.urlsafe_b64encode(json.dumps({"preferred_username": user}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{body}.signature"


@pytest.mark.asyncio
async def test_marketprices_list_and_query(monkeypatch):
    async def handler(request):
        assert request.path == "/v1/p4/organizations/org/connections/e/marketprices"
        assert request.query == {
            "priceDate": "2026-06-01",
            "includeVat": "true",
            "includeExtraCosts": "false",
        }
        assert request.headers["Authorization"].startswith("Bearer ")
        return web.json_response([{"price": 0}])

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(module, "API_BASE_MIJN", str(server.make_url("")))
        client = VandebronApi(session, VandebronTokens(jwt(), "refresh", time.time() + 300))
        assert await client.async_get_market_prices(
            "org", "e", price_date=date(2026, 6, 1), include_extra_costs=False
        ) == [{"price": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,status,exception",
    [
        ("broken", 200, VandebronApiError),
        ({}, 403, VandebronApiError),
        ({}, 500, VandebronApiError),
        ({}, 401, VandebronAuthError),
    ],
)
async def test_request_errors(response, status, exception):
    async def handler(request):
        return (
            web.Response(text="not json", status=status)
            if response == "broken"
            else web.json_response(response, status=status)
        )

    app = web.Application()
    app.router.add_get("/", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = VandebronApi(session, VandebronTokens(jwt(), "r", time.time() + 300))
        with pytest.raises(exception):
            await client._request("GET", str(server.make_url("")), "/", _retry=False)


@pytest.mark.asyncio
async def test_refresh_serialized_and_rotated():
    tokens = VandebronTokens(jwt(), "old", 0, customer_reference="customer")
    client = VandebronApi(None, tokens)

    async def refresh(data):
        await asyncio.sleep(0)
        return VandebronTokens(jwt(), "new", time.time() + 300)

    client._token_request = AsyncMock(side_effect=refresh)
    saved = []
    client.on_tokens_refreshed = saved.append
    await asyncio.gather(*(client._async_ensure_token() for _ in range(8)))
    assert client._token_request.await_count == 1
    assert client.tokens.refresh_token == "new"
    assert client.tokens.customer_reference == "customer"
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_401_retry(monkeypatch):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return web.json_response({}, status=401 if calls == 1 else 200)

    app = web.Application()
    app.router.add_get("/", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = VandebronApi(session, VandebronTokens(jwt(), "r", time.time() + 300))
        client._token_request = AsyncMock(
            return_value=VandebronTokens(jwt(), "new", time.time() + 300)
        )
        assert await client._request("GET", str(server.make_url("")), "/") == {}
        assert calls == 2
        client._token_request.assert_awaited_once()


async def test_legacy_parameters_match_apk():
    from datetime import UTC, datetime

    client = VandebronApi(None)
    client._request = AsyncMock(return_value={})
    start = datetime(2026, 10, 31, 23, tzinfo=UTC)
    end = datetime(2026, 11, 12, 23, tzinfo=UTC)
    await client.async_get_usage("org", "conn", start, end)
    assert client._request.call_args.kwargs["params"] == {
        "resolution": "Days",
        "startDate": "2026-11-01",
        "endDate": "2026-11-13",
    }
    await client.async_get_costs("org", "conn", start, end)
    assert client._request.call_args.kwargs["params"] == {
        "connectionIds": "CONN",
        "resolution": "Days",
        "includeEnergyTax": "true",
        "startDate": "2026-11-01",
        "endDate": "2026-11-13",
    }
    await client.async_get_contract_details("org", "cluster-uuid")
    assert client._request.call_args.args[2].endswith(
        "/shippingAddress/cluster-uuid/contractDetails"
    )
    await client.async_get_legacy_invoices("org")
    assert (
        client._request.call_args.kwargs["params"]["invoiceType"]
        == "AdvancePayment,Settlement,Purchase,Custom"
    )


async def test_modern_401_does_not_revoke_legacy_token(monkeypatch):
    async def handler(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(module, "API_BASE_USERS", str(server.make_url("")))
        client = VandebronApi(session, VandebronTokens(jwt(), "r", time.time() + 300))
        client._token_request = AsyncMock()
        with pytest.raises(VandebronApiError) as exc:
            await client.async_get_accounts()
        assert exc.value.status == 401
        client._token_request.assert_not_awaited()
        assert client.tokens.refresh_token == "r"


async def test_contract_prices_are_scoped_and_dated():
    client = VandebronApi(None)
    rows = [{"priceComponentType": "Base", "priceTaxed": 0.121}]
    client._request = AsyncMock(return_value=rows)
    assert await client.async_get_contract_prices("org", "contract", date(2026, 11, 12)) == rows
    assert client._request.call_args.args[2] == "/v1/energyConsumers/org/contracts/contract/prices"
    assert client._request.call_args.kwargs["params"] == {"priceDate": "2026-11-12"}
    client._request.return_value = {"price": 0.1}
    with pytest.raises(VandebronApiError):
        await client.async_get_contract_prices("org", "contract", date(2026, 11, 12))
