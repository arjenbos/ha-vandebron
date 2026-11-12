"""Cover translated names, including components discovered after setup."""

import json
import re
from datetime import datetime
from pathlib import Path
from string import Formatter
from unittest.mock import Mock

import pytest

from custom_components.vandebron.models import (
    AMSTERDAM,
    COMPONENT_LABELS,
    Source,
    contract_price_key,
)
from custom_components.vandebron.sensor import VandebronSensor, metrics

ROOT = Path(__file__).parents[1] / "custom_components/vandebron"


def catalogs():
    return [
        json.loads((ROOT / path).read_text())
        for path in ("translations/en.json", "translations/nl.json")
    ]


def fields(value):
    return {field for _, field, _, _ in Formatter().parse(value) if field is not None}


def leaves(value, prefix=()):
    if isinstance(value, dict):
        return {
            key: leaf
            for name, child in value.items()
            for key, leaf in leaves(child, (*prefix, name)).items()
        }
    return {prefix: value}


def test_catalogs_have_matching_keys_and_placeholders():
    english, dutch = catalogs()
    en, nl = leaves(english), leaves(dutch)
    assert en.keys() == nl.keys()
    for key in en:
        assert fields(en[key]) == fields(nl[key]), key


@pytest.mark.parametrize(
    "kind,segment",
    [
        ("organization", None),
        ("address", None),
        ("account", None),
        ("connection", "Electricity"),
        ("connection", "Gas"),
    ],
)
@pytest.mark.parametrize("contract", ["Fixed", "MarketPriceVariable"])
def test_all_sensor_names_resolve_in_both_languages(kind, segment, contract):
    now = datetime.now(AMSTERDAM)
    source = Source(
        "test",
        "Test",
        kind,
        segment=segment,
        metadata={"contract": {"contractType": contract}},
        blocks={
            "agreements": [{"market": "ELECTRICITY"}, {"market": "GAS"}],
            "rates": {"synthetic": {"price": 0.12}},
            contract_price_key(now): [
                {
                    "priceComponentType": component,
                    "priceUnit": "KWh",
                    "price": 0.1,
                    "priceTaxed": 0.121,
                    "rangeBegin": 0,
                    "rangeEnd": 100,
                }
                for component in [*COMPONENT_LABELS, "FutureApiComponent"]
            ],
        },
    )
    coordinator = Mock(data={source.key: source})
    for metric in metrics(source):
        entity = VandebronSensor(coordinator, source.key, metric)
        assert not hasattr(entity, "_attr_name"), "A hardcoded name bypasses HA translations"
        entity.platform_data = Mock(platform_name="vandebron", domain="sensor")
        key = entity.translation_key
        placeholders = entity.translation_placeholders
        for catalog in catalogs():
            translations = {
                f"component.vandebron.entity.sensor.{k}.name": v["name"]
                for k, v in catalog["entity"]["sensor"].items()
            }
            expected = catalog["entity"]["sensor"][key]["name"].format(**placeholders)
            assert entity._name_internal(None, translations) == expected
        assert catalogs()[0]["entity"]["sensor"][key]["name"].format(**placeholders) == metric.name
        device = entity.device_info
        for catalog in catalogs():
            assert catalog["device"][device["translation_key"]]["name"].format(
                **device["translation_placeholders"]
            )


def test_translation_keys_match_hassfest_rules():
    # hassfest also rejects adjacent separators, e.g. the old day_-1 suffix.
    pattern = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")
    for catalog in catalogs():
        for domain in catalog["entity"].values():
            for key in domain:
                assert pattern.fullmatch(key), key
        for key in catalog["device"]:
            assert pattern.fullmatch(key), key
