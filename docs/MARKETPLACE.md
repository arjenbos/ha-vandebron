# Marketplace support assessment

Assessed on November 12, 2026 against the official Home Assistant contributor
documentation. This project supports **Home Assistant 2026.11 and later through
the built-in Marketplace**. Standalone HACS is not a supported distribution path.
Local bind mounts are for development only.

## Findings and changes

| Area | Previous state | Marketplace decision |
| --- | --- | --- |
| Minimum version | 2026.5.0, predating Marketplace | Require 2026.11.0 in repository metadata and integration setup |
| Installation | Manual file copying | Marketplace repository link with category `integration`; no standalone HACS instructions |
| Repository metadata | Root `hacs.json` already present | Keep this required filename, update minimum version, identify the Netherlands |
| Integration manifest | Version, config flow and tracker present; no categories or maintainers | Keep domain `vandebron`; add energy price/monitoring categories and the repository owner's handle |
| Translations | Complete English/Dutch files plus duplicate `strings.json` | Keep `translations/en.json` as the English source and `nl.json` as its translation; remove unused `strings.json` |
| Brand | No bundled images | Include the original 512px Vandebron app icon in the integration's `brand/` directory |
| Validation | Local tests only | Add hassfest, the Marketplace catalog validator and integration tests to GitHub Actions |
| Development image | Floating `stable` tag, actually running 2026.5.2 | Pin the development definition to 2026.11.1, matching the test dependencies |
| Releases/catalog | Not published or verified in this session | Document publication prerequisites; do not claim catalog acceptance |

## Why names containing HACS remain

The Marketplace is integrated into Home Assistant Core from 2026.11. It still
reads the catalog from `hacs/default`, requires the root file `hacs.json`, and
uses `hacs/action` for catalog validation. Renaming that file to
`marketplace.json`, removing it or inventing a replacement validator would break
the supported publishing contract. We use the new `marketplace_repository`
My Home Assistant link and do not offer the legacy `hacs_repository` route.

The installed code is still a custom integration in `custom_components/vandebron/`.
Marketplace is the installer and update manager, not a different integration
API. No installer detection or dependency on an integration named `hacs` is
needed. The minimum-version setup check also protects direct development loads
on older Home Assistant versions, since the repository minimum is enforced only
for published releases by Marketplace.

## Packaging and updates

The repository contains one integration. All runtime files and the icon live
inside `custom_components/vandebron/`. Marketplace installs that directory;
research documents, the APK and development configuration are not runtime
requirements. There is no generated ZIP asset or `zip_release` configuration.

English UI strings are maintained in `translations/en.json`, with matching
Dutch keys and placeholders in `translations/nl.json`. The icon is copied
unchanged from `icon.png` in the supplied Vandebron 3.4.15 XAPK; it identifies
the connected service and does not imply that this project is official.

A first install can load immediately because the integration has a config flow.
Updates/removal of loaded Python code require a restart, which Marketplace
requests through Repairs. Tokens stay in the Home Assistant config entry and
statistics in recorder; nothing persistent is written to the integration folder.

## Publication prerequisites

The local work does not publish a repository, create a release or submit to the
catalog. Before announcing availability:

1. Publish the repository at the address used by the manifest and README,
   currently `arjenbos/ha-vandebron`, or update those links and the maintainer
   handle to the actual owner. No Git remote is configured in this checkout.
2. Enable issues and set the repository description and relevant topics. Confirm
   the applicable license and redistribution terms, including the service icon.
3. Run both hassfest and catalog validation without ignored checks, plus tests.
   Remote validation needs the published repository and its GitHub metadata;
   local tests cannot establish these conditions.
4. Publish a full GitHub release whose tag matches `manifest.json` (currently
   `0.2.0`). A Git tag alone is not a release. The 2026.11 minimum in `hacs.json`
   applies to this release; keep later releases consistent with their manifest.
5. Users can install the accessible repository through its Marketplace link
   before catalog inclusion, with a GitHub connection. For default discovery,
   submit a separate pull request to the `integration` list in `hacs/default`
   once the publishing requirements are met. Acceptance is an external review.

The shared repository format also remains readable by older installers. This
project's support policy and minimum Home Assistant version target Marketplace;
there is no supported way to prevent a third-party tool from copying Python
files, and doing so is unnecessary for Marketplace support.

## Official references

- [How the Marketplace works](https://contributors.home-assistant.io/marketplace/how-it-works/)
- [Publish a custom integration](https://contributors.home-assistant.io/marketplace/guides/integrations/)
- [Repository metadata (`hacs.json`)](https://contributors.home-assistant.io/marketplace/reference/hacs-json/)
- [Validation checks](https://contributors.home-assistant.io/marketplace/reference/validation/)
- [Releases and updates](https://contributors.home-assistant.io/marketplace/guides/releases/)
- [Get into the catalog](https://contributors.home-assistant.io/marketplace/guides/catalog/)
- [Integration categories](https://contributors.home-assistant.io/core/integrations/categories/)
- [Brand images](https://contributors.home-assistant.io/core/integrations/brands/)

## Verification in this checkout

All 68 tests pass, including direct checks with Home Assistant 2026.11.1's
Marketplace repository-manifest validators and rejection of older Home Assistant
versions before any API request. Ruff and whitespace checks pass.

The development configuration and database were backed up before upgrading the
container from 2026.5.2 to 2026.11.1. The updated container passes the Marketplace
loadable-manifest check and completes a live Vandebron refresh without integration
or translation errors. This verifies local loading and account access; it is not
an end-to-end download/install from a published Marketplace repository. The
hassfest and catalog GitHub Actions are configured but have not run on GitHub in
this session.
