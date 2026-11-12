# Vandebron Thuis for Home Assistant

Unofficial Vandebron integration, based on the `nl.vandebron.thuis` Android app,
version 3.4.15. Requires **Home Assistant 2026.11 or later**.

[![Open Vandebron in the Home Assistant Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=arjenbos&repository=ha-vandebron&category=integration)

English is the default language for the integration and documentation. A Dutch
translation is included and follows Home Assistant's language setting. API field
names, technical identifiers and labels supplied by Vandebron are preserved.

## Available information

The integration creates separate devices for customers, supply addresses,
connections and energy accounts. Legacy connections and newer accounts remain
separate because the API does not always provide a reliable link between them.
Choose one source when both represent the same supply.

| Information | Sensors and attributes |
| --- | --- |
| Fixed and variable contract rates | Electricity single, peak and off-peak rates and gas rate, including VAT and domestic energy tax; the same sensors update when a new rate takes effect |
| Dynamic prices | Current and next price period; duration-weighted average, minimum and maximum today |
| Price schedules | Yesterday, today and tomorrow as attributes, once published by Vandebron |
| Tariff components | Separate sensors for supplied components and tax bands, with units, validity and VAT information |
| Consumption and grid return | Today, yesterday, this month and this year; peak/off-peak where supplied |
| Costs | Monthly and yearly fixed/variable costs and feed-in revenue; newer accounts can also expose tax-inclusive and tax-exclusive totals |
| Forecasts | Expected monthly consumption and costs, and expected totals up to now |
| Advance payments | Current monthly advance and effective date, per supply address |
| Invoices | Latest amount, date and payment status; sum of supplied outstanding amounts |
| Contracts | Type and validity dates, per account/market or legacy customer/connection |
| Diagnostics | Meter status, connection details, last reading date, errors and last successful fetch per data group |
| Energy dashboard | Optional historical daily consumption, grid return and gas statistics |

Unavailable data stays unavailable rather than becoming zero. Supplier readings
can arrive late; this integration does not provide live P1 meter readings.

## Installation and sign-in

This project supports the **built-in Home Assistant Marketplace only**. Separate
HACS installations and Home Assistant versions before 2026.11 are not supported.
The repository is currently under development; a Marketplace link is not a claim
that it is published or included in the default catalog.

1. Open the Marketplace link above in Home Assistant 2026.11 or later. If the
   repository is not in the catalog, connect GitHub when prompted and add it as
   a custom repository of type **Integration**, then install it. This requires
   the GitHub repository to be published and accessible to your GitHub account.
2. Add **Vandebron Thuis** under **Settings → Devices & services**. A first install
   with a config flow can load immediately. For updates to an already loaded
   integration, follow the Marketplace repair that requests a restart.
3. Try email and password. If Vandebron does not allow password login for this
   client, choose browser sign-in.
4. For browser sign-in, open the provided link and sign in. Copy the **complete**
   redirect URL starting with `nl.vandebron.thuis.auth://oauth?…`, including both
   `code` and `state`, and paste it into Home Assistant. A bare code is rejected.

Your browser may hand the native redirect to the Vandebron app. Whether you can
copy it depends on your browser and device; not every browser supports this.
Refresh tokens are stored in the config entry and updated after rotation.
Expired or revoked access triggers sign-in again with the same account.

## Contract rates and market prices

**Market prices are not your fixed contract rates.** Fixed and variable contract
rates are fetched automatically from:

```text
/v1/energyConsumers/{organizationId}/contracts/{contractId}/prices?priceDate=YYYY-MM-DD
```

For a fixed dual-tariff contract, use **Electricity peak rate** and **Electricity
off-peak rate**. The Dutch names are **Stroomtarief normaal** and **Stroomtarief
dal**. **Electricity single rate** is the single-rate alternative returned by
Vandebron; it does not replace your dual tariffs. The generic **Electricity rate**
sensor is disabled by default and unavailable for fixed dual tariffs because the
API does not supply an authoritative active meter tariff indicator.

Full delivery rates follow the customer portal calculation: supply component,
lowest domestic energy-tax band and renewable energy surcharge, using the API's
VAT-inclusive amounts. Gas additionally includes the regional surcharge and
local investment premium. Fixed fees and other tax bands are separate sensors.
Feed-in compensation follows the portal's untaxed `price` field.

Future rates are fetched in advance where available. **The existing sensor
changes value on the effective date**, including January 1; there are no separate
sensors for each year. Component attributes can include an `upcoming` price.

Market price sensors are populated only for dynamic contracts. An unknown
contract type is never assumed to be dynamic. VAT and extra-cost options affect
market prices only, not contract rates. Enable both if you want the supplied
VAT, purchase fee and energy tax included in dynamic prices.

## Energy dashboard: what to select

### Using Vandebron's historical readings

Enable **Import daily totals for the Energy Dashboard** in the integration
options. Then open **Settings → Dashboards → Energy** and select the imported
statistics for your supply. These are **statistics**, not ordinary sensor
entities, and their IDs start with `vandebron:`.

| Energy dashboard field | Select | Purpose |
| --- | --- | --- |
| Electricity grid → Grid consumption | `vandebron:…_electricity_usage` | Electricity taken from the grid, in kWh |
| Electricity grid → Return to grid | `vandebron:…_electricity_redelivery` | Electricity exported to the grid, in kWh |
| Gas consumption | `vandebron:…_gas_usage` | Gas consumed, in m³ |
| Solar production | No source provided by this integration | Grid export does not measure total solar production |
| Costs / compensation for these imported totals | **Do not track costs** | Historical cost statistics and historical tariff matching are not imported |

For example, IDs look like:

```text
vandebron:account_a_123_electricity_usage
vandebron:account_a_123_electricity_redelivery
vandebron:account_a_123_gas_usage
vandebron:connection_123_456_electricity_usage
```

The middle section identifies the account or connection. Choose the matching
address and fuel. **Do not add both legacy and account statistics for the same
supply**, which would count it twice.

Do not select **Electricity consumption this month**, **Electricity consumption
this year**, their gas equivalents or daily summary sensors as cumulative meter
inputs. These sensors describe a reporting period and are not lifetime meters.
The imported statistics are the intended inputs for supplier history.

Available completed daily readings are imported from the start of the current
year. Each daily total is assigned to the final hour of the Dutch supply day;
the chart's hourly profile is therefore not a measured hourly profile. Repeated
imports replace the same periods and process corrections without double
counting, preserving sums from before the year. Disabling or removing the
integration does not delete previously imported long-term statistics.

### Using a live meter with Vandebron prices

If another integration supplies cumulative P1 meter readings, you can use those
readings for energy and Vandebron sensors for the **current price** option:

| Your meter / contract | Price sensor | Purpose |
| --- | --- | --- |
| Fixed/variable single tariff | **Electricity single rate** (`contract_price_base`) | Full electricity delivery price in €/kWh |
| Fixed/variable dual tariff, peak counter | **Electricity peak rate** (`contract_price_peak`) | Full normal/peak delivery price in €/kWh |
| Fixed/variable dual tariff, off-peak counter | **Electricity off-peak rate** (`contract_price_offpeak`) | Full off-peak delivery price in €/kWh |
| Dynamic electricity contract | **Electricity rate** (`electricity_price_now`) | Current contract price period in €/kWh, with the selected tax options |
| Gas meter | **Gas rate** (`gas_price_now`) | Current gas delivery price in €/m³ |

For **your fixed dual-tariff contract**, add the two P1 import counters separately
and match each to its peak or off-peak price. Verify the counter mapping in your
meter integration; do not infer it from counter numbers. The combined historical
Vandebron import cannot be split into two tariffs by selecting a price sensor.

Names above omit the device/address prefix. Keys in parentheses identify the
metric within this integration; actual `sensor.…` IDs depend on the device name
and Home Assistant registry. New IDs use the English names. You can inspect an
entity's ID in its settings.

Current prices do not reconstruct historical bills. Fixed supply fees, network
costs, tax credits, net metering and feed-in fees are not accounted for by a
simple €/kWh multiplication. Individual **supply components** exclude the other
parts of the full delivery rate, so use the full rate sensors listed above.
Feed-in compensation components are available for inspection, but select one
for cost tracking only if its meaning matches your contract and settlement
method. Monthly advances, invoices and fixed yearly fees are not per-kWh prices
or cumulative energy meters.

## Other sensors and what they mean

| Sensors | Use |
| --- | --- |
| Electricity/gas consumption today, yesterday, this month, this year | Supplier reporting totals; may be delayed |
| Electricity peak/off-peak consumption this month/year | Split consumption where supplied by the API |
| Electricity returned to grid | Grid export; not total solar production |
| Fixed costs, variable costs, feed-in revenue this month/year | Supplier cost breakdown for the displayed period |
| Total costs including/excluding tax, gas/electricity costs | New-account cost breakdown where available |
| Electricity/gas/cost forecasts | Current month's actual and expected totals |
| Monthly advance amount | Your payment instalment, not measured energy cost |
| Last invoice amount/status, outstanding invoice amount | Billing information; not an energy meter |
| Contract, contract type, contract rates | Contract metadata and available rate count |
| Supply, feed-in, tax, credit and fee components | Individual contract price components and bands; inspect their units and validity |
| Data status, meter status, connection, customer, last reading date | Diagnostics and source identification |

## Updates and price attributes

Dynamic prices are polled every 30 minutes per connection for yesterday, today
and tomorrow. Local timers publish new values at interval boundaries. Intervals
need not be one hour; Dutch day boundaries and daylight-saving changes are
handled. Gas uses the valid previous-day price before its price transition,
according to `startDate` and `endDate`.

Consumption, costs, advances and invoices are fetched every two hours. Account
and connection metadata, contracts, rates and meter status are fetched every six
hours. Failed calls are retried at the next poll. Errors affect the corresponding
data group; other groups continue working. Cached market intervals remain valid
until their end time.

Dynamic price attributes include `prices` (today), `yesterday`, `today` and
`tomorrow`. Each interval has `start`, `end` and `price`, suitable for chart cards
such as ApexCharts. Attributes also describe tax options and fetch status.

## Development and verification

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check custom_components tests
```

Tests use a local mock API and Home Assistant 2026.11.1. They cover response
shapes, token rotation, reauthentication, multiple connections, error recovery,
price transitions, daylight-saving changes, units, zero/negative amounts and
statistics imports. Measurement fixtures follow the app mock; contract fixtures
use synthetic values with a live-verified schema. Fixed delivery totals were
compared with the customer portal. Statistics imports were also verified with
a temporary recorder database. An earlier check on Home Assistant 2026.5.2 is
historical verification, not a supported deployment target. The development
container now targets Home Assistant 2026.11.1.

For local development (a bind mount, not a supported end-user installation):

```bash
docker compose up   # http://localhost:8123
docker compose down
```

Marketplace packaging, release prerequisites and the analysis of this change
are documented in [Marketplace support](docs/MARKETPLACE.md). The root
`hacs.json` and the `hacs/action` CI validator keep their upstream names because
the built-in Marketplace still requires them; they do not indicate support for
a separate HACS installation. English translations ship directly in
`translations/en.json`; custom integrations do not use `strings.json`.

See the [original assessment](docs/ANALYSE.md),
[implementation notes](docs/IMPLEMENTATIE.md) and [APK evidence](docs/apk-evidence.txt).
The analysis describes version 0.1.0; missing features in that historical document
are addressed by the current implementation. Legacy consumption, costs,
contracts and invoices were checked against an authorized live account. Newer
account services are not accessible to every legacy account; their HTTP 401
responses do not block legacy data. This is an undocumented API and may change.
Downloadable diagnostics exclude tokens, addresses, customer numbers, invoice
details and readings.
