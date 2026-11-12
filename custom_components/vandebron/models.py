"""Validated, Home Assistant independent representations of supplier data."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def number(value: Any) -> float | None:
    """Keep zero and negative values; reject booleans and non-finite numbers."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except TypeError, ValueError, OverflowError:
        return None


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        # Legacy dates have no offset. They describe Dutch delivery periods.
        if result.tzinfo is None:
            result = result.replace(tzinfo=AMSTERDAM)
        return result.astimezone(UTC)
    except ValueError:
        return None


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC boundaries of a Dutch day, including 23/25 hour days."""
    return (
        datetime.combine(day, time.min, AMSTERDAM).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, AMSTERDAM).astimezone(UTC),
    )


@dataclass(frozen=True)
class Price:
    start: datetime
    end: datetime
    price: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "price": self.price,
        }


def parse_prices(rows: list[dict[str, Any]]) -> list[Price]:
    result = {}
    for row in rows:
        start, end = timestamp(row.get("startDate")), timestamp(row.get("endDate"))
        value = number(row.get("price"))
        if start is not None and end is not None and end > start and value is not None:
            result[start, end] = Price(start, end, value)
    return sorted(result.values(), key=lambda p: p.start)


def price_stats(prices: list[Price], day: date) -> tuple[float | None, float | None, float | None]:
    start, end = day_bounds(day)
    weighted = []
    for p in prices:
        seconds = (min(end, p.end) - max(start, p.start)).total_seconds()
        if seconds > 0:
            weighted.append((p.price, seconds))
    if not weighted:
        return None, None, None
    return (
        sum(v * s for v, s in weighted) / sum(s for _, s in weighted),
        min(v for v, _ in weighted),
        max(v for v, _ in weighted),
    )


def items(value: Any, key: str = "data") -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get(key)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def latest_invoice(value: Any) -> dict[str, Any]:
    rows = items(value)
    # New invoices expose dueDate; legacy invoices expose invoiceDate.
    return max(
        rows,
        key=lambda v: (
            timestamp(v.get("invoiceDate") or v.get("dueDate")) or datetime.min.replace(tzinfo=UTC)
        ),
        default={},
    )


def invoice_amount(row: dict[str, Any]) -> float | None:
    return number(
        row.get("totalAmount") if row.get("totalAmount") is not None else row.get("amount")
    )


def invoice_attributes(row: dict[str, Any]) -> dict[str, Any]:
    """A bounded whitelist: no PDF URLs, bank details or customer addresses."""
    return {
        k: row[k]
        for k in (
            "invoiceDate",
            "dueDate",
            "invoiceTypeLocalized",
            "status",
            "paymentStatus",
            "description",
        )
        if k in row and isinstance(row[k], (str, int, float, bool))
    }


@dataclass(frozen=True)
class Rate:
    key: str
    name: str
    value: float
    unit: str | None
    attributes: dict[str, Any]


def parse_rates(payload: Any) -> dict[str, Rate]:
    """Expose individual components without calculating an invented total.

    Rate responses may group components by fuel, tariff or effective period.
    Preserve the API label/path and unit; never infer VAT or units from a name.
    Numeric IDs, dates and metadata are not prices. Stable component identity
    uses IDs/types/labels and period, never a list's current ordering.
    """
    result: dict[str, Rate] = {}
    monetary = {
        "price",
        "rate",
        "amount",
        "value",
        "priceInclVat",
        "priceExclVat",
        "priceIncludingVat",
        "priceExcludingVat",
        "tariff",
    }
    metadata = {
        "id",
        "name",
        "label",
        "description",
        "type",
        "code",
        "unit",
        "priceUnit",
        "startDate",
        "endDate",
        "validFrom",
        "validUntil",
        "includeVat",
        "includesVat",
        "includeExtraCosts",
        "marketSegment",
        "rateType",
        "currency",
        "costType",
        "costCategory",
    }

    def walk(node: Any, path: tuple[str, ...], inherited: dict[str, Any]) -> None:
        if isinstance(node, list):
            for index, child in enumerate(node):
                if not isinstance(child, dict):
                    continue
                identity = next(
                    (
                        str(child[k])
                        for k in (
                            "id",
                            "code",
                            "type",
                            "rateType",
                            "costType",
                            "costCategory",
                            "name",
                            "label",
                        )
                        if isinstance(child.get(k), (str, int))
                    ),
                    None,
                )
                period = str(child.get("startDate") or child.get("validFrom") or "")
                if identity is None:
                    # No stable identity in the API: retain every position rather
                    # than silently overwriting indistinguishable components.
                    identity = f"item_{index}"
                walk(child, (*path, identity, period), inherited)
        elif isinstance(node, dict):
            attrs = {
                **inherited,
                **{
                    k: v
                    for k, v in node.items()
                    if k in metadata and isinstance(v, (str, bool, int, float))
                },
            }
            if node.get("unit") in ("KWh", "kWh", "M3", "m3", "m³", "Day"):
                attrs["rate_unit"] = node["unit"]
            for k, v in node.items():
                if k in metadata:
                    continue
                if isinstance(v, (dict, list)):
                    walk(v, (*path, k), attrs)
                elif (
                    k in monetary or str(attrs.get("unit", "")).startswith(("EUR/", "eur/", "€/"))
                ) and (amount := number(v)) is not None:
                    full = tuple(p for p in (*path, k) if p)
                    key = hashlib.sha256("/".join(full).encode()).hexdigest()[:20]
                    unit = attrs.get("unit") or attrs.get("priceUnit")
                    denominator = attrs.get("rate_unit")
                    if denominator:
                        unit = {
                            "KWh": "€/kWh",
                            "kWh": "€/kWh",
                            "M3": "€/m³",
                            "m3": "€/m³",
                            "m³": "€/m³",
                            "Day": "€/day",
                        }[denominator]
                    normalized = {
                        "EUR/kWh": "€/kWh",
                        "eur/kwh": "€/kWh",
                        "EUR/m3": "€/m³",
                        "eur/m3": "€/m³",
                        "EUR": "EUR",
                        "eur": "EUR",
                    }.get(unit, unit)
                    result[key] = Rate(
                        key,
                        " / ".join(full),
                        amount,
                        normalized,
                        {"api_path": "/".join(full), **attrs},
                    )

    walk(payload, (), {})
    return result


def legacy_usage(payload: Any) -> dict[str, Any]:
    """Normalize legacy daily measurements; do not add peak + single twice."""
    rows = items(payload, "values")
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = items(payload)
    unit = payload.get("unit") if isinstance(payload, dict) else None
    factor = {
        "WH": 0.001,
        "Wh": 0.001,
        "kWh": 1,
        "KWH": 1,
        "kwh": 1,
        "mqt": 1,
        "MQT": 1,
        "M3": 1,
        "m3": 1,
        "m³": 1,
    }.get(unit)
    if factor is None:
        return {"usage": None, "redelivery": None, "intervals": []}
    total = 0.0
    production = 0.0
    found = produced = False
    intervals = []
    peaks: dict[str, list[float]] = {"peak": [], "offPeak": []}
    for row in rows:
        if row.get("tag") not in (None, "Measured"):
            continue
        when = timestamp(row.get("readingDate") or row.get("time"))
        # Consumption is provided explicitly by the app's legacy usage model.
        use = number(row.get("consumption"))
        prod = number(row.get("production"))
        if when is None:
            continue
        if use is not None:
            use *= factor
            total += use
            found = True
        if prod is not None:
            prod *= factor
            production += prod
            produced = True
        extra = {}
        for label, key in (("peak", "consumptionPeak"), ("offPeak", "consumptionOffPeak")):
            value = number(row.get(key))
            extra[label] = value * factor if value is not None else None
            if value is not None:
                peaks[label].append(value * factor)
        intervals.append({"timestamp": when.isoformat(), "usage": use, "redelivery": prod, **extra})
    return {
        "usage": total if found else None,
        "redelivery": production if produced else None,
        "intervals": sorted(intervals, key=lambda r: r["timestamp"]),
        **{k: sum(v) if v else None for k, v in peaks.items()},
    }


@dataclass
class Source:
    key: str
    name: str
    kind: str
    organization: str | None = None
    connection: str | None = None
    segment: str | None = None
    cluster: str | None = None
    account: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    blocks: dict[str, Any] = field(default_factory=dict)
    updated: dict[str, datetime] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    prices: dict[str, list[Price]] = field(default_factory=dict)

    @property
    def contract_type(self) -> str | None:
        contract = self.metadata.get("contract") or {}
        kind = contract.get("contractType")
        return kind.get("pricingType") if isinstance(kind, dict) else kind

    @property
    def is_dynamic(self) -> bool:
        # Never assume dynamic when the contract type is absent or unknown.
        return self.contract_type == "MarketPriceVariable"

    def all_prices(self) -> list[Price]:
        return sorted(
            {(p.start, p.end): p for group in self.prices.values() for p in group}.values(),
            key=lambda p: p.start,
        )

    def current_price(self, now: datetime) -> Price | None:
        return next((p for p in self.all_prices() if p.start <= now < p.end), None)

    def next_price(self, now: datetime) -> Price | None:
        return next((p for p in self.all_prices() if p.start > now), None)

    def rates(self) -> dict[str, Rate]:
        now = datetime.now(UTC)
        if self.kind == "connection":
            today = now.astimezone(AMSTERDAM).date()
            current = contract_components(self.blocks.get(contract_price_key(now)), today)
            for block, payload in self.blocks.items():
                if not block.startswith("contract_prices_"):
                    continue
                day = date.fromisoformat(block[len("contract_prices_") :])
                if day <= today or block in self.errors:
                    continue
                for key, rate in contract_components(payload, day).items():
                    if key in current:
                        current[key].attributes.setdefault("upcoming", []).append(
                            {"valid_on": day.isoformat(), "price": rate.value, "unit": rate.unit}
                        )
            return current
        return parse_rates(self.blocks.get("rates", {}))


def contract_price_key(now: datetime) -> str:
    return f"contract_prices_{now.astimezone(AMSTERDAM).date()}"


COMPONENT_LABELS = {
    "Base": "Single-rate supply component",
    "Peak": "Peak supply component",
    "OffPeak": "Off-peak supply component",
    "BaseODN": "Single-rate feed-in compensation",
    "PeakODN": "Peak feed-in compensation",
    "OffPeakODN": "Off-peak feed-in compensation",
    "BaseODNExcess": "Single-rate excess feed-in compensation",
    "PeakODNExcess": "Peak excess feed-in compensation",
    "OffPeakODNExcess": "Off-peak excess feed-in compensation",
    "FixedFee": "Fixed supply costs",
    "FixedProductionFee": "Fixed feed-in costs",
    "EnergyTax": "Energy tax",
    "RenewableEnergyMarkup": "Renewable energy surcharge",
    "TaxCredit": "Energy tax credit",
    "NetworkCosts": "Network costs",
    "Discount": "Discount",
    "ActionDiscount": "Promotional discount",
    "PowerTypeSurcharge": "Energy source surcharge",
    "SmartMeterServiceFee": "Smart meter service fee",
    "RegionMarkup": "Gas regional surcharge",
    "Premium": "Gas local investment premium",
    "SalesFee": "Sales fee",
    "CollectionFees": "Collection fees",
}


def contract_components(payload: Any, day: date) -> dict[str, Rate]:
    """Preserve all supplied components and bands; use the server's VAT total."""
    result = {}
    units = {
        "KWh": "€/kWh",
        "M3": "€/m³",
        "Year": "€/year",
        "Month": "€/month",
        "Day": "€/day",
        "OneOff": "EUR",
        "Invoice": "€/invoice",
    }
    for row in items(payload):
        kind, unit = row.get("priceComponentType"), row.get("priceUnit")
        # The portal displays redelivery compensation using the untaxed price
        # for both VAT display settings; mirror that rather than adding VAT.
        redelivery = isinstance(kind, str) and kind in {
            "BaseODN",
            "PeakODN",
            "OffPeakODN",
            "BaseODNExcess",
            "PeakODNExcess",
            "OffPeakODNExcess",
        }
        value = number(row.get("price") if redelivery else row.get("priceTaxed"))
        if not isinstance(kind, str) or unit not in units or value is None:
            continue
        identity = f"{kind}_{unit}_{row.get('rangeBegin', '')}_{row.get('rangeEnd', '')}"
        key = hashlib.sha256(identity.encode()).hexdigest()[:20]
        name = COMPONENT_LABELS.get(kind, kind)
        if "rangeBegin" in row:
            name += f" ({row['rangeBegin']}–{row.get('rangeEnd', '∞')})"
        attrs = {
            k: v
            for k, v in row.items()
            if k
            in (
                "priceComponentType",
                "priceUnit",
                "price",
                "priceTaxed",
                "vatPercentage",
                "vatInclusive",
                "rangeBegin",
                "rangeEnd",
            )
        }
        attrs.update(
            {
                "valid_on": day.isoformat(),
                "uses_price_taxed": not redelivery,
                "includes_vat": not redelivery,
                "includes_energy_tax": kind == "EnergyTax",
            }
        )
        result[key] = Rate(key, name, value, units[unit], attrs)
    return result


