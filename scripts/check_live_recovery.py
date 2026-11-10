"""Inject recovery faults into temporary copies; query actual runs and receipts only.

The source ledger is opened read-only. Runtime dispatch and settlement methods
are replaced with guards, so this check cannot create model runs or payments.
"""

import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
from functools import partial
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from hermes_sell.adapters import HermesRunsRuntime
from hermes_sell.chain import ChainReader
from hermes_sell.contracts import canonical
from hermes_sell.engine import Engine
from hermes_sell.runtime_container import lookup_run_id
from hermes_sell.settings import TestnetSettings, private_text
from hermes_sell.storage import Store
from hermes_sell.vault import AuthorizationVault
from hermes_sell.x402_adapter import X402Payments


class ReadOnlyRuntime(HermesRunsRuntime):
    starts = 0

    async def start(self, *args):
        self.starts += 1
        raise AssertionError("The recovery check cannot create a model run")


class ReadOnlyPayments(X402Payments):
    settlements = 0

    async def settle(self, *args):
        self.settlements += 1
        raise AssertionError("The recovery check cannot submit a payment")


def intent_counts(connection):
    return dict(connection.execute(
        "SELECT kind,count(*) FROM events WHERE kind IN ('dispatch_intent','settlement_intent') GROUP BY kind"
    ).fetchall())


async def check(config):
    settings = TestnetSettings.load(config)
    token = private_text(settings.runtime_token_file)
    runtime = ReadOnlyRuntime(settings.runtime_url, token, hashlib.sha256(token.encode()).hexdigest(),
        admission_lookup=partial(lookup_run_id, context=settings.docker_context, container=settings.runtime_container))
    chain = ChainReader(settings.rpc_url)
    payments = ReadOnlyPayments(None, chain=chain)
    vault = AuthorizationVault.load(settings.vault_key_file)
    source = sqlite3.connect((settings.state_dir / "seller.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        originals = [json.loads(row[0]) for row in source.execute(
            "SELECT document FROM orders WHERE execution='result_ready' AND payment='settled' ORDER BY created_at LIMIT 2"
        )]
        if not originals:
            raise RuntimeError("This check needs an existing settled testnet order")
        before = intent_counts(source)
        cases = []
        for original in originals:
            for fault in ("lost_settlement_response", "lost_admission_response_in_backup", "pre_admission_backup"):
                with tempfile.TemporaryDirectory(prefix="hermes-sell-recovery-check-") as directory:
                    path = Path(directory) / "seller.sqlite3"
                    target = sqlite3.connect(path)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                    store = Store(path)
                    order = deepcopy(original)
                    order.pop("receipt", None)
                    order.pop("settlement_candidate", None)
                    if fault == "lost_settlement_response":
                        order["payment"] = "settling"
                    else:
                        order.pop("run_id", None)
                        order.pop("result", None)
                        order.pop("provenance", None)
                        order.update(execution="dispatching", payment="verified")
                        if fault == "pre_admission_backup":
                            order.update(execution="created", payment="required")
                            for field in ("runtime_binding", "execution_deadline", "authorization_digest",
                                          "authorization_expires_at", "authorization_encrypted", "authorization",
                                          "chain_start_block"):
                                order.pop(field, None)
                        store.flag_recovery()
                    with store.tx() as connection:
                        connection.execute("UPDATE orders SET document=?,execution=?,payment=? WHERE id=?",
                            (canonical(order), order["execution"], order["payment"], order["id"]))
                    seller = Engine(store, runtime, payments, vault=vault)
                    seller.recover_interrupted()
                    if store.recovery_required:
                        assert seller.view(store.order(order["id"]))["payment"] == "unknown"
                        await seller.tick()
                    # A transaction hash is only a hint. Exact nonce, payer, payee,
                    # amount, canonical block and confirmation depth are checked.
                    recovered = await seller.reconcile_order(order["id"], transaction=original["receipt"]["transaction"])
                    saved = store.order(order["id"])
                    assert recovered["payment"] == "settled", recovered
                    assert recovered["execution"] == "result_ready", recovered
                    assert recovered["result"] == original["result"]
                    assert saved["run_id"] == original["run_id"]
                    with closing(store.connect()) as connection:
                        assert intent_counts(connection) == before
                    cases.append({"fault": fault, "order_id": order["id"], "run_id": saved["run_id"],
                        "result_matches": True, "transaction": recovered["receipt"]["transaction"],
                        "payment": recovered["payment"], "restore_lock_preserved": store.recovery_required})
        assert runtime.starts == payments.settlements == 0
        assert intent_counts(source) == before
        return {"scope": "Temporary ledger fault injection with actual Hermes and Base Sepolia reads",
            "source_ledger_read_only": True, "new_runs": runtime.starts, "new_payments": payments.settlements,
            "source_intents_unchanged": before, "cases": cases}
    finally:
        source.close()
        await runtime.close()
        await chain.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(".state/testnet/config.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(check(args.config))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
