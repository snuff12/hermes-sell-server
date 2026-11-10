import json
from pathlib import Path
import time

from eth_account import Account
from eth_account.messages import encode_defunct
import httpx
import pytest
from x402.schemas import VerifyResponse, SettleResponse

from hermes_sell.adapters import SimulationRuntime
from hermes_sell.api import public_app, admin_app
from hermes_sell.chain import ChainReader, TRANSFER, AUTHORIZATION_USED, address_topic
from hermes_sell.contracts import ASSET, NETWORK, Problem, digest
from hermes_sell.engine import Engine
from hermes_sell.payment_signing import sign_order
from hermes_sell.storage import Store
from hermes_sell.vault import AuthorizationVault
from hermes_sell.wallet_auth import WalletAuth
from hermes_sell.x402_adapter import X402Payments


class FixtureRuntime(SimulationRuntime):
    # Contract fixture only; never instantiated by the testnet CLI.
    mode = "hermes"


class FixtureChain:
    confirmed = False

    async def block_number(self):
        return 100

    async def reconcile(self, order):
        if not self.confirmed:
            return {"status": "unknown"}
        return {"status": "settled", "receipt": {**order["settlement_candidate"], "verified_by": "fixture"}}


class FixtureFacilitator:
    def __init__(self, address):
        self.address = address
        self.settlements = 0

    async def verify(self, payload, requirements):
        return VerifyResponse(is_valid=True, payer=self.address)

    async def settle(self, payload, requirements):
        self.settlements += 1
        return SettleResponse(success=True, payer=self.address, transaction="0x" + "a" * 64, network=NETWORK)


async def test_wallet_to_encrypted_order_to_confirmed_result(tmp_path, revision):
    account = Account.create()
    origin = "http://127.0.0.1:8785"
    chain = FixtureChain()
    facilitator = FixtureFacilitator(account.address)
    vault = AuthorizationVault.create(tmp_path / "authorization.key")
    seller = Engine(Store(tmp_path / "seller.sqlite3"), FixtureRuntime(),
                    X402Payments(facilitator, chain=chain), vault=vault)
    seller.put_revision(revision)
    report = await seller.test_revision(revision["public"]["revision"])
    seller.publish(revision["public"]["revision"], report["id"], True)
    auth = WalletAuth(tmp_path / "auth.sqlite3", origin)
    app = public_app(seller, origin, wallet_auth=auth, worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=origin) as client:
        assert (await client.post("/dev/session", json={"actor": "demo-browser"})).status_code == 404
        challenge = (await client.post("/auth/challenge", json={"address": account.address})).json()
        signature = "0x" + account.sign_message(encode_defunct(text=challenge["message"])).signature.hex()
        proof = {**challenge["payload"], "signature": signature}
        session = (await client.post("/auth/verify", json={"payload": proof})).json()
        assert (await client.post("/auth/verify", json={"payload": proof})).status_code == 401
        client.headers["Authorization"] = "Bearer " + session["token"]
        service = revision["public"]
        response = await client.post("/v1/services/diff-review/orders", headers={"Idempotency-Key": "testnet-full-flow"},
            json={"revision": service["revision"], "input": service["examples"][0]["input"]})
        assert response.status_code == 201
        order = response.json()
        payload = sign_order(account, order)
        admission = await client.post("/v1/orders/" + order["id"], json={"authorization": payload})
        assert admission.status_code == 202
        assert "authorization" not in admission.json()
        stored = seller.store.order(order["id"])
        assert "authorization" not in stored and stored["authorization_encrypted"]
        assert payload["payload"]["signature"] not in Path(seller.store.path).read_bytes().decode(errors="ignore")
        assert vault.open(stored["authorization_encrypted"], stored) == payload
        await seller.tick()
        pending = (await client.get("/v1/orders/" + order["id"])).json()
        assert pending["payment"] == "unknown" and "result" not in pending
        assert facilitator.settlements == 1
        chain.confirmed = True
        await seller.reconcile_payment(order["id"])
        settled = (await client.get("/v1/orders/" + order["id"])).json()
        assert settled["payment"] == "settled" and settled["result"]
        assert seller.store.order(order["id"])["authorization_encrypted"] is None
        assert facilitator.settlements == 1
        replay = await client.post("/v1/orders/" + order["id"], json={"authorization": payload})
        assert replay.status_code == 202 and replay.json()["payment"] == "settled"
        assert (await client.get("/v1/services", headers={"Host": "evil.example"})).status_code == 403
        assert (await client.post("/auth/challenge", json={"address": account.address},
            headers={"Origin": "https://evil.example"})).status_code == 403
    owner = admin_app(seller, "a" * 32, "http://127.0.0.1:8786")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(owner), base_url="http://127.0.0.1:8786") as client:
        response = await client.get("/admin/orders", headers={"Authorization": "Bearer " + "a" * 32, "CF-Connecting-IP": "1.2.3.4"})
        assert response.status_code == 403


