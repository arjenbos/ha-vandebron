from unittest.mock import Mock

from custom_components.vandebron.migration import async_migrate_entities
from custom_components.vandebron.models import Source


async def test_existing_entity_id_kept(hass, monkeypatch):
    import custom_components.vandebron.migration as module

    old = Mock(
        platform="vandebron", unique_id="org_electricity_price_now", entity_id="sensor.original"
    )
    registry = Mock()
    registry.async_get_entity_id.return_value = None
    monkeypatch.setattr(module.er, "async_get", lambda h: registry)
    monkeypatch.setattr(module.er, "async_entries_for_config_entry", lambda r, e: [old])
    source = Source(
        "connection_org_e1", "Home", "connection", organization="org", segment="Electricity"
    )
    async_migrate_entities(hass, "entry", {source.key: source})
    registry.async_update_entity.assert_called_once_with(
        "sensor.original", new_unique_id="connection_org_e1_electricity_price_now"
    )


async def test_obsolete_year_entities_are_removed(hass, monkeypatch):
    import custom_components.vandebron.migration as module

    source = Source(
        "connection_org_e1", "Home", "connection", organization="org", segment="Electricity"
    )
    future = Mock(
        platform="vandebron",
        unique_id=f"{source.key}_contract_next_year_peak",
        entity_id="sensor.old_future",
    )
    current = Mock(
        platform="vandebron",
        unique_id=f"{source.key}_contract_price_peak",
        entity_id="sensor.current",
    )
    registry = Mock()
    monkeypatch.setattr(module.er, "async_get", lambda h: registry)
    monkeypatch.setattr(module.er, "async_entries_for_config_entry", lambda r, e: [future, current])
    async_migrate_entities(hass, "entry", {source.key: source})
    registry.async_remove.assert_called_once_with("sensor.old_future")
    registry.async_update_entity.assert_not_called()
