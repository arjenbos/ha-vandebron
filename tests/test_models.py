from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.vandebron.models import (
    Source,
    day_bounds,
    invoice_amount,
    invoice_attributes,
    latest_invoice,
    legacy_usage,
    number,
    parse_prices,
    parse_rates,
    price_stats,
)
from custom_components.vandebron.sensor import metrics, period_key


@pytest.mark.parametrize("value", [True, False, "nan", "inf", None, {}, "oops"])
def test_invalid_numbers(value):
    assert number(value) is None


def test_zero_and_credit():
    assert number({"value": "0"}) == 0
    assert number("-1.5") == -1.5
    assert invoice_amount({"totalAmount": 0, "amount": 100}) == 0


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_dutch_dst(day, hours):
    start, end = day_bounds(day)
    assert (end - start).total_seconds() == hours * 3600
    prices = parse_prices(
        [{"startDate": start.isoformat(), "endDate": end.isoformat(), "price": -0.1}]
    )
    assert price_stats(prices, day) == (-0.1, -0.1, -0.1)


def test_weighted_prices_and_dutch_midnight():
    day = date(2026, 6, 1)
    start, end = day_bounds(day)
    rows = [
        {
            "startDate": start.isoformat(),
            "endDate": (start + timedelta(hours=1)).isoformat(),
            "price": 0,
        },
        {
            "startDate": (start + timedelta(hours=1)).isoformat(),
            "endDate": end.isoformat(),
            "price": 0.24,
        },
    ]
    prices = parse_prices(rows + rows + [{"startDate": "bad", "price": True}])
    assert len(prices) == 2
    avg, low, high = price_stats(prices, day)
    assert avg == pytest.approx(0.23)
    assert low == 0 and high == 0.24


def test_gas_previous_day_and_next_quarter():
    p = parse_prices(
        [
            {
                "startDate": "2026-06-01T06:00:00+02:00",
                "endDate": "2026-06-02T06:00:00+02:00",
                "price": 0.9,
            },
            {
                "startDate": "2026-06-02T06:00:00+02:00",
                "endDate": "2026-06-03T06:00:00+02:00",
                "price": 1.1,
            },
        ]
    )
    s = Source("g", "g", "connection", prices={"2026-06-01": p})
    assert s.current_price(datetime(2026, 6, 2, 3, tzinfo=UTC)).price == 0.9
    assert s.current_price(datetime(2026, 6, 2, 4, tzinfo=UTC)).price == 1.1
    assert s.current_price(datetime(2026, 6, 3, 4, tzinfo=UTC)) is None
    assert s.next_price(datetime(2026, 6, 2, 3, tzinfo=UTC)).price == 1.1


def test_invoice_order_and_privacy():
    rows = {
        "data": [
            {"dueDate": "2026-01-02", "amount": {"value": "30"}},
            {
                "dueDate": "2026-06-02",
                "amount": {"value": "-30"},
                "pdfUrl": "secret",
                "status": "PAID",
            },
        ]
    }
    last = latest_invoice(rows)
    assert invoice_amount(last) == -30
    assert "pdfUrl" not in invoice_attributes(last)


def test_tariff_identity_units_and_values():
    # Synthetic rates: the response path is known, production schema is not.
    rows = [
        {"type": "PeakDeliveryCost", "unit": "KWh", "amount": {"value": "0.31", "unit": "eur"}},
        {"type": "OffPeakDeliveryCost", "unit": "KWh", "amount": {"value": "0", "unit": "eur"}},
        {"type": "EnergyTaxCredit", "unit": "Day", "amount": {"value": "-1.5", "unit": "eur"}},
    ]
    rates = parse_rates({"electricity": rows})
    assert len(rates) == 3
    assert sorted(r.value for r in rates.values()) == [-1.5, 0, 0.31]
    assert {r.unit for r in rates.values()} == {"€/kWh", "€/day"}
    assert set(rates) == set(parse_rates({"electricity": list(reversed(rows))}))


def test_legacy_wh_conversion():
    data = {
        "unit": "WH",
        "values": [
            {
                "time": "2026-06-01T00:00:00Z",
                "consumption": 7800,
                "production": 1000,
                "consumptionPeak": 7000,
            }
        ],
    }
    usage = legacy_usage(data)
    assert usage["usage"] == 7.8
    assert usage["redelivery"] == 1
    assert legacy_usage({**data, "unit": "unknown"})["usage"] is None


def test_account_graph_sensors(measurements):
    now = datetime(2026, 6, 3, tzinfo=UTC)
    source = Source(
        "a",
        "Home",
        "account",
        blocks={
            period_key("month", now): measurements,
            "costs": {"month": "2026-06-01", "costs": {"actual": {"value": "0", "unit": "eur"}}},
        },
    )
    m = {metric.key: metric for metric in metrics(source)}
    assert m["electricity_usage_month"].value(source, now) == 15.9
    assert m["electricity_redelivery_month"].value(source, now) == 4
    assert m["electricity_peak_month"].value(source, now) == 10
    assert m["gas_usage_month"].value(source, now) == 4
    assert m["costs_actual"].value(source, now) == 0
    assert all(not key.startswith("gas_redelivery") for key in m)


def test_live_legacy_costs_envelope_and_contract():
    from custom_components.vandebron.sensor import _legacy_contract, _legacy_cost

    now = datetime(2026, 11, 12, tzinfo=UTC)
    source = Source(
        "c",
        "Home",
        "connection",
        connection="one",
        blocks={
            "cost_month_2026-11-01": {
                "costs": [
                    {"connectionId": "other", "totalFixed": 99},
                    {"connectionId": "one", "totalFixed": 0},
                ]
            },
            "contracts": [
                {
                    "startDate": "2026-01-01",
                    "contractType": {"pricingType": "Fixed", "duration": 12},
                }
            ],
        },
    )
    assert _legacy_cost(source, now, "month", "totalFixed") == 0
    assert _legacy_contract(source, now)["contractType"]["pricingType"] == "Fixed"


def test_unidentified_tariffs_do_not_overwrite_each_other():
    assert len(parse_rates([{"amount": 1}, {"amount": 2}])) == 2


def test_estimates_are_not_reported_as_consumption():
    data = {
        "unit": "WH",
        "values": [
            {"time": "2026-11-01T00:00:00Z", "consumption": 1000, "tag": "Measured"},
            {"time": "2026-11-02T00:00:00Z", "consumption": 5000, "tag": "Estimated"},
        ],
    }
    assert legacy_usage(data)["usage"] == 1
