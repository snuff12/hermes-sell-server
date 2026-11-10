"""Cross-seller discovery and approval using real local auth and signature checks."""

from copy import deepcopy
import json

from eth_account import Account
from eth_account.messages import encode_defunct
import httpx
import pytest

from hermes_sell.api import public_app
from hermes_sell.buyer import Buyer, TestnetBuyer, purchase_route
from hermes_sell.buyer_gateway import buyer_gateway_app
from hermes_sell.contracts import Problem, canonical, make_revision
from hermes_sell.directory import ServiceDirectory, directory_app, publication_key
from hermes_sell.discovery import DirectoryClient
from hermes_sell.engine import Engine
from hermes_sell.payment_signing import sign_order
from hermes_sell.plugin import register
from hermes_sell.storage import Store
from hermes_sell.vault import AuthorizationVault
from hermes_sell.wallet_auth import WalletAuth
from hermes_sell.x402_adapter import X402Payments
from test_testnet import FixtureRuntime, FixtureChain, FixtureFacilitator

ORIGINS = ['http://127.0.0.1:18801', 'http://127.0.0.1:18803']
DIRECTORY = 'http://127.0.0.1:18805'
GATEWAY = 'http://127.0.0.1:18807'


async def test_two_sellers_share_discovery_but_never_sessions_approvals_or_results(tmp_path, revision, monkeypatch):
    account = Account.create()
    vault = AuthorizationVault(b'b' * 32)
    engines, apps, facilitators, chains = {}, {}, {}, {}
    for number, origin in enumerate(ORIGINS):
        state = tmp_path / str(number)
        state.mkdir()
        facilitator, chain = FixtureFacilitator(account.address), FixtureChain()
        seller = Engine(Store(state / 'seller.sqlite3'), FixtureRuntime(), X402Payments(facilitator, chain=chain), vault=vault)
        spec = {**deepcopy(revision['public']), 'name': f'Seller {number + 1} review', 'amount': '600000'}
        document = make_revision(spec, revision['bundle'])
        seller.put_revision(document)
        report = await seller.test_revision(document['public']['revision'])
        seller.publish(document['public']['revision'], report['id'], True)
        with seller.store.tx() as db:
            db.execute('INSERT INTO meta VALUES(?,?)', (publication_key('diff-review'), canonical({
                'revision': document['public']['revision'], 'directory_origin': DIRECTORY, 'enabled': True})))
        engines[origin], facilitators[origin], chains[origin] = seller, facilitator, chain
        apps[origin] = public_app(seller, origin, worker=False, wallet_auth=WalletAuth(state / 'auth.sqlite3', origin))

    seen = []
    async def network(request):
        origin = str(request.url).split(request.url.path)[0]
        seen.append((origin, request.method, request.url.path))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[origin]), base_url=origin) as client:
            return await client.request(request.method, request.url.path, content=request.content, headers=request.headers)
    transport = httpx.MockTransport(network)
    directory = ServiceDirectory(tmp_path / 'directory.sqlite3', DIRECTORY, ORIGINS, transport=transport)
    listings = [await directory.refresh(origin, 'diff-review') for origin in ORIGINS]
    assert len(directory.search()['listings']) == 2
    buyers = {origin: TestnetBuyer(origin, tmp_path / 'buyer.sqlite3', vault=vault, approval_origin=GATEWAY, transport=transport) for origin in ORIGINS}
    from hermes_sell.buyer_hub import BuyerHub
    original = BuyerHub.for_origin
    def configured(self, origin):
        self.clients[origin] = buyers[origin]
        return original(self, origin)
    monkeypatch.setattr(BuyerHub, 'for_origin', configured)
    app = buyer_gateway_app(buyers[ORIGINS[0]], directory_origin=DIRECTORY)
    # The catalog is still checked through the actual HTTP boundary.
    monkeypatch.setattr(DirectoryClient, 'get', lambda self, identity: get_listing(identity))
    async def get_listing(identity):
        return directory.get(identity)
    purchases = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=GATEWAY) as browser:
            for listing in listings:
                headers = {'Origin': GATEWAY, 'X-Listing-ID': listing['id']}
                config = (await browser.get('/ui-config', headers=headers)).json()
                assert config['seller_origin'] == listing['origin']
                challenge = (await browser.post('/buyer/auth/challenge', json={'address': account.address}, headers=headers)).json()
                signature = '0x' + account.sign_message(encode_defunct(text=challenge['message'])).signature.hex()
                connected = await browser.post('/buyer/auth/verify', json={'payload': {**challenge['payload'], 'signature': signature}}, headers=headers)
                assert connected.status_code == 200
                client = buyers[listing['origin']]
                purchase = await client.quote('diff-review', listing['service']['examples'][0]['input'], expected_revision=listing['service']['revision'])
                purchases.append(purchase)
            chosen, other = purchases[1], purchases[0]
            headers = {'Origin': GATEWAY, 'X-Purchase-ID': chosen['purchase_id']}
            payload = sign_order(account, chosen['order'])
            response = await browser.post('/buyer/purchases/' + chosen['purchase_id'] + '/approve', headers=headers,
                json={'order_digest': chosen['order']['order_digest'], 'authorization': payload})
            assert response.status_code == 200 and response.json()['status'] == 'approved'
            assert 'signature' not in canonical(response.json()) and 'authorization_encrypted' not in canonical(response.json())
            crossed = await browser.post('/buyer/purchases/' + other['purchase_id'] + '/approve',
                headers={'Origin': GATEWAY, 'X-Purchase-ID': other['purchase_id']},
                json={'order_digest': other['order']['order_digest'], 'authorization': payload})
            assert crossed.status_code == 401
            budget = await browser.post('/buyer/purchases/' + other['purchase_id'] + '/approve',
                headers={'Origin': GATEWAY, 'X-Purchase-ID': other['purchase_id']},
                json={'order_digest': other['order']['order_digest'], 'authorization': sign_order(account, other['order'])})
            assert budget.status_code == 409 and budget.json()['error']['code'] == 'budget_exceeded'
            assert purchase_route(tmp_path / 'buyer.sqlite3', chosen['purchase_id']) == (ORIGINS[1], 'testnet')
            # A fresh gateway's configured default is seller 1; purchase 2 still routes to seller 2.
            config = (await browser.get('/ui-config', headers=headers)).json()
            assert config['seller_origin'] == ORIGINS[1]
            await buyers[ORIGINS[1]].purchase(chosen['purchase_id'])
            await engines[ORIGINS[1]].tick()
            chains[ORIGINS[1]].confirmed = True
            await engines[ORIGINS[1]].reconcile_payment(chosen['order']['id'])
            result = (await browser.get('/buyer/purchases/' + chosen['purchase_id'] + '/status')).json()
            assert result['status'] == 'settled' and result['order']['result'] == revision['public']['examples'][0]['output']
            history = (await browser.get('/buyer/purchases')).json()['purchases']
            assert {p['origin'] for p in history} == set(ORIGINS)
            assert facilitators[ORIGINS[0]].settlements == 0 and facilitators[ORIGINS[1]].settlements == 1
            assert not [r for r in seen if r[0] == ORIGINS[0] and r[1] == 'POST' and r[2].startswith('/v1/orders/')]
    finally:
        for buyer in buyers.values():
            await buyer.close()
        await directory.close()


