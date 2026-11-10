from copy import deepcopy
from contextlib import closing
import json

from eth_account import Account
from eth_account.messages import encode_defunct
import httpx
import pytest

from conftest import authorization
from hermes_sell.api import public_app
from hermes_sell.chain import ChainReader, AUTHORIZATION_USED, TRANSFER, address_topic
from hermes_sell.contracts import ASSET, Problem, canonical
from hermes_sell.directory import ServiceDirectory, publication_key, directory_app
from hermes_sell.discovery import DirectoryClient
from hermes_sell.engine import Engine
from hermes_sell.payment_signing import sign_order
from hermes_sell.reviews import issuer_key, purchase_proof
from hermes_sell.storage import Store
from hermes_sell.vault import AuthorizationVault
from hermes_sell.x402_adapter import X402Payments
from test_testnet import FixtureRuntime, FixtureChain, FixtureFacilitator

ORIGIN = 'http://127.0.0.1:18801'
DIRECTORY = 'http://127.0.0.1:18805'


@pytest.fixture
async def paid(tmp_path, revision):
    account = Account.create()
    execution_chain = FixtureChain()
    seller = Engine(Store(tmp_path / 'seller.sqlite3'), FixtureRuntime(),
                    X402Payments(FixtureFacilitator(account.address), chain=execution_chain), vault=AuthorizationVault(b'b' * 32))
    seller.put_revision(revision)
    report = await seller.test_revision(revision['public']['revision'])
    seller.publish(revision['public']['revision'], report['id'], True)
    service = revision['public']
    value = {**service['examples'][0]['input'], 'context': 'Private customer context, never publish this.'}
    order = seller.create_order(account.address.lower(), service['slug'], service['revision'], value, 'review-purchase', ORIGIN)
    await seller.admit(order['id'], account.address.lower(), sign_order(account, order))
    await seller.tick(); execution_chain.confirmed = True
    await seller.reconcile_payment(order['id'])
    order = seller.store.order(order['id'])
    with seller.store.tx() as db:
        db.execute('INSERT INTO meta VALUES(?,?)', (publication_key('diff-review'), canonical({
            'revision': service['revision'], 'enabled': True, 'directory_origin': DIRECTORY})))
    # Wallet auth is not used by the public issuer endpoint.
    from hermes_sell.wallet_auth import WalletAuth
    source = public_app(seller, ORIGIN, worker=False, wallet_auth=WalletAuth(tmp_path / 'wallet.sqlite3', ORIGIN), directory_origin=DIRECTORY)
    async def fetch(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source), base_url=ORIGIN) as client:
            return await client.get(request.url.path)
    state = {'confirmed': True, 'methods': []}
    block_hash = '0x' + 'b' * 64
    def rpc(request):
        body = json.loads(request.content); method = body['method']; state['methods'].append(method)
        receipt = {'transactionHash': order['receipt']['transaction'], 'status': '0x1' if state['confirmed'] else '0x0',
            'blockNumber': '0x14', 'blockHash': block_hash, 'logs': [
                {'address': ASSET, 'topics': [AUTHORIZATION_USED, address_topic(order['owner']), order['payment_nonce']], 'data': '0x'},
                {'address': ASSET, 'topics': [TRANSFER, address_topic(order['owner']), address_topic(order['pay_to'])], 'data': hex(int(order['amount']))}]}
        result = {'eth_chainId': '0x14a34', 'eth_blockNumber': '0x15', 'eth_getTransactionReceipt': receipt,
                  'eth_getBlockByNumber': {'hash': block_hash}}[method]
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': result})
    chain = ChainReader('https://rpc.example', transport=httpx.MockTransport(rpc))
    directory = ServiceDirectory(tmp_path / 'directory.sqlite3', DIRECTORY, [ORIGIN], transport=httpx.MockTransport(fetch), chain=chain)
    listing = await directory.refresh(ORIGIN, 'diff-review')
    yield account, seller, order, directory, listing, state
    await directory.close()


def signature(account, challenge):
    return '0x' + account.sign_message(encode_defunct(text=challenge['message'])).signature.hex()


