import asyncio
from copy import deepcopy
from types import SimpleNamespace
import time
import uuid

import httpx
import pytest

from hermes_sell.buyer import Buyer
from hermes_sell.contracts import Problem


def order(*, now=None, amount='100000'):
    return {
        'id': str(uuid.uuid4()), 'order_digest': uuid.uuid4().hex * 2,
        'origin': 'http://testserver', 'slug': 'diff-review', 'revision': 'revision-1',
        'input': {'diff': 'test'}, 'amount': amount,
        'pay_to': '0x1111111111111111111111111111111111111111',
        'network': 'eip155:84532', 'asset': 'USDC',
        'quote_expires_at': (int(time.time()) if now is None else now) + 300,
        'payment': 'required', 'execution': 'created',
    }


def seed(buyer, quote):
    purchase = {'purchase_id': str(uuid.uuid4()), 'origin': buyer.origin,
                'status': 'awaiting_approval', 'order': deepcopy(quote)}
    buyer.save(purchase)
    return purchase['purchase_id']


async def test_slow_status_preserves_concurrent_terminal_approval(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    quote = order()

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=quote)

    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(handler))
    purchase_id = seed(buyer, quote)
    pending = asyncio.create_task(buyer.status(purchase_id))
    await entered.wait()
    approved = buyer.approve_from_terminal(purchase_id, quote['order_digest'])
    release.set()
    observed = await pending
    assert observed['status'] == 'approved'
    assert observed['authorization'] == approved['authorization']
    assert observed['approved_digest'] == approved['approved_digest']
    with pytest.raises(Problem):
        buyer.approve_from_terminal(purchase_id, quote['order_digest'])
    await buyer.close()


async def test_slow_admission_response_cannot_roll_back_settled_purchase(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    quote = order()

    async def handler(request):
        if request.method == 'POST':
            entered.set()
            await release.wait()
            return httpx.Response(202, json={**quote, 'payment': 'verified', 'execution': 'queued'})
        return httpx.Response(200, json={**quote, 'payment': 'settled', 'execution': 'result_ready', 'result': {'ok': True}})

    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(handler))
    purchase_id = seed(buyer, quote)
    approved = buyer.approve_from_terminal(purchase_id, quote['order_digest'])
    pending = asyncio.create_task(buyer.purchase(purchase_id))
    await entered.wait()
    assert (await buyer.status(purchase_id))['status'] == 'settled'
    release.set()
    observed = await pending
    assert observed['status'] == 'settled'
    assert observed['order']['result'] == {'ok': True}
    assert observed['authorization'] == approved['authorization']
    await buyer.close()


async def test_lost_quote_response_returns_durable_id_and_reuses_key(tmp_path):
    quote = order()
    keys = []

    async def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'revision': quote['revision']})
        keys.append(request.headers['Idempotency-Key'])
        if len(keys) == 1:
            raise httpx.ReadTimeout('Quote admitted; response lost', request=request)
        return httpx.Response(201, json=quote)

    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(handler))
    unknown = await buyer.quote('diff-review', quote['input'])
    assert unknown['status'] == 'quote_unknown' and unknown['purchase_id']
    recovered = await buyer.status(unknown['purchase_id'])
    assert recovered['status'] == 'awaiting_approval'
    assert keys == [unknown['purchase_id'], unknown['purchase_id']]
    assert 'authorization' not in recovered
    await buyer.close()


async def test_malformed_quote_response_keeps_recoverable_purchase_id(tmp_path):
    def handler(request):
        return httpx.Response(200, json={'revision': 'r1'} if request.method == 'GET' else {'unexpected': True})
    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(handler))
    pending = await buyer.quote('diff-review', {'diff': 'test'})
    assert pending['status'] == 'quote_unknown'
    assert buyer.get(pending['purchase_id']) == pending
    assert 'order' not in pending
    await buyer.close()


async def test_expired_approval_releases_budget_only_after_confirmed_unsubmitted(tmp_path, monkeypatch):
    clock = [1000]
    monkeypatch.setattr('hermes_sell.buyer.time', SimpleNamespace(time=lambda: clock[0]))
    quote = order(now=1000)

    def handler(request):
        if request.method == 'POST':
            return httpx.Response(400, json={'error': {'code': 'authorization_expiry', 'message': 'expired'}})
        return httpx.Response(200, json=quote)

    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(handler))
    purchase_id = seed(buyer, quote)
    approved = buyer.approve_from_terminal(purchase_id, quote['order_digest'])
    clock[0] = 1200
    expired = await buyer.purchase(purchase_id)
    assert expired['status'] == 'expired'
    assert expired['error']['code'] == 'expired_unsubmitted'
    assert expired['authorization'] == approved['authorization']
    another = order(now=1200, amount='1000000')
    another_id = seed(buyer, another)
    assert buyer.approve_from_terminal(another_id, another['order_digest'])['status'] == 'approved'
    await buyer.close()


async def test_expiry_and_failed_lookup_preserve_uncertain_budget(tmp_path, monkeypatch):
    clock = [1000]
    monkeypatch.setattr('hermes_sell.buyer.time', SimpleNamespace(time=lambda: clock[0]))

    def handler(request):
        return httpx.Response(503, json={'error': {'code': 'unavailable', 'message': 'unknown'}})

    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(handler))
    quote = order(now=1000)
    purchase_id = seed(buyer, quote)
    approved = buyer.approve_from_terminal(purchase_id, quote['order_digest'])
    clock[0] = 1200
    unknown = await buyer.purchase(purchase_id)
    assert unknown['status'] == 'unknown'
    assert unknown['authorization'] == approved['authorization']
    another = order(now=1200, amount='1000000')
    another_id = seed(buyer, another)
    with pytest.raises(Problem) as over_budget:
        buyer.approve_from_terminal(another_id, another['order_digest'])
    assert over_budget.value.code == 'budget_exceeded'
    await buyer.close()


async def test_lookup_started_before_expiry_cannot_prove_unsubmitted_after_expiry(tmp_path, monkeypatch):
    clock = [1000]
    monkeypatch.setattr('hermes_sell.buyer.time', SimpleNamespace(time=lambda: clock[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    quote = order(now=1000)

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=quote)

    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(handler))
    purchase_id = seed(buyer, quote)
    buyer.approve_from_terminal(purchase_id, quote['order_digest'])
    pending = asyncio.create_task(buyer.status(purchase_id))
    await entered.wait()
    clock[0] = 1200
    release.set()
    assert (await pending)['status'] == 'approved'
    await buyer.close()


async def test_changed_order_response_never_replaces_approved_contract(tmp_path):
    quote = order()
    buyer = Buyer('http://testserver', tmp_path / 'buyer.db', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={**quote, 'amount': '200000'})))
    purchase_id = seed(buyer, quote)
    approved = buyer.approve_from_terminal(purchase_id, quote['order_digest'])
    with pytest.raises(Problem) as changed:
        await buyer.status(purchase_id)
    assert changed.value.code == 'order_changed'
    assert buyer.get(purchase_id) == approved
    await buyer.close()
