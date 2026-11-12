from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.vandebron.api import VandebronApiError, VandebronAuthError
from custom_components.vandebron.coordinator import VandebronCoordinator
from custom_components.vandebron.models import AMSTERDAM
from custom_components.vandebron.sensor import VandebronSensor, async_setup_entry, metrics


@pytest.fixture
def api(measurements):
    client = Mock()
    values = {
        "organization_ids": ["org"],
        "customer_details": {"person": {"firstName": "Test", "lastName": "User"}},
        "energy_consumer": {
            "shippingAddresses": [
                {
                    "clusterReference": "1234/ab",
                    "clusterId": "cluster-uuid",
                    "shippingAddress": {"street": "Test", "number": 1},
                    "connections": [
                        {
                            "connectionId": "e1",
                            "marketSegment": "Electricity",
                            "contract": {"contractType": "MarketPriceVariable"},
                        },
                        {
                            "connectionId": "g1",
                            "marketSegment": "Gas",
                            "contract": {"contractType": "MarketPriceVariable"},
                        },
                    ],
                },
                {
                    "shippingAddressId": "second",
                    "connections": [
                        {
                            "connectionId": "e2",
                            "marketSegment": "Electricity",
                            "contract": {"contractType": "MarketPriceVariable"},
                        }
                    ],
                },
            ]
        },
        "market_prices": [],
        "meter_status": {"administrativeStatusSmartMeter": True, "communicationStatus": True},
        "costs": [],
        "contracts": [],
        "contract_details": {},
        "advance_payments": {"currentAdvancePayment": {"amount": 120}},
        "usage": {"unit": "WH", "values": []},
        "legacy_invoices": [],
        "accounts": {
            "accounts": [
                {"accountNumber": "one", "isActive": True},
                {"accountNumber": "two", "isActive": True},
                {"accountNumber": "old", "isActive": False},
            ]
        },
        "rates": {
            "electricity": [
                {
                    "type": "PeakDeliveryCost",
                    "unit": "KWh",
                    "amount": {"value": "0.3", "unit": "eur"},
                }
            ]
        },
        "agreements": {
            "agreements": [
                {"market": "ELECTRICITY", "productType": "Variable", "startDate": "2020-01-01"}
            ]
        },
        "latest_costs": {
            "month": "2026-06-01",
            "costs": {"actual": {"value": "12", "unit": "eur"}},
        },
        "invoices": {"data": []},
        "last_reading_dates": {},
        "usage_graphs": measurements,
    }
    for key, value in values.items():
        setattr(client, f"async_get_{key}", AsyncMock(return_value=deepcopy(value)))
    return client


async def test_all_sources_and_per_connection_prices(hass, entry, api):
    coordinator = VandebronCoordinator(hass, entry, api)
    data = await coordinator._async_update_data()
    assert {
        "connection_org_e1",
        "connection_org_g1",
        "connection_org_e2",
        "account_one",
        "account_two",
    } <= set(data)
    assert "account_old" not in data
    calls = api.async_get_market_prices.await_args_list
    assert len(calls) == 9
    today = datetime.now(UTC).astimezone(AMSTERDAM).date()
    for conn in ("e1", "g1", "e2"):
        assert {c.kwargs["price_date"] for c in calls if c.args[1] == conn} == {
            today - timedelta(days=1),
            today,
            today + timedelta(days=1),
        }
    assert api.async_get_rates.await_count == 2
    house = data["address_org_1234/ab"]
    metric = next(m for m in metrics(house) if m.key == "monthly_advance_amount")
    assert metric.value(house, datetime.now(UTC)) == 120


async def test_optional_failure_and_recovery(hass, entry, api):
    coordinator = VandebronCoordinator(hass, entry, api)
    api.async_get_rates.side_effect = VandebronApiError("Forbidden", 403)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.data["account_one"].errors["rates"] == "403"
    assert "usage_year_" in " ".join(coordinator.data["account_one"].blocks)
    api.async_get_rates.side_effect = None
    coordinator.data = await coordinator._async_update_data()
    assert "rates" not in coordinator.data["account_one"].errors


