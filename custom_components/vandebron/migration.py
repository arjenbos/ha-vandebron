"""Retain entity IDs when upgrading the original single-device integration."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .models import Source


def async_migrate_entities(hass: HomeAssistant, entry_id: str, sources: dict[str, Source]) -> None:
    registry = er.async_get(hass)
    mapping: dict[str, str] = {}
    for source in sources.values():
        org = source.organization
        if source.kind == "connection":
            prefix = "gas" if source.segment == "Gas" else "electricity"
            suffixes = (
                "price_now",
                "price_next_hour",
                "price_avg_today",
                "price_min_today",
                "price_max_today",
            )
            for suffix in suffixes:
                mapping.setdefault(f"{org}_{prefix}_{suffix}", f"{source.key}_{prefix}_{suffix}")
        elif source.kind == "address":
            mapping.setdefault(
                f"{org}_monthly_advance_amount", f"{source.key}_monthly_advance_amount"
            )
        elif source.kind == "organization":
            mapping[f"{org}_last_invoice_amount"] = f"{source.key}_last_invoice_amount"
            mapping[f"{org}_customer_name"] = f"{source.key}_customer_name"
    accounts = [s for s in sources.values() if s.kind == "account"]
    organizations = [s for s in sources.values() if s.kind == "organization"]
    # The old month-cost sensor read account zero, not the organization. Only
    # migrate this ambiguous association if exactly one account/org exists.
    if len(accounts) == 1 and len(organizations) == 1:
        mapping[f"{organizations[0].organization}_current_month_costs"] = (
            f"{accounts[0].key}_costs_actual"
        )
    obsolete = {
        f"{source.key}_contract_next_year_{tariff}"
        for source in sources.values()
        if source.kind == "connection"
        for tariff in ("base", "peak", "offpeak")
    }
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.platform == DOMAIN and entity.unique_id in obsolete:
            registry.async_remove(entity.entity_id)
            continue
        if entity.platform != DOMAIN or entity.unique_id not in mapping:
            continue
        new = mapping[entity.unique_id]
        if not registry.async_get_entity_id("sensor", DOMAIN, new):
            registry.async_update_entity(entity.entity_id, new_unique_id=new)
