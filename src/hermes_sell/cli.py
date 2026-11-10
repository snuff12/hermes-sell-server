import argparse
import asyncio
import fcntl
import json
from pathlib import Path
import sys

import uvicorn

from .adapters import SimulationPayments, SimulationRuntime
from .api import admin_app, public_app
from .authoring import from_files
from .buyer import Buyer
from .contracts import Problem
from .engine import Engine
from .storage import Store

ROOT = Path(__file__).resolve().parents[2]


def engine(path):
    return Engine(Store(path), SimulationRuntime(), SimulationPayments())


async def seed(seller):
    for name in ("diff-review", "release-summary"):
        path = ROOT / "examples" / name
        revision = from_files(path / "service.json", path / "SKILL.md")
        seller.put_revision(revision)
        report = await seller.test_revision(revision["public"]["revision"])
        seller.publish(revision["public"]["revision"], report["id"], True)


async def serve(args):
    seller = engine(args.state / "seller.sqlite3")
    lock = (args.state / "seller.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Problem("seller_running", "A seller server is already running with this ledger.")
    if not seller.services():
        await seed(seller)
    if not (ROOT / "web/dist/index.html").exists():
        raise Problem("web_build_required", "Run npm --prefix web run build first.")
    print(f"Local simulation only · no real model or payment\nStorefront: http://127.0.0.1:{args.port}\nStudio: http://127.0.0.1:{args.admin_port}\nStudio demo token: local-preview-only", flush=True)
    apps = [public_app(seller, f"http://127.0.0.1:{args.port}", ROOT / "web/dist",
                       directory_origin=getattr(args, "directory_origin", "http://127.0.0.1:8795")),
            admin_app(seller, "local-preview-only", f"http://127.0.0.1:{args.admin_port}", ROOT / "web/dist",
                      seller_origin=f"http://127.0.0.1:{args.port}",
                      directory_origin=getattr(args, "directory_origin", "http://127.0.0.1:8795"))]
    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
               for app, port in zip(apps, (args.port, args.admin_port))]
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        lock.close()


async def buy(args):
    if args.mode == "testnet":
        from .buyer import TestnetBuyer
        from .vault import AuthorizationVault
        state = args.state / "buyer-testnet" if args.state == ROOT / ".state" else args.state
        buyer = TestnetBuyer(args.origin, state / "buyer.sqlite3",
                            vault=AuthorizationVault.load(args.vault_key_file or state / "authorization.key"),
                            approval_origin=args.approval_origin)
    else:
        buyer = Buyer(args.origin, args.state / "buyer.sqlite3")
    try:
        await buyer.connect()
        if args.action == "quote":
            result = await buyer.quote(args.slug, json.loads(args.input))
        elif args.action == "approve":
            if args.mode == "testnet":
                raise Problem("browser_approval_required", "Open the purchase approval URL and approve with your browser wallet.", 409)
            purchase = buyer.get(args.purchase_id)
            order = purchase["order"]
            print(json.dumps({k: order[k] for k in ("origin", "amount", "pay_to", "network", "asset", "quote_expires_at", "order_digest")}, indent=2))
            # Do not accept a --yes/approved flag or piped stdin for payment approval.
            with open("/dev/tty", "r+") as terminal:
                terminal.write("Approve simulation purchase: enter the full order_digest shown above: ")
                terminal.flush()
                confirmation = terminal.readline().strip()
            result = buyer.approve_from_terminal(args.purchase_id, confirmation)
        elif args.action == "purchase":
            result = await buyer.purchase(args.purchase_id)
        else:
            result = await buyer.status(args.purchase_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await buyer.close()


async def serve_buyer_gateway(args):
    from .buyer import TestnetBuyer
    from .buyer_gateway import buyer_gateway_app
    from .vault import AuthorizationVault
    state = args.buyer_state
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = args.vault_key_file or state / "authorization.key"
    vault = AuthorizationVault.load(key) if key.exists() else AuthorizationVault.create(key)
    origin = f"http://127.0.0.1:{args.port}"
    buyer = TestnetBuyer(args.origin, state / "buyer.sqlite3", vault=vault, approval_origin=origin)
    app = buyer_gateway_app(buyer, ROOT / "web/dist", directory_origin=args.directory_origin)
    print(f"Local buyer wallet approval: {origin}\nSeller: {args.origin}\nNo wallet signing key is held by this process.", flush=True)
    try:
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                           proxy_headers=False, log_level="warning")).serve()
    finally:
        await buyer.close()


