# Vandebron integration assessment

This document records the state of **version 0.1.0 before implementation**,
assessed on November 12, 2026. Current capabilities are described in the
[README](../README.md) and [implementation notes](IMPLEMENTATIE.md). Findings
about missing functionality below are historical.

The original integration did not meet the requested functionality and lacked
runtime validation. Authentication and data retrieval existed, but its price
parser differed from the APK client contract. Contract rates and consumption
were not exposed to users.

## Evidence and certainty

- Source: `Vandebron_3.4.15_APKPure.xapk`, main APK `nl.vandebron.thuis.apk`,
  `assets/index.android.bundle`.
- Hermes bytecode version 99; bundle size 16,119,860 bytes; SHA-256:
  `05d1b7173636d9fb02cb7761ff84fbcffcf5952dfd02a5711be8eaa6df461b08`.
- The bundle was extracted again. An existing local disassembly had the same
  bundle hash and was used alongside a fresh string-table extraction.
  [APK evidence](apk-evidence.txt) includes function indices and byte offsets.
- MCP code graph tools were unavailable, so source files were inspected directly.
- This original assessment was static: no credentials, authenticated production
  requests or Home Assistant runtime trials were used at this stage. App mocks
  establish client expectations, not current server responses. Later live
  validation is documented separately in the implementation notes.

The APK shows what this app version expects. It does not prove that today's
production API is unchanged. The original README presented some assumptions as
established facts.

## Authentication and two API generations

The app contains Keycloak configuration for client `app-home`, scope `openid`,
native OAuth login and token refresh. The integration implements Authorization
Code with PKCE and a password grant. A grant supported by the realm need not be
allowed for this client; the original claim that browser sign-in always works
was not established.

| Model | Identification | API host |
| --- | --- | --- |
| Legacy energy | customerReference → organizationId → address/connectionId | `https://mijn.vandebron.nl/api` |
| New account model | accountNumber, called accountId in generated functions | `https://api.vandebron.tech/{service}` |

The bundle contains `users`, `agreements`, `billing`, `measurements`,
`usage-insights-kotlin`, `green-energy-mix-kotlin` and `smart-devices` services.
Their presence does not imply access for every account. The app distinguishes
legacy and new variants. Version 0.1.0 tried both models and chose the first
account, without guaranteeing the correct address association.

## Dynamic price response mismatch

Function **F55370**, byte offset **12654067**, constructs:

```text
GET /v1/p4/organizations/{organizationId}/connections/{connectionId}/marketprices
    ?includeVat={boolean}&includeExtraCosts={boolean}&priceDate=YYYY-MM-DD
```

**F37132** requests yesterday, today and tomorrow separately per connection. The
app uses the same tax setting for both booleans; the integration exposes them
independently, so supported combinations require validation.

The expected response is a **list** for the requested connection:

```json
[
  {"startDate": "...", "endDate": "...", "price": 0.1234, "priceUnit": "kWh"}
]
```

**F37135** reads `length` and `reduce`; **F37139** reads gas prices at index `[0]`.
Mock handler **F41097** returns only the electricity or gas list for a connection.
Helper **F41099** builds a combined object internally, but that is not the HTTP
response returned by the handler.

The original `coordinator.py:208` used `(prices or {}).get(other.segment, [])`,
which fails with `AttributeError` for a nonempty list. That exception also
escaped `_safe`. One request supplying both fuels for an address was therefore
an unsupported assumption. Fetch each connection and validate response types.

**F37139** combines yesterday's gas price before six and today's after six
(`beforeSixPrices` / `afterSixPrices`). Fetching only today is insufficient.
Validity must follow validated timestamps and the Dutch timezone. Never assume
one-hour intervals: use `startDate` and `endDate`. Current interval lengths and
next-day publication times had not yet been verified live at assessment time.

## Contracts, costs and consumption

These GET routes appear directly in the selected functions:

| Service | Route | Evidence |
| --- | --- | --- |
| users | `/v1/accounts` | F27795 |
| agreements | `/v1/accounts/{accountId}/rates` | F43591 |
| billing | `/v1/accounts/{accountId}/invoices`, with query parameters | F43945 |
| measurements | `/v1/accounts/{accountId}/insights-overview/latest-costs` | F28643 |
| measurements | `/v2/accounts/{accountId}/insights-overview/latest-costs` | F28646 |
| measurements | `/v1/accounts/{accountId}/usage-graphs`, with parameters | F28649 |
| measurements | `/v1/accounts/{accountId}/usage-graphs/latest`, with parameters | F28652 |
| legacy | `/consumers/{org}/connections/{conn}/usage?resolution=...&startDate=...&endDate=...` | F33507 |

