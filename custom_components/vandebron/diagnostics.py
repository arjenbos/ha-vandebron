"""Downloadable operational diagnostics without credentials or customer data."""

from typing import Any

from homeassistant.core import HomeAssistant

from . import VandebronConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VandebronConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "version": "0.2.0",
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "discovery_errors": dict(coordinator.discovery_errors),
        "sources": [
            {
                "kind": source.kind,
                "segment": source.segment,
                "blocks": sorted(source.blocks),
                "last_success": {k: v.isoformat() for k, v in source.updated.items()},
                "errors": dict(source.errors),
                "price_periods": {day: len(periods) for day, periods in source.prices.items()},
                "rate_components": len(source.rates()),
            }
            for source in (coordinator.data or {}).values()
        ],
    }
