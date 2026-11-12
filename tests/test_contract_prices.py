from datetime import UTC, datetime

import pytest

from custom_components.vandebron.models import (
    Source,
    contract_components,
    contract_price_key,
    delivery_price,
)
from custom_components.vandebron.sensor import metrics


@pytest.fixture
def fixed_rows():
    # Synthetic prices with the live-verified schema, not customer tariff data.
    return [
        {
            "priceComponentType": "Base",
            "priceUnit": "KWh",
            "price": 0.1,
            "priceTaxed": 0.121,
            "vatInclusive": False,
            "vatPercentage": 21,
        },
        {
            "priceComponentType": "Peak",
            "priceUnit": "KWh",
            "price": 0.2,
            "priceTaxed": 0.242,
            "vatInclusive": False,
            "vatPercentage": 21,
        },
        {
            "priceComponentType": "OffPeak",
            "priceUnit": "KWh",
            "price": 0.05,
            "priceTaxed": 0.0605,
            "vatInclusive": False,
            "vatPercentage": 21,
        },
        {
            "priceComponentType": "EnergyTax",
            "priceUnit": "KWh",
            "price": 0.2,
            "priceTaxed": 0.242,
            "rangeBegin": 0,
            "rangeEnd": 2900,
        },
        {
            "priceComponentType": "EnergyTax",
            "priceUnit": "KWh",
            "price": 0.01,
            "priceTaxed": 0.0121,
            "rangeBegin": 10000,
        },
        {
            "priceComponentType": "RenewableEnergyMarkup",
            "priceUnit": "KWh",
            "price": 0,
            "priceTaxed": 0,
            "rangeBegin": 0,
            "rangeEnd": 2900,
        },
        {
            "priceComponentType": "FixedProductionFee",
            "priceUnit": "Year",
            "priceTaxed": 100,
            "rangeBegin": 0,
            "rangeEnd": 1000,
        },
        {
            "priceComponentType": "FixedProductionFee",
            "priceUnit": "Year",
            "priceTaxed": 200,
            "rangeBegin": 1000,
        },
    ]


def test_fixed_contract_uses_own_prices_not_market(fixed_rows):
    now = datetime.now(UTC)
    source = Source(
        "fixed",
        "Home",
        "connection",
        segment="Electricity",
        metadata={"contract": {"contractType": "Fixed1Year"}, "billAsSingle": False},
        blocks={contract_price_key(now): fixed_rows},
    )
    assert not source.is_dynamic
    assert delivery_price(source, now, "Base") == pytest.approx(0.363)
    assert delivery_price(source, now, "Peak") == pytest.approx(0.484)
    assert delivery_price(source, now, "OffPeak") == pytest.approx(0.3025)
    by_key = {m.key: m for m in metrics(source)}
    assert by_key["electricity_price_now"].value(source, now) is None
    assert by_key["contract_price_base"].value(source, now) == pytest.approx(0.363)
    assert by_key["contract_price_peak"].value(source, now) == pytest.approx(0.484)
    assert by_key["contract_price_offpeak"].value(source, now) == pytest.approx(0.3025)


def test_tax_bands_and_fees_remain_separate(fixed_rows):
    parsed = contract_components(fixed_rows, datetime.now(UTC).date())
    assert len(parsed) == 8
    assert len({r.key for r in parsed.values()}) == 8
    assert {r.unit for r in parsed.values()} == {"€/kWh", "€/year"}
    assert (
        len([r for r in parsed.values() if r.attributes["priceComponentType"] == "EnergyTax"]) == 2
    )
    assert set(parsed) == set(
        contract_components(list(reversed(fixed_rows)), datetime.now(UTC).date())
    )


def test_never_substitute_market_for_missing_fixed_rate():
    source = Source(
        "fixed",
        "Home",
        "connection",
        segment="Electricity",
        metadata={"contract": {"contractType": "Fixed1Year"}},
    )
    assert delivery_price(source, datetime.now(UTC), "Base") is None


def test_prices_stop_at_contract_end(fixed_rows):
    now = datetime(2026, 11, 12, tzinfo=UTC)
    source = Source(
        "ended",
        "Home",
        "connection",
        segment="Electricity",
        metadata={"contract": {"endDate": "2026-11-11"}},
        blocks={contract_price_key(now): fixed_rows},
    )
    assert delivery_price(source, now, "Base") is None


def test_single_contract_price_and_redelivery_follow_portal(fixed_rows):
    now = datetime.now(UTC)
    fixed_rows.append(
        {"priceComponentType": "BaseODN", "priceUnit": "KWh", "price": 0.1, "priceTaxed": 0.121}
    )
    source = Source(
        "single",
        "Home",
        "connection",
        segment="Electricity",
        metadata={"contract": {"contractType": "Fixed1Year"}, "billAsSingle": True},
        blocks={contract_price_key(now): fixed_rows},
    )
    by_key = {m.key: m for m in metrics(source)}
    assert by_key["electricity_price_now"].value(source, now) == pytest.approx(0.363)
    redelivery = next(
        r for r in source.rates().values() if r.attributes["priceComponentType"] == "BaseODN"
    )
    assert redelivery.value == 0.1
    assert redelivery.attributes["uses_price_taxed"] is False


def test_gas_includes_region_and_local_investment(fixed_rows):
    now = datetime.now(UTC)
    for row in fixed_rows:
        if row["priceUnit"] == "KWh":
            row["priceUnit"] = "M3"
    fixed_rows.extend(
        [
            {"priceComponentType": "RegionMarkup", "priceUnit": "M3", "priceTaxed": 0.01},
            {"priceComponentType": "Premium", "priceUnit": "M3", "priceTaxed": 0.02},
        ]
    )
    source = Source(
        "gas", "Home", "connection", segment="Gas", blocks={contract_price_key(now): fixed_rows}
    )
    assert delivery_price(source, now, "Base") == pytest.approx(0.393)


def test_same_tariff_sensor_switches_at_dutch_new_year(fixed_rows):
    from copy import deepcopy

    before = datetime(2026, 12, 31, 22, 59, tzinfo=UTC)
    after = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
    future_rows = deepcopy(fixed_rows)
    next(row for row in future_rows if row["priceComponentType"] == "Peak")["priceTaxed"] = 0.5
    source = Source(
        "fixed",
        "Home",
        "connection",
        segment="Electricity",
        metadata={"contract": {"contractType": "Fixed1Year"}},
        blocks={contract_price_key(before): fixed_rows, contract_price_key(after): future_rows},
    )
    descriptions = {metric.key: metric for metric in metrics(source)}
    normal = descriptions["contract_price_peak"]
    assert normal.name == "Electricity peak rate"
    assert normal.value(source, before) == pytest.approx(0.484)
    assert normal.value(source, after) == pytest.approx(0.742)
    assert not any(key.startswith("contract_next_year_") for key in descriptions)
