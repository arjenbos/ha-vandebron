"""Sensors for Vandebron Thuis."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VandebronConfigEntry
from .const import DOMAIN
from .coordinator import ConnectionData, VandebronCoordinator, VandebronData

EUR_PER_KWH = f"{CURRENCY_EURO}/kWh"
EUR_PER_M3 = f"{CURRENCY_EURO}/m³"


@dataclass(frozen=True, kw_only=True)
class VandebronSensorDescription(SensorEntityDescription):
    """Describes a Vandebron sensor backed by a value function."""

    value_fn: Callable[[VandebronData], Any]
    attrs_fn: Callable[[VandebronData], dict[str, Any]] | None = None
    exists_fn: Callable[[VandebronData], bool] = lambda data: True


def _price_attrs(conn: ConnectionData | None) -> dict[str, Any]:
    if conn is None:
        return {}
    return {
        "unit": conn.unit,
        "connection_id": conn.connection_id,
        "prices": [
            {"start": e.get("startDate"), "end": e.get("endDate"), "price": e.get("price")}
            for e in conn.prices
        ],
    }


# --- Market price sensors (the headline feature) ---------------------------
_ELEC_SENSORS: tuple[VandebronSensorDescription, ...] = (
    VandebronSensorDescription(
        key="electricity_price_now",
        translation_key="electricity_price_now",
        native_unit_of_measurement=EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        icon="mdi:flash",
        value_fn=lambda d: d.electricity().current_price() if d.electricity() else None,
        attrs_fn=lambda d: _price_attrs(d.electricity()),
    ),
    VandebronSensorDescription(
        key="electricity_price_next_hour",
        translation_key="electricity_price_next_hour",
        native_unit_of_measurement=EUR_PER_KWH,
        suggested_display_precision=4,
        icon="mdi:flash-outline",
        value_fn=lambda d: d.electricity().next_price() if d.electricity() else None,
    ),
    VandebronSensorDescription(
        key="electricity_price_avg_today",
        translation_key="electricity_price_avg_today",
        native_unit_of_measurement=EUR_PER_KWH,
        suggested_display_precision=4,
        icon="mdi:chart-line",
        value_fn=lambda d: d.electricity().today_average() if d.electricity() else None,
    ),
    VandebronSensorDescription(
        key="electricity_price_min_today",
        translation_key="electricity_price_min_today",
        native_unit_of_measurement=EUR_PER_KWH,
        suggested_display_precision=4,
        icon="mdi:arrow-down-bold",
        value_fn=lambda d: d.electricity().today_min() if d.electricity() else None,
    ),
    VandebronSensorDescription(
        key="electricity_price_max_today",
        translation_key="electricity_price_max_today",
        native_unit_of_measurement=EUR_PER_KWH,
        suggested_display_precision=4,
        icon="mdi:arrow-up-bold",
        value_fn=lambda d: d.electricity().today_max() if d.electricity() else None,
    ),
)

_GAS_SENSORS: tuple[VandebronSensorDescription, ...] = (
    VandebronSensorDescription(
        key="gas_price_now",
        translation_key="gas_price_now",
        native_unit_of_measurement=EUR_PER_M3,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        icon="mdi:fire",
        value_fn=lambda d: d.gas().current_price() if d.gas() else None,
        attrs_fn=lambda d: _price_attrs(d.gas()),
    ),
)


def _customer_name(d: VandebronData) -> Any:
    person = (d.customer or {}).get("person") or {}
    first = person.get("firstName")
    last = person.get("lastName")
    if first or last:
        return " ".join(p for p in (first, last) if p)
    return (d.customer or {}).get("organizationName")


def _advance_amount(d: VandebronData) -> Any:
    ap = d.advance_payment or {}
    for key in ("amount", "currentAmount", "monthlyAmount"):
        value = ap.get(key)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, dict) and isinstance(value.get("value"), (int, float, str)):
            try:
                return float(value["value"])
            except (TypeError, ValueError):
                pass
    return None


def _month_costs(d: VandebronData) -> Any:
    costs = (d.latest_costs or {}).get("costs") or {}
    actual = costs.get("actual") or (d.latest_costs or {}).get("actual")
    if isinstance(actual, dict):
        try:
            return float(actual.get("value"))
        except (TypeError, ValueError):
            return None
    return None


def _last_invoice(d: VandebronData) -> Any:
    invoices = d.invoices
    items = invoices.get("data") if isinstance(invoices, dict) else invoices
    if isinstance(items, list) and items:
        amount = items[0].get("totalAmount") or items[0].get("amount")
        if isinstance(amount, dict):
            try:
                return float(amount.get("value"))
            except (TypeError, ValueError):
                return None
        return amount
    return None


_INFO_SENSORS: tuple[VandebronSensorDescription, ...] = (
    VandebronSensorDescription(
        key="customer_name",
        translation_key="customer_name",
        icon="mdi:account",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_customer_name,
    ),
    VandebronSensorDescription(
        key="monthly_advance_amount",
        translation_key="monthly_advance_amount",
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        icon="mdi:cash-multiple",
        value_fn=_advance_amount,
    ),
    VandebronSensorDescription(
        key="current_month_costs",
        translation_key="current_month_costs",
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        icon="mdi:currency-eur",
        value_fn=_month_costs,
    ),
    VandebronSensorDescription(
        key="last_invoice_amount",
        translation_key="last_invoice_amount",
        native_unit_of_measurement=CURRENCY_EURO,
        suggested_display_precision=2,
        icon="mdi:file-document-outline",
        value_fn=_last_invoice,
        attrs_fn=lambda d: {
            "invoices": d.invoices.get("data")
            if isinstance(d.invoices, dict)
            else d.invoices
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VandebronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vandebron sensors."""
    coordinator = entry.runtime_data
    data = coordinator.data

    descriptions: list[VandebronSensorDescription] = list(_INFO_SENSORS)
    if data.electricity() is not None:
        descriptions.extend(_ELEC_SENSORS)
    if data.gas() is not None:
        descriptions.extend(_GAS_SENSORS)

    async_add_entities(
        VandebronSensor(coordinator, description) for description in descriptions
    )


class VandebronSensor(CoordinatorEntity[VandebronCoordinator], SensorEntity):
    """A single Vandebron sensor."""

    _attr_has_entity_name = True
    entity_description: VandebronSensorDescription

    def __init__(
        self,
        coordinator: VandebronCoordinator,
        description: VandebronSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        org = coordinator.data.organization_id
        self._attr_unique_id = f"{org}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, org)},
            name="Vandebron Thuis",
            manufacturer="Vandebron",
            model="Energiecontract",
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
