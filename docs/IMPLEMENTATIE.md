# Implementation and verification, November 12, 2026

Version 0.2.0 exposes consumption, grid return, costs, advances, invoices,
contracts, meter status, individual tariff components and optional historical
Energy statistics. Prices and identifiers remain separate per connection or
account. Data from the two API generations is not matched by list order.

Live checks revealed and corrected differences from the original assessment:

- Legacy consumption and costs use `resolution=Days`, dates without a time and
  an exclusive end date. Costs also require `includeEnergyTax=true`.
- Invoices require `invoiceType=AdvancePayment,Settlement,Purchase,Custom`.
- Contract details require `clusterId`; advances use `clusterReference`.
- Contract prices come from
  `GET /v1/energyConsumers/{org}/contracts/{contractId}/prices?priceDate=...`.
  The connection provides the contract ID. The user identified this portal
  request, and the official web client confirmed it.
- Price responses contain `priceComponentType`, `priceUnit`, `price`,
  `priceTaxed`, `vatInclusive`, `vatPercentage` and optional `rangeBegin` and
  `rangeEnd`. Each band has a separate identity.
- Full fixed rates follow the official portal calculation. Peak and off-peak
  rates remain separate sensors; no active dual tariff is inferred from an
  assumed schedule. Feed-in compensation uses `price` as displayed by the
  portal; other VAT-inclusive amounts use `priceTaxed`.
- Next year's prices are fetched in advance if within the contract term. The
  same entities change locally at the year boundary; there are no yearly
  entities. Market-price VAT options do not affect contract rates.
- A legacy account can have valid access to `mijn.vandebron.nl/api` while
  `api.vandebron.tech/users` returns HTTP 401. That service error does not rotate
  tokens or trigger reauthentication for the working legacy API.

Live legacy requests and sensor processing were checked with the authorized
account in the development container, without exposing tokens or personal
values in research output. All three delivery rates matched the supplied portal
screenshot after equivalent rounding. Private responses are not test fixtures.

Automated tests use Home Assistant 2026.11.1 and synthetic values. A separate
check with a temporary recorder database confirmed that Energy statistics
imports also work on Home Assistant 2026.5.2, including repeated imports without
double counting.

English is the default for integration-owned names and documentation. Dutch
translations use Home Assistant's entity translation mechanism. Technical API
identifiers and user-supplied account/address labels are preserved. See the
[README](../README.md) for Energy dashboard inputs and tariff selection.

The subsequent Marketplace-only change raises the supported minimum to Home
Assistant 2026.11. The development container was upgraded to 2026.11.1 and a live
refresh succeeded. Earlier 2026.5.2 results above describe historical checks.
See the [Marketplace assessment](MARKETPLACE.md) for packaging, validation and
publication prerequisites.
