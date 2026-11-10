# Shared service marketplace

The official Hermes catalog distributes one plugin. The Hermes Sell directory
indexes the APIs that users publish with that plugin. Listing an API does not
require a new Nous PR or installing the seller's skill on a buyer's machine.

## Start two local sellers

Use separate terminals and private state directories. Do not start another
process on a port already used by the existing preview.

```sh
.venv/bin/hermes-sell --state .state/alice dev --port 8765 --admin-port 8766
.venv/bin/hermes-sell --state .state/river dev --port 8775 --admin-port 8776
.venv/bin/hermes-sell --state .state/directory directory \
  --allow-seller http://127.0.0.1:8765 \
  --allow-seller http://127.0.0.1:8775 \
  --owner-origin http://127.0.0.1:8766
```

These two seller commands are simulations. For actual Hermes execution and
Base Sepolia payments, prepare a separate testnet seller as described in
[local-testnet.md](local-testnet.md), then include its exact origin in
`--allow-seller`. Directory mode labels never turn a simulation into a real run.
Testnet configuration accepts `directory_origin`; its default is
`http://127.0.0.1:8795`.

## Publish and discover

1. Open `http://127.0.0.1:8795/sell`. Select your configured seller API and choose **Check seller**. Enter the owner URL printed by that seller and open its private workspace.
2. Enter the seller's admin token there. **Get started** explains how to create/import, test, review and publish a service.
3. In **Services**, select **List in directory** for that published version. **Share usage and settled volume publicly** is off by default; enable it before listing if desired.
4. Open `http://127.0.0.1:8795`. Search by name or seller, or filter payment mode.
5. Open a service to review the seller, payee, price, version, examples and reviews.
6. **Open storefront** visits that seller directly. Its current contract and exact quote must be reviewed again before approval.
7. **Unlist** removes discovery publication. The original API URL and existing purchases keep their original contract.

The seller stores opt-in publication locally. The directory fetches
`/.well-known/hermes-sell/listings/{slug}`, validates its directory scope and
public contract, and stores metadata. Public registration cannot supply arbitrary
descriptions or replace another seller's contract. A version change withdraws
the old listing until the owner explicitly lists the new version.

The background monitor checks registered sellers every 30 seconds. An online
record older than 90 seconds becomes stale. Failed health checks show offline;
withdrawn publications disappear from search. Buyers verify the live revision
before creating a quote, so a stale index cannot silently select a new price.

## Buy with a separate Hermes

Configure the agent and buyer gateway to use the same directory, ledger, key,
mode and approval origin. `buyer_state_dir` must be explicitly configured.

```yaml
role: buyer
directory_origin: http://127.0.0.1:8795
buyer_mode: testnet
buyer_origin: http://127.0.0.1:8785
buyer_state_dir: /absolute/path/to/hermes-sell/.state/buyer-testnet
buyer_vault_key_file: /absolute/path/to/hermes-sell/.state/buyer-testnet/authorization.key
buyer_approval_origin: http://127.0.0.1:8767
```

```sh
.venv/bin/hermes-sell buyer-gateway --directory-origin http://127.0.0.1:8795
```

1. `service_search` returns listing IDs in the configured buyer mode.
2. `service_inspect` accepts a `listing_id`, or a slug at the configured default seller.
3. `service_quote` accepts that listing ID and input. A changed live revision is rejected before creating a quote.
4. If needed, open the returned connection URL. Wallet authentication is bound to that seller. Retry the same listing after connecting.
5. Open the purchase's approval URL, review the exact quote, and sign with the wallet.
6. Ask Hermes to call `service_purchase`, then `service_status`, using the saved purchase ID.

Purchase recovery reads its origin from the ledger rather than the current
default seller. Auth sessions are separate per origin. A signature cannot be
moved to a different seller or order. Spending reservations include all sellers,
including unknown outcomes. Open `http://127.0.0.1:8767/purchases` for history,
results and receipts. Refreshing status does not submit another purchase.

## Ratings and reviews

After a completed, settled purchase, choose **Rate this service** below the
result, or from the buyer's **Purchases** page. Select 1-5 stars and optionally
write a comment. Rating and comment become public, with a shortened reviewer
wallet address for testnet purchases. One buyer has one vote per service;
publishing again updates that vote rather than adding another rating.

A testnet review requires three checks: a compact Ed25519 purchase attestation
from the original seller, a separate EIP-191 signature from the original buyer,
and an independently checked Base Sepolia USDC receipt with the exact payer,
recipient, amount and authorization nonce. The review signature authorizes no
payment. The directory receives no order input, result, session token or model
credential. Wallet signatures are retained only as hashes for retry matching.
An uncertain response retries the same review signature. A later review cannot
be overwritten by an older challenge.

Simulation ratings are labeled **Simulation review**. They have no wallet or
chain verification and never count as verified testnet purchases. Ratings from
different payment modes remain separate. Reviews record the purchased revision;
the aggregate is for that seller origin and service slug across revisions.
The seller cannot edit or delete buyer reviews through its management API.

These checks establish a purchase, not the reviewer's independence or the
quality of the result. Separate wallets can still collude. Seller-issued
attestations establish the association between a transfer and a service; chain
evidence independently establishes the transfer. The current preview has no
moderation or dispute workflow.

## Usage and payment dashboards

The owner's **Analytics** tab shows 7-day, 30-day and all-time totals:

- Quotes, executions started, paid requests and distinct paying customers.
- Confirmed settled volume, pending confirmation, failed and unknown outcomes.
- Daily executions, observed execution time, per-service totals and up to 100 confirmed receipts.

Executions come from durable dispatch events, so HTTP retries and status polls
do not increase usage. Confirmed volume is counted once per settled order on
the date the ledger first observed confirmation, rather than the quote date.
Amounts use exact integer USDC units; pending/unknown approvals are excluded
from settled volume. Pending and unknown cards always cover all time. The
all-time chart shows only the latest 30 days. A restored ledger is marked as
potentially incomplete; payments with missing timestamps appear only in
all-time totals. Values are gross transfers, not model costs or profit.

The buyer's **Purchases** page shows all-time settled spending, unresolved
approved amounts, committed/available shared budget, and spending by seller.
Viewing or refreshing analytics creates no payment, execution or approval.

Private owner analytics require the owner token. Public usage is a separate
opt-in for each listed version. If enabled, only aggregate executions, paid
requests and settled volume are indexed. The directory labels these values
**Seller-reported usage**, including their simulation/testnet mode. It does
not independently audit every aggregate. Public usage is suppressed under a
seller recovery lock and updates on directory refresh.

## Current boundary

All servers remain loopback-only. The directory fetches only explicitly allowed
loopback seller origins, disables redirects and ignores ambient proxies. It is
not an open Internet registry. Public HTTPS enrollment, ownership/DNS checks,
abuse handling and deployment remain a separate publication step. The original
Cloudflare Tunnel plan is still deferred. Merely changing a bind address does
not enable public federation.

The existing live preview indexes two local seller processes and four services,
with simulation and testnet badges. Automated tests also exercise two isolated
testnet seller engines, wallet authentication, approval rejection across sellers,
a shared spending limit, and result recovery without a new payment.
