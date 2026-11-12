"""Config flow for Vandebron Thuis."""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    VandebronApi,
    VandebronAuthError,
    build_authorize_url,
    generate_pkce_pair,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CUSTOMER_REFERENCE,
    CONF_EMAIL,
    CONF_INCLUDE_EXTRA_COSTS,
    CONF_INCLUDE_VAT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class VandebronConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Vandebron Thuis config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._code_verifier: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step: let the user pick a login method."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["credentials", "oauth"],
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with e-mail + password (Keycloak password grant)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api = VandebronApi(async_get_clientsession(self.hass))
            try:
                tokens = await api.async_password_login(
                    user_input[CONF_EMAIL], user_input["password"]
                )
            except VandebronAuthError as err:
                _LOGGER.warning("Password login failed: %s", err)
                # If the realm disabled direct grants for this client, steer
                # the user to the browser flow instead.
                if "unauthorized_client" in str(err) or "invalid_client" in str(err):
                    errors["base"] = "direct_grant_disabled"
                else:
                    errors["base"] = "invalid_auth"
            else:
                return await self._async_create(tokens)

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Browser login: show an authorize URL, user pastes the redirect."""
        errors: dict[str, str] = {}
        if user_input is not None and self._code_verifier is not None:
            code = _extract_code(user_input["redirect_url"])
            if not code:
                errors["base"] = "no_code"
            else:
                api = VandebronApi(async_get_clientsession(self.hass))
                try:
                    tokens = await api.async_exchange_code(code, self._code_verifier)
                except VandebronAuthError as err:
                    _LOGGER.warning("Code exchange failed: %s", err)
                    errors["base"] = "invalid_auth"
                else:
                    return await self._async_create(tokens)

        verifier, challenge = generate_pkce_pair()
        self._code_verifier = verifier
        authorize_url = build_authorize_url(challenge, secrets.token_urlsafe(16))

        return self.async_show_form(
            step_id="oauth",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            errors=errors,
            description_placeholders={"authorize_url": authorize_url},
        )

    async def _async_create(self, tokens) -> ConfigFlowResult:
        """Finish the flow with a token set."""
        await self.async_set_unique_id(tokens.customer_reference or tokens.email)
        self._abort_if_unique_id_configured()
        title = tokens.email or tokens.customer_reference or "Vandebron"
        return self.async_create_entry(
            title=title,
            data={
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_CUSTOMER_REFERENCE: tokens.customer_reference,
                CONF_EMAIL: tokens.email,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return VandebronOptionsFlow()


def _extract_code(value: str) -> str | None:
    """Accept either the full redirect URL or a bare authorization code."""
    value = value.strip()
    if "code=" in value:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        if "code" in query:
            return query["code"][0]
    if value and "=" not in value and "/" not in value:
        return value
    return None


class VandebronOptionsFlow(OptionsFlow):
    """Options: toggle VAT and extra costs in the shown market price."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INCLUDE_VAT,
                        default=options.get(CONF_INCLUDE_VAT, True),
                    ): bool,
                    vol.Required(
                        CONF_INCLUDE_EXTRA_COSTS,
                        default=options.get(CONF_INCLUDE_EXTRA_COSTS, True),
                    ): bool,
                }
            ),
        )
