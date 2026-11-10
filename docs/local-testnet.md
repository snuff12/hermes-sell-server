# Local Base Sepolia guide

This guide runs the implemented testnet path on one machine. The default `hermes-sell dev` command remains a simulation. Mainnet is disabled, and external hosting and Cloudflare Tunnel setup are explicitly deferred. See [status.md](status.md) for completed integration evidence and remaining checks.

## Boundaries and addresses

| Process | Default address | Private state |
| --- | --- | --- |
| Seller API and storefront | `http://127.0.0.1:8785` | `.state/testnet/seller.sqlite3` |
| Owner workspace | `http://127.0.0.1:8786` | Private owner token and shared draft directory |
| Buyer approval gateway | `http://127.0.0.1:8767` | `.state/buyer-testnet/` |
| Dedicated Hermes Runs API | `http://127.0.0.1:18790` | Dedicated container volumes |

The seller runtime uses the `colima-hermes-sell` Docker context. It has no host directory mounts and does not share the personal Hermes home, memory, browser profile, or wallet. Its model connection receives one explicitly authorized access token, with no refresh token or unrelated provider credentials. Model-provider network access remains necessary; this is not an offline runtime.

The buyer process has no wallet signing key. Its encryption key protects stored sessions and signed authorizations; it is not a wallet key. Human wallet approval and agent submission remain separate actions.

## Prepare the runtime

Use the source checkout, Python 3.14, Node.js 22, Docker CLI, and Colima. Run commands from the repository root.

```sh
./scripts/setup.sh
.venv/bin/hermes-sell init-testnet
```

Initialization creates private configuration, a vault key, and a random owner token under `.state/testnet/`. It does not start sales or print credentials. Existing configuration is not overwritten.

Create a dedicated Colima profile without host mounts. This command also leaves the current default Docker context unchanged:

```sh
colima start hermes-sell --runtime docker --cpus 2 --memory 4 --mount none --activate=false
.venv/bin/python scripts/hermes_runtime.py build
```

The runtime image uses the pinned Hermes base in `deployment/hermes/Dockerfile`. Its fixed policy disables tools, MCP servers, project plugins, memory, and user-profile loading for execution. The container runs as UID 10000, uses a read-only root, drops capabilities, and mounts only dedicated state and read-only credential volumes.

Select only the model credential store whose reuse the owner has authorized. Pass its path, never its token value:

```sh
.venv/bin/python scripts/hermes_runtime.py start \
  --codex-auth-store /absolute/path/to/authorized/auth.json
.venv/bin/python scripts/hermes_runtime.py inspect
```

The importer reads a private file owned by the current user and copies one current `openai-codex` access token. The token must have at least ten minutes remaining. It does not copy refresh tokens, mutate the original credential store, or mount the original home. The runtime API bearer token is a separate private file at `.state/testnet/runtime-token`.

## Check and start the seller

```sh
.venv/bin/hermes-sell testnet-doctor
.venv/bin/hermes-sell testnet
```

`testnet-doctor` checks the isolated container, runtime API authentication and idempotency policy, disabled tools, facilitator support, and Base Sepolia RPC. It does not itself prove that a model completion or payment succeeded. If it reports `ready: false`, correct the named integration before starting sales. The server can still start for existing-order reads and authenticated owner recovery; readiness gates block new execution and settlement until dependencies recover.

The seller command uses `.state/testnet/config.json` by default. A custom configuration can be selected with `--config`. This release requires an explicit loopback seller origin and a separate owner port. It uses the official public testnet facilitator and Base Sepolia USDC.

Open the owner workspace at <http://127.0.0.1:8786>. Enter the private value from `.state/testnet/owner-token` in **Admin token** using a local secret viewer. Do not print the value or copy it into documentation, command arguments, screenshots, or source control.

## Publish a skill

A dedicated owner Hermes host loads this plugin with these plugin settings:

```yaml
role: owner
draft_dir: /absolute/path/to/hermes-sell/.state/testnet/drafts
```

