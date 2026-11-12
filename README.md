# Vandebron Thuis — Home Assistant integration

An unofficial Home Assistant integration for [Vandebron](https://vandebron.nl)
energy customers. It surfaces your contract/account information and — the
headline feature — your **dynamic market prices** (stroomprijzen and gasprijs),
so you can automate on cheap/green hours.

It was built by reverse-engineering the official **Vandebron** Android app
(`nl.vandebron.thuis`, v3.4.15). There is no public API; endpoints may change.

> ⚠️ Unofficial. Use at your own risk. Not affiliated with or endorsed by
> Vandebron.

## What you get

A single device (your energy contract) with sensors:

| Sensor | Unit | Notes |
| --- | --- | --- |
| Stroomprijs nu | €/kWh | Current hourly dynamic price |
| Stroomprijs komend uur | €/kWh | Next hour |
| Stroomprijs gemiddeld / min / max vandaag | €/kWh | Today's statistics |
| Gasprijs vandaag | €/m³ | Daily gas price (valid 06:00–06:00) |
| Kosten deze maand | € | From the usage-insights overview (best effort) |
| Maandbedrag | € | Monthly advance amount (best effort) |
| Laatste factuurbedrag | € | Most recent invoice (best effort) |
| Klant | — | Diagnostic |

The two price sensors expose the full day's price list in their
`prices` attribute, ready for charting with
[ApexCharts Card](https://github.com/RomRider/apexcharts-card).

Whether prices include VAT and the purchase fee + energy tax is configurable
via the integration's **Options** (`includeVat` / `includeExtraCosts`, matching
the toggle in the app).

## Installation

1. Copy `custom_components/vandebron` into your Home Assistant
   `config/custom_components/` folder (or add this repo via HACS as a custom
   repository).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Vandebron Thuis**.

### Signing in

You pick one of two methods in the config flow:

- **E-mail and password** — enter your Vandebron credentials; the integration
  requests tokens directly from Keycloak (password grant). Simplest, but the
  realm may not allow it for this client.
- **Sign in via browser (OAuth)** — Home Assistant shows a login link. Open it,
  log in, and your browser is redirected to an address starting with
  `nl.vandebron.thuis.auth://oauth?code=…`. The page won't open; copy that full
  address (or just the `code`) from the address bar and paste it back into Home
  Assistant. This mirrors the app's Authorization-Code + PKCE flow and always
  works.

If password login fails, use the browser method.

## Testing with Docker

A `docker-compose.yml` is included to spin up a throwaway Home Assistant with
the integration mounted:

```bash
docker compose up         # http://localhost:8123
docker compose down
```

The integration lives at `/config/custom_components/vandebron` inside the
container. Restart the container after code changes. Debug logs for
`custom_components.vandebron` are enabled in `dev-config/configuration.yaml`.
Generated Home Assistant files under `dev-config/` are git-ignored.

## How the API works (reverse-engineering notes)

The app is an Expo / React Native build with the JavaScript compiled to Hermes
bytecode (`assets/index.android.bundle`). These were recovered by disassembling
that bytecode.

### Authentication — Keycloak

- Realm: `https://vandebron.nl/auth/realms/vandebron`
- Public client: `app-home` (no secret), scope `openid`
- Authorization Code + PKCE (S256), native redirect
  `nl.vandebron.thuis.auth://oauth`; `refresh_token` grant for refresh.
- The access token is a JWT; `preferred_username` is your `customerReference`.

### API hosts (production)

| Service | Base URL |
| --- | --- |
| Legacy monolith (market prices, consumer, contract) | `https://mijn.vandebron.nl/api` |
| Users | `https://api.vandebron.tech/users` |
| Agreements (rates, contracts) | `https://api.vandebron.tech/agreements` |
| Billing (invoices) | `https://api.vandebron.tech/billing` |
| Measurements (usage, costs) | `https://api.vandebron.tech/measurements` |

All authenticated requests send `Authorization: Bearer <token>`, plus
`X-Request-ID`, `X-App-Version`, `app-version` and `X-Platform: android`.

### ID resolution chain

1. `customerReference` ← JWT `preferred_username`.
2. `organizationId` ← `GET /v1/customers/{customerReference}/productGroups`
   → `productGroups[].resources[0].organizationId`.
3. Connections ← `GET /v1/energyConsumers/{organizationId}` →
   `shippingAddresses[].connections[]` (each has `connectionId`,
   `marketSegment`, and the address has `clusterReference` / `clusterId`).

### Market prices (the important one)

```
GET https://mijn.vandebron.nl/api/v1/p4/organizations/{organizationId}
      /connections/{connectionId}/marketprices
      ?includeVat=true&includeExtraCosts=true&priceDate=YYYY-MM-DD
```

Response:

```json
{
  "Electricity": [
    { "startDate": "...", "endDate": "...", "price": 0.1234, "priceUnit": "kWh" }
  ],
  "Gas": [
    { "startDate": "...", "endDate": "...", "price": 0.98, "priceUnit": "m3" }
  ]
}
```

Electricity is hourly; gas is one daily price. `includeVat` /
`includeExtraCosts` toggle whether VAT and the purchase fee + energy tax are
folded into `price`.

### Other endpoints the app uses (not all surfaced yet)

`/v1/customers/{ref}/details`, `/v1/p4/.../meterstatus`,
`/v1/energyConsumers/{org}/shippingAddress/{cluster}/contractDetails`,
`/v1/energyConsumers/{org}/advancePayments`,
`/consumers/{org}/connections/{conn}/usage`,
`/consumers/{org}/connections/costs`, `users/v1/accounts`,
`agreements/v1/accounts/{accountNumber}/rates`,
`measurements/v1/accounts/{accountNumber}/insights-overview/latest-costs`,
`billing/v1/accounts/{accountNumber}/invoices`, and green-forecast endpoints.

## License

Provided as-is for personal use.
