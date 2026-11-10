# Implementation status · Local checkpoint 2026-11-07, publication update 2026-11-10

Public source publication is complete. [The plugin](https://github.com/snuff12/hermes-sell)
and [server source](https://github.com/snuff12/hermes-sell-server) have public CI,
and [catalog PR #152641](https://github.com/NousResearch/hermes-agent/pull/152641)
is open with a full SHA pin. An 84-second English working demo is available.
Catalog acceptance and final hackathon form submission are separate states;
see [submission status](submission-2026-11-10.md). Local server hosting remains private.

Current checkpoint: **all three local outcomes have completed with actual model and testnet evidence**. The default simulation remains available. Mainnet is disabled. External hosting and Cloudflare Tunnel work are explicitly deferred by the user.

The shared directory and multiple-seller workflow are now implemented locally.
**Browse** searches owner-published services, **My services** lists or withdraws
tested versions, and **Purchases** shows records, results and receipts across
sellers. `service_search` adds discovery to the Hermes tools; quotes can use a
listing ID. Existing purchases route by the origin saved in the buyer ledger.
The current preview indexes four listings from two local seller processes.

Seller registration now has a guided API check and **Get started** flow. Owner
**Analytics** shows usage, confirmed USDC volume, pending/unknown amounts,
per-service totals and receipts. Buyer **Purchases** shows cross-seller spending
and budget. Completed buyers can publish or edit a rating; testnet reviews
require seller attestation, a buyer review-only signature and independent chain
evidence. Public usage sharing is opt-in. See the
[analytics and review evidence](marketplace-insights-verification-2026-11-07.json).

The [marketplace guide](marketplace.md) covers operation and local trust limits.
The [catalog package guide](catalog-package.md) explains the source-only export,
which excludes the developer provider-token importer. A native SDK launcher
opens the browser workspaces; it is not an embedded wallet UI. The candidate
has not been installed into the user's personal Hermes profile. Its published
source has been installed successfully in a fresh disposable Hermes home.

## Implemented

- Original skill and static-reference snapshots, bounded JSON Schema, immutable service revisions, and separation of public contracts from private bundles.
- A private `DraftStore` shared by the owner Hermes plugin and seller management application. **Import Hermes draft** preserves the saved bundle; bundles with reference files cannot be overwritten by the manual editor.
- Separate simulation and testnet commands: `dev`, `init-testnet`, `testnet-doctor`, and `testnet`.
- Dedicated Colima/Docker seller runtime with no host directory mounts, a separate home, an unprivileged user, a read-only root, restricted resources, and disabled execution toolsets.
- Explicit reuse of one authorized model access token. The isolated runtime receives no refresh token, unrelated provider credentials, or personal Hermes home.
- Address-bound SIWX challenges, one-time nonce consumption, EIP-191 verification, and wallet sessions. Browser signing is separate from payment approval.
- Base Sepolia USDC / x402 v2 exact EIP-3009 payments. The signed nonce is `0x` followed by the immutable order digest.
- Encrypted persisted payment payloads, one authorization per order, durable execution intent, fixed run keys, and same-payload retries.
- Chain receipt validation and reconciliation. A successful receipt must contain the expected adjacent `AuthorizationUsed` and `Transfer` pair from the configured USDC contract, with the right nonce, payer, recipient, and amount.
- A separate testnet buyer ledger and browser approval gateway. The wallet signs only in the browser; the buyer process stores encrypted authorization and the agent submits only an approved purchase.
- Hermes-style storefront, owner workspace, and buyer approval page; light/dark themes, narrow layouts, explicit amount/payee/origin review, and recovery without automatic re-signing.
- English-only first-party content and a repository language check.

## Verified

- React/TypeScript production build and 38 browser tests passed.
- Browser tests cover the existing simulation flows plus wallet sign-in, exact payment signatures, explicit rejection, wrong-chain/domain/nonce rejection, wallet changes, same-payload retries, recovery, buyer approval, and Hermes draft import.
- A deterministic expired EOA payment fixture produced with JavaScript x402 2.30.0 and viem 2.57.16 is shared with the Python x402 2.27.0 boundary checks.
- Dependency audit reported zero findings for the web dependency set.
- 273 Python tests passed, covering wallet authentication, encrypted state, runtime policy, order races, buyer approval, restored backups, chain evidence, scoped directory publication, cross-seller approvals, source-only export, registration, analytics and purchase-backed reviews. `pip check` reported no broken requirements. The earlier PyPI check reported no active advisories for the 67 pinned Python packages; no dependencies were changed in this step.
- A temporary clean source checkout installed its dependencies, built the UI, exposed the CLI, and initialized private testnet configuration successfully on Python 3.14.6.

The automated unit and browser suites use test doubles for external outcomes. The separate live integration checks below record actual model and chain evidence; they are not inferred from those fixtures.

## Live integration evidence

| Check | Current evidence |
| --- | --- |
| Isolated model execution | Passed with the original frozen Diff Review skill in the dedicated container. The actual output identified token logging; the owner execution test completed in 2.917 seconds. |
| Owner `service_draft` through a real Hermes host | Passed. Actual `ctx.llm` drafts were generated for Diff Review and Release Summary. Both original bundles were imported, passed real execution tests, and were reviewed and published locally. Diff Review completed in 2.917 seconds; Release Summary completed in 2.503 seconds. |
| Browser purchase with an actual testnet settlement | Passed: actual model output, a 0.10 USDC Base Sepolia transfer, verified receipt, and reload recovery of the same order. One payment signature and two login signatures were observed. |
| Independent Hermes purchase followed by use of the purchased result | Passed in a separate clean Hermes host. It called `service_purchase`, then `service_status` twice, completed the run, and used the purchased finding to recommend logging a session event instead of the session token. It did not create a new quote or sign payment. |
| Normal seller restart with existing settled orders | Passed. After stopping and restarting the seller, the same buyer retrieved the original settled result. Both run IDs and the counts of execution and settlement intents remained unchanged: two each. |
| Injected response loss and old-backup recovery against actual runtime and chain state | Passed six cases on temporary ledger copies: two paid orders, each tested with a lost settlement response, lost admission response, and pre-admission backup. Original runs and results were restored with zero new runs or payments. This was controlled fault injection, not a live mid-settlement process kill. |
| External demo | Deferred by the user. No public hosting or tunnel is part of this checkpoint. |
| Review and business analytics | An existing browser purchase was restored and reviewed through the live UI with one login signature, one review-only signature and zero payment signatures. The directory independently verified its original chain receipt. Seller analytics matched two executions and 0.20 USDC; buyer analytics matched one payment and 0.10 USDC. |

The [sanitized integration evidence](verification-2026-11-07.json) records the runtime version, two owner test reports, actual receipts, the independent Hermes tool sequence and answer, and the normal restart check.

The [full review](review-2026-11-07.md) and [review evidence](review-verification-2026-11-07.json) record the additional fixes, current-model check, browser recovery after loading the fixes, and read-only recovery exercises. Wallet disconnect now invalidates in-flight sign-ins. Persistent signing intent and a Web Lock prevent repeated signatures across tabs, while an unsigned quote remains usable after reload.

The live browser check used an automated EIP-1193 bridge to a Node-held encrypted test wallet. It did not demonstrate a person accepting a request in a wallet extension. Order: `6832feb4-db66-49ad-8d48-f104ac4fdba1`. Transaction: `0x238c81836b7db4e45bf6bc3be1d1203f966d6209ba69901cfa3e41596f8adeab`. Local browser evidence is retained in `.state/testnet/browser-purchase-check.json` and `.state/testnet/screens/browser-result.png`. The independent Hermes check is recorded in `.state/testnet/hermes-buyer-check.json`. Private state is excluded from Git.

`testnet-doctor` checks container isolation, runtime API policy, facilitator support, and the RPC network. Its success alone does not demonstrate a model completion or paid end-to-end flow.

## Confirmation and recovery limits

The default chain policy requires a successful receipt in the canonical RPC block and at least two Base Sepolia L2 blocks of depth, counting the inclusion block. This is a testnet soft confirmation. It does not establish Ethereum L1 finality or eliminate later reorg risk.

Unknown execution and settlement outcomes remain blocked until evidence resolves them. A lost response never authorizes a new nonce or run. The browser requires a fresh original-order lookup after the prior authorization can no longer be used before enabling a replacement for an unadmitted signed order.

Normal restart preserves the ledger. A restored old backup requires the recovery lock and reconciliation. Losing the ledger, encryption key, or runtime idempotency state can prevent recovery; do not replace them to bypass an unknown outcome.

Locked snapshots expose uncertain states as unknown, so buyers retain their spending reservation. Status and owner recovery remain available during dependency outages. Exact runtime identity lookup and receipt hints can recover retained evidence without another execution or payment. A scan beyond 5,000 blocks requires an owner-supplied transaction hash, verified against the complete original order. The restore lock is not removed automatically; an old backup can omit later orders entirely.

## Follow-up validation

1. If broader resilience certification is needed, exercise an actual process kill during a new testnet settlement. The completed review used fault-injected copies and actual read-only remote evidence.
2. Verify acceptance through a human-operated wallet extension; the successful automated checks used the EIP-1193 test bridge.
3. Validate on another device or architecture. Clean source installation on this machine passed; wheel-only installation and an embedded native marketplace remain outside this release. The SDK launcher opens browser workspaces. Public federation and hosting remain deferred.

The [local testnet guide](local-testnet.md) documents the implemented operation path. The [2026-11-06 review](review-2026-11-06.md) and [final design review](final-review.md) remain historical records.
