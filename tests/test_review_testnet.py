"""Regression checks for settlement evidence, races, and readiness boundaries."""

import asyncio
from copy import deepcopy
import json
import uuid

import httpx
import pytest

from conftest import authorization, quote
from hermes_sell.adapters import SimulationRuntime
from hermes_sell.chain import ChainReader, TRANSFER, AUTHORIZATION_USED, AUTHORIZATION_CANCELED, address_topic
from hermes_sell.contracts import ASSET, NETWORK, Problem
from hermes_sell.engine import Engine
from hermes_sell.storage import Store
from hermes_sell.vault import AuthorizationVault
from hermes_sell.x402_adapter import X402Payments


@pytest.mark.parametrize("layout", ["paired", "unrelated_transfer", "intervening_log", "reversed"])
async def test_receipt_requires_the_transfer_paired_with_the_exact_authorization(layout):
    transaction = "0x" + "b" * 64
    order = {"owner": "0x" + "1" * 40, "pay_to": "0x" + "2" * 40, "amount": "100000",
             "payment_nonce": "0x" + "3" * 64, "network": NETWORK}
    used = {"address": ASSET, "topics": [AUTHORIZATION_USED, address_topic(order["owner"]), order["payment_nonce"]], "data": "0x"}
    transfer = {"address": ASSET, "topics": [TRANSFER, address_topic(order["owner"]), address_topic(order["pay_to"])], "data": hex(100000)}
    if layout == "paired":
        logs = [used, transfer]
    elif layout == "unrelated_transfer":
        different_use = {**deepcopy(used), "topics": [AUTHORIZATION_USED, address_topic(order["owner"]), "0x" + "4" * 64]}
        logs = [used, {**transfer, "data": "0x0"}, different_use, transfer]
    elif layout == "intervening_log":
        logs = [used, {"address": "0x" + "9" * 40, "topics": [], "data": "0x"}, transfer]
    else:
        logs = [transfer, used]
    receipt = {"transactionHash": transaction, "status": "0x1", "blockNumber": "0x64",
               "blockHash": "0x" + "c" * 64, "logs": logs}

    def respond(request):
        method = json.loads(request.content)["method"]
        result = {"eth_chainId": "0x14a34", "eth_blockNumber": "0x65", "eth_getTransactionReceipt": receipt,
                  "eth_getBlockByNumber": {"hash": receipt["blockHash"]}}[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    reader = ChainReader("https://rpc.example", transport=httpx.MockTransport(respond))
    try:
        result = await reader.confirm(order, transaction)
        assert bool(result) == (layout == "paired")
        if result:
            assert result["confirmation_policy"] == "testnet-l2-depth-2"
    finally:
        await reader.close()


async def test_stale_reconciliation_cannot_overwrite_confirmed_settlement(tmp_path, revision):
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = {"transaction": "0x" + "7" * 64, "network": NETWORK, "success": True}

    class RacingChain:
        calls = 0

        async def reconcile(self, order):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
                return {"status": "expired_unspent"}
            return {"status": "settled", "receipt": receipt}

    payments = X402Payments(None, chain=RacingChain())
    seller = Engine(Store(tmp_path / "seller.sqlite3"), SimulationRuntime(), payments,
                    vault=AuthorizationVault(b"t" * 32))
    seller.put_revision(revision)
    report = await seller.test_revision(revision["public"]["revision"])
    seller.publish(revision["public"]["revision"], report["id"], True)
    service = revision["public"]
    order = seller.create_order("0x" + "1" * 40, service["slug"], service["revision"],
                                service["examples"][0]["input"], str(uuid.uuid4()), "https://seller.example")
    seller.transition(order["id"], "fixture_pending_settlement", execution="result_ready", payment="unknown",
                      result=service["examples"][0]["output"], authorization_digest="a" * 64)
    stale = asyncio.create_task(seller.reconcile_payment(order["id"]))
    await entered.wait()
    confirmed = await seller.reconcile_payment(order["id"])
    assert confirmed["payment"] == "settled"
    release.set()
    observed = await stale
    assert observed["payment"] == "settled"
    stored = seller.store.order(order["id"])
    assert stored["payment"] == "settled" and stored["receipt"] == receipt
    assert not seller.store.one("SELECT 1 FROM events WHERE order_id=? AND kind='chain_expired_unspent'", (order["id"],))


@pytest.mark.parametrize("blocker", ["backup_recovery", "dependencies"])
async def test_readiness_loss_during_poll_prevents_new_settlement(seller, monkeypatch, blocker):
    order = quote(seller)
    await seller.admit(order["id"], "demo-browser", authorization(order))
    original_poll = seller.runtime.poll

    async def poll_then_block(*args):
        result = await original_poll(*args)
        if blocker == "backup_recovery":
            seller.store.flag_recovery()
        else:
            seller.readiness_check = lambda: False
        return result

    monkeypatch.setattr(seller.runtime, "poll", poll_then_block)
    await seller.tick()
    stored = seller.store.order(order["id"])
    assert stored["execution"] == "result_ready"
    assert stored["payment"] == "verified"
    assert seller.payments.settlements == 0
    assert "result" not in seller.view(stored)


async def test_dependency_outage_allows_read_only_poll_but_no_dispatch_or_settlement(seller, monkeypatch):
    order = quote(seller)
    await seller.admit(order["id"], "demo-browser", authorization(order))
    seller.readiness_check = lambda: False
    await seller.tick()
    assert seller.runtime.starts == 0
    assert seller.store.order(order["id"])["execution"] == "queued"
    seller.readiness_check = lambda: True
    original_poll = seller.runtime.poll
    monkeypatch.setattr(seller.runtime, "poll", lambda *args: pending_result())

    async def pending_result():
        return {"status": "running"}

    await seller.tick()
    assert seller.runtime.starts == 1
    assert seller.store.order(order["id"])["execution"] == "running"
    seller.readiness_check = lambda: False
    monkeypatch.setattr(seller.runtime, "poll", original_poll)
    await seller.tick()
    assert seller.store.order(order["id"])["execution"] == "result_ready"
    assert seller.payments.settlements == 0


@pytest.mark.parametrize("fault", [None, "unconfirmed", "wrong_nonce", "wrong_wallet", "wrong_contract", "reorg", "removed", "reverted"])
async def test_cancellation_requires_exact_wallet_nonce_and_canonical_confirmed_receipt(fault):
    transaction = "0x" + "a" * 64
    order = {"owner": "0x" + "1" * 40, "pay_to": "0x" + "2" * 40, "amount": "100000",
             "payment_nonce": "0x" + "3" * 64, "network": NETWORK, "chain_start_block": 100,
             "authorization_expires_at": 10000}
    entry = {"address": ASSET, "topics": [AUTHORIZATION_CANCELED, address_topic(order["owner"]), order["payment_nonce"]],
             "data": "0x", "transactionHash": transaction}
    if fault == "wrong_nonce":
        entry["topics"][2] = "0x" + "9" * 64
    if fault == "wrong_wallet":
        entry["topics"][1] = address_topic("0x" + "9" * 40)
    if fault == "wrong_contract":
        entry["address"] = "0x" + "9" * 40
    if fault == "removed":
        entry["removed"] = True
    receipt = {"transactionHash": transaction, "status": "0x0" if fault == "reverted" else "0x1",
               "blockNumber": "0x64", "blockHash": "0x" + "c" * 64, "logs": [entry]}

    def respond(request):
        body = json.loads(request.content)
        method = body["method"]
        if method == "eth_getLogs":
            assert body["params"][0]["topics"] == [
                [AUTHORIZATION_USED, AUTHORIZATION_CANCELED], address_topic(order["owner"]), order["payment_nonce"]]
        result = {"eth_chainId": "0x14a34", "eth_blockNumber": "0x64" if fault == "unconfirmed" else "0x65",
                  "eth_getTransactionReceipt": receipt, "eth_getLogs": [entry],
                  "eth_getBlockByNumber": {"hash": "0x" + "d" * 64 if fault == "reorg" else receipt["blockHash"],
                                           "timestamp": hex(9999)}}[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    reader = ChainReader("https://rpc.example", transport=httpx.MockTransport(respond))
    try:
        result = await reader.reconcile(order)
        if fault is None:
            assert result["status"] == "expired_unspent"
            assert result["reason"] == "authorization_cancelled"
            assert result["receipt"]["transaction"] == transaction
            assert result["receipt"]["nonce"] == order["payment_nonce"]
            assert result["receipt"]["confirmation_policy"] == "testnet-l2-depth-2"
            assert result["receipt"]["verified_by"] == "receipt-authorization-cancelled"
        else:
            assert result == {"status": "unknown"}
    finally:
        await reader.close()


async def test_cancellation_reconciliation_rejects_mainnet_orders_before_rpc():
    def unexpected_request(request):
        raise AssertionError("A mainnet order must not reach the RPC endpoint.")

    reader = ChainReader("https://rpc.example", transport=httpx.MockTransport(unexpected_request))
    try:
        with pytest.raises(Problem) as caught:
            await reader.reconcile({"network": "eip155:8453"})
        assert caught.value.code == "wrong_chain"
    finally:
        await reader.close()
