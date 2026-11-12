"""The Vandebron Thuis integration."""

from __future__ import annotations

import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VandebronApi, VandebronTokens
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CUSTOMER_REFERENCE,
    CONF_EMAIL,
    CONF_REFRESH_TOKEN,
)
from .coordinator import VandebronCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

type VandebronConfigEntry = ConfigEntry[VandebronCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: VandebronConfigEntry) -> bool:
    """Set up Vandebron Thuis from a config entry."""
    if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11):
        raise ConfigEntryError("Vandebron requires Home Assistant 2026.11 or later (Marketplace).")

    session = async_get_clientsession(hass)
    tokens = VandebronTokens(
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        # Force a refresh on first use; the stored access token may be stale.
        expires_at=time.time(),
        customer_reference=entry.data.get(CONF_CUSTOMER_REFERENCE),
        email=entry.data.get(CONF_EMAIL),
    )
    api = VandebronApi(session, tokens)

    def _persist(new_tokens: VandebronTokens) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: new_tokens.access_token,
                CONF_REFRESH_TOKEN: new_tokens.refresh_token,
            },
        )

    api.on_tokens_refreshed = _persist

    coordinator = VandebronCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    from .migration import async_migrate_entities

    async_migrate_entities(hass, entry.entry_id, coordinator.data)
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VandebronConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: VandebronConfigEntry) -> None:
    """Reload when options change."""
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)
