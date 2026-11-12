"""Config flow for Vandebron Thuis."""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    VandebronApi,
    VandebronApiError,
    VandebronAuthError,
    build_authorize_url,
    generate_pkce_pair,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CUSTOMER_REFERENCE,
    CONF_EMAIL,
    CONF_IMPORT_STATISTICS,
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
        self._oauth_state: str | None = None
        self._authorize_url: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
            except VandebronApiError:
                errors["base"] = "cannot_connect"
            except VandebronAuthError as err:
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

    async def async_step_oauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Browser login: show an authorize URL, user pastes the redirect."""
        errors: dict[str, str] = {}
        if user_input is not None and self._code_verifier is not None:
            code = _extract_code(user_input["redirect_url"], self._oauth_state)
            if not code:
                errors["base"] = "invalid_callback"
            else:
                api = VandebronApi(async_get_clientsession(self.hass))
                try:
                    tokens = await api.async_exchange_code(code, self._code_verifier)
                except VandebronApiError:
                    errors["base"] = "cannot_connect"
                except VandebronAuthError:
                    errors["base"] = "invalid_auth"
                    self._code_verifier = None
                else:
                    return await self._async_create(tokens)

        if self._code_verifier is None:
            verifier, challenge = generate_pkce_pair()
            self._code_verifier = verifier
            self._oauth_state = secrets.token_urlsafe(32)
            self._authorize_url = build_authorize_url(challenge, self._oauth_state)

        return self.async_show_form(
            step_id="oauth",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            errors=errors,
            description_placeholders={"authorize_url": self._authorize_url},
        )

    async def _async_create(self, tokens) -> ConfigFlowResult:
        """Finish the flow with a token set."""
        identity = tokens.customer_reference or tokens.email
        if not identity or not tokens.refresh_token:
            return self.async_abort(reason="missing_identity")
        await self.async_set_unique_id(identity)
        data = {
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_CUSTOMER_REFERENCE: tokens.customer_reference,
            CONF_EMAIL: tokens.email,
        }
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=tokens.email or identity, data=data)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return VandebronOptionsFlow()


def _extract_code(value: str, expected_state: str | None) -> str | None:
    """Validate the full native callback and the original OAuth state."""
    parsed = urlparse(value.strip())
    if parsed.scheme != "nl.vandebron.thuis.auth" or parsed.netloc != "oauth":
        return None
    query = parse_qs(parsed.query)
    if (
        not expected_state
        or len(query.get("state", [])) != 1
        or not secrets.compare_digest(query["state"][0], expected_state)
        or "error" in query
        or len(query.get("code", [])) != 1
    ):
        return None
    return query["code"][0]


class VandebronOptionsFlow(OptionsFlow):
    """Options: toggle VAT and extra costs in the shown market price."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IMPORT_STATISTICS,
                        default=options.get(CONF_IMPORT_STATISTICS, False),
                    ): bool,
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
