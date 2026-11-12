"""Poll supplier data independently per account, address and connection."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VandebronApi, VandebronApiError, VandebronAuthError
from .const import (
    CONF_IMPORT_STATISTICS,
    CONF_INCLUDE_EXTRA_COSTS,
    CONF_INCLUDE_VAT,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .models import AMSTERDAM, UTC, Source, day_bounds, items, parse_prices, timestamp

_LOGGER = logging.getLogger(__name__)


class VandebronCoordinator(DataUpdateCoordinator[dict[str, Source]]):
    """Failures in one product do not discard another product's data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: VandebronApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.entry = entry
        self.options = dict(entry.options)
        self.discovery_errors: dict[str, str] = {}
        self._discovery_updated: dict[str, datetime] = {}
        self._organizations: list[str] = []

    @property
    def include_vat(self) -> bool:
        return self.entry.options.get(CONF_INCLUDE_VAT, True)

    @property
    def include_extra_costs(self) -> bool:
        return self.entry.options.get(CONF_INCLUDE_EXTRA_COSTS, True)

    async def _fetch(self, source: Source, key: str, call, *, hours: float = 2) -> Any:
        now = datetime.now(UTC)
        last = source.updated.get(key)
        if last and key not in source.errors and now - last < timedelta(hours=hours):
            return source.blocks.get(key)
        try:
            value = await call()
            expected = (
                (list,)
                if key.startswith(("prices_", "contract_prices_"))
                else (
                    (dict, list)
                    if key in ("rates", "invoices", "contracts") or key.startswith("cost_")
                    else (dict,)
                )
            )
            if not isinstance(value, expected):
                raise VandebronApiError("Unexpected response type")
        except VandebronApiError as err:
            # Store only a bounded status, never response bodies or customer URLs.
            source.errors[key] = str(err.status or "unavailable")
            _LOGGER.debug("Vandebron %s unavailable (%s)", key, source.errors[key])
            return None
        source.blocks[key] = value
        source.updated[key] = now
        source.errors.pop(key, None)
        return value

    async def _discover(self, name: str, call) -> Any:
        try:
            result = await call()
            self.discovery_errors.pop(name, None)
            self._discovery_updated[name] = datetime.now(UTC)
            return result
        except VandebronApiError as err:
            self.discovery_errors[name] = str(err.status or "unavailable")
            return None

    async def _async_update_data(self) -> dict[str, Source]:
        data = deepcopy(self.data or {})
        now = datetime.now(UTC)
        today = now.astimezone(AMSTERDAM).date()
        try:
            # Refresh discovery every six hours; retry failures on next poll.
            if self._due("organizations", now):
                orgs = await self._discover("organizations", self.api.async_get_organization_ids)
                if orgs is not None:
                    self._organizations = orgs
                    for source in data.values():
                        if source.organization and source.organization not in orgs:
                            source.errors["discovery"] = "inactive"
            for org in self._organizations:
                org_key = f"organization_{org}"
                organization = data.setdefault(
                    org_key,
                    Source(org_key, "Vandebron", "organization", organization=org),
                )
                organization.errors.pop("discovery", None)
                await self._fetch(
                    organization,
                    "contracts",
                    lambda org=org: self.api.async_get_contracts(org),
                    hours=6,
                )
                await self._fetch(
                    organization,
                    "customer",
                    self.api.async_get_customer_details,
                    hours=6,
                )
                await self._fetch(
                    organization,
                    "invoices",
                    lambda org=org: self.api.async_get_legacy_invoices(org),
                )
                consumer = await self._fetch(
                    organization,
                    "consumer",
                    lambda org=org: self.api.async_get_energy_consumer(org),
                    hours=6,
                )
                # Use cached topology on temporary failure, but mark it unavailable.
                consumer = (
                    consumer if consumer is not None else organization.blocks.get("consumer", {})
                )
                if not isinstance(consumer, dict):
                    raise VandebronApiError("Invalid energy consumer response")
                if "consumer" not in organization.errors:
                    for existing in data.values():
                        if existing.organization == org and existing.kind in (
                            "address",
                            "connection",
                        ):
                            existing.errors["discovery"] = "inactive"
                for address in items(consumer, "shippingAddresses"):
                    if address.get("isSwitchedOut"):
                        continue
                    cluster = address.get("clusterReference")
                    address_id = (
                        cluster or address.get("clusterId") or address.get("shippingAddressId")
                    )
                    if address_id is None:
                        # No address-level identity: connections can still be represented.
                        address_id = "connections"
                    name = _address_name(address.get("shippingAddress")) or str(address_id)
                    address_key = f"address_{org}_{address_id}"
                    house = data.setdefault(
                        address_key,
                        Source(
                            address_key,
                            name,
                            "address",
                            organization=org,
                            cluster=cluster,
                        ),
                    )
                    house.errors.pop("discovery", None)
                    if "consumer" in organization.errors:
                        house.errors["discovery"] = "unavailable"
                    cluster_id = address.get("clusterId")
                    if cluster_id:
                        await self._fetch(
                            house,
                            "contract",
                            lambda org=org, cluster_id=cluster_id: (
                                self.api.async_get_contract_details(org, str(cluster_id))
                            ),
                            hours=6,
                        )
                    if cluster:
                        await self._fetch(
                            house,
                            "advance",
                            lambda org=org, cluster=cluster: self.api.async_get_advance_payments(
                                org, cluster
                            ),
                        )
                    for connection in items(address, "connections"):
                        conn_id = connection.get("connectionId")
                        segment = connection.get("marketSegment")
                        if conn_id is None or segment not in ("Electricity", "Gas"):
                            continue
                        key = f"connection_{org}_{conn_id}"
                        source = data.setdefault(
                            key,
                            Source(
                                key,
                                name,
                                "connection",
                                organization=org,
                                connection=str(conn_id),
                                segment=segment,
                                cluster=cluster,
                            ),
                        )
                        source.metadata = connection
                        source.metadata["address_key"] = address_key
                        source.errors.pop("discovery", None)
                        if "consumer" in organization.errors:
                            source.errors["discovery"] = "unavailable"
                        await self._fetch(
                            source,
                            "meter",
                            lambda org=org, conn_id=conn_id: self.api.async_get_meter_status(
                                org, str(conn_id)
                            ),
                            hours=6,
                        )
                        contract = source.metadata.get("contract") or {}
                        contract_id = contract.get("contractId")
                        if contract_id:
                            price_days = [today]
                            next_year = today.replace(year=today.year + 1, month=1, day=1)
                            end_date = timestamp(contract.get("endDate"))
                            if (
                                end_date is None
                                or next_year < end_date.astimezone(AMSTERDAM).date()
                            ):
                                price_days.append(next_year)
                            for price_day in price_days:
                                await self._fetch(
                                    source,
                                    f"contract_prices_{price_day}",
                                    lambda org=org, contract_id=contract_id, price_day=price_day: (
                                        self.api.async_get_contract_prices(
                                            org, contract_id, price_day
                                        )
                                    ),
                                    hours=6,
                                )
                            allowed_contract_dates = {f"contract_prices_{d}" for d in price_days}
                            for block in list(source.blocks):
                                if (
                                    block.startswith("contract_prices_")
                                    and block not in allowed_contract_dates
                                ):
                                    source.blocks.pop(block, None)
                                    source.updated.pop(block, None)
                                    source.errors.pop(block, None)
                        if not source.is_dynamic:
                            source.prices.clear()
                        else:
                            for offset in (-1, 0, 1):
                                day = today + timedelta(days=offset)
                                price_key = f"prices_{day.isoformat()}"
                                rows = await self._fetch(
                                    source,
                                    price_key,
                                    lambda day=day, org=org, conn_id=conn_id: (
                                        self.api.async_get_market_prices(
                                            org,
                                            str(conn_id),
                                            include_vat=self.include_vat,
                                            include_extra_costs=self.include_extra_costs,
                                            price_date=day,
                                        )
                                    ),
                                    hours=0,
                                )
                                if rows is not None:
                                    source.prices[day.isoformat()] = parse_prices(rows)
                        allowed = {(today + timedelta(days=i)).isoformat() for i in (-1, 0, 1)}
                        source.prices = {k: v for k, v in source.prices.items() if k in allowed}
                        for k in list(source.blocks):
                            if k.startswith("prices_") and k[7:] not in allowed:
                                source.blocks.pop(k, None)
                                source.updated.pop(k, None)
                                source.errors.pop(k, None)
                        for period, day in (
                            ("recent", today - timedelta(days=1)),
                            ("month", today.replace(day=1)),
                            ("year", today.replace(month=1, day=1)),
                        ):
                            start, _ = day_bounds(day)
                            _, end = day_bounds(today)
                            await self._fetch(
                                source,
                                f"usage_{period}_{day}",
                                lambda start=start, end=end, org=org, conn_id=conn_id: (
                                    self.api.async_get_usage(org, str(conn_id), start, end)
                                ),
                                hours=2,
                            )
                            if period != "recent":
                                await self._fetch(
                                    source,
                                    f"cost_{period}_{day}",
                                    lambda start=start, end=end, org=org, conn_id=conn_id: (
                                        self.api.async_get_costs(org, str(conn_id), start, end)
                                    ),
                                )
                        # Prune previous month/year snapshots to bound memory.
                        _prune_periods(source, today)
            if self._due("accounts", now):
                accounts = await self._discover("accounts", self.api.async_get_accounts)
                if accounts is not None:
                    if not isinstance(accounts, dict):
                        raise VandebronApiError("Invalid accounts response")
                    for existing in data.values():
                        if existing.kind == "account":
                            existing.errors["discovery"] = "inactive"
                    for account in items(accounts, "accounts"):
                        if account.get("isActive") is False or not account.get("accountNumber"):
                            continue
                        number = str(account["accountNumber"])
                        key = f"account_{number}"
                        source = data.setdefault(
                            key,
                            Source(
                                key,
                                str(account.get("deliveryAddress") or number),
                                "account",
                                account=number,
                            ),
                        )
                        source.metadata = account
                        source.errors.pop("discovery", None)
            for source in data.values():
                if source.kind != "account" or source.errors.get("discovery") == "inactive":
                    continue
                account = source.account
                await self._fetch(
                    source,
                    "rates",
                    lambda account=account: self.api.async_get_rates(account),
                    hours=6,
                )
                await self._fetch(
                    source,
                    "agreements",
                    lambda account=account: self.api.async_get_agreements(account),
                    hours=6,
                )
                await self._fetch(
                    source,
                    "costs",
                    lambda account=account: self.api.async_get_latest_costs(account),
                )
                await self._fetch(
                    source,
                    "invoices",
                    lambda account=account: self.api.async_get_invoices(account),
                )
                await self._fetch(
                    source,
                    "reading_dates",
                    lambda account=account: self.api.async_get_last_reading_dates(account),
                )
                for period, day in (
                    ("recent", today - timedelta(days=1)),
                    ("month", today.replace(day=1)),
                    ("year", today.replace(month=1, day=1)),
                ):
                    await self._fetch(
                        source,
                        f"usage_{period}_{day}",
                        lambda day=day, account=account: self.api.async_get_usage_graphs(
                            account, day, today
                        ),
                    )
                _prune_periods(source, today)
            if not data:
                raise UpdateFailed("No energy connections or active accounts could be discovered")
            if self.options.get(CONF_IMPORT_STATISTICS, False):
                from .statistics import async_import_usage

                await async_import_usage(self.hass, data)
            return data
        except VandebronAuthError as err:
            raise ConfigEntryAuthFailed("Sign in to Vandebron again") from err
        except VandebronApiError as err:
            raise UpdateFailed("Invalid response from Vandebron") from err

    def _due(self, key: str, now: datetime) -> bool:
        return (
            key in self.discovery_errors
            or key not in self._discovery_updated
            or now - self._discovery_updated[key] >= timedelta(hours=6)
        )


def _prune_periods(source: Source, today) -> None:
    keep = {
        f"usage_recent_{today - timedelta(days=1)}",
        f"usage_month_{today.replace(day=1)}",
        f"usage_year_{today.replace(month=1, day=1)}",
        f"cost_month_{today.replace(day=1)}",
        f"cost_year_{today.replace(month=1, day=1)}",
    }
    for key in list(source.blocks):
        if key.startswith(("usage_", "cost_")) and key not in keep:
            source.blocks.pop(key, None)
            source.updated.pop(key, None)
            source.errors.pop(key, None)


def _address_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return (
        " ".join(
            str(value[k]) for k in ("street", "number", "numberAddition", "city") if value.get(k)
        )
        or None
    )
