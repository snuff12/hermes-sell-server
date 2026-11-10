import uuid
import json
from pathlib import Path

from eth_account import Account
import httpx
import pytest
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.schemas import PaymentPayload, ResourceInfo

from hermes_sell.contracts import ASSET, NETWORK, Problem, digest
from hermes_sell.payment_signing import sign_order, payment_message


def test_browser_230_payload_matches_python_227_schema_and_eip712():
    fixture = json.loads((Path(__file__).resolve().parents[1] / "web/e2e/fixtures/payment-v2.json").read_text())
    order, raw = fixture["order"], fixture["payload"]
    payload, requirement, authorization = X402Payments.bound_payload(raw, order)
    assert payload.model_dump(by_alias=True, exclude_none=True) == raw
    assert requirement.model_dump(by_alias=True, exclude_none=True) == order["payment_requirements"]
    _, _, message = payment_message(order, order["owner"], valid_before=int(authorization["validBefore"]))
    assert Account.recover_message(message, signature=payload.payload["signature"]).lower() == order["owner"].lower()
from hermes_sell.x402_adapter import X402Payments


async def test_official_sdk_signs_and_serializes_bound_eip3009_payload_offline():
    # Ephemeral unfunded account; private key never leaves this test process.
    account = Account.create()
    order = {'id': str(uuid.uuid4()), 'owner': account.address, 'origin': 'https://seller.example',
             'amount': '100000', 'pay_to': '0x1111111111111111111111111111111111111111'}
    order['order_digest'] = digest(order)
    order['payment_nonce'] = '0x' + order['order_digest']
    requirement = X402Payments.requirements(order)
    scheme = ExactEvmClientScheme(account)
    inner = scheme.create_payment_payload(requirement)
    other = scheme.create_payment_payload(requirement)
    assert inner['authorization']['nonce'] != other['authorization']['nonce']
    raw = sign_order(account, order)
    _, _, signable = payment_message(order, account.address,
        valid_before=int(raw['payload']['authorization']['validBefore']))
    assert Account.recover_message(signable, signature=raw['payload']['signature']) == account.address
    calls = []
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body['x402Version'] == 2
        assert body['paymentRequirements']['network'] == NETWORK
        assert body['paymentRequirements']['asset'] == ASSET
        calls.append(request.url.path)
        if request.url.path == '/verify':
            return httpx.Response(200, json={'isValid': True, 'payer': account.address})
        return httpx.Response(200, json={'success': True, 'payer': account.address, 'transaction': '0x' + 'a' * 64, 'network': NETWORK})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        facilitator = HTTPFacilitatorClient(FacilitatorConfig(url='https://facilitator.example', http_client=client))
        adapter = X402Payments(facilitator)
        verified = await adapter.verify(raw, order)
        assert verified['valid_before']
        assert calls == ['/verify']  # Verification never settles.
        receipt = await adapter.settle(raw, order)
        assert receipt['success'] and calls == ['/verify', '/settle']
        for field, bad in [('amount', '200000'), ('owner', order['pay_to']), ('id', 'another-order')]:
            changed = {**order, field: bad}
            with pytest.raises(Problem):
                await adapter.verify(raw, changed)


@pytest.mark.parametrize('transaction,amount', [('not-a-transaction', '100000'), ('0x' + 'a' * 64, '1')])
async def test_inconsistent_success_receipt_stays_unconfirmed(transaction, amount):
    from x402.schemas import SettleResponse
    account = Account.create()
    order = {'id': str(uuid.uuid4()), 'owner': account.address, 'origin': 'https://seller.example',
             'amount': '100000', 'pay_to': '0x1111111111111111111111111111111111111111'}
    order['order_digest'] = digest(order)
    order['payment_nonce'] = '0x' + order['order_digest']
    requirement = X402Payments.requirements(order)
    raw = sign_order(account, order)
    class Facilitator:
        async def settle(self, payload, requirement):
            return SettleResponse(success=True, network=NETWORK, payer=account.address, transaction=transaction, amount=amount)
    with pytest.raises(RuntimeError, match='expected transaction'):
        await X402Payments(Facilitator()).settle(raw, order)
