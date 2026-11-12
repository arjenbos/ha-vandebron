"""Exercise the built-in Marketplace's packaging checks against this repository."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from awesomeversion import AwesomeVersion
from homeassistant.components.marketplace.repositories.integration import (
    _check_loadable_manifest,
    _validated_domain,
)
from homeassistant.components.marketplace.utils.validate import REPOSITORY_MANIFEST_VALUES
from homeassistant.exceptions import ConfigEntryError

from custom_components import vandebron

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components/vandebron"


def test_package_passes_marketplace_install_checks():
    repository = json.loads((ROOT / "hacs.json").read_text())
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    for key, validate in REPOSITORY_MANIFEST_VALUES.items():
        if key in repository:
            assert validate(repository[key]) == repository[key]
    assert AwesomeVersion(repository["homeassistant"]) >= AwesomeVersion("2026.11.0")
    assert _validated_domain(manifest["domain"]) == COMPONENT.name
    _check_loadable_manifest(COMPONENT, manifest["domain"])
    assert manifest["config_flow"] is True
    assert manifest["issue_tracker"]
    assert (COMPONENT / "translations/en.json").is_file()
    assert not (COMPONENT / "strings.json").exists()
    assert (COMPONENT / "brand/icon.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


async def test_pre_marketplace_home_assistant_rejected_before_api_calls(hass, monkeypatch):
    monkeypatch.setattr(vandebron, "MAJOR_VERSION", 2026)
    monkeypatch.setattr(vandebron, "MINOR_VERSION", 10)
    session = Mock()
    monkeypatch.setattr(vandebron, "async_get_clientsession", session)
    with pytest.raises(ConfigEntryError, match="2026.11"):
        await vandebron.async_setup_entry(hass, Mock())
    session.assert_not_called()


def test_manifest_key_order_matches_hassfest():
    keys = list(json.loads((COMPONENT / "manifest.json").read_text()))
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
