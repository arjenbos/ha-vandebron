"""Per-connection and per-account sensors, including discovered tariff components."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VandebronConfigEntry
from .const import DOMAIN
from .coordinator import VandebronCoordinator
from .models import (
    AMSTERDAM,
    COMPONENT_LABELS,
    UTC,
    Source,
    contract_price_key,
    day_bounds,
    delivery_price,
    invoice_amount,
    invoice_attributes,
    items,
    latest_invoice,
    legacy_usage,
    number,
    price_stats,
    timestamp,
)


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    value: Callable[[Source, datetime], Any]
    block: str | Callable[[datetime], str] | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    attributes: Callable[[Source, datetime], dict[str, Any]] | None = None
    diagnostic: bool = False
    price: bool = False
    market: bool = False
    translation_key: str | None = None
    translation_placeholders: dict[str, str] | None = None


def period_key(period: str, now: datetime) -> str:
    day = now.astimezone(AMSTERDAM).date()
    if period == "recent":
        day -= timedelta(days=1)
    else:
        day = day.replace(day=1) if period == "month" else day.replace(month=1, day=1)
    return f"usage_{period}_{day}"


def _graph_value(s: Source, now: datetime, period: str, fuel: str, field: str) -> float | None:
    data = s.blocks.get(period_key(period, now), {})
    if not isinstance(data, dict):
        return None
    if s.kind == "connection":
        return legacy_usage(data).get(field)
    block = data.get(fuel, {})
    if not isinstance(block, dict) or block.get("kind") == "error":
        return None
    total = block.get("total")
    if fuel == "electricity":
        return number(total.get(field)) if isinstance(total, dict) else None
    return number(total) if field == "usage" else None


def _usage_attrs(s: Source, now: datetime, period: str, fuel: str) -> dict[str, Any]:
    data = s.blocks.get(period_key(period, now), {})
    if not isinstance(data, dict):
        return {}
    if s.kind == "connection":
        normalized = legacy_usage(data)
        intervals = normalized["intervals"]
        return {
            "period_start": period_key(period, now).rsplit("_", 1)[-1],
            "last_reading": data.get("latestReadingDate"),
            "intervals": intervals[-32:],
            "source_unit": data.get("unit"),
        }
    block = data.get(fuel, {})
    if not isinstance(block, dict):
        return {}
    return {
        "period_start": data.get("startDate"),
        "period_end": data.get("endDate"),
        "resolution": data.get("resolution"),
        "intervals": [
            {
                "timestamp": r.get("timestamp"),
                "status": r.get("status"),
                **{k: number(r.get(k)) for k in ("usage", "peak", "offPeak", "redelivery")},
            }
            for r in items(block, "intervals")[-32:]
        ],
    }


def _daily_value(s: Source, now: datetime, fuel: str, field: str, offset: int) -> float | None:
    day = now.astimezone(AMSTERDAM).date() + timedelta(days=offset)
    attrs = _usage_attrs(s, now, "recent", fuel)
    values = []
    for row in attrs.get("intervals", []):
        when = timestamp(row.get("timestamp"))
        if (
            when
            and when.astimezone(AMSTERDAM).date() == day
            and row.get("status") in (None, "AVAILABLE")
        ):
            value = number(row.get(field))
            if value is not None:
                values.append(value)
    return sum(values) if values else None


def _legacy_cost(s: Source, now: datetime, period: str, field: str) -> float | None:
    key = period_key(period, now).replace("usage_", "cost_", 1)
    data = s.blocks.get(key)
    rows = items(data, "costs")
    if isinstance(data, dict) and "connectionId" in data:
        rows = [data]
    connection = next((r for r in rows if str(r.get("connectionId")) == s.connection), {})
    return number(connection.get(field))


def _period_cost(s: Source, now: datetime, period: str, field: str) -> float | None:
    data = s.blocks.get(period_key(period, now), {})
    group = data.get("costs", {})
    return (
        number(group.get(field))
        if isinstance(group, dict) and group.get("kind") != "error"
        else None
    )


def _connection_price(s: Source, now: datetime) -> float | None:
    if s.is_dynamic:
        return p.price if (p := s.current_price(now)) else None
    if s.segment == "Gas" or s.metadata.get("billAsSingle") is True:
        return delivery_price(s, now, "Base")
    # A dual tariff has no single current rate without an authoritative
    # meter tariff indicator. Both contract tariffs are exposed separately.
    return None


def _contract_price_attrs(s: Source, now: datetime) -> dict[str, Any]:
    return {
        "contract_type": s.contract_type,
        "valid_on": now.astimezone(AMSTERDAM).date().isoformat(),
        "price_type": "contract",
        "last_success": s.updated[contract_price_key(now)].isoformat()
        if contract_price_key(now) in s.updated
        else None,
        "includes_vat": True,
        "includes_energy_tax": True,
        "energy_tax_range_begin": 0,
        "includes_fixed_costs": False,
        "billing_tariff": "single" if s.metadata.get("billAsSingle") else "peak_off_peak",
    }


def _price_attrs(s: Source, now: datetime) -> dict[str, Any]:
    today = now.astimezone(AMSTERDAM).date()
    result = {}
    for name, offset in (("yesterday", -1), ("today", 0), ("tomorrow", 1)):
        start, end = day_bounds(today + timedelta(days=offset))
        result[name] = [p.as_dict() for p in s.all_prices() if p.start < end and p.end > start]
    current = s.current_price(now)
    return {
        "prices": result["today"],
        **result,
        "period_start": current.start.isoformat() if current else None,
        "period_end": current.end.isoformat() if current else None,
    }


def _invoice_metrics() -> list[Metric]:
    return [
        Metric(
            "last_invoice_amount",
            "Last invoice amount",
            lambda s, n: invoice_amount(latest_invoice(s.blocks.get("invoices"))),
            "invoices",
            "EUR",
            attributes=lambda s, n: invoice_attributes(latest_invoice(s.blocks.get("invoices"))),
        ),
        Metric(
            "last_invoice_status",
            "Last invoice status",
            lambda s, n: _invoice_status(s.blocks.get("invoices")),
            "invoices",
            diagnostic=True,
        ),
        Metric(
            "outstanding_amount",
            "Outstanding invoice amount",
            lambda s, n: _outstanding(s.blocks.get("invoices")),
            "invoices",
            "EUR",
        ),
    ]


def _invoice_status(data: Any) -> str | None:
    row = latest_invoice(data)
    return row.get("status") or row.get("paymentStatus")


def _outstanding(data: Any) -> float | None:
    amounts = []
    for row in items(data):
        remaining = row.get("remainingAmount")
        if remaining is None and isinstance(row.get("paymentStatusDetails"), dict):
            remaining = row["paymentStatusDetails"].get("amountDue")
        value = number(remaining)
        if value is not None:
            amounts.append(value)
    return sum(amounts) if amounts else None


def metrics(source: Source) -> list[Metric]:
    result = [
        Metric(
            "data_status",
            "Data status",
            lambda s, n: "partial" if s.errors else "ok",
            diagnostic=True,
            attributes=lambda s, n: {
                "errors": s.errors,
                "last_success": {k: v.isoformat() for k, v in s.updated.items()},
            },
        )
    ]
    if source.kind in ("organization", "account"):
        result.extend(_invoice_metrics())
    if source.kind == "organization":
        result.append(
            Metric(
                "contract",
                "Contract",
                lambda s, n: (
                    (_legacy_contract(s, n).get("contractType") or {}).get("pricingType")
                    if isinstance(_legacy_contract(s, n).get("contractType"), dict)
                    else _legacy_contract(s, n).get("pricingType")
                ),
                "contracts",
                diagnostic=True,
                attributes=lambda s, n: {
                    k: v
                    for k, v in _legacy_contract(s, n).items()
                    if k in ("startDate", "endDate", "reference")
                },
            )
        )
    if source.kind == "organization":
        result.append(
            Metric(
                "customer_name",
                "Customer",
                lambda s, n: _customer_name(s.blocks.get("customer", {})),
                "customer",
                diagnostic=True,
            )
        )
    if source.kind == "address":
        result.append(
            Metric(
                "monthly_advance_amount",
                "Monthly advance amount",
                lambda s, n: number(
                    (s.blocks.get("advance", {}).get("currentAdvancePayment") or {}).get("amount")
                ),
                "advance",
                "EUR",
                attributes=lambda s, n: {
                    "start_date": (
                        s.blocks.get("advance", {}).get("currentAdvancePayment") or {}
                    ).get("startDate")
                },
            )
        )
    if source.kind == "connection":
        gas = source.segment == "Gas"
        prefix = "gas" if gas else "electricity"
        unit = "€/m³" if gas else "€/kWh"
        result.extend(
            [
                Metric(
                    f"{prefix}_price_now",
                    "Gas rate" if gas else "Electricity rate",
                    _connection_price,
                    unit=unit,
                    attributes=lambda s, n: (
                        _price_attrs(s, n) if s.is_dynamic else _contract_price_attrs(s, n)
                    ),
                    price=True,
                ),
                Metric(
                    f"{prefix}_price_next_hour",
                    "Next market price",
                    lambda s, n: p.price if (p := s.next_price(n)) else None,
                    unit=unit,
                    price=True,
                    market=True,
                    attributes=lambda s, n: {
                        "start": p.start.isoformat() if (p := s.next_price(n)) else None,
                        "end": p.end.isoformat() if p else None,
                    },
                ),
            ]
        )
        if not source.is_dynamic:
            for kind, label in (
                ("Base", "Electricity single rate"),
                ("Peak", "Electricity peak rate"),
                ("OffPeak", "Electricity off-peak rate"),
            ):
                if gas:
                    continue
                result.append(
                    Metric(
                        f"contract_price_{kind.lower()}",
                        label,
                        lambda s, n, k=kind: delivery_price(s, n, k),
                        contract_price_key,
                        unit=unit,
                        attributes=_contract_price_attrs,
                    )
                )
            result.append(
                Metric(
                    "contract_rates",
                    "Tariff component count",
                    lambda s, n: len(s.rates()),
                    contract_price_key,
                    diagnostic=True,
                )
            )
            for rate in source.rates().values():
                result.append(
                    Metric(
                        f"contract_component_{rate.key}",
                        rate.name,
                        lambda s, n, k=rate.key: r.value if (r := s.rates().get(k)) else None,
                        contract_price_key,
                        rate.unit,
                        translation_key="component_" + rate.attributes["priceComponentType"]
                        if rate.attributes["priceComponentType"] in COMPONENT_LABELS
                        else "api_rate",
                        translation_placeholders={
                            "label": rate.name,
                            "band": (
                                f" ({rate.attributes['rangeBegin']}–{rate.attributes.get('rangeEnd', '∞')})"
                                if "rangeBegin" in rate.attributes
                                else ""
                            ),
                        },
                        attributes=lambda s, n, k=rate.key: (
                            r.attributes if (r := s.rates().get(k)) else {}
                        ),
                    )
                )
        for index, (suffix, label) in enumerate(
            (("avg", "average"), ("min", "minimum"), ("max", "maximum"))
        ):
            result.append(
                Metric(
                    f"{prefix}_price_{suffix}_today",
                    f"Market price {label} today",
                    lambda s, n, i=index: price_stats(
                        s.all_prices(), n.astimezone(AMSTERDAM).date()
                    )[i],
                    unit=unit,
                    price=True,
                    market=True,
                )
            )
        result.append(
            Metric(
                f"{prefix}_market_price_now",
                "Market price now",
                lambda s, n: p.price if (p := s.current_price(n)) else None,
                unit=unit,
                attributes=_price_attrs,
                price=True,
                market=True,
            )
        )
        result.extend(
            [
                Metric(
                    "meter_status",
                    "Meter status",
                    lambda s, n: _meter_state(s.blocks.get("meter")),
                    "meter",
                    diagnostic=True,
                    attributes=lambda s, n: {
                        k: v
                        for k, v in s.blocks.get("meter", {}).items()
                        if k
                        in (
                            "administrativeStatusSmartMeter",
                            "communicationStatus",
                            "status",
                        )
                    },
                ),
                Metric(
                    "connection",
                    "Connection",
                    lambda s, n: s.connection,
                    diagnostic=True,
                    attributes=lambda s, n: {
                        k: v
                        for k, v in s.metadata.items()
                        if k
                        in (
                            "marketSegment",
                            "ean",
                            "eanCode",
                            "EAN",
                            "hasSmartMeter",
                            "billAsSingle",
                        )
                    },
                ),
                Metric(
                    "contract_type",
                    "Contract type",
                    lambda s, n: (s.metadata.get("contract") or {}).get("contractType"),
                    diagnostic=True,
                    attributes=lambda s, n: {
                        k: v
                        for k, v in (s.metadata.get("contract") or {}).items()
                        if k in ("startDate", "endDate", "description", "reference")
                    },
                ),
            ]
        )
    if source.kind == "connection":
        for period, label in (("month", "this month"), ("year", "this year")):
            for field, name in (
                ("totalFixed", "Fixed costs"),
                ("totalVariable", "Variable costs"),
                ("totalEarnings", "Feed-in revenue"),
            ):
                result.append(
                    Metric(
                        f"legacy_cost_{field}_{period}",
                        f"{name} {label}",
                        lambda s, n, p=period, f=field: _legacy_cost(s, n, p, f),
                        lambda n, p=period: period_key(p, n).replace("usage_", "cost_", 1),
                        "EUR",
                    )
                )
    if source.kind in ("connection", "account"):
        fuels = (
            ("electricity", "gas")
            if source.kind == "account"
            else (("gas",) if source.segment == "Gas" else ("electricity",))
        )
        for period, label in (("month", "this month"), ("year", "this year")):

            def block(n, period=period):
                return period_key(period, n)

            for fuel in fuels:
                for field, text in (
                    ("usage", "Gas consumption" if fuel == "gas" else "Electricity consumption"),
                    ("redelivery", "Electricity returned to grid"),
                    ("peak", "Electricity peak consumption"),
                    ("offPeak", "Electricity off-peak consumption"),
                ):
                    if fuel == "gas" and field != "usage":
                        continue
                    result.append(
                        Metric(
                            f"{fuel}_{field}_{period}",
                            f"{text} {label}",
                            lambda s, n, p=period, f=fuel, k=field: _graph_value(s, n, p, f, k),
                            block,
                            "m³" if fuel == "gas" else "kWh",
                            SensorDeviceClass.GAS if fuel == "gas" else SensorDeviceClass.ENERGY,
                            lambda s, n, p=period, f=fuel: _usage_attrs(s, n, p, f),
                        )
                    )
    if source.kind in ("connection", "account"):
        for fuel in fuels:
            for field, text in (
                ("usage", "Gas consumption" if fuel == "gas" else "Electricity consumption"),
                ("redelivery", "Electricity returned to grid"),
            ):
                if fuel == "gas" and field != "usage":
                    continue
                for offset, label in ((0, "today"), (-1, "yesterday")):
                    result.append(
                        Metric(
                            f"{fuel}_{field}_day_{offset}",
                            f"{text} {label}",
                            lambda s, n, f=fuel, k=field, o=offset: _daily_value(s, n, f, k, o),
                            lambda n: period_key("recent", n),
                            "m³" if fuel == "gas" else "kWh",
                        )
                    )
    if source.kind == "account":
        for period, label in (("month", "this month"), ("year", "this year")):
            for field, text in (
                ("totalIncTax", "Total costs including tax"),
                ("totalExTax", "Total costs excluding tax"),
                ("totalFixed", "Fixed costs"),
                ("totalGasIncTax", "Gas costs"),
                ("totalElecIncTax", "Electricity costs"),
                ("totalRedeliveryIncTax", "Feed-in amount"),
            ):
                result.append(
                    Metric(
                        f"cost_{field}_{period}",
                        f"{text} {label}",
                        lambda s, n, p=period, k=field: _period_cost(s, n, p, k),
                        lambda n, p=period: period_key(p, n),
                        "EUR",
                    )
                )
    if source.kind == "account":
        for fuel, text in (
            ("electricity", "Electricity"),
            ("gas", "Gas"),
            ("costs", "Costs"),
        ):
            for field, label in (
                ("actual", "this month"),
                ("expectedMonth", "expected this month"),
                ("expectedUntilNow", "expected until now"),
            ):
                result.append(
                    Metric(
                        f"{fuel}_{field}",
                        f"{text} {label}",
                        lambda s, n, f=fuel, k=field: number(
                            (s.blocks.get("costs", {}).get(f) or {}).get(k)
                        ),
                        "costs",
                        "EUR" if fuel == "costs" else ("kWh" if fuel == "electricity" else "m³"),
                        attributes=lambda s, n: {"month": s.blocks.get("costs", {}).get("month")},
                    )
                )
        for fuel, text in (
            ("electricity", "Electricity"),
            ("gas", "Gas"),
            ("costs", "Costs"),
        ):
            result.append(
                Metric(
                    f"last_reading_{fuel}",
                    f"{text} last reading date",
                    lambda s, n, f=fuel: timestamp(
                        (s.blocks.get("reading_dates", {}).get(f) or {}).get("lastReadingDate")
                    ),
                    "reading_dates",
                    device_class=SensorDeviceClass.TIMESTAMP,
                    diagnostic=True,
                )
            )
        result.append(
            Metric(
                "rates",
                "Contract rates",
                lambda s, n: len(s.rates()),
                "rates",
                diagnostic=True,
                attributes=lambda s, n: {
                    "components": [
                        {
                            "name": r.name,
                            "value": r.value,
                            "unit": r.unit,
                            **r.attributes,
                        }
                        for r in s.rates().values()
                    ]
                },
            )
        )
        for rate in source.rates().values():
            result.append(
                Metric(
                    f"rate_{rate.key}",
                    rate.name,
                    lambda s, n, k=rate.key: r.value if (r := s.rates().get(k)) else None,
                    "rates",
                    rate.unit,
                    translation_key="api_rate",
                    translation_placeholders={"label": rate.name},
                    attributes=lambda s, n, k=rate.key: (
                        r.attributes if (r := s.rates().get(k)) else {}
                    ),
                )
            )
        for agreement in items(source.blocks.get("agreements"), "agreements"):
            market = agreement.get("market")
            if market not in ("ELECTRICITY", "GAS"):
                continue
            # One current agreement per market, selected at read time by validity.
            key = f"contract_{market.lower()}"
            if any(m.key == key for m in result):
                continue
            result.append(
                Metric(
                    key,
                    "Electricity contract" if market == "ELECTRICITY" else "Gas contract",
                    lambda s, n, m=market: _agreement(s, n, m).get("productType"),
                    "agreements",
                    diagnostic=True,
                    attributes=lambda s, n, m=market: {
                        k: v
                        for k, v in _agreement(s, n, m).items()
                        if k in ("startDate", "endDate", "duration", "market")
                    },
                )
            )
    return result


def _agreement(s: Source, now: datetime, market: str) -> dict[str, Any]:
    return next(
        (
            a
            for a in items(s.blocks.get("agreements"), "agreements")
            if a.get("market") == market
            and (timestamp(a.get("startDate")) or datetime.min.replace(tzinfo=UTC)) <= now
            and (timestamp(a.get("endDate")) or datetime.max.replace(tzinfo=UTC)) > now
        ),
        {},
    )


def _legacy_contract(s: Source, now: datetime) -> dict[str, Any]:
    candidates = [
        a
        for a in items(s.blocks.get("contracts"))
        if (timestamp(a.get("startDate")) or datetime.min.replace(tzinfo=UTC)) <= now
        and (timestamp(a.get("endDate")) or datetime.max.replace(tzinfo=UTC)) > now
    ]
    return max(
        candidates,
        key=lambda a: timestamp(a.get("startDate")) or datetime.min.replace(tzinfo=UTC),
        default={},
    )


def _customer_name(data: dict[str, Any]) -> str | None:
    person = data.get("person") or {}
    return " ".join(str(person[k]) for k in ("firstName", "lastName") if person.get(k)) or data.get(
        "organizationName"
    )


def _meter_state(value: Any) -> str | None:
    if not isinstance(value, dict):
        return str(value) if value is not None else None
    if isinstance(value.get("status"), str):
        return value["status"]
    a, c = value.get("administrativeStatusSmartMeter"), value.get("communicationStatus")
    if a is False or c is False:
        return "not_operational"
    return "operational" if a is True and c is True else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VandebronConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: dict[tuple[str, str], VandebronSensor] = {}

    @callback
    def discover() -> None:
        entities = []
        for source in coordinator.data.values():
            for metric in metrics(source):
                key = (source.key, metric.key)
                if key not in known:
                    entity = known[key] = VandebronSensor(coordinator, source.key, metric)
                    entities.append(entity)
                else:
                    entity = known[key]
                    entity.metric = metric
                    entity._attr_native_unit_of_measurement = metric.unit
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class VandebronSensor(CoordinatorEntity[VandebronCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: VandebronCoordinator, source_key: str, metric: Metric) -> None:
        super().__init__(coordinator)
        self.source_key = source_key
        self.metric = metric
        source = coordinator.data[source_key]
        self._attr_unique_id = f"{source_key}_{metric.key}"
        self._attr_translation_key = (metric.translation_key or metric.key).lower()
        self._attr_translation_placeholders = metric.translation_placeholders or {}
        if metric.market:
            self._attr_entity_registry_enabled_default = source.is_dynamic
        elif metric.key.endswith("_price_now") and not source.is_dynamic:
            self._attr_entity_registry_enabled_default = (
                source.segment == "Gas" or source.metadata.get("billAsSingle", False)
            )
        self._attr_native_unit_of_measurement = metric.unit
        self._attr_device_class = metric.device_class
        self._attr_suggested_display_precision = (
            5
            if metric.price
            or metric.key.startswith(("rate_", "contract_price_", "contract_component_"))
            else (2 if metric.unit else None)
        )
        if (metric.price and metric.key.endswith("_now")) or metric.key.startswith(
            "contract_price_"
        ):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if metric.diagnostic:
            from homeassistant.const import EntityCategory

            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, source_key)},
            translation_key=(
                "connection_gas"
                if source.segment == "Gas"
                else "connection_electricity"
                if source.segment == "Electricity"
                else source.kind
            ),
            translation_placeholders={
                "source": source.name if source.name == "Vandebron" else f"Vandebron {source.name}"
            },
            manufacturer="Vandebron",
            model={
                "account": "Energy account",
                "connection": "Energy connection",
                "address": "Supply address",
                "organization": "Customer",
            }[source.kind],
        )
        self._cancel_timer: Callable[[], None] | None = None

    @property
    def source(self) -> Source:
        return self.coordinator.data[self.source_key]

    @property
    def available(self) -> bool:
        if not super().available or self.source_key not in self.coordinator.data:
            return False
        if "discovery" in self.source.errors:
            return False
        now = datetime.now(UTC)
        if self.source.is_dynamic and self.metric.key.startswith("contract_price_"):
            return False
        if self.metric.price:
            if not self.metric.market and not self.source.is_dynamic:
                key = contract_price_key(now)
                return (
                    key in self.source.blocks
                    and key not in self.source.errors
                    and self.native_value is not None
                )
            # Cached periods remain valid until their end, despite a fetch outage.
            if self.metric.key.endswith("_now"):
                return self.source.current_price(now) is not None
            if self.metric.key.endswith("_next_hour"):
                return self.source.next_price(now) is not None
            return bool(_price_attrs(self.source, now)["today"])
        key = self.metric.block(now) if callable(self.metric.block) else self.metric.block
        return key is None or (key in self.source.blocks and key not in self.source.errors)

    @property
    def native_value(self) -> Any:
        return self.metric.value(self.source, datetime.now(UTC))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        attrs = self.metric.attributes(self.source, now) if self.metric.attributes else {}
        key = self.metric.block(now) if callable(self.metric.block) else self.metric.block
        if key and key in self.source.updated:
            attrs["last_success"] = self.source.updated[key].isoformat()
        if self.metric.price and (self.source.is_dynamic or self.metric.market):
            attrs.update(
                {
                    "price_type": "market"
                    if self.metric.market
                    else "contract_dynamic"
                    if self.source.is_dynamic
                    else "contract_unavailable",
                    "contract_type": self.source.contract_type,
                    "include_vat": self.coordinator.include_vat,
                    "include_extra_costs": self.coordinator.include_extra_costs,
                    "last_success": {
                        k[7:]: v.isoformat()
                        for k, v in self.source.updated.items()
                        if k.startswith("prices_")
                    },
                    "fetch_errors": {
                        k[7:]: v for k, v in self.source.errors.items() if k.startswith("prices_")
                    },
                }
            )
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule()
        super()._handle_coordinator_update()

    @callback
    def _schedule(self) -> None:
        if self._cancel_timer:
            self._cancel_timer()
        now = datetime.now(UTC)
        _, midnight = day_bounds(now.astimezone(AMSTERDAM).date())
        boundaries = [midnight]
        if self.metric.price:
            boundaries.extend(
                t for p in self.source.all_prices() for t in (p.start, p.end) if t > now
            )
        self._cancel_timer = async_track_point_in_utc_time(self.hass, self._tick, min(boundaries))

    @callback
    def _tick(self, now: datetime) -> None:
        self._cancel_timer = None
        self.async_write_ha_state()
        self._schedule()

    async def async_will_remove_from_hass(self) -> None:
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        await super().async_will_remove_from_hass()
