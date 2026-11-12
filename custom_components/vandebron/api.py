"""Vandebron Thuis API client.

Reverse-engineered from the official Android app (v3.4.15). The app uses:

* Keycloak (realm ``vandebron``, public client ``app-home``) for auth, with
  the OAuth Authorization-Code + PKCE flow. Password-grant login is attempted
  as an alternative, subject to the server's client policy.
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
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlencode
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
from .models import AMSTERDAM

_LOGGER = logging.getLogger(__name__)

# Refresh the access token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60


class VandebronAuthError(Exception):
    """Raised when authentication fails or the session cannot be refreshed."""


class VandebronApiError(Exception):
    """Raised for non-auth API errors without retaining response PII."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(claims, dict):
            raise TypeError("Invalid JWT claims")
        return claims
    except (IndexError, ValueError, TypeError) as err:  # pragma: no cover - defensive
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
    def from_response(cls, data: dict[str, Any]) -> VandebronTokens:
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
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    error = body.get("error") if isinstance(body, dict) else None
                    if resp.status in (400, 401, 403) and error in (
                        "invalid_grant",
                        "invalid_client",
                        "unauthorized_client",
                        "access_denied",
                        "invalid_request",
                    ):
                        raise VandebronAuthError(f"Token rejected: {error}")
                    raise VandebronApiError("Token service unavailable", resp.status)
                try:
                    return VandebronTokens.from_response(body)
                except (KeyError, TypeError, ValueError) as err:
                    raise VandebronApiError("Invalid token response") from err
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise VandebronApiError("Token service request failed") from err

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
            if not new_tokens.refresh_token:
                new_tokens.refresh_token = self._tokens.refresh_token
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
        _retry: bool = True,
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
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401:
                    if base in (
                        API_BASE_USERS,
                        API_BASE_AGREEMENTS,
                        API_BASE_BILLING,
                        API_BASE_MEASUREMENTS,
                    ):
                        # The modern service rejects some valid legacy accounts.
                        # It must not invalidate their working Keycloak session.
                        raise VandebronApiError("Account not authorized for this service", 401)
                    if authenticated and _retry and self._tokens:
                        # Only expire the token used here; a concurrent request may
                        # already have rotated it. The refresh lock serializes refresh.
                        if self._tokens.access_token == token:
                            self._tokens.expires_at = 0
                        await self._async_ensure_token()
                        return await self._request(
                            method,
                            base,
                            path,
                            params=params,
                            json_body=json_body,
                            authenticated=authenticated,
                            _retry=False,
                        )
                    raise VandebronAuthError("Access token rejected")
                if resp.status >= 400:
                    raise VandebronApiError("API request failed", resp.status)
                if resp.status == 204 or resp.content_length == 0:
                    return None
                body = await resp.json(content_type=None)
                return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise VandebronApiError("API returned an unreadable response") from err

    # -- Identity resolution -------------------------------------------------

    @property
    def customer_reference(self) -> str | None:
        return self._tokens.customer_reference if self._tokens else None

    async def async_get_organization_ids(self) -> list[str]:
        """Resolve all organizations, rather than silently choosing the first."""
        ref = self.customer_reference
        if not ref:
            return []
        data = await self._request("GET", API_BASE_MIJN, f"/v1/customers/{ref}/productGroups")
        if not isinstance(data, dict):
            raise VandebronApiError("Invalid product groups response")
        return list(
            dict.fromkeys(
                str(resource["organizationId"])
                for group in data.get("productGroups", [])
                for resource in group.get("resources", [])
                if resource.get("organizationId")
            )
        )

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
        return await self._request("GET", API_BASE_MIJN, f"/v1/energyConsumers/{organization_id}")

    async def async_get_market_prices(
        self,
        organization_id: str,
        connection_id: str,
        *,
        include_vat: bool = True,
        include_extra_costs: bool = True,
        price_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Market (dynamic) prices for a connection.

        ``GET mijn/v1/p4/organizations/{org}/connections/{conn}/marketprices``
        The response is a list for this connection, not an object containing
        both market segments. Interval timestamps determine validity.
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
            f"/v1/p4/organizations/{organization_id}/connections/{connection_id}/marketprices",
            params=params,
        )
        if data is None:
            return []
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise VandebronApiError("Invalid market price response")
        return data

    async def async_get_meter_status(
        self, organization_id: str, connection_id: str
    ) -> dict[str, Any]:
        """``GET mijn/v1/p4/organizations/{org}/connections/{conn}/meterstatus``."""
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/p4/organizations/{organization_id}/connections/{connection_id}/meterstatus",
        )

    async def async_get_contract_details(
        self, organization_id: str, cluster_id: str
    ) -> dict[str, Any]:
        """Contract details require clusterId, not clusterReference (F55127)."""
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/energyConsumers/{organization_id}"
            f"/shippingAddress/{quote(cluster_id, safe='')}/contractDetails",
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
        """Use the new insights format, falling back for unmigrated accounts."""
        try:
            return await self._request(
                "GET",
                API_BASE_MEASUREMENTS,
                f"/v2/accounts/{account_number}/insights-overview/latest-costs",
            )
        except VandebronApiError as err:
            if err.status != 404:
                raise
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

    async def async_get_usage(
        self,
        organization_id: str,
        connection_id: str,
        start: datetime,
        end: datetime,
        resolution: str = "Days",
    ) -> Any:
        """Legacy metered usage; never treated as a live meter reading."""
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/consumers/{organization_id}/connections/{connection_id}/usage",
            params={
                "resolution": resolution,
                "startDate": start.astimezone(AMSTERDAM).date().isoformat(),
                "endDate": end.astimezone(AMSTERDAM).date().isoformat(),
            },
        )

    async def async_get_usage_graphs(self, account: str, start: date, end: date) -> Any:
        return await self._request(
            "GET",
            API_BASE_MEASUREMENTS,
            f"/v1/accounts/{account}/usage-graphs",
            params={
                "startDate": start.astimezone(AMSTERDAM).date().isoformat(),
                "endDate": end.astimezone(AMSTERDAM).date().isoformat(),
                "resolution": "DAY",
            },
        )

    async def async_get_last_reading_dates(self, account: str) -> Any:
        return await self._request(
            "GET",
            API_BASE_MEASUREMENTS,
            f"/v1/accounts/{account}/usage-graphs/last-reading-dates",
        )

    async def async_get_contracts(self, organization: str) -> Any:
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/contracts/{organization}",
        )

    async def async_get_legacy_invoices(self, organization: str) -> Any:
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/organizations/{organization}/invoices",
            params={"invoiceType": "AdvancePayment,Settlement,Purchase,Custom"},
        )

    async def async_get_agreements(self, account: str) -> Any:
        return await self._request(
            "GET",
            API_BASE_AGREEMENTS,
            f"/v1/accounts/{account}/agreement-metadata",
        )

    async def async_get_costs(
        self, organization: str, connection: str, start: datetime, end: datetime
    ) -> Any:
        """Legacy cost components, scoped to exactly one connection."""
        return await self._request(
            "GET",
            API_BASE_MIJN,
            f"/consumers/{organization}/connections/costs",
            params={
                "connectionIds": connection.upper(),
                "resolution": "Days",
                "includeEnergyTax": "true",
                "startDate": start.astimezone(AMSTERDAM).date().isoformat(),
                "endDate": end.astimezone(AMSTERDAM).date().isoformat(),
            },
        )

    async def async_get_contract_prices(
        self, organization: str, contract: str, day: date
    ) -> list[dict[str, Any]]:
        """Customer-specific fixed/variable tariffs, as used by the legacy portal."""
        data = await self._request(
            "GET",
            API_BASE_MIJN,
            f"/v1/energyConsumers/{organization}/contracts/{contract}/prices",
            params={"priceDate": day.isoformat()},
        )
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise VandebronApiError("Invalid contract prices response")
        return data
