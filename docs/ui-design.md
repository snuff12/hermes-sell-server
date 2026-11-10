# Hermes UI reference

The reference version is Hermes Agent **v0.22.7 Desktop**. These are standalone React web surfaces. They adapt Hermes visual rules and tokens without requiring the host SDK; they are not a native panel installed in Hermes Desktop.

| Official reference | Application in this project |
| --- | --- |
| Flat layout with clear work areas | Sidebar, main content, and inspector; no nested dashboard cards |
| Shared color tokens | `--theme-primary`, `--ui-text-*`, `--ui-stroke-*`, `--ui-bg-quaternary` |
| One button component | `Button` in `web/src/ui.tsx`: primary/secondary/ghost/text, busy and disabled states |
| Compact controls | 12px default control text, 2.5px corner radius, thin borders, larger mobile touch targets |
| Consistent themes and states | Light/dark themes and shared Badge, Message, Toolbar, and EmptyState components |
| User-controlled transitions | Block service and draft switches during an action; preserve the current work |

The default accent is `#0053fd`. Light backgrounds and text levels use the official token meanings. The dark theme keeps the same hierarchy with readable web contrast. All first-party copy, accessible labels, examples, and comments are in English; times use English formatting in the browser's local time zone.

## Storefront

The main pane shows the service, frozen version, input, and example output. The inspector shows the exact price and order state. Generated fields without schema titles use readable names such as **Context** and **Diff**; full input paths remain available for validation and field identifiers.

Simulation mode keeps **Connect test session** and **Approve … simulation**. Testnet mode uses **Connect wallet**, a separate SIWX login signature, and **Approve … on testnet**. Before payment approval, the inspector exposes the seller origin, complete payee address, wallet, network, order digest, and expiry. Full-width action text can wrap on narrow screens.

The browser supports injected EIP-1193 EOA wallets on Base Sepolia. It checks the account and network before signing and submitting. Account/network changes clear the session. No private key is requested or stored by the application.

**Retry same approval** reuses the original signature. **Reconnect and restore order** reauthenticates and queries the same order after reload. It does not re-sign. After an uncertain attempt, **Start new order** remains blocked until an original-order lookup confirms that an unadmitted approval can no longer be used.

Result and receipt labels distinguish simulated settlement from confirmed testnet settlement. A testnet badge describes the configured network; it is not evidence that a live purchase has completed.

## Owner workspace

The manual editor shows price and payee as separate fields. Advanced schema JSON cannot override them. **Save version** creates a new immutable revision.

With Hermes execution configured, **Import Hermes draft** accepts the draft ID from `service_draft`, plus slug, price, and payee. The private saved bundle remains intact. A bundle containing static reference files is shown read-only in the manual editor; the owner can test and publish it or import a new draft.

The primary flow is **Run execution test** → review the output → explicitly confirm review → **Publish service**. Simulation keeps **Run simulation test** and **Publish locally**, with fixed-example wording. The interface does not claim that manual editing itself invokes an LLM.

## Independent buyer approval

The separate buyer gateway shows the configured seller origin and the agent's immutable input, version, amount, payee, digest, and expiry. It also shows the configured buyer spending limit when available.

Signing in connects the local buyer process to the seller. **Approve … for agent** saves one signed authorization in the buyer ledger. The completion message says: **Approved. Ask your agent to submit this purchase.** The page has no purchase-submit action.

Unknown purchase outcomes are shown explicitly and do not offer a fresh payment signature. A lost approval response can retry the same in-memory payload. After reload, persisted attempt metadata prevents a new signature when the original payload is unavailable.

## Verification and previews

The 34-test browser suite covers simulation, wallet signing, buyer approval, Hermes draft import, themes, schema forms, recovery, cross-seller discovery, purchase history, and narrow screens. Unsigned quotes remain approvable after reload. A Web Lock and persistent intent marker prevent concurrent tabs from signing the same order; unsupported browsers stop before signing. The 360px storefront, buyer approval and directory views were checked for horizontal overflow. Tests use real deterministic signatures and controlled transport responses; they do not prove onchain settlement.

The retained [storefront](previews/storefront.png), [dark Studio](previews/studio-dark.png), and [mobile Studio](previews/studio-mobile.png) images are simulation snapshots from the earlier UI review. New wallet test captures are generated under the ignored `web/test-results/` directory. These sample images are not evidence of a live payment.

Actual local testnet screens: [paid result](previews/testnet-result.png) · [separate buyer approval](previews/buyer-approval.png). These captures use dedicated test wallets and testnet USDC; no private keys or bearer tokens are shown.

Current shared workspaces: [directory](previews/directory.png), [directory at 360px](previews/directory-mobile.png), and [purchases](previews/purchases.png). The directory capture mixes simulation and testnet listings with explicit labels. `desktop/plugin.js` contributes an SDK page and sidebar row that launch the default local browser workspaces; wallet approval remains in the browser.

## Sources

- [Desktop DESIGN.md](https://github.com/NousResearch/hermes-agent/blob/v0.22.7/apps/desktop/DESIGN.md)
- [Official style tokens](https://github.com/NousResearch/hermes-agent/blob/v0.22.7/apps/desktop/src/styles.css)
- [Button primitive](https://github.com/NousResearch/hermes-agent/blob/v0.22.7/apps/desktop/src/components/ui/button.tsx)
- [Form control primitive](https://github.com/NousResearch/hermes-agent/blob/v0.22.7/apps/desktop/src/components/ui/control.ts)
