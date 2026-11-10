"""Signer-free buyer checks with local signatures and a mock seller transport."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import time
import uuid

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
import httpx
import pytest

from hermes_sell.buyer import TestnetBuyer
from hermes_sell.buyer_gateway import buyer_gateway_app
from hermes_sell.contracts import ASSET, NETWORK, PROTOCOL, Problem, digest
from hermes_sell.vault import AuthorizationVault
from hermes_sell.wallet_auth import WalletAuth
from hermes_sell.x402_adapter import X402Payments


SELLER = "http://127.0.0.1:8765"
GATEWAY = "http://127.0.0.1:8767"
LOCAL_HEADERS = {"Origin": GATEWAY}


def signed_payment(account, order, *, valid_before=None, chain=84532, changes=None):
    """Build typed data independently from the production signing helper."""
    authorization = {
        "from": account.address, "to": order["pay_to"], "value": order["amount"],
        "validAfter": "0", "validBefore": str(valid_before or int(time.time()) + 180),
        "nonce": order["payment_nonce"],
    }
    authorization.update(changes or {})
    types = {"TransferWithAuthorization": [
        {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
    ]}
    message = {**authorization, "value": int(authorization["value"]),
               "validAfter": int(authorization["validAfter"]), "validBefore": int(authorization["validBefore"])}
    signable = encode_typed_data(
        domain_data={"name": "USDC", "version": "2", "chainId": chain, "verifyingContract": ASSET},
        message_types=types, message_data=message,
    )
    signature = "0x" + account.sign_message(signable).signature.hex()
    return {"x402Version": 2, "accepted": deepcopy(order["payment_requirements"]),
            "resource": {"url": SELLER + "/v1/orders/" + order["id"]},
            "payload": {"authorization": authorization, "signature": signature}}


class MockSeller:
    def __init__(self, tmp_path, revision):
        self.auth = WalletAuth(tmp_path / "seller-auth.sqlite3", SELLER)
        self.service = revision["public"]
        self.orders = {}
        self.keys = {}
        self.requests = []
        self.admissions = []
        self.drop_admission = False
        self.fail_lookup = False

    async def __call__(self, request):
        self.requests.append((request.method, request.url.path))
        try:
            body = json.loads(request.content) if request.content else {}
            if request.url.path == "/auth/challenge":
                return httpx.Response(200, json=self.auth.challenge(body["address"]))
            if request.url.path == "/auth/verify":
                return httpx.Response(200, json=await self.auth.verify(body["payload"]))
            if request.url.path == "/v1/services/diff-review":
                return httpx.Response(200, json=self.service)
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            owner = self.auth.owner(token)
            if request.url.path == "/v1/services/diff-review/orders":
                key = request.headers["Idempotency-Key"]
                if key not in self.keys:
                    order_id = str(uuid.uuid4())
                    current = int(time.time())
                    order = {"id": order_id, "owner": owner, "slug": "diff-review", "revision": body["revision"],
                             "input": body["input"], "origin": SELLER, "amount": self.service["amount"],
                             "pay_to": self.service["pay_to"], "network": NETWORK, "asset": ASSET,
                             "created_at": current, "quote_expires_at": current + 300, "execution": "created",
                             "payment": "required", "run_key": "sell-" + order_id, "protocol": PROTOCOL, "mode": "testnet"}
                    order["order_digest"] = digest(order)
                    order["payment_nonce"] = "0x" + order["order_digest"]
                    order.pop("run_key")
                    order["payment_requirements"] = X402Payments.requirements(order).model_dump(by_alias=True, exclude_none=True)
                    order["service_contract"] = deepcopy(self.service)
                    self.orders[order_id] = order
                    self.keys[key] = order_id
                return httpx.Response(201, json=self.orders[self.keys[key]])
            order = self.orders[request.url.path.split("/")[-1]]
            if order["owner"] != owner:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "Order not found."}})
            if request.method == "POST":
                self.admissions.append(deepcopy(body["authorization"]))
                order.update(payment="verified", execution="queued")
                if self.drop_admission:
                    raise httpx.ReadTimeout("The admission response was lost", request=request)
                return httpx.Response(202, json=order)
            if self.fail_lookup:
                return httpx.Response(503, json={"error": {"code": "unavailable", "message": "Unavailable"}})
            return httpx.Response(200, json=order)
        except Problem as exc:
            return httpx.Response(exc.status, json={"error": {"code": exc.code, "message": exc.message}})


@pytest.fixture
async def setup(tmp_path, revision):
    seller = MockSeller(tmp_path, revision)
    buyer = TestnetBuyer(SELLER, tmp_path / "buyer.sqlite3", vault=AuthorizationVault(b"b" * 32),
                        transport=httpx.MockTransport(seller))
    app = buyer_gateway_app(buyer)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=GATEWAY)
    account = Account.create()
    yield SimpleNamespace(seller=seller, buyer=buyer, client=client, account=account, app=app)
    await client.aclose()
    await buyer.close()


async def connect(setup, account=None):
    account = account or setup.account
    response = await setup.client.post("/buyer/auth/challenge", headers=LOCAL_HEADERS, json={"address": account.address})
    assert response.status_code == 200, response.text
    challenge = response.json()
    signature = "0x" + account.sign_message(encode_defunct(text=challenge["message"])).signature.hex()
    proof = {**challenge["payload"], "signature": signature}
    response = await setup.client.post("/buyer/auth/verify", headers=LOCAL_HEADERS, json={"payload": proof})
    assert response.status_code == 200, response.text
    assert response.json()["connected"] is True
    assert "token" not in response.json()
    return response.json()


async def quote(setup):
    return await setup.buyer.quote("diff-review", setup.seller.service["examples"][0]["input"])


async def approve(setup, purchase, authorization=None):
    authorization = authorization or signed_payment(setup.account, purchase["order"])
    return await setup.client.post("/buyer/purchases/" + purchase["purchase_id"] + "/approve", headers=LOCAL_HEADERS,
                                   json={"order_digest": purchase["order"]["order_digest"], "authorization": authorization})


def assert_redacted(document):
    assert "authorization_encrypted" not in document
    assert "authorization" not in document
    assert "token" not in document


async def test_agent_requires_browser_connection_and_cannot_approve(setup):
    result = await quote(setup)
    assert result["status"] == "needs_wallet_connection"
    assert result["approval_url"] == GATEWAY + "/connect"
    assert setup.seller.requests == []
    await connect(setup)
    purchase = await quote(setup)
    assert purchase["status"] == "awaiting_approval"
    assert purchase["approval_url"].endswith("/approve/" + purchase["purchase_id"])
    with pytest.raises(Problem) as caught:
        await setup.buyer.purchase(purchase["purchase_id"])
    assert caught.value.code == "human_approval_required"
    with pytest.raises(Problem) as caught:
        setup.buyer.approve_from_terminal(purchase["purchase_id"], purchase["order"]["order_digest"])
    assert caught.value.code == "wallet_approval_required"
    assert setup.seller.admissions == []


async def test_purchase_review_uses_original_seller_session_and_rejects_unsettled_or_other_wallet(setup):
    await connect(setup)
    purchase = await quote(setup)
    path = '/buyer/purchases/' + purchase['purchase_id'] + '/review-challenge'
    body = {'score': 4, 'comment': 'Useful findings.'}
    assert (await setup.client.post(path, headers=LOCAL_HEADERS, json=body)).status_code == 409
    saved = setup.buyer.get(purchase['purchase_id'])
    saved['status'] = 'settled'; saved['order']['payment'] = 'settled'
    setup.buyer.save(saved)
    session = setup.buyer._session_for(saved)
    calls = []
    async def upstream(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer ' + session['token']
        # Deliberately malformed upstream data must not escape as an exception or session leak.
        return httpx.Response(200, json=['unexpected', session['token']])
    original = setup.buyer.client
    setup.buyer.client = httpx.AsyncClient(base_url=SELLER, transport=httpx.MockTransport(upstream))
    try:
        response = await setup.client.post(path, headers=LOCAL_HEADERS, json=body)
        assert response.status_code == 502 and session['token'] not in response.text
        assert str(calls[0].url) == SELLER + '/v1/orders/' + saved['order']['id'] + '/review-challenge'
        assert len(calls) == 1
        for invalid in [True, '4', 0, 6]:
            assert (await setup.client.post(path, headers=LOCAL_HEADERS, json={**body, 'score': invalid})).status_code == 422
        assert len(calls) == 1
        setup.buyer.disconnect()
        assert (await setup.client.post(path, headers=LOCAL_HEADERS, json=body)).status_code == 401
        assert len(calls) == 1
    finally:
        await setup.buyer.client.aclose()
        setup.buyer.client = original


async def test_browser_approves_exact_quote_then_agent_submits_encrypted_payload(setup):
    session_info = await connect(setup)
    purchase = await quote(setup)
    displayed = await setup.client.get("/buyer/purchases/" + purchase["purchase_id"])
    assert displayed.json()["order"] == purchase["order"]
    payload = signed_payment(setup.account, purchase["order"])
    response = await approve(setup, purchase, payload)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert_redacted(response.json())
    assert setup.seller.admissions == []
    stored = setup.buyer.get(purchase["purchase_id"])
    assert stored["budget_reserved"] is True
    assert "authorization_encrypted" in stored and "authorization" not in stored
    session = setup.buyer._session()
    with setup.buyer.db() as db:
        documents = str(db.execute("SELECT * FROM purchases").fetchall()) + str(db.execute("SELECT * FROM buyer_wallet_sessions").fetchall())
    assert session["token"] not in documents
    assert payload["payload"]["signature"] not in documents
    submitted = await setup.buyer.purchase(purchase["purchase_id"])
    assert submitted["status"] == "submitted"
    assert_redacted(submitted)
    assert setup.seller.admissions == [payload]
    assert session_info["owner"] == purchase["order"]["owner"]
    assert "/dev/session" not in [path for _, path in setup.seller.requests]


async def test_repeated_approval_is_idempotent_and_cannot_replace_saved_signature(setup):
    await connect(setup)
    purchase = await quote(setup)
    payload = signed_payment(setup.account, purchase["order"])
    assert (await approve(setup, purchase, payload)).status_code == 200
    original = setup.buyer.get(purchase["purchase_id"])
    assert (await approve(setup, purchase, payload)).status_code == 200
    assert setup.buyer.get(purchase["purchase_id"]) == original
    changed = signed_payment(setup.account, purchase["order"], valid_before=int(payload["payload"]["authorization"]["validBefore"]) - 1)
    response = await approve(setup, purchase, changed)
    assert response.status_code == 409
    assert setup.buyer.get(purchase["purchase_id"]) == original


@pytest.mark.parametrize("changes", [
    {"nonce": "0x" + "11" * 32}, {"value": "100001"},
    {"to": "0x2222222222222222222222222222222222222222"}, {"validAfter": "1"},
])
async def test_valid_signature_for_changed_payment_fields_is_rejected(setup, changes):
    await connect(setup)
    purchase = await quote(setup)
    response = await approve(setup, purchase, signed_payment(setup.account, purchase["order"], changes=changes))
    assert response.status_code == 401
    assert setup.buyer.get(purchase["purchase_id"])["status"] == "awaiting_approval"
    assert setup.seller.admissions == []


async def test_other_chain_and_other_signer_signatures_are_rejected(setup):
    await connect(setup)
    purchase = await quote(setup)
    wrong_chain = signed_payment(setup.account, purchase["order"], chain=8453)
    assert (await approve(setup, purchase, wrong_chain)).status_code == 401
    wrong_wallet = signed_payment(Account.create(), purchase["order"])
    assert (await approve(setup, purchase, wrong_wallet)).status_code == 401
    assert "authorization_encrypted" not in setup.buyer.get(purchase["purchase_id"])


async def test_wrong_digest_and_short_authorization_are_rejected(setup):
    await connect(setup)
    purchase = await quote(setup)
    payload = signed_payment(setup.account, purchase["order"])
    response = await setup.client.post("/buyer/purchases/" + purchase["purchase_id"] + "/approve", headers=LOCAL_HEADERS,
        json={"order_digest": "f" * 64, "authorization": payload})
    assert response.status_code == 409
    short = signed_payment(setup.account, purchase["order"], valid_before=int(time.time()) + 119)
    assert (await approve(setup, purchase, short)).status_code == 409


async def test_concurrent_approvals_cannot_exceed_budget(setup):
    await connect(setup)
    setup.buyer.budget = 100000
    first, second = await quote(setup), await quote(setup)
    async def attempt(purchase):
        return await asyncio.to_thread(setup.buyer.approve_from_browser, purchase["purchase_id"],
                                       purchase["order"]["order_digest"], signed_payment(setup.account, purchase["order"]))
    outcomes = await asyncio.gather(attempt(first), attempt(second), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    failures = [value for value in outcomes if isinstance(value, Problem)]
    assert len(failures) == 1 and failures[0].code == "budget_exceeded"
    with setup.buyer.db() as db:
        records = [json.loads(row[0]) for row in db.execute("SELECT document FROM purchases")]
    assert sum(record["order"]["amount"] == "100000" for record in records if record.get("budget_reserved")) == 1


async def test_unknown_submission_reuses_same_authorization_and_budget(setup):
    await connect(setup)
    purchase = await quote(setup)
    payload = signed_payment(setup.account, purchase["order"])
    assert (await approve(setup, purchase, payload)).status_code == 200
    setup.seller.drop_admission = True
    setup.seller.fail_lookup = True
    unknown = await setup.buyer.purchase(purchase["purchase_id"])
    assert unknown["status"] == "unknown"
    assert_redacted(unknown)
    assert setup.buyer.get(purchase["purchase_id"])["budget_reserved"] is True
    setup.seller.drop_admission = False
    setup.seller.fail_lookup = False
    retried = await setup.buyer.purchase(purchase["purchase_id"])
    assert retried["status"] == "submitted"
    assert setup.seller.admissions == [payload, payload]


async def test_wallet_reconnection_preserves_order_and_requires_original_wallet(setup):
    await connect(setup)
    purchase = await quote(setup)
    payload = signed_payment(setup.account, purchase["order"])
    assert (await approve(setup, purchase, payload)).status_code == 200
    setup.buyer.disconnect()
    needed = await setup.buyer.purchase(purchase["purchase_id"])
    assert needed["status"] == "needs_wallet_connection"
    assert needed["purchase_id"] == purchase["purchase_id"]
    await connect(setup, Account.create())
    with pytest.raises(Problem) as caught:
        await setup.buyer.purchase(purchase["purchase_id"])
    assert caught.value.code == "wallet_mismatch"
    await connect(setup)
    assert (await setup.buyer.purchase(purchase["purchase_id"]))["status"] == "submitted"
    assert setup.seller.admissions == [payload]


async def test_restarted_buyer_reads_encrypted_session_and_existing_approval(setup):
    await connect(setup)
    purchase = await quote(setup)
    payload = signed_payment(setup.account, purchase["order"])
    assert (await approve(setup, purchase, payload)).status_code == 200
    reopened = TestnetBuyer(SELLER, setup.buyer.path, vault=AuthorizationVault(b"b" * 32),
                           transport=httpx.MockTransport(setup.seller))
    try:
        assert (await reopened.connect())["connected"] is True
        assert (await reopened.purchase(purchase["purchase_id"]))["status"] == "submitted"
        assert setup.seller.admissions == [payload]
    finally:
        await reopened.close()


async def test_only_fresh_unsubmitted_expiry_releases_reserved_budget(setup, monkeypatch):
    await connect(setup)
    purchase = await quote(setup)
    payload = signed_payment(setup.account, purchase["order"])
    assert (await approve(setup, purchase, payload)).status_code == 200
    expiry = int(payload["payload"]["authorization"]["validBefore"])
    monkeypatch.setattr("hermes_sell.buyer.time", SimpleNamespace(time=lambda: expiry + 1))
    assert setup.buyer.get(purchase["purchase_id"])["budget_reserved"] is True
    result = await setup.buyer.status(purchase["purchase_id"])
    assert result["status"] == "expired"
    assert setup.buyer.get(purchase["purchase_id"])["budget_reserved"] is False
    assert_redacted(result)


async def test_changed_order_cannot_replace_an_approved_contract(setup):
    await connect(setup)
    purchase = await quote(setup)
    assert (await approve(setup, purchase)).status_code == 200
    original = setup.buyer.get(purchase["purchase_id"])
    setup.seller.orders[purchase["order"]["id"]]["input"] = {"diff": "changed"}
    with pytest.raises(Problem) as caught:
        await setup.buyer.status(purchase["purchase_id"])
    assert caught.value.code == "invalid_quote"
    assert setup.buyer.get(purchase["purchase_id"]) == original


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "https://attacker.example"}, {"Origin": "null"},
    {"Origin": GATEWAY, "Host": "localhost:8767"},
    {"Origin": GATEWAY, "Host": "attacker.example"},
    {"Origin": GATEWAY, "X-Forwarded-Host": "127.0.0.1:8767"},
    {"Origin": GATEWAY, "Forwarded": "for=127.0.0.1"},
    {"Origin": GATEWAY, "CF-Connecting-IP": "127.0.0.1"},
    {"Origin": GATEWAY, "Sec-Fetch-Site": "cross-site"},
])
async def test_gateway_rejects_foreign_or_missing_origin_and_proxy_requests(setup, headers):
    response = await setup.client.post("/buyer/auth/challenge", headers=headers, json={"address": setup.account.address})
    assert response.status_code == 403
    assert setup.seller.requests == []


async def test_gateway_rejects_remote_peers_and_cross_site_reads(setup):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app, client=("203.0.113.1", 4000)), base_url=GATEWAY) as remote:
        assert (await remote.get("/ui-config")).status_code == 403
    assert (await setup.client.get("/buyer/session", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
    config = (await setup.client.get("/ui-config")).json()
    assert config["role"] == "buyer" and config["seller_origin"] == SELLER
    assert config["origin"] == GATEWAY


async def test_gateway_bounds_bodies_and_does_not_echo_validation_input(setup):
    response = await setup.client.post("/buyer/auth/verify", headers=LOCAL_HEADERS, json={"payload": "private-proof-sentinel"})
    assert response.status_code == 422 and "private-proof-sentinel" not in response.text
    response = await setup.client.post("/buyer/auth/challenge", headers=LOCAL_HEADERS, json={"address": "x" * 180001})
    assert response.status_code == 413
    response = await setup.client.post("/buyer/auth/challenge", headers={**LOCAL_HEADERS, "Content-Type": "application/json"}, content='{"address":NaN}')
    assert response.status_code == 422
    assert (await setup.client.post("/buyer/purchases/any/submit", headers=LOCAL_HEADERS)).status_code == 404
    assert (await setup.client.get("/openapi.json")).status_code == 404


async def test_gateway_does_not_accept_sessions_or_proofs_for_unrequested_challenges(setup):
    challenge = setup.seller.auth.challenge(setup.account.address)
    signature = "0x" + setup.account.sign_message(encode_defunct(text=challenge["message"])).signature.hex()
    response = await setup.client.post("/buyer/auth/verify", headers=LOCAL_HEADERS,
                                       json={"payload": {**challenge["payload"], "signature": signature}})
    assert response.status_code == 401
    assert setup.buyer.session_info() == {"connected": False}


async def test_wrong_vault_key_does_not_trigger_new_login_or_signature(setup):
    await connect(setup)
    reopened = TestnetBuyer(SELLER, setup.buyer.path, vault=AuthorizationVault(b"c" * 32),
                           transport=httpx.MockTransport(setup.seller))
    before = list(setup.seller.requests)
    try:
        with pytest.raises(Problem) as caught:
            await reopened.connect()
        assert caught.value.code == "authorization_unavailable"
        assert setup.seller.requests == before
    finally:
        await reopened.close()


async def test_rejected_first_quote_preserves_original_recovery_handle(setup):
    await connect(setup)
    original_transport = setup.seller
    rejected = False
    keys = []

    async def handler(request):
        nonlocal rejected
        response = await original_transport(request)
        if request.url.path.endswith("/orders"):
            keys.append(request.headers["Idempotency-Key"])
            if not rejected:
                rejected = True
                return httpx.Response(201, json={**response.json(), "origin": "https://other.example"})
        return response

    setup.buyer.client._transport = httpx.MockTransport(handler)
    pending = await quote(setup)
    assert pending["status"] == "quote_unknown"
    assert "order" not in pending
    assert setup.buyer.get(pending["purchase_id"])["status"] == "quote_unknown"
    restored = await setup.buyer.status(pending["purchase_id"])
    assert restored["status"] == "awaiting_approval"
    assert keys == [pending["purchase_id"], pending["purchase_id"]]
    assert len(setup.seller.orders) == 1
    assert setup.seller.admissions == []


async def test_disconnect_invalidates_inflight_wallet_sign_in(setup):
    challenge = await setup.buyer.wallet_challenge(setup.account.address)
    signature = "0x" + setup.account.sign_message(encode_defunct(text=challenge["message"])).signature.hex()
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        response = await setup.seller(request)
        if request.url.path == "/auth/verify":
            entered.set()
            await release.wait()
        return response

    setup.buyer.client._transport = httpx.MockTransport(handler)
    pending = asyncio.create_task(setup.buyer.wallet_verify({**challenge["payload"], "signature": signature}))
    await entered.wait()
    assert setup.buyer.disconnect() == {"connected": False}
    release.set()
    with pytest.raises(Problem) as caught:
        await pending
    assert caught.value.code == "wallet_challenge_used"
    assert setup.buyer.session_info() == {"connected": False}
    with setup.buyer.db() as db:
        assert db.execute("SELECT COUNT(*) FROM buyer_wallet_sessions").fetchone()[0] == 0


async def test_disconnect_rejects_challenge_response_started_before_disconnect(setup):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        response = await setup.seller(request)
        if request.url.path == "/auth/challenge":
            entered.set()
            await release.wait()
        return response

    setup.buyer.client._transport = httpx.MockTransport(handler)
    pending = asyncio.create_task(setup.buyer.wallet_challenge(setup.account.address))
    await entered.wait()
    setup.buyer.disconnect()
    release.set()
    with pytest.raises(Problem) as caught:
        await pending
    assert caught.value.code == "wallet_disconnected"
    assert setup.buyer.session_info() == {"connected": False}
    with setup.buyer.db() as db:
        assert db.execute("SELECT COUNT(*) FROM buyer_wallet_challenges").fetchone()[0] == 0
    # A subsequent explicit connection remains possible after the old response is rejected.
    assert (await connect(setup))["connected"] is True


async def test_restored_unknown_order_keeps_expired_authorization_budget_reserved(setup, monkeypatch):
    await connect(setup)
    purchase = await quote(setup)
    payload = signed_payment(setup.account, purchase["order"])
    assert (await approve(setup, purchase, payload)).status_code == 200
    expiry = int(payload["payload"]["authorization"]["validBefore"])
    monkeypatch.setattr("hermes_sell.buyer.time", SimpleNamespace(time=lambda: expiry + 1))

    async def restored_view(request):
        response = await setup.seller(request)
        # A restored pre-admission snapshot cannot establish that a later
        # admission or settlement did not happen. The seller masks that state.
        if request.url.path.startswith("/v1/orders/"):
            assert response.json()["payment"] == "required"
            return httpx.Response(200, json={**response.json(), "payment": "unknown", "execution": "unknown",
                                             "recovery_required": True, "error": "recovery_required"})
        return response

    setup.buyer.client._transport = httpx.MockTransport(restored_view)
    result = await setup.buyer.status(purchase["purchase_id"])
    assert result["status"] == "unknown"
    assert result["order"]["recovery_required"] is True
    assert setup.buyer.get(purchase["purchase_id"])["budget_reserved"] is True
    assert setup.seller.admissions == []


@pytest.mark.parametrize("recovery_required", [0, 1, "true", None, []])
async def test_quote_recovery_flag_requires_a_boolean(setup, recovery_required):
    await connect(setup)
    purchase = await quote(setup)
    original = setup.buyer.get(purchase["purchase_id"])
    setup.seller.orders[purchase["order"]["id"]]["recovery_required"] = recovery_required
    with pytest.raises(Problem) as caught:
        await setup.buyer.status(purchase["purchase_id"])
    assert caught.value.code == "invalid_quote"
    assert setup.buyer.get(purchase["purchase_id"]) == original
