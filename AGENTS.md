# Hermes Sell

Read `docs/architecture.md` and `docs/status.md` before changing the product.

- Write all code comments, UI text, errors, documentation, examples, tests, filenames, and repository metadata in English. Run `.venv/bin/python scripts/check_english.py` before committing.
- Three required outcomes: skill → service; browser purchase; independent Hermes purchase.
- Keep seller HTTP, owner management, and buyer signing boundaries separate.
- The default development mode is a local simulation. The separate testnet mode runs real Hermes execution and Base Sepolia payments behind verified isolation, wallet authentication, encrypted authorizations, and receipt checks. Keep both modes clearly labeled.
- Never expose the user's existing Hermes runtime, credentials, personal home, or browser profile.
- Persist intent before external side effects. Never create a new payment nonce or Hermes run merely to retry an uncertain request.
- Result delivery requires validated output AND confirmed settlement. Unknown outcomes require reconciliation.
- The shared directory indexes only owner-opted-in public contracts. Keep listing metadata separate from private bundles and the official Hermes plugin catalog. Local directory fetches are restricted to configured loopback sellers.
- Route existing purchases by their persisted origin. Keep wallet sessions scoped per seller and spending reservations shared across sellers.
- Prepare catalog source with `scripts/build_catalog_plugin.py`. Developer provider-token importers and runtime runners must remain outside the exported plugin; use the host LLM interface for plugin inference.
- Mainnet is disabled. External hosting is deferred at the user's request; keep all servers on loopback unless the user resumes that work.
- Keep documentation, tests, and runtime status truthful. Do not publish or enable a tunnel as part of a routine local check.
- Run `.venv/bin/python -m pytest`, `npm --prefix web run build`, and `npm --prefix web run test:e2e` for relevant changes.
- Follow `/Users/mini/AGENTS.md` Git signing requirements. Do not add secrets or local state to Git.