def test_encrypted_approval_cannot_move_to_another_order_or_key(tmp_path):
    vault = AuthorizationVault.create(tmp_path / "key")
    order = {"id": "order-1", "order_digest": "a" * 64}
    sealed = vault.seal({"signature": "private-authorization"}, order)
    for replacement in ({**order, "id": "order-2"}, {**order, "order_digest": "b" * 64}):
        with pytest.raises(Problem):
            vault.open(sealed, replacement)
    with pytest.raises(Problem):
        AuthorizationVault(b"x" * 32).open(sealed, order)
    assert AuthorizationVault.load(tmp_path / "key").open(sealed, order)["signature"] == "private-authorization"
    (tmp_path / "key").chmod(0o644)
    with pytest.raises(ValueError):
        AuthorizationVault.load(tmp_path / "key")


def test_payment_signature_cannot_be_rebound_by_changing_unsigned_resource():
    account = Account.create()
    order = {"id": "first", "owner": account.address, "origin": "https://seller.example",
             "amount": "100000", "pay_to": "0x" + "1" * 40, "order_digest": "a" * 64,
             "payment_nonce": "0x" + "a" * 64}
    payload = sign_order(account, order)
    different = {**order, "id": "second", "order_digest": "b" * 64, "payment_nonce": "0x" + "b" * 64}
    payload["resource"]["url"] = "https://seller.example/v1/orders/second"
    with pytest.raises(Problem):
        X402Payments.bound_payload(payload, different)


@pytest.mark.parametrize("fault", [None, "wrong_transfer", "wrong_nonce", "other_contract", "reverted", "reorg", "unconfirmed"])
async def test_receipt_requires_matching_transfer_and_authorization(fault):
    transaction = "0x" + "b" * 64
    order = {"owner": "0x" + "1" * 40, "pay_to": "0x" + "2" * 40, "amount": "100000",
             "payment_nonce": "0x" + "3" * 64, "network": NETWORK}
    receipt = {"transactionHash": transaction, "status": "0x1", "blockNumber": "0x64", "blockHash": "0x" + "c" * 64,
        "logs": [{"address": ASSET, "topics": [AUTHORIZATION_USED, address_topic(order["owner"]), order["payment_nonce"]], "data": "0x"},
                 {"address": ASSET, "topics": [TRANSFER, address_topic(order["owner"]), address_topic(order["pay_to"])], "data": hex(100000)}]}
    if fault == "wrong_transfer":
        receipt["logs"][1]["data"] = hex(1)
    if fault == "wrong_nonce":
        receipt["logs"][0]["topics"][2] = "0x" + "4" * 64
    if fault == "other_contract":
        receipt["logs"][0]["address"] = order["pay_to"]
    if fault == "reverted":
        receipt["status"] = "0x0"

    def respond(request):
        method = json.loads(request.content)["method"]
        result = {"eth_chainId": "0x14a34", "eth_blockNumber": "0x64" if fault == "unconfirmed" else "0x65",
                  "eth_getTransactionReceipt": receipt,
                  "eth_getBlockByNumber": {"hash": "different" if fault == "reorg" else receipt["blockHash"]}}[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    reader = ChainReader("https://rpc.example", transport=httpx.MockTransport(respond))
    try:
        confirmed = await reader.confirm(order, transaction)
        assert bool(confirmed) == (fault is None)
    finally:
        await reader.close()