The `draft_dir` must be the same private directory used by the seller configuration. The `service_draft` tool snapshots the selected original `SKILL.md` and static references, asks the host LLM for a public contract, validates it, and saves the bundle and contract under a draft ID.

1. Ask the owner Hermes to run `service_draft` for the selected skill and public input/output example.
2. In the owner workspace, open **Import Hermes draft**.
3. Enter the returned draft ID, URL slug, USDC price, and recipient address. Select **Import saved draft**.
4. Select **Run execution test** and review the actual output. Do not publish a failed model or schema test.
5. Confirm review, then select **Publish service**.

Bundles with reference files remain read-only in the manual editor. Import a new draft to change that bundle. A generated description never replaces the original skill. Manual single-file services can also use **New service** and **Save version** before testing.

For an isolated plugin registry/Doctor check, the repository includes `scripts/check_hermes_host.py`. It requires an explicitly selected clean official source tree and matching dependency directory:

```sh
.venv/bin/python scripts/check_hermes_host.py doctor \
  --hermes-source /absolute/path/to/clean/hermes-source \
  --hermes-site-packages /absolute/path/to/hermes/site-packages
```

This helper uses a temporary Hermes home. Its `draft` and `buyer-followup` actions require a separately selected authorized model auth store; consult `--help` for their input paths. They do not load or modify the personal Hermes installation.

## Buy through the storefront

Use an EOA browser wallet on Base Sepolia with testnet USDC. The configured USDC address is `0x036CbD53842c5426634e7929541eC2318f3dCF7e` on chain ID `84532`.

1. Open <http://127.0.0.1:8785> and select the published service.
2. Select **Connect wallet**. The SIWX signature proves ownership and authorizes no payment.
3. Enter the input and select **Review quote**.
4. Review the exact amount, complete payee address, seller origin, wallet, order digest, and expiry.
5. Select **Approve … on testnet** and approve that EIP-712 request in the wallet.
6. Wait for the result. A saved execution result remains hidden until chain settlement is confirmed.

The signed nonce is fixed by the order digest. **Retry same approval** reuses the same signature. After reload, use **Reconnect and restore order**; the page does not create another payment signature. Switching wallet account or network invalidates the session.

## Buy through a separate Hermes agent

Start the gateway in a second terminal:

```sh
.venv/bin/hermes-sell buyer-gateway --origin http://127.0.0.1:8785
```

Open <http://127.0.0.1:8767/connect> and connect the browser wallet. This signs the seller's SIWX challenge while keeping the seller bearer token inside the buyer process.

Configure the separate buyer Hermes with the same private state and vault paths as the gateway:

```yaml
role: buyer
buyer_mode: testnet
buyer_origin: http://127.0.0.1:8785
buyer_state_dir: /absolute/path/to/hermes-sell/.state/buyer-testnet
buyer_vault_key_file: /absolute/path/to/hermes-sell/.state/buyer-testnet/authorization.key
buyer_approval_origin: http://127.0.0.1:8767
```

The agent uses `service_inspect` and `service_quote`. Open the returned approval URL, review the immutable quote, and select **Approve … for agent**. The gateway saves the signed authorization but does not submit a purchase. Ask the agent to call `service_purchase` with that same purchase ID, then `service_status`, and use the returned result in its answer.

The CLI exercises the same separate buyer ledger:

```sh
.venv/bin/hermes-sell --state .state/buyer-testnet buyer quote \
  --mode testnet --origin http://127.0.0.1:8785 --slug diff-review
# Open the returned approval_url and approve with the browser wallet first.
.venv/bin/hermes-sell --state .state/buyer-testnet buyer purchase \
  --mode testnet --origin http://127.0.0.1:8785 --purchase-id PURCHASE_ID
.venv/bin/hermes-sell --state .state/buyer-testnet buyer status \
  --mode testnet --origin http://127.0.0.1:8785 --purchase-id PURCHASE_ID
```

There is no testnet terminal approval or agent approval flag. The default buyer spending limit is 1 USDC across reserved purchases. The approval page shows the configured limit.

## Read the settlement evidence