Market prices are different from contract rates. A fixed customer's delivery
price must come from the applicable contract, with tariff type, unit, validity
and tax status. The original static assessment did not establish the complete
schema for single/peak/off-peak, gas, feed-in or fixed-fee components.

## Coverage in version 0.1.0

| Information | Original state | Assessment |
| --- | --- | --- |
| Current dynamic electricity price | Sensor existed, response parser mismatched | Probably blocked |
| Next price period | Called next hour; tomorrow not fetched | Incomplete |
| Today's schedule | Attribute existed | Parser and day boundaries needed correction |
| Tomorrow's schedule | Not fetched | Missing |
| Daily average/minimum/maximum | Sensors existed | Needed timezone and duration weighting |
| Current gas price | Sensor existed | Parser and previous-day price missing |
| Contract tariffs | `rates` fetched but not displayed | Missing for users |
| Contract type and dates | `contract` fetched but not displayed | Missing |
| Electricity and gas consumption | No requests or sensors | Missing |
| Grid return | No sensors | Missing |
| Monthly costs | One sensor with limited parser | Unverified |
| Advance payments | Guessed fields | Unverified |
| Invoices | First item's amount and raw list | Incomplete and unverified |
| Meter status | API method unused | Missing |
| Multiple addresses/accounts | First organization/account/connection dominated | Insufficient |
| Energy dashboard | No consumption sensors or imports | Missing |

## Other concrete problems

1. **Prices could lag.** Thirty-minute polling did not publish state at price
   boundaries. Reading the clock inside a property does not trigger a state
   update; local timers were required.
2. **Inconsistent day selection.** System dates, UTC dates and timestamps with
   original offsets could disagree around Dutch midnight. Use a single Dutch
   delivery timezone and compare normalized timestamps.
3. **Unweighted daily average.** A simple average is valid only for equal-length
   intervals. Weight by duration within the local day, including DST changes.
4. **Merged connections.** Only the first electricity/gas connections were used,
   IDs lacked connection identity, and missing `clusterReference` values could
   incorrectly associate unrelated prices.
5. **Permanent gaps after temporary failures.** Sensors were created only at
   setup, so sources discovered after recovery required a reload.
6. **Hidden partial failures.** `_safe` replaced errors with empty dictionaries;
   sensors could become unknown while the coordinator still appeared healthy.
   Track errors, freshness and availability per data group.
7. **Missing authentication recovery.** No reauthentication flow; 401 and 403 were
   treated alike, and timeouts/invalid JSON lacked consistent domain errors.
8. **Unchecked OAuth state.** Generated state was not compared with the pasted
   callback. A bare code prevented validation, and redisplaying the form could
   replace the verifier. Preserve request context and require the full callback.
9. **Assumed invoice order.** Item zero was used without date sorting or paging.
   Truthy fallbacks also discarded valid zero amounts.
10. **Excessive invoice attributes.** Raw invoices could store unnecessary private
    data in recorder. Use a small explicit selection of fields.
11. **No automated tests.** Docker setup alone did not verify login, parsing,
    refresh, recovery, price changes, units or tax semantics.

## Required implementation and validation

Keep each source identifiable with stable connection/account identities. Expose
consumption in kWh, gas in m³ and grid return in kWh, with reporting periods and
reading dates. Supplier readings must not be presented as live P1 data.

Provide daily/monthly/yearly reporting where supplied. Choose real cumulative
meters or imported historical statistics for Energy: a monthly sum must not be
labelled an indefinitely increasing meter. Expose actual contract components,
validity, taxes, fixed costs, forecasts, advances, invoice details and diagnostics.
Fetch dynamic prices for yesterday/today/tomorrow and publish interval changes
locally. Green-energy forecasts and smart charging are optional product features,
not prerequisites for the core energy integration.

First confirm schemas and source associations, then fix price parsing and
lifecycle, expose tariffs, and add consumption, costs and diagnostics. Validate
synthetic or anonymized fixtures for list responses, zero/negative values,
missing data, multiple sources, midnight, early gas hours, DST, token rotation
and outages. Finish with an authorized Home Assistant runtime trial.

No integration files were changed as part of this original assessment. Static
APK evidence was sufficient to reject the old price implementation, but did
not establish every production schema. Subsequent implementation and live
verification are recorded in the implementation notes.