async def test_verified_review_requires_purchase_wallet_and_chain_then_retries_once(paid):
    account, seller, order, directory, listing, chain = paid
    challenge = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 4, 'Useful findings.')
    signed = signature(account, challenge)
    chain['confirmed'] = False
    with pytest.raises(Problem, match='cannot confirm'):
        await directory.reviews.submit(listing['id'], challenge['challenge_id'], signed)
    assert directory.reviews.summary(listing['id'])['count'] == 0
    chain['confirmed'] = True
    published = await directory.reviews.submit(listing['id'], challenge['challenge_id'], signed)
    repeated = await directory.reviews.submit(listing['id'], challenge['challenge_id'], signed)
    assert published == repeated
    assert published['summary'] == {'average': 4.0, 'count': 1, 'verified_count': 1}
    assert published['reviews'][0]['verification'] == 'chain_and_seller'
    assert directory.get(listing['id'])['ratings']['average'] == 4.0
    assert not any(method.startswith('eth_send') for method in chain['methods'])
    with closing(directory.db()) as db:
        stored = db.execute('SELECT document FROM review_challenges').fetchone()[0]
    assert 'Private customer context' not in stored and signed not in stored
    assert 'input' not in canonical(published) and 'transaction' not in canonical(published)


async def test_another_wallet_cannot_review_and_a_buyer_edits_one_existing_vote(paid):
    account, seller, order, directory, listing, chain = paid
    first = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 2, 'Needs improvement.')
    with pytest.raises(Problem, match='wallet that bought'):
        await directory.reviews.submit(listing['id'], first['challenge_id'], signature(Account.create(), first))
    assert chain['methods'] == []
    await directory.reviews.submit(listing['id'], first['challenge_id'], signature(account, first))
    edit = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 5, 'Updated after another look.')
    result = await directory.reviews.submit(listing['id'], edit['challenge_id'], signature(account, edit))
    assert result['summary']['count'] == 1 and result['summary']['average'] == 5
    assert (await directory.reviews.submit(listing['id'], first['challenge_id'], signature(account, first)))['summary']['average'] == 5


async def test_old_unsigned_challenge_cannot_overwrite_a_newer_review(paid):
    account, seller, order, directory, listing, _ = paid
    old = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 1, 'Old draft.')
    new = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 4, 'New decision.')
    await directory.reviews.submit(listing['id'], new['challenge_id'], signature(account, new))
    with pytest.raises(Problem, match='newer review'):
        await directory.reviews.submit(listing['id'], old['challenge_id'], signature(account, old))


async def test_expired_or_changed_review_signature_cannot_publish(paid):
    account, seller, order, directory, listing, _ = paid
    prepared = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 5, 'Original comment.')
    altered = {**prepared, 'message': prepared['message'].replace('Original comment.', 'Changed comment.')}
    with pytest.raises(Problem, match='wallet that bought'):
        await directory.reviews.submit(listing['id'], prepared['challenge_id'], signature(account, altered))
    with closing(directory.db()) as db:
        db.execute('UPDATE review_challenges SET expires=0'); db.commit()
    with pytest.raises(Problem, match='expired'):
        await directory.reviews.submit(listing['id'], prepared['challenge_id'], signature(account, prepared))
    assert directory.reviews.summary(listing['id'])['count'] == 0


async def test_a_confirmed_transfer_cannot_be_reused_for_another_listing(paid):
    import base64
    from hermes_sell.discovery import listing_id
    account, seller, order, directory, listing, _ = paid
    first = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 4, '')
    await directory.reviews.submit(listing['id'], first['challenge_id'], signature(account, first))
    other = deepcopy(listing); other['service']['slug'] = 'other-review'
    other['id'] = listing_id(ORIGIN, 'other-review')
    with closing(directory.db()) as db:
        db.execute('INSERT INTO listings VALUES(?,?,?,?,?)', (other['id'], ORIGIN, 'other-review', 1, canonical(other))); db.commit()
    # Even a seller-issued attestation must not reuse one transfer for another service.
    proof = purchase_proof(seller, order, ORIGIN)
    proof['payload'].update(slug='other-review', listing_id=other['id'])
    proof['signature'] = base64.b64encode(issuer_key(seller.store).sign(canonical(proof['payload']).encode())).decode()
    challenge = await directory.reviews.challenge(other['id'], proof, 5, '')
    with pytest.raises(Problem, match='different service review'):
        await directory.reviews.submit(other['id'], challenge['challenge_id'], signature(account, challenge))
    assert directory.reviews.summary(other['id'])['count'] == 0