async def test_changed_listing_version_cannot_silently_create_a_quote(tmp_path, revision):
    seen = []
    def network(request):
        seen.append(request.method)
        return httpx.Response(200, json=revision['public'])
    buyer = Buyer(ORIGINS[0], tmp_path / 'buyer.sqlite3', transport=httpx.MockTransport(network))
    try:
        with pytest.raises(Problem, match='version changed'):
            await buyer.quote('diff-review', {}, expected_revision='f' * 64)
        assert seen == ['GET']
        with buyer.db() as db:
            assert db.execute('SELECT count(*) FROM purchases').fetchone()[0] == 0
    finally:
        await buyer.close()


async def test_plugin_status_routes_by_original_purchase_after_default_seller_changes(tmp_path, monkeypatch):
    tools = {}
    buyer = Buyer(ORIGINS[1], tmp_path / 'buyer.sqlite3')
    buyer.save({'purchase_id': 'saved-purchase', 'origin': ORIGINS[1], 'mode': 'simulation', 'status': 'submitted'})
    await buyer.close()
    class Client:
        def __init__(self, origin, path):
            assert origin == ORIGINS[1]
        async def connect(self): pass
        async def status(self, identity): return {'purchase_id': identity, 'status': 'settled'}
        async def close(self): pass
    class Context:
        def get_config(self, key, default=None): return ORIGINS[0] if key == 'buyer_origin' else default
        def register_tool(self, **kwargs): tools[kwargs['name']] = kwargs
        def register_skill(self, *args): pass
    monkeypatch.setattr('hermes_sell.plugin.Buyer', Client)
    register(Context(), data_dir=tmp_path)
    result = json.loads(await tools['service_status']['handler']({'purchase_id': 'saved-purchase'}))
    assert result['result']['status'] == 'settled'