The facilitator response alone does not release the result. The seller checks a successful receipt and a canonical block hash through Base Sepolia RPC. In that receipt, the expected USDC `AuthorizationUsed` event must immediately precede the matching `Transfer`, binding nonce, payer, recipient, and amount.

The default depth is two L2 blocks, counting the inclusion block. This is a testnet soft confirmation, not Ethereum L1 finality. A later reorg is still possible. Mainnet remains disabled.

The owner endpoint `POST /admin/orders/{id}/reconcile` can request read-only execution and payment reconciliation. The worker also checks eligible unknown executions and settlements. Reconciliation never re-signs, submits another run, or resubmits payment. Missing evidence or an incomplete block scan remains unknown.

For payments outside the 5,000-block automatic scan, send an optional JSON body containing the full transaction hash: `{"transaction":"0x…"}`. The server verifies the exact receipt against the original order before accepting this hint. A wrong or unconfirmed transaction leaves the order unresolved.

## Expired model access and restart

The isolated runtime cannot refresh the imported model access token. Renew model access through the original authorized sign-in flow, then explicitly import only the new current access token. Never copy a refresh token or mount the personal home to work around expiry.

For a planned renewal:

1. Stop admitting work and let known in-flight orders finish. Preserve any unknown order for reconciliation.
2. Stop the seller with `Ctrl-C` in its terminal.
3. Stop and remove only the named runtime container, preserving its dedicated volumes:

```sh
docker --context colima-hermes-sell stop hermes-sell-runtime
docker --context colima-hermes-sell rm hermes-sell-runtime
.venv/bin/python scripts/hermes_runtime.py start \
  --codex-auth-store /absolute/path/to/authorized/auth.json
.venv/bin/hermes-sell testnet-doctor
.venv/bin/hermes-sell testnet
```

The existing runtime bearer-token file, seller ledger, vault key, and Docker state volume must remain in place. The start helper refreshes the access-only credential volume and preserves the runtime authentication generation. Run an owner execution test before accepting new purchases. This test creates no buyer payment; model-provider usage still applies.

A forced interruption can leave execution or payment unknown. Recover using the original order, run key, and authorization. Do not delete state or create a replacement signature to clear an error. If restoring an old seller backup, apply the recovery lock before serving it:

```sh
.venv/bin/hermes-sell --state .state/testnet recovery-lock
```

Start `testnet` with that locked ledger to access existing orders and the owner recovery endpoint. Unconfirmed snapshot states are shown as unknown, even if the backup predates admission. A lost run ID is looked up in the pinned container's admission ledger using the original key, credential scope, and complete request fingerprint. Missing retained records remain unknown. A confirmed paid result can be restored after the original execution deadline; recovery cannot use a late output to start another charge.

This lock does not unlock the ledger automatically. Operator review and consistent runtime/chain evidence are required. Keep encryption keys with protected backups; losing a key prevents decryption of pending approvals. Do not remove the lock merely because the currently visible orders look complete: an old backup can omit later orders entirely.

## Scope of current evidence

The implementation includes deterministic signing tests, server and browser tests, and a local integration harness. Automated wallet tests can use a Node-held test wallet through an EIP-1193 bridge. That validates the browser protocol path but is distinct from a person accepting a request in a wallet extension. The [status page](status.md) and [sanitized integration evidence](verification-2026-11-07.json) record the completed actual model calls, chain receipts, independent-agent check, and normal seller restart. The [full review](review-2026-11-07.md) also verified six injected recovery faults on temporary ledger copies against actual runtime and chain records. It did not kill a live process during settlement.

To repeat the recovery check against existing settled testnet orders:

```sh
.venv/bin/python scripts/check_live_recovery.py
```

The script opens the source ledger read-only, uses disposable copies, and replaces model submission and payment settlement with guards. It creates no new model run or payment.

## Sources

- [Hermes plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [Hermes Plugin LLM](https://github.com/NousResearch/hermes-agent/blob/v0.22.7/website/docs/developer-guide/plugin-llm-access.md)
- [Official x402 SIWX](https://docs.x402.org/extensions/sign-in-with-x)
- [x402 buyer integration](https://docs.x402.org/getting-started/quickstart-for-buyers)
