"""Vandebron Thuis API client.

Reverse-engineered from the official Android app (v3.4.15). The app uses:

* Keycloak (realm ``vandebron``, public client ``app-home``) for auth, with
  both the OAuth Authorization-Code + PKCE flow and (per the realm's discovery
  document) the Resource-Owner Password grant.
* A legacy monolith at ``mijn.vandebron.nl/api`` that serves market prices,
  consumer/connection data and contract details, keyed by ``customerReference``
  (from the JWT ``preferred_username``) and a derived ``organizationId``.
* A set of newer per-service hosts under ``api.vandebron.tech`` keyed by an
  ``accountNumber`` from ``users/v1/accounts``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp

from .const import (
    API_BASE_AGREEMENTS,
    API_BASE_BILLING,
    API_BASE_MEASUREMENTS,
    API_BASE_MIJN,
    API_BASE_USERS,
    APP_VERSION,
    AUTH_CLIENT_ID,
    AUTH_REDIRECT_URI,
    AUTH_SCOPE,
    HTTP_PLATFORM,
    OIDC_AUTH_URL,
    OIDC_TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

# Refresh the access token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60


class VandebronAuthError(Exception):
    """Raised when authentication fails or the session cannot be refreshed."""


class VandebronApiError(Exception):
    """Raised for non-auth API errors."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce_pair() -> tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` PKCE pair (S256)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(code_challenge: str, state: str) -> str:
    """Build the Keycloak authorization URL the user opens in a browser."""
    query = urlencode(
        {
            "client_id": AUTH_CLIENT_ID,
            "response_type": "code",
            "scope": AUTH_SCOPE,
            "redirect_uri": AUTH_REDIRECT_URI,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{OIDC_AUTH_URL}?{query}"


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode (without verifying) the claims of a JWT access token.

    The server validates the signature on every request; we only need to read
    ``preferred_username`` etc. locally to know which customer we are.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as err:  # pragma: no cover - defensive
        raise VandebronAuthError("Could not decode access token") from err


@dataclass
class VandebronTokens:
    """Holds the OAuth token set and its derived identity."""

    access_token: str
    refresh_token: str
    expires_at: float = 0.0
    customer_reference: str | None = None
    email: str | None = None
    user_type: str | None = None

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "VandebronTokens":
        access = data["access_token"]
        claims = decode_jwt_claims(access)
        return cls(
            access_token=access,
            refresh_token=data.get("refresh_token", ""),
            expires_at=time.time() + float(data.get("expires_in", 300)),
            customer_reference=claims.get("preferred_username"),
            email=claims.get("email"),
            user_type=claims.get("user_type"),
        )


class VandebronApi:
    """Thin async client around the Vandebron backend."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: VandebronTokens | None = None,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._refresh_lock = asyncio.Lock()
        # Called with a VandebronTokens whenever tokens are refreshed so the
        # integration can persist the new refresh token to the config entry.
        self.on_tokens_refreshed = None

    @property
    def tokens(self) -> VandebronTokens | None:
        return self._tokens

    # -- Authentication ------------------------------------------------------

    async def async_password_login(self, username: str, password: str) -> VandebronTokens:
        """Log in with the Resource-Owner Password grant.

        May fail if the realm has direct access grants disabled for the
        ``app-home`` client, in which case the OAuth code flow must be used.
        """
        data = {
            "grant_type": "password",
            "client_id": AUTH_CLIENT_ID,
            "scope": AUTH_SCOPE,
            "username": username,
            "password": password,
        }
        self._tokens = await self._token_request(data)
        return self._tokens

    async def async_exchange_code(self, code: str, code_verifier: str) -> VandebronTokens:
        """Exchange an authorization code (+ PKCE verifier) for tokens."""
        data = {
            "grant_type": "authorization_code",
            "client_id": AUTH_CLIENT_ID,
            "code": code,
            "redirect_uri": AUTH_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
        self._tokens = await self._token_request(data)
        return self._tokens

    async def _token_request(self, data: dict[str, str]) -> VandebronTokens:
        try:
            async with self._session.post(
                OIDC_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    error = body.get("error") if isinstance(body, dict) else None
                    desc = body.get("error_description") if isinstance(body, dict) else None
                    raise VandebronAuthError(f"{resp.status} {error}: {desc}")
                return VandebronTokens.from_response(body)
        except aiohttp.ClientError as err:
            raise VandebronAuthError(f"Token request failed: {err}") from err

    async def _async_ensure_token(self) -> str:
        """Return a valid access token, refreshing it if (almost) expired."""
        if self._tokens is None:
            raise VandebronAuthError("Not authenticated")
        if time.time() < self._tokens.expires_at - _TOKEN_EXPIRY_MARGIN:
            return self._tokens.access_token

        async with self._refresh_lock:
            # Re-check: another task may have refreshed while we waited.
            if time.time() < self._tokens.expires_at - _TOKEN_EXPIRY_MARGIN:
                return self._tokens.access_token
            if not self._tokens.refresh_token:
                raise VandebronAuthError("No refresh token available")
            _LOGGER.debug("Refreshing Vandebron access token")
            new_tokens = await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "client_id": AUTH_CLIENT_ID,
                    "refresh_token": self._tokens.refresh_token,
                }
            )
            # Keycloak rotates refresh tokens; keep identity fields.
            new_tokens.customer_reference = self._tokens.customer_reference
            new_tokens.email = self._tokens.email
            new_tokens.user_type = self._tokens.user_type
            self._tokens = new_tokens
            if self.on_tokens_refreshed is not None:
                self.on_tokens_refreshed(new_tokens)
            return self._tokens.access_token

    # -- Low-level request helper -------------------------------------------

    async def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "X-Request-ID": str(uuid4()),
            "X-App-Version": APP_VERSION,
            "app-version": APP_VERSION,
            "X-Platform": HTTP_PLATFORM,
        }
        if authenticated:
            token = await self._async_ensure_token()
            headers["Authorization"] = f"Bearer {token}"

        url = f"{base}{path}"
        try:
            async with self._session.request(
                method, url, params=params, json=json_body, headers=headers
            ) as resp:
                if resp.status in (401, 403):
                    raise VandebronAuthError(f"{resp.status} on {path}")
                if resp.status == 204 or resp.content_length == 0:
                    return None
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise VandebronApiError(f"{resp.status} on {path}: {body}")
                return body
        except aiohttp.ClientError as err:
            raise VandebronApiError(f"Request to {path} failed: {err}") from err

    # -- Identity resolution -------------------------------------------------

    @property
    def customer_reference(self) -> str | None:
        return self._tokens.customer_reference if self._tokens else None

    async def async_get_organization_id(self) -> str:
        """Resolve the organizationId for the logged-in customer.

        ``GET mijn/v1/customers/{customerReference}/productGroups`` ->
        ``productGroups[].resources[0].organizationId``.
        """
        ref = self.customer_reference
        if not ref:
            raise VandebronAuthError("Missing customer reference")
        data = await self._request(
            "GET", API_BASE_MIJN, f"/v1/customers/{ref}/productGroups"
        )
        groups = (data or {}).get("productGroups") or []
        for group in groups:
            resources = group.get("resources") or []
            if resources and resources[0].get("organizationId"):
                return str(resources[0]["organizationId"])
        raise VandebronApiError("No organizationId found in productGroups")

    # -- Legacy "mijn" endpoints --------------------------------------------

    async def async_get_customer_details(self) -> dict[str, Any]:
        """``GET mijn/v1/customers/{customerReference}/details``."""
        ref = self.customer_reference
        return await self._request("GET", API_BASE_MIJN, f"/v1/customers/{ref}/details")

    async def async_get_energy_consumer(self, organization_id: str) -> dict[str, Any]:
        """``GET mijn/v1/energyConsumers/{organizationId}``.

        Returns shipping addresses, each with connections (connectionId,
        marketSegment, ...), clusterReference and clusterId.
        """
        return await self._request(
            "GET", API_BASE_MIJN, f"/v1/energyConsumers/{organization_id}"
        )

    async def async_get_market_prices(
        self,
        organization_id: str,
        connection_id: str,
        *,
        include_vat: bool = True,
        include_extra_costs: bool = True,
        price_date: date | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Market (dynamic) prices for a connection.

        ``GET mijn/v1/p4/organizations/{org}/connections/{conn}/marketprices``
        Returns ``{"Electricity": [...], "Gas": [...]}`` where each entry is
        ``{startDate, endDate, price, priceUnit}``. Electricity is hourly
        (per kWh); gas is daily (per m3, valid 06:00-06:00).
        """
        params = {
            "includeVat": str(include_vat).lower(),
            "includeExtraCosts": str(include_extra_costs).lower(),
        }
        if price_date is not None:
            params["priceDate"] = price_date.isoformat()
        data = await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/p4/organizations/{organization_id}"
            f"/connections/{connection_id}/marketprices",
            params=params,
        )
        return data or {}

    async def async_get_meter_status(
        self, organization_id: str, connection_id: str
    ) -> dict[str, Any]:
        """``GET mijn/v1/p4/organizations/{org}/connections/{conn}/meterstatus``."""
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/p4/organizations/{organization_id}"
            f"/connections/{connection_id}/meterstatus",
        )

    async def async_get_contract_details(
        self, organization_id: str, cluster_reference: str
    ) -> dict[str, Any]:
        """Contract details for a shipping address (cluster)."""
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/energyConsumers/{organization_id}"
            f"/shippingAddress/{cluster_reference}/contractDetails",
        )

    async def async_get_advance_payments(
        self, organization_id: str, cluster_reference: str
    ) -> dict[str, Any]:
        """Monthly advance-payment info for a cluster."""
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/energyConsumers/{organization_id}/advancePayments",
            params={"clusterReference": cluster_reference},
        )

    # -- Newer api.vandebron.tech (orval) endpoints (best effort) -----------

    async def async_get_accounts(self) -> dict[str, Any]:
        """``GET users/v1/accounts`` -> {name, accounts:[{accountNumber,...}]}."""
        return await self._request("GET", API_BASE_USERS, "/v1/accounts")

    async def async_get_rates(self, account_number: str) -> dict[str, Any]:
        """``GET agreements/v1/accounts/{accountNumber}/rates``."""
        return await self._request(
            "GET", API_BASE_AGREEMENTS, f"/v1/accounts/{account_number}/rates"
        )

    async def async_get_latest_costs(self, account_number: str) -> dict[str, Any]:
        """``GET measurements/v1/accounts/{accountNumber}/insights-overview/latest-costs``."""
        return await self._request(
            "GET",
            API_BASE_MEASUREMENTS,
            f"/v1/accounts/{account_number}/insights-overview/latest-costs",
        )

    async def async_get_invoices(self, account_number: str) -> Any:
        """``GET billing/v1/accounts/{accountNumber}/invoices``."""
        return await self._request(
            "GET", API_BASE_BILLING, f"/v1/accounts/{account_number}/invoices"
        )
