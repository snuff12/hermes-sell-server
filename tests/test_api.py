import httpx
import pytest
from types import SimpleNamespace

from hermes_sell.api import admin_app, public_app
from hermes_sell.buyer import Buyer
from conftest import authorization, quote


async def test_public_routes_do_not_expose_owner_or_raw_runtime(seller):
    app = public_app(seller, 'http://testserver', worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        for path in ['/admin/revisions', '/v1/runs', '/admin/orders']:
            assert (await client.get(path)).status_code == 404
        assert (await client.get('/v1/orders/missing')).status_code == 401
        assert (await client.post('/dev/session', json={'actor': 'demo-browser'}, headers={'Origin': 'https://evil.example'})).status_code == 403
        assert (await client.get('/v1/services', headers={'CF-Connecting-IP': '1.2.3.4'})).status_code == 403
        assert (await client.get('/health/ready')).json()['live_ready'] is False
        assert (await client.get('/v1/services/diff-review')).headers['cache-control'] == 'no-store'


async def test_admin_requires_own_authentication(seller):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app(seller, 'owner-secret', 'http://testserver')), base_url='http://testserver') as client:
        assert (await client.get('/admin/revisions')).status_code == 401
        assert (await client.get('/admin/revisions', headers={'Authorization': 'Bearer owner-secret'})).status_code == 200
        assert (await client.get('/v1/services')).status_code == 404


async def test_separate_buyer_approval_and_durable_result(seller, tmp_path):
    transport = httpx.ASGITransport(app=public_app(seller, 'http://testserver', worker=False))
    path = tmp_path / 'buyer' / 'ledger.sqlite3'
    buyer = Buyer('http://testserver', path, transport=transport)
    await buyer.connect()
    service = await buyer.inspect('diff-review')
    purchase = await buyer.quote('diff-review', service['examples'][0]['input'])
    from hermes_sell.contracts import Problem
    with pytest.raises(Problem) as unapproved:
        await buyer.purchase(purchase['purchase_id'])
    assert unapproved.value.code == 'human_approval_required'
    buyer.approve_from_terminal(purchase['purchase_id'], purchase['order']['order_digest'])
    accepted = await buyer.purchase(purchase['purchase_id'])
    assert accepted['order']['payment'] == 'verified'
    await buyer.close()
    await seller.tick()
    restarted = Buyer('http://testserver', path, transport=transport)
    await restarted.connect()
    recovered = await restarted.status(purchase['purchase_id'])
    assert recovered['status'] == 'settled' and recovered['order']['result']
    assert seller.runtime.starts == 1
    await restarted.close()


def test_buyer_rejects_remote_origin(tmp_path):
    from hermes_sell.contracts import Problem
    with pytest.raises(Problem):
        Buyer('https://external.example', tmp_path / 'buyer.sqlite3')


async def test_dead_worker_blocks_new_orders_and_admission_but_allows_lookup(seller):
    order = quote(seller)
    app = public_app(seller, 'http://testserver')
    app.state.worker = SimpleNamespace(done=lambda: True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        token = (await client.post('/dev/session', json={'actor': 'demo-browser'})).json()['token']
        client.headers['Authorization'] = 'Bearer ' + token
        assert (await client.get('/health/ready')).status_code == 503
        service = seller.service('diff-review')
        new_quote = await client.post('/v1/services/diff-review/orders', headers={'Idempotency-Key': 'worker-stopped'},
            json={'revision': service['revision'], 'input': service['examples'][0]['input']})
        assert new_quote.status_code == 503
        admission = await client.post('/v1/orders/' + order['id'], json={'authorization': authorization(order)})
        assert admission.status_code == 503
        assert admission.json()['error']['code'] == 'worker_unavailable'
        assert (await client.get('/v1/orders/' + order['id'])).status_code == 200
        assert seller.store.order(order['id'])['payment'] == 'required'


async def test_worker_readiness_rechecked_after_payment_verification(seller):
    order = quote(seller)
    app = public_app(seller, 'http://testserver')
    worker_state = {'stopped': False}
    app.state.worker = SimpleNamespace(done=lambda: worker_state['stopped'])
    original_verify = seller.payments.verify

    async def verify_and_stop(*args):
        verified = await original_verify(*args)
        worker_state['stopped'] = True
        return verified

    seller.payments.verify = verify_and_stop
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        token = (await client.post('/dev/session', json={'actor': 'demo-browser'})).json()['token']
        response = await client.post('/v1/orders/' + order['id'], json={'authorization': authorization(order)},
                                     headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 503
        assert seller.store.order(order['id'])['payment'] == 'required'
        assert not seller.store.all('SELECT * FROM authorizations')


@pytest.mark.parametrize('bad_value', ['NaN', 'Infinity', '1e400', '"\\ud800"'])
async def test_invalid_json_values_return_validation_error_not_server_error(seller, bad_value):
    app = public_app(seller, 'http://testserver', worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.post('/dev/session', content='{"actor":' + bad_value + '}', headers={'Content-Type': 'application/json'})
        assert response.status_code == 422
        assert response.json()['error']['code'] == 'invalid_json'


async def test_owner_can_reopen_exact_revision_without_public_bundle_exposure(seller, revision):
    revision_id = revision['public']['revision']
    path = '/admin/revisions/' + revision_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app(seller, 'private-token', 'http://testserver')), base_url='http://testserver') as owner:
        assert (await owner.get(path)).status_code == 401
        response = await owner.get(path, headers={'Authorization': 'Bearer private-token'})
        assert response.status_code == 200
        assert response.json() == revision
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app(seller, 'http://testserver', worker=False)), base_url='http://testserver') as public:
        assert (await public.get(path)).status_code == 404
        contract = (await public.get('/v1/services/diff-review')).json()
        assert 'bundle' not in contract and 'execution_digest' not in contract
