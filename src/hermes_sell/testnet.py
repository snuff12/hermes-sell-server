"""Build and run the separate local testnet seller and owner applications."""

import asyncio
from contextlib import suppress
import fcntl
import hashlib
from functools import partial

import httpx
import uvicorn
from x402.http import FacilitatorConfig, HTTPFacilitatorClient

from .adapters import HermesRunsRuntime
from .api import admin_app, public_app
from .chain import ChainReader
from .contracts import Problem
from .drafts import DraftStore
from .engine import Engine
from .settings import Readiness, TestnetSettings, private_text
from .runtime_container import lookup_run_id
from .storage import Store
from .vault import AuthorizationVault
from .wallet_auth import WalletAuth
from .x402_adapter import X402Payments


async def run_testnet(config_path, web_dist, *, doctor=False):
    settings = TestnetSettings.load(config_path)
    token = private_text(settings.runtime_token_file)
    generation = hashlib.sha256(token.encode()).hexdigest()
    runtime = HermesRunsRuntime(settings.runtime_url, token, generation, admission_lookup=partial(
        lookup_run_id, context=settings.docker_context, container=settings.runtime_container))
    http = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url, http_client=http))
    chain = ChainReader(settings.rpc_url)
    payments = X402Payments(facilitator, chain=chain)
    readiness = Readiness(settings, runtime, payments)
    lock = None
    monitor = None
    try:
        report = await readiness.check()
        if doctor:
            return report
        vault = AuthorizationVault.load(settings.vault_key_file)
        owner_token = private_text(settings.owner_token_file)
        if len(owner_token) < 32 or owner_token == "local-preview-only":
            raise ValueError("Testnet owner access requires a random private token.")
        store = Store(settings.state_dir / "seller.sqlite3")
        lock = (settings.state_dir / "seller.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Problem("seller_running", "A seller server already owns this ledger.", 409) from None
        seller = Engine(store, runtime, payments, vault=vault, readiness_check=readiness.ready)
        # Auth challenges are transient security state, not part of ledger backups.
        auth = WalletAuth(settings.state_dir / "wallet-auth.sqlite3", settings.origin)
        # A restored ledger remains locked, but authenticated owner reconciliation
        # and existing-order reads must stay available. Engine gates every new
        # dispatch and settlement while this flag is present.
        owner_origin = f"http://127.0.0.1:{settings.admin_port}"
        apps = [public_app(seller, settings.origin, web_dist, wallet_auth=auth, directory_origin=settings.directory_origin),
                admin_app(seller, owner_token, owner_origin, web_dist, draft_store=DraftStore(settings.draft_dir),
                          seller_origin=settings.origin, directory_origin=settings.directory_origin)]
        servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                   proxy_headers=False, log_level="warning"))
                   for app, port in zip(apps, (settings.port, settings.admin_port))]
        monitor = asyncio.create_task(readiness.monitor())
        print(f"Base Sepolia testnet · real Hermes execution\nStorefront: {settings.origin}\nOwner workspace: {owner_origin}\nOwner token file: {settings.owner_token_file}", flush=True)
        if not report["ready"]:
            print("Dependencies unavailable: existing orders and owner reconciliation remain available; new execution and settlement are blocked.", flush=True)
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        if monitor:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
        if lock:
            lock.close()
        await runtime.close()
        await chain.close()
        await http.aclose()
