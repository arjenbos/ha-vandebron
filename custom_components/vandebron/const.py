"""Constants for the Vandebron Thuis integration.

All values were derived by reverse-engineering the official
"Vandebron" Android app (nl.vandebron.thuis, v3.4.15). See README.md.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "vandebron"

# --- OAuth / Keycloak -------------------------------------------------------
# The app authenticates against a Keycloak realm with the public client
# "app-home" (no client secret). Discovery lives under the website host.
OIDC_REALM_URL: Final = "https://vandebron.nl/auth/realms/vandebron"
OIDC_AUTH_URL: Final = f"{OIDC_REALM_URL}/protocol/openid-connect/auth"
OIDC_TOKEN_URL: Final = f"{OIDC_REALM_URL}/protocol/openid-connect/token"
OIDC_LOGOUT_URL: Final = f"{OIDC_REALM_URL}/protocol/openid-connect/logout"

AUTH_CLIENT_ID: Final = "app-home"
AUTH_SCOPE: Final = "openid"
# The native redirect scheme registered by the app.
AUTH_REDIRECT_URI: Final = "nl.vandebron.thuis.auth://oauth"

# --- API base URLs (production) ---------------------------------------------
# The app keeps per-service base URLs. "mijn" is the legacy monolith that
# serves the market-price / consumer endpoints we rely on most.
API_BASE_MIJN: Final = "https://mijn.vandebron.nl/api"
API_BASE_USERS: Final = "https://api.vandebron.tech/users"
API_BASE_AGREEMENTS: Final = "https://api.vandebron.tech/agreements"
API_BASE_BILLING: Final = "https://api.vandebron.tech/billing"
API_BASE_MEASUREMENTS: Final = "https://api.vandebron.tech/measurements"

# Headers the app always sends.
APP_VERSION: Final = "3.4.15"
HTTP_PLATFORM: Final = "android"

# --- Config entry keys ------------------------------------------------------
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_CUSTOMER_REFERENCE: Final = "customer_reference"
CONF_ORGANIZATION_ID: Final = "organization_id"
CONF_EMAIL: Final = "email"

CONF_IMPORT_STATISTICS: Final = "import_statistics"

CONF_INCLUDE_VAT: Final = "include_vat"
CONF_INCLUDE_EXTRA_COSTS: Final = "include_extra_costs"

# --- Misc -------------------------------------------------------------------
UPDATE_INTERVAL: Final = timedelta(minutes=30)

# Market segments as returned by the market-prices endpoint.
SEGMENT_ELECTRICITY: Final = "Electricity"
SEGMENT_GAS: Final = "Gas"
