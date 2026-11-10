# Catalog submission package

The source checkout includes development, isolated runtime and verification
tools. The catalog candidate is a separate allowlisted source export:

```sh
.venv/bin/python scripts/build_catalog_plugin.py .state/catalog-plugin
```

Choose a new destination. Existing exports are never overwritten. The exporter
includes the Python plugin and its shared contract/buyer modules, `plugin.yaml`,
the buy-service skill, a minimal dependency declaration, a disclosure README,
and `desktop/plugin.js`. It excludes server runners, Docker tooling, developer
credential importers, personal state and compiled assets. The draft tool uses
the host `ctx.llm` interface. No provider credential store is read by the candidate.

## What installation provides

- Six tools: search, draft, inspect, quote, purchase and status.
- Listing-ID selection across seller origins, with exact live revision checks.
- Original-origin purchase recovery, signer-free approval and a shared budget.
- An optional Desktop SDK page and sidebar row with **Browse**, **My services**,
  and **Purchases** launch buttons. They open the default local browser addresses.

Seller APIs, the directory and the wallet gateway are separately operated
services. Installing the plugin does not start background servers, expose a
machine, approve transactions, or install the seller's skill. The existing
runtime credential-copy script remains a developer-only helper outside the
export, and is not a provider-auth route offered by this plugin.

## Verification and admission

Run the official check against the exact source tree that will be committed:

```sh
hermes plugins validate /absolute/path/to/catalog-plugin --install-deps
```

The candidate passed validation in a disposable pinned Hermes v0.23.0 container,
with no personal home or credential volumes. The official validator checks the
manifest, capability registration, dependencies, security scan, core override
and Desktop SDK surface. The dependency check also installs the four declared
requirements against the installed Hermes core constraints before importing the
payment/auth modules. See the [verification record](marketplace-verification-2026-11-07.json)
for the actual outcome. All 16 checks passed, the security scan was safe, and
there were zero warnings. This is not maintainer admission.

The export must be put in a public Git repository before a catalog entry can
pin its real 40-character commit SHA. That publication and the catalog PR have
not happened; external publication remains deferred. A successful local
validator is not maintainer approval or automatic catalog inclusion.

## References

- [Official catalog submission rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
- [Desktop Plugin SDK](https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk)
