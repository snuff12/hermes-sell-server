# Hermes Sell

[Install the Hermes plugin](https://github.com/snuff12/hermes-sell) · [Run the local server](docs/quickstart.md)

[84-second working demo](https://github.com/snuff12/hermes-sell/releases/download/v0.1.0/hermes-sell-demo.mp4) · [Catalog submission PR](https://github.com/NousResearch/hermes-agent/pull/152641)

**Turn existing Hermes skills into paid APIs, with separate seller and buyer processes.** The default command runs a local simulation. A separate Base Sepolia path now connects wallet authentication, isolated Hermes execution, payment authorization, and chain reconciliation. All three local outcomes have now been exercised: a real Hermes draft and execution test, an automated browser purchase with a testnet receipt, and an independent Hermes buyer that used its purchased result. See [implementation status](docs/status.md) for the evidence and test-wallet boundary.

| Required outcome | Implemented path |
| --- | --- |
| Turn a skill into a service | Original skill snapshots, a private draft store, Hermes draft import, contract editing, execution tests, and owner-reviewed publication |
| Let a person buy from a browser | EOA wallet connection, SIWX sign-in, an explicit quote, one order-bound USDC signature, status lookup, and recovery |
| Let a separate Hermes buy and use a result | Separate buyer ledger and tools; the human signs in a local browser approval page, then the agent submits the approved purchase |

Mainnet is disabled. External hosting and Cloudflare Tunnel setup are explicitly deferred. The local testnet path does not expose the personal Hermes home or give an agent a wallet signing key.

## Shared service directory

Users install **one Hermes Sell plugin** from the official plugin catalog. The
services they sell are separate listings in our directory; each service does
not need its own Nous catalog PR.

The shared workspace now has **Browse**, **My services**, and **Purchases**.
Owners opt in a tested, published version with **List in directory**. Buyers
search across sellers, open the seller's storefront, or use `service_search`
and a `listing_id` in Hermes. Saved purchase IDs always recover through their
original seller, with a shared buyer budget and separate wallet sessions.

**Register a seller** guides setup and checks a configured local API. The owner
workspace includes **Get started** and **Analytics** for execution counts,
confirmed USDC payments, pending amounts and per-service totals. Public usage
sharing is optional. Buyers can rate a completed purchase from its result or
**Purchases**; testnet reviews require a buyer signature and matching chain
receipt. Purchase history also shows spending and the shared remaining budget.

Start a directory for explicitly selected local sellers:

```sh
.venv/bin/hermes-sell --state .state/directory directory \
  --allow-seller http://127.0.0.1:8765 \
  --allow-seller http://127.0.0.1:8785
```

Open <http://127.0.0.1:8795>. The current local preview has two seller processes
and four listings, clearly labeled simulation or Base Sepolia testnet. Only
public listing metadata is indexed. See the [marketplace guide](docs/marketplace.md)
for a two-seller setup, publication, agent discovery, and the local-only boundary.

## Start the simulation

Requires Python 3.14 and Node.js 22. Run from the source checkout:

```sh
./scripts/setup.sh
.venv/bin/hermes-sell dev
```

- Storefront: <http://127.0.0.1:8765/s/diff-review>
- Seller workspace: <http://127.0.0.1:8766>
- Development admin token: `local-preview-only`. This fixed simulation value is not a secret.
- Stop: press `Ctrl-C` in the server terminal.
- State: `.state/seller.sqlite3`, separate from source updates and web builds.

The simulation returns the first saved output example without executing the skill. It makes no model calls or onchain payments. Its servers bind to loopback and reject proxy requests and requests from other origins.

## Try the simulation flow

1. Enter the admin token in the seller workspace.
2. Select **New service**, load an example skill or paste the original `SKILL.md`, and edit the input/output contract.
3. Select **Save version**, then **Run simulation test**. Review the result and select **Publish locally**.
4. Open the storefront, connect a test session, review a quote, and approve a simulated purchase.
5. Receive the result, refresh the page, and select **Reconnect and restore order** to recover the same order.

`authoring.snapshot()` preserves the original skill and selected static references. A generated public description never replaces the executable skill.

A separate simulation buyer uses its own ledger:

```sh
.venv/bin/hermes-sell --state .state/buyer buyer quote
# Use the purchase_id returned by the quote command.
.venv/bin/hermes-sell --state .state/buyer buyer approve --purchase-id PURCHASE_ID
.venv/bin/hermes-sell --state .state/buyer buyer purchase --purchase-id PURCHASE_ID
.venv/bin/hermes-sell --state .state/buyer buyer status --purchase-id PURCHASE_ID
```

The simulation `approve` command requires the user to enter the order digest in an interactive terminal. Testnet purchases use browser wallet approval instead; agent tools cannot grant approval.

## Local Base Sepolia path

Follow the [local testnet guide](docs/local-testnet.md) to prepare the dedicated Colima runtime, private configuration, seller, and buyer approval gateway. `init-testnet` creates configuration; `testnet-doctor` checks readiness; `testnet` starts the separate seller and owner applications.

| Surface | Default local address |
| --- | --- |
| Testnet storefront | <http://127.0.0.1:8785> |
| Owner workspace | <http://127.0.0.1:8786> |
| Buyer wallet approval | <http://127.0.0.1:8767/connect> |
| Isolated Hermes Runs API | `http://127.0.0.1:18790` |

Only Base Sepolia USDC, x402 v2 exact EIP-3009, and EOA wallets are supported. Results require a validated execution output and chain evidence. The default two-block L2 depth is a testnet soft-confirmation policy, not Ethereum L1 finality.

## Verify

```sh
.venv/bin/python scripts/check_english.py
.venv/bin/python -m pytest -q
npm --prefix web run build
npm --prefix web run test:e2e
npm --prefix web audit
```

The Python suite has 273 passing tests. The browser suite has 38 passing tests, including real deterministic EIP-191/EIP-712 signatures with mocked transport and chain outcomes. It sends no blockchain transaction. Cross-seller tests cover discovery, wallet isolation, approval binding, a shared budget, and original-seller recovery. Registration, review edits/retries, exact analytics and public sharing are also covered. Python x402 2.27.0 and browser x402 2.30.0 share a signed wire-format fixture. Browser dependencies currently have zero audit findings. Actual model and testnet results are recorded separately in the [sanitized integration evidence](docs/verification-2026-11-07.json). The [full review](docs/review-2026-11-07.md) adds read-only recovery checks against actual runs and chain receipts, plus a clean source-install check. The [analytics and review evidence](docs/marketplace-insights-verification-2026-11-07.json) records a live review of an existing testnet purchase without another payment.

## Prepare the catalog plugin

```sh
.venv/bin/python scripts/build_catalog_plugin.py .state/catalog-plugin
```

This creates a standalone source tree with six agent tools and an optional
Desktop SDK launcher for the default local workspaces. It excludes developer
credential importers, runtime runners, private state, and compiled payloads.
Draft generation uses the host `ctx.llm` API. The plugin source is published
separately and its catalog PR is open. Maintainer acceptance is pending;
see [submission preparation](docs/catalog-package.md).

Browser tests use macOS Chrome by default. Set `CHROME_PATH` to a Chromium executable on other systems. Python dependencies are pinned in `requirements.lock`; web dependencies are locked in `web/package-lock.json`.

All first-party code comments, UI text, documentation, examples, and repository metadata must be written in English. The language check rejects Korean text in tracked and unignored project text files and filenames.

## Project layout

```text
src/hermes_sell/  Contracts, seller, buyer, auth, encrypted state, and host tools
web/             Storefront, owner workspace, and buyer approval page
examples/        Two skills and their public service contracts
deployment/      Dedicated Hermes runtime image and policy
scripts/         Local setup and isolated host/runtime checks
tests/           State, authorization, retry, chain, and adapter checks
docs/            Architecture, protocol, status, and local operation
```

[Architecture](docs/architecture.md) · [Protocol](docs/protocol.md) · [Status](docs/status.md) · [Local testnet](docs/local-testnet.md) · [Historical implementation review](docs/review-2026-11-06.md) · [Hermes UI reference](docs/ui-design.md)
