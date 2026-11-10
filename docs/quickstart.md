# Run Hermes Sell locally

The plugin adds Hermes tools. This repository runs the separate seller API,
owner workspace, shared service directory and browser approval gateway.
The default walkthrough uses a simulation with no model calls or real payments.

## Requirements

- Python 3.14, Node.js 22, Git, and a supported shell.
- Hermes Agent 0.22.7 or newer for the plugin; verification uses 0.23.0.
- Docker/Colima and a Base Sepolia EOA wallet only for the separate testnet path.

## Install the server source

```sh
git clone https://github.com/snuff12/hermes-sell-server.git
cd hermes-sell-server
./scripts/setup.sh
```

Keep private state in the ignored `.state` directory. The setup builds the web
application from source; Node is not needed to serve the built application.

## Start a simulation seller

In the first terminal:

```sh
.venv/bin/hermes-sell dev
```

This starts the storefront at <http://127.0.0.1:8765> and owner workspace at
<http://127.0.0.1:8766>. The simulation admin token is `local-preview-only`.
Both example services are already tested and published locally. The simulation
returns saved example output; it does not run a model or transfer USDC.

In a second terminal, start the shared directory:

```sh
.venv/bin/hermes-sell --state .state/directory directory \
  --allow-seller http://127.0.0.1:8765 \
  --owner-origin http://127.0.0.1:8766
```

## Register, buy and review

1. Open <http://127.0.0.1:8795/sell>, choose the seller API and select **Check seller**.
2. Enter the owner URL `http://127.0.0.1:8766` and open that workspace.
3. Sign in with the simulation token. In **Services**, select **List in directory** for Diff review.
4. Open <http://127.0.0.1:8795>, select Diff review, then **Open storefront**.
5. Select **Connect test session**, **Review quote**, and **Approve 0.10 USDC simulation**.
6. Read the result. Reload and **Reconnect and restore order** to recover it.
7. Select **Rate this service** to publish a clearly labeled simulation review.
8. In the owner workspace, select **Analytics** for execution counts and simulated settled volume.

To add another seller, run `dev` with a different `--state`, `--port` and
`--admin-port`, then restart the directory with another exact `--allow-seller`.
The [marketplace guide](marketplace.md) explains publication and trust limits.

## Install the Hermes plugin

```sh
hermes plugins install snuff12/hermes-sell
hermes plugins enable hermes-sell
```

In your active profile, configure the plugin's `buyer_state_dir` as the absolute
path to this checkout's `.state/buyer` directory. Start with `buyer_mode: simulation`,
`buyer_origin: http://127.0.0.1:8765`, and
`directory_origin: http://127.0.0.1:8795`. The
[plugin README](https://github.com/snuff12/hermes-sell) includes a configuration example.

Ask Hermes to search, inspect and quote a listed service. The simulation's
`human_approval_command` requires you to enter the order digest in your terminal.
Then ask Hermes to submit that same purchase ID and read its status. Agent tools
cannot grant approval.

## Real model execution and testnet payment

Follow [local-testnet.md](local-testnet.md). That path requires an isolated
runtime, explicit model configuration, encrypted buyer state and browser wallet
approval. It supports Base Sepolia test USDC only. The development credential
import helper is outside the catalog plugin and is not a user authentication API.
Never point an exposed seller at your personal Hermes home.

All workspaces remain local in this release. Public hosting and open Internet
seller registration are separate work. A computer that is off cannot serve orders.

## Verify and stop

```sh
.venv/bin/python -m pytest -q
npm --prefix web run build
npm --prefix web run test:e2e
.venv/bin/python scripts/check_english.py
```

Browser tests use Chrome on macOS. On Linux, set `CHROME_PATH` to your Chromium
executable. Tests use disposable simulation ledgers and external test doubles.
Press Ctrl-C in each server terminal to stop. Preserve `.state` and its keys;
do not replace a ledger or key to bypass an unknown outcome.