def delivery_price(source: Source, now: datetime, kind: str) -> float | None:
    """Portal delivery total including VAT, at the standard domestic tax band.

    Higher tax bands remain separately available as components. Fixed yearly
    costs and feed-in charges are never folded into a per-kWh tariff.
    """
    contract = source.metadata.get("contract") or {}
    start, end = timestamp(contract.get("startDate")), timestamp(contract.get("endDate"))
    if (start and now < start) or (end and now >= end):
        return None
    rows = items(source.blocks.get(contract_price_key(now)))
    expected_unit = "M3" if source.segment == "Gas" else "KWh"
    applicable = [r for r in rows if r.get("priceUnit") == expected_unit]
    main = next((r for r in applicable if r.get("priceComponentType") == kind), None)
    if not main or number(main.get("priceTaxed")) is None:
        return None
    total = number(main["priceTaxed"])
    extras = (
        ("EnergyTax", "RenewableEnergyMarkup", "RegionMarkup", "Premium")
        if source.segment == "Gas"
        else ("EnergyTax", "RenewableEnergyMarkup")
    )
    for component in extras:
        band = next(
            (
                r
                for r in applicable
                if r.get("priceComponentType") == component and number(r.get("rangeBegin", 0)) == 0
            ),
            None,
        )
        if band is None or number(band.get("priceTaxed")) is None:
            return None
        total += number(band["priceTaxed"])
    return total
