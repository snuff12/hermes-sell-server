# Isolated Hermes runtime

This image runs the official Hermes Runs API for text/JSON services. It uses the
Hermes v0.23.0 ARM64 image pinned in `Dockerfile`. The current image pin is for an
Apple Silicon local test environment; other architectures need a separately
verified official image digest.

The seller runtime has a fresh home, no host directory mounts, no execution tools,
no MCP servers, no memory, and no plugins. It runs as UID/GID 10000 with a read-only
root filesystem, dropped capabilities, resource limits, and a loopback-only API
at `http://127.0.0.1:18790`. Its two dedicated Docker volumes retain execution state
and the selected credentials. The credential volume is read-only in the runtime.

## Build and start

Use a dedicated Docker context. For example, the verified local context is
`colima-hermes-sell`, created with no host mounts or SSH agent forwarding. The
script always selects its context explicitly and does not change the global
Docker context.

```sh
.venv/bin/python scripts/hermes_runtime.py build
.venv/bin/python scripts/hermes_runtime.py start --codex-auth-store /absolute/path/to/authorized/auth.json
.venv/bin/python scripts/hermes_runtime.py inspect
```

The start command requires an explicitly selected, private auth file owned by the
current user. It extracts only one currently valid OpenAI Codex access token into
a new credential pool entry. It does not copy refresh tokens, unrelated providers,
or the original home, and it does not change the source auth file. The selected
access token must have at least ten minutes remaining. Expiry requires renewing
authentication in the original host, then explicitly recreating the dedicated
runtime with a fresh access token. There is no background refresh of the personal
auth store.

The runtime API bearer token is created once at
`.state/testnet/runtime-token` with mode `0600`. Its hash identifies the runtime
credential generation used by an order. Preserve this file and both named volumes
across a normal restart. Replacing a credential, volume, or origin while orders are
unresolved is not a recovery strategy.

The script refuses to replace an existing container or an unrelated service on
port 18790. Before a deliberate runtime replacement, resolve active orders and
retain `hermes-sell-runtime-state` and `hermes-sell-runtime-secrets`. External
hosting, a public tunnel, and mainnet payments are outside this local setup.

## What preflight proves

`hermes_sell.runtime_container.inspect_runtime()` checks the running container's
mounts, process isolation, API binding, fixed configuration hash, and access-only
credential shape. `HermesRunsRuntime.preflight()` separately checks bearer
authentication, durable idempotency support, and disabled toolsets through the
actual Runs API. Neither check proves that a model call or payment succeeds; the
integration checks must supply that evidence.

The runtime can contact the model provider through Docker's network. This is not
an egress allowlist. The policy limits the supported service scope to supplied
text/JSON and static references, with no execution tools.

When a run-admission response is lost, the seller can inspect the pinned v0.23
runtime's `runs_idempotency.db` through `lookup_run_id`. It opens SQLite read-only
with `query_only`, matches the default-profile credential scope, original run
key, and exact request fingerprint, then polls the recovered run ID through the
Runs API. It neither initializes/prunes the database nor repeats submission.
Missing or incompatible retained evidence stays unknown. A different Hermes
release needs a new verification of this internal read-only integration.

## Check the plugin in a fresh host

`scripts/check_hermes_host.py` loads an exported official Hermes source tree and a
compatible dependency directory in a temporary home. Python starts with
`-I -S -B`, so editable-install `.pth` files are not executed. The plugin is copied
into that temporary home; the personal Hermes installation is not modified.

```sh
.venv/bin/python scripts/check_hermes_host.py doctor \
  --hermes-source /absolute/path/to/exported/hermes-source \
  --hermes-site-packages /absolute/path/to/compatible/site-packages
```

The `draft` action calls the real host `ctx.llm` interface and saves an immutable
draft for owner review. It requires an authorized auth file, a selected skill, and
an example JSON object with `input` and `output` fields. The `buyer-followup` action
runs a separate Hermes agent with the buyer role. It requires an existing
browser-approved purchase ID, the buyer state directory, and its authorization
vault key file. It cannot approve or sign a payment. See `--help` for arguments.

The host-check harness currently uses this project's Python 3.14 environment.
It demonstrates integration with a clean Hermes home; it is not a clean-device
installer or an operating-system sandbox. The seller model runtime is the
separate container described above.

## Primary references

- [Hermes plugin interface](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
- [Hermes API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
- [Hermes v0.23.0 canonical Runs projection](https://github.com/NousResearch/hermes-agent/blob/v0.23.0/gateway/platforms/api_server_authority_runs.py)
- [Hermes Plugin LLM interface](https://github.com/NousResearch/hermes-agent/blob/v0.23.0/website/docs/developer-guide/plugin-llm-access.md)