def main():
    parser = argparse.ArgumentParser(description="Hermes Sell local development preview")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("dev", help="Start local simulation storefront and private studio")
    start.add_argument("--port", type=int, default=8765)
    start.add_argument("--admin-port", type=int, default=8766)
    start.add_argument("--directory-origin", default="http://127.0.0.1:8795")
    directory = commands.add_parser("directory", help="Start a shared local service directory")
    directory.add_argument("--port", type=int, default=8795)
    directory.add_argument("--allow-seller", action="append", required=True,
                           help="Explicit loopback seller origin; repeat for multiple sellers")
    directory.add_argument("--buyer-origin", default="http://127.0.0.1:8767")
    directory.add_argument("--owner-origin", default="http://127.0.0.1:8786")
    buyer = commands.add_parser("buyer", help="Separate simulation buyer process")
    buyer.add_argument("action", choices=["quote", "approve", "purchase", "status"])
    buyer.add_argument("--origin", default="http://127.0.0.1:8765")
    buyer.add_argument("--slug", default="diff-review")
    buyer.add_argument("--input", default='{"diff":"+console.log(token)","context":"Review a public diff."}')
    buyer.add_argument("--purchase-id")
    buyer.add_argument("--mode", choices=["simulation", "testnet"], default="simulation")
    buyer.add_argument("--vault-key-file", type=Path)
    buyer.add_argument("--approval-origin", default="http://127.0.0.1:8767")
    gateway = commands.add_parser("buyer-gateway", help="Start the separate local browser wallet approval gateway")
    gateway.add_argument("--origin", default="http://127.0.0.1:8785")
    gateway.add_argument("--port", type=int, default=8767)
    gateway.add_argument("--buyer-state", type=Path, default=ROOT / ".state/buyer-testnet")
    gateway.add_argument("--vault-key-file", type=Path)
    gateway.add_argument("--directory-origin", default="http://127.0.0.1:8795")
    commands.add_parser("recovery-lock", help="Block side effects after restoring an old ledger")
    commands.add_parser("init-testnet", help="Create private local testnet configuration without starting sales")
    for name in ("testnet", "testnet-doctor"):
        command = commands.add_parser(name, help="Run or check the isolated local Base Sepolia seller")
        command.add_argument("--config", type=Path, default=ROOT / ".state/testnet/config.json")
    args = parser.parse_args()
    try:
        if args.command == "dev":
            asyncio.run(serve(args))
        elif args.command == "directory":
            from .directory import ServiceDirectory, directory_app
            origin = f"http://127.0.0.1:{args.port}"
            catalog = ServiceDirectory(args.state / "directory.sqlite3", origin, args.allow_seller)
            print(f"Local service directory: {origin}", flush=True)
            uvicorn.run(directory_app(catalog, ROOT / "web/dist", buyer_origin=args.buyer_origin,
                                     owner_origin=args.owner_origin), host="127.0.0.1", port=args.port,
                        proxy_headers=False, log_level="warning")
        elif args.command == "init-testnet":
            from .settings import initialize_testnet
            print(json.dumps(initialize_testnet(args.state / "testnet"), indent=2))
        elif args.command in {"testnet", "testnet-doctor"}:
            from .testnet import run_testnet
            report = asyncio.run(run_testnet(args.config, ROOT / "web/dist", doctor=args.command == "testnet-doctor"))
            if report is not None:
                print(json.dumps(report, indent=2))
                if not report["ready"]:
                    raise SystemExit(1)
        elif args.command == "buyer-gateway":
            asyncio.run(serve_buyer_gateway(args))
        elif args.command == "buyer":
            if args.action != "quote" and not args.purchase_id:
                parser.error("--purchase-id is required")
            asyncio.run(buy(args))
        else:
            Store(args.state / "seller.sqlite3").flag_recovery()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (Problem, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