async def test_late_discovery_adds_entities(hass, entry, api):
    coordinator = VandebronCoordinator(hass, entry, api)
    api.async_get_energy_consumer.side_effect = VandebronApiError("offline")
    coordinator.data = await coordinator._async_update_data()
    entry.runtime_data = coordinator
    added = []
    await async_setup_entry(hass, entry, added.extend)
    before = len(added)
    api.async_get_energy_consumer.side_effect = None
    coordinator.async_set_updated_data(await coordinator._async_update_data())
    assert len(added) > before
    assert len({e.unique_id for e in added}) == len(added)
    # The listener registered by sensor setup is removed on config-entry unload.
    coordinator._listeners.clear()


async def test_auth_failure_requests_reauth(hass, entry, api):
    api.async_get_organization_ids.side_effect = VandebronAuthError("expired")
    with pytest.raises(ConfigEntryAuthFailed):
        await VandebronCoordinator(hass, entry, api)._async_update_data()


async def test_no_accounts_no_connections_retries(hass, entry, api):
    api.async_get_organization_ids.return_value = []
    api.async_get_accounts.side_effect = VandebronApiError("offline")
    with pytest.raises(UpdateFailed):
        await VandebronCoordinator(hass, entry, api)._async_update_data()


async def test_cached_price_expiration_and_timers(hass, entry, api, monkeypatch):
    coordinator = VandebronCoordinator(hass, entry, api)
    now = datetime.now(UTC)
    api.async_get_market_prices.return_value = [
        {
            "startDate": (now - timedelta(minutes=5)).isoformat(),
            "endDate": (now + timedelta(minutes=10)).isoformat(),
            "price": -0.1,
        }
    ]
    coordinator.data = await coordinator._async_update_data()
    source = coordinator.data["connection_org_e1"]
    metric = next(m for m in metrics(source) if m.key == "electricity_price_now")
    entity = VandebronSensor(coordinator, source.key, metric)
    entity.hass = hass
    assert entity.native_value == -0.1 and entity.available
    scheduled = []
    cancelled = Mock()
    monkeypatch.setattr(
        "custom_components.vandebron.sensor.async_track_point_in_utc_time",
        lambda h, cb, at: (scheduled.append(at), cancelled)[1],
    )
    entity._schedule()
    assert scheduled[-1] == now + timedelta(minutes=10)
    entity._schedule()
    cancelled.assert_called_once()
    api.async_get_market_prices.side_effect = VandebronApiError("offline")
    coordinator.data = await coordinator._async_update_data()
    assert entity.available  # validity, not fetch success, controls cached prices
    coordinator.data[source.key].prices = {}
    assert not entity.available
    await entity.async_will_remove_from_hass()


async def test_discovery_removal(hass, entry, api):
    coordinator = VandebronCoordinator(hass, entry, api)
    coordinator.data = await coordinator._async_update_data()
    coordinator._discovery_updated.clear()
    api.async_get_accounts.return_value = {"accounts": []}
    api.async_get_energy_consumer.return_value = {"shippingAddresses": []}
    for source in coordinator.data.values():
        source.updated.clear()
    data = await coordinator._async_update_data()
    assert data["connection_org_e1"].errors["discovery"] == "inactive"
    assert data["account_one"].errors["discovery"] == "inactive"


async def test_fixed_contract_does_not_fetch_market_prices(hass, entry, api):
    for address in api.async_get_energy_consumer.return_value["shippingAddresses"]:
        for connection in address["connections"]:
            connection["contract"] = {"contractType": "Fixed1Year"}
    data = await VandebronCoordinator(hass, entry, api)._async_update_data()
    api.async_get_market_prices.assert_not_awaited()
    assert all(not source.prices for source in data.values())


async def test_contract_prices_use_connection_contract_id(hass, entry, api):
    api.async_get_contract_prices = AsyncMock(return_value=[])
    for address in api.async_get_energy_consumer.return_value["shippingAddresses"]:
        for connection in address["connections"]:
            connection["contract"] = {
                "contractType": "Fixed1Year",
                "contractId": "contract-" + connection["connectionId"],
            }
    data = await VandebronCoordinator(hass, entry, api)._async_update_data()
    assert {c.args[1] for c in api.async_get_contract_prices.await_args_list} == {
        "contract-e1",
        "contract-g1",
        "contract-e2",
    }
    api.async_get_market_prices.assert_not_awaited()
    assert (
        len([k for k in data["connection_org_e1"].blocks if k.startswith("contract_prices_")]) == 2
    )