@pytest.mark.parametrize('field,value', [('amount', '999999'), ('slug', 'other-service'), ('payer', '0x' + '1' * 40), ('origin', 'http://127.0.0.1:19999')])
async def test_tampered_purchase_proof_is_rejected(paid, field, value):
    _, seller, order, directory, listing, _ = paid
    proof = purchase_proof(seller, order, ORIGIN)
    proof['payload'][field] = value
    with pytest.raises(Problem, match='valid settled purchase'):
        await directory.reviews.challenge(listing['id'], proof, 5, 'Not legitimate.')


@pytest.mark.parametrize('score', [True, 0, 6, '5'])
async def test_invalid_review_score_is_rejected(paid, score):
    _, seller, order, directory, listing, _ = paid
    with pytest.raises(Problem):
        await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), score, '')


async def test_unsettled_orders_cannot_issue_purchase_proofs(paid):
    _, seller, order, _, _, _ = paid
    for payment in ('required', 'verified', 'unknown', 'failed'):
        with pytest.raises(Problem, match='settled purchase'):
            purchase_proof(seller, {**order, 'payment': payment}, ORIGIN)


async def test_mode_change_cannot_mix_simulated_and_testnet_reviews(paid):
    account, seller, order, directory, listing, _ = paid
    challenge = await directory.reviews.challenge(listing['id'], purchase_proof(seller, order, ORIGIN), 5, '')
    await directory.reviews.submit(listing['id'], challenge['challenge_id'], signature(account, challenge))
    with closing(directory.db()) as db:
        document = json.loads(db.execute('SELECT document FROM listings WHERE id=?', (listing['id'],)).fetchone()[0])
        document['mode'] = 'simulation'
        db.execute('UPDATE listings SET document=? WHERE id=?', (canonical(document), listing['id'])); db.commit()
    assert directory.reviews.list(listing['id'])['summary']['count'] == 0
    with pytest.raises(Problem, match='payment mode changed'):
        await directory.reviews.submit(listing['id'], challenge['challenge_id'], signature(account, challenge))


async def test_authenticated_storefront_can_publish_a_simulation_review_without_leaking_input(tmp_path, seller, monkeypatch):
    service = seller.service('diff-review')
    order = seller.create_order('demo-browser', service['slug'], service['revision'], service['examples'][0]['input'], 'review-simulation', ORIGIN)
    await seller.admit(order['id'], 'demo-browser', authorization(order)); await seller.tick()
    with seller.store.tx() as db:
        db.execute('INSERT INTO meta VALUES(?,?)', (publication_key('diff-review'), canonical({'revision': service['revision'], 'enabled': True, 'directory_origin': DIRECTORY})))
    source = public_app(seller, ORIGIN, worker=False, directory_origin=DIRECTORY)
    async def fetch(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source), base_url=ORIGIN) as client:
            return await client.get(request.url.path)
    directory = ServiceDirectory(tmp_path / 'directory.sqlite3', DIRECTORY, [ORIGIN], transport=httpx.MockTransport(fetch))
    await directory.refresh(ORIGIN, 'diff-review')
    app = directory_app(directory)
    class Client(DirectoryClient):
        def __init__(self, origin): super().__init__(origin, transport=httpx.ASGITransport(app=app))
    monkeypatch.setattr('hermes_sell.review_proxy.DirectoryClient', Client)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source), base_url=ORIGIN) as browser:
            route = '/v1/orders/' + order['id']
            assert (await browser.post(route + '/review-challenge', json={'score': 4, 'comment': 'Good demo.'})).status_code == 401
            token = seller.session('demo-browser')['token']
            browser.headers['Authorization'] = 'Bearer ' + token
            challenge = (await browser.post(route + '/review-challenge', json={'score': 4, 'comment': 'Good demo.'})).json()
            published = await browser.post(route + '/review', json={'challenge_id': challenge['challenge_id'], 'signature': None})
            assert published.status_code == 200
            assert published.json()['reviews'][0]['verification'] == 'simulation'
            assert token not in published.text
    finally:
        await directory.close()
