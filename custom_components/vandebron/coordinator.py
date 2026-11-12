"""Data update coordinator for Vandebron Thuis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VandebronApi, VandebronApiError, VandebronAuthError
from .const import (
    CONF_INCLUDE_EXTRA_COSTS,
    CONF_INCLUDE_VAT,
    DOMAIN,
    SEGMENT_ELECTRICITY,
    SEGMENT_GAS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, treating naive values as UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_float(value: Any) -> float | None:
    try:
        return round(float(value), 5)
    except (TypeError, ValueError):
        return None


@dataclass
class ConnectionData:
    """Prices + metadata for a single connection (electricity or gas)."""

    connection_id: str
    cluster_reference: str | None
    segment: str
    prices: list[dict[str, Any]] = field(default_factory=list)

    def current_entry(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        for entry in self.prices:
            start = _parse_dt(entry.get("startDate"))
            end = _parse_dt(entry.get("endDate"))
            if start and end and start <= now < end:
                return entry
        return None

    def current_price(self) -> float | None:
        entry = self.current_entry()
        return _as_float(entry.get("price")) if entry else None

    def next_price(self) -> float | None:
        now = datetime.now(timezone.utc)
        upcoming = [
            (_parse_dt(e.get("startDate")), e)
            for e in self.prices
            if _parse_dt(e.get("startDate")) and _parse_dt(e["startDate"]) > now
        ]
        upcoming.sort(key=lambda x: x[0])
        return _as_float(upcoming[0][1].get("price")) if upcoming else None

    def _today_values(self) -> list[float]:
        today = datetime.now(timezone.utc).date()
        values: list[float] = []
        for entry in self.prices:
            start = _parse_dt(entry.get("startDate"))
            price = _as_float(entry.get("price"))
            if start and price is not None and start.date() == today:
                values.append(price)
        return values

    def today_average(self) -> float | None:
        values = self._today_values()
        return round(sum(values) / len(values), 5) if values else None

    def today_min(self) -> float | None:
        values = self._today_values()
        return min(values) if values else None

    def today_max(self) -> float | None:
        values = self._today_values()
        return max(values) if values else None

    @property
    def unit(self) -> str | None:
        if self.prices:
            return self.prices[0].get("priceUnit")
        return "kWh" if self.segment == SEGMENT_ELECTRICITY else "m3"


@dataclass
class VandebronData:
    """Everything a single refresh produced."""

    organization_id: str
    customer: dict[str, Any] = field(default_factory=dict)
    connections: dict[str, ConnectionData] = field(default_factory=dict)
    contract: dict[str, Any] = field(default_factory=dict)
    advance_payment: dict[str, Any] = field(default_factory=dict)
    accounts: dict[str, Any] = field(default_factory=dict)
    latest_costs: dict[str, Any] = field(default_factory=dict)
    rates: dict[str, Any] = field(default_factory=dict)
    invoices: Any = None

    def electricity(self) -> ConnectionData | None:
        for conn in self.connections.values():
            if conn.segment == SEGMENT_ELECTRICITY:
                return conn
        return None

    def gas(self) -> ConnectionData | None:
        for conn in self.connections.values():
            if conn.segment == SEGMENT_GAS:
                return conn
        return None


class VandebronCoordinator(DataUpdateCoordinator[VandebronData]):
    """Fetches and caches the account's data on a fixed interval."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: VandebronApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self.api = api
        self._organization_id: str | None = None

    @property
    def include_vat(self) -> bool:
        return self.entry.options.get(CONF_INCLUDE_VAT, True)

    @property
    def include_extra_costs(self) -> bool:
        return self.entry.options.get(CONF_INCLUDE_EXTRA_COSTS, True)

    async def _async_update_data(self) -> VandebronData:
        try:
            if self._organization_id is None:
                self._organization_id = await self.api.async_get_organization_id()
            org = self._organization_id

            data = VandebronData(organization_id=org)

            data.customer = await self._safe(self.api.async_get_customer_details())
            consumer = await self._safe(self.api.async_get_energy_consumer(org))

            # Walk the consumer tree to find connections (per shipping address).
            connections: dict[str, ConnectionData] = {}
            primary_cluster: str | None = None
            for addr in (consumer or {}).get("shippingAddresses", []):
                cluster_ref = addr.get("clusterReference")
                if primary_cluster is None:
                    primary_cluster = cluster_ref
                for conn in addr.get("connections", []):
                    conn_id = str(conn.get("connectionId"))
                    segment = conn.get("marketSegment") or SEGMENT_ELECTRICITY
                    connections[conn_id] = ConnectionData(
                        connection_id=conn_id,
                        cluster_reference=cluster_ref,
                        segment=segment,
                    )

            # Fetch market prices. One connection returns both segments, so we
            # only need to call it once per address, then split the arrays out.
            today = date.today()
            seen_addresses: set[str] = set()
            for conn in list(connections.values()):
                if conn.cluster_reference in seen_addresses:
                    continue
                seen_addresses.add(conn.cluster_reference or conn.connection_id)
                prices = await self._safe(
                    self.api.async_get_market_prices(
                        org,
                        conn.connection_id,
                        include_vat=self.include_vat,
                        include_extra_costs=self.include_extra_costs,
                        price_date=today,
                    )
                )
                # Assign the matching segment array to each connection on this
                # address.
                for other in connections.values():
                    if other.cluster_reference != conn.cluster_reference:
                        continue
                    other.prices = (prices or {}).get(other.segment, []) or []
            data.connections = connections

            if primary_cluster:
                data.contract = await self._safe(
                    self.api.async_get_contract_details(org, primary_cluster)
                )
                data.advance_payment = await self._safe(
                    self.api.async_get_advance_payments(org, primary_cluster)
                )

            # Newer api.vandebron.tech endpoints (best effort; account layout
            # may differ between customers).
            accounts = await self._safe(self.api.async_get_accounts())
            data.accounts = accounts or {}
            account_number = self._first_account_number(accounts)
            if account_number:
                data.latest_costs = await self._safe(
                    self.api.async_get_latest_costs(account_number)
                )
                data.rates = await self._safe(self.api.async_get_rates(account_number))
                data.invoices = await self._safe(
                    self.api.async_get_invoices(account_number)
                )

            return data
        except VandebronAuthError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except VandebronApiError as err:
            raise UpdateFailed(f"Error talking to Vandebron: {err}") from err

    @staticmethod
    def _first_account_number(accounts: dict[str, Any] | None) -> str | None:
        for acc in (accounts or {}).get("accounts", []):
            if acc.get("accountNumber"):
                return str(acc["accountNumber"])
        return None

    async def _safe(self, coro) -> Any:
        """Await a non-critical call, logging (not raising) on failure."""
        try:
            return await coro
        except VandebronAuthError:
            raise
        except VandebronApiError as err:
            _LOGGER.debug("Optional Vandebron call failed: %s", err)
            return {}
