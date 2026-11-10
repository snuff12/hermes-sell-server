import asyncio
from copy import deepcopy
import json
import time

import httpx
import pytest

from hermes_sell.api import admin_app, public_app
from hermes_sell.contracts import Problem, canonical, make_revision
from hermes_sell.directory import ServiceDirectory, directory_app, publication_key
from hermes_sell.discovery import DirectoryClient, exact_origin, listing_id

SELLER = "http://127.0.0.1:18801"
CATALOG = "http://127.0.0.1:18805"


def opt_in(seller, directory=CATALOG, *, enabled=True):
    service = seller.service('diff-review')
    record = {'slug': service['slug'], 'revision': service['revision'], 'directory_origin': directory, 'enabled': enabled}
    with seller.store.tx() as connection:
        connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (publication_key(service['slug']), canonical(record)))
    return record


@pytest.fixture
async def directory(tmp_path, seller):
    app = public_app(seller, SELLER, worker=False)
    calls = []
    async def fetch(request):
        calls.append(str(request.url))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=SELLER) as client:
            return await client.get(request.url.path)
    result = ServiceDirectory(tmp_path / 'directory.sqlite3', CATALOG, [SELLER], transport=httpx.MockTransport(fetch))
    result.observed_requests = calls
    yield result
    await result.close()


async def test_registration_requires_explicit_seller_publication_and_keeps_private_bundle_out(directory, seller):
    with pytest.raises(Problem, match='has not published'):
        await directory.refresh(SELLER, 'diff-review')
    opt_in(seller)
    record = await directory.refresh(SELLER, 'diff-review')
    assert record['state'] == 'online'
    assert record['id'] == listing_id(SELLER, 'diff-review')
    assert 'bundle' not in canonical(record) and 'SKILL.md' not in canonical(record)
    assert record['service'] == seller.service('diff-review')
    assert directory.search('review')['listings'][0]['id'] == record['id']
    assert 'examples' not in directory.search()['listings'][0]['service']
    assert directory.search('unrelated')['listings'] == []
    assert directory.search(mode='testnet')['listings'] == []


@pytest.mark.parametrize('origin', ['http://169.254.169.254', 'http://127.0.0.1:18802', 'https://example.com', 'http://127.0.0.1:18801@evil.example'])
async def test_registration_cannot_fetch_an_unconfigured_url(directory, origin):
    with pytest.raises(Problem):
        await directory.refresh(origin, 'diff-review')
    assert directory.observed_requests == []


async def test_publication_is_scoped_to_one_directory(directory, seller):
    opt_in(seller, 'http://127.0.0.1:19999')
    with pytest.raises(Problem):
        await directory.refresh(SELLER, 'diff-review')
    assert directory.search()['listings'] == []


async def test_withdrawal_and_new_revision_do_not_silently_replace_owner_review(directory, seller, revision):
    opt_in(seller)
    original = await directory.refresh(SELLER, 'diff-review')
    spec = deepcopy(revision['public'])
    spec['amount'] = '200000'
    changed = make_revision(spec, revision['bundle'])
    seller.put_revision(changed)
    report = await seller.test_revision(changed['public']['revision'])
    seller.publish(changed['public']['revision'], report['id'], True)
    withdrawn = await directory.refresh(SELLER, 'diff-review')
    assert withdrawn['state'] == 'withdrawn'
    assert withdrawn['service']['revision'] == original['service']['revision']
    assert directory.search()['listings'] == []
    opt_in(seller)
    current = await directory.refresh(SELLER, 'diff-review')
    assert current['id'] == original['id'] and current['service']['amount'] == '200000'
    opt_in(seller, enabled=False)
    assert (await directory.refresh(SELLER, 'diff-review'))['state'] == 'withdrawn'


async def test_offline_and_old_checks_never_appear_online(directory, seller, monkeypatch):
    opt_in(seller)
    record = await directory.refresh(SELLER, 'diff-review')
    seller.readiness_check = lambda: False
    assert (await directory.refresh(SELLER, 'diff-review'))['state'] == 'offline'
    seller.readiness_check = lambda: True
    await directory.refresh(SELLER, 'diff-review')
    monkeypatch.setattr('hermes_sell.directory.time.time', lambda: record['checked_at'] + 91)
    assert directory.get(record['id'])['state'] == 'stale'


async def test_directory_boundary_and_client_round_trip(directory, seller):
    opt_in(seller)
    app = directory_app(directory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CATALOG) as browser:
        assert (await browser.post('/v1/listings', json={'origin': SELLER, 'slug': 'diff-review'})).status_code == 403
        assert (await browser.get('/v1/listings', headers={'Origin': 'https://evil.example'})).status_code == 403
        assert (await browser.get('/v1/listings', headers={'X-Forwarded-Host': 'localhost'})).status_code == 403
        response = await browser.post('/v1/listings', json={'origin': SELLER, 'slug': 'diff-review'}, headers={'Origin': CATALOG})
        assert response.status_code == 200
    client = DirectoryClient(CATALOG, transport=httpx.ASGITransport(app=app))
    try:
        listing = (await client.search('review'))['listings'][0]
        assert (await client.get(listing['id']))['service']['examples']
    finally:
        await client.close()


async def test_seller_registration_checks_only_configured_origins_without_asking_for_owner_tokens(directory):
    app = directory_app(directory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=CATALOG) as browser:
        allowed = (await browser.get('/v1/sellers')).json()
        assert allowed['seller_origins'] == [SELLER]
        response = await browser.post('/v1/sellers/check', headers={'Origin': CATALOG}, json={'origin': 'http://169.254.169.254'})
        assert response.status_code == 403 and directory.observed_requests == []
        response = await browser.post('/v1/sellers/check', headers={'Origin': CATALOG}, json={'origin': SELLER})
        assert response.status_code == 200 and response.json()['ready'] is True
        assert response.json()['mode'] == 'simulation'
        assert all('/admin' not in url for url in directory.observed_requests)


async def test_stale_refresh_cannot_overwrite_a_newer_directory_result(tmp_path, seller):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    service = seller.service('diff-review')
    async def fetch(request):
        nonlocal calls
        if request.url.path == '/health/ready':
            return httpx.Response(200, json={'ready': True})
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return httpx.Response(404)
        return httpx.Response(200, json={'protocol': 'hermes-sell/0.1', 'origin': SELLER, 'directory_origin': CATALOG,
                                       'mode': 'simulation', 'service': service})
    directory = ServiceDirectory(tmp_path / 'catalog.sqlite3', CATALOG, [SELLER], transport=httpx.MockTransport(fetch))
    try:
        # Seed a valid listing, then race an older unavailable response with a successful refresh.
        calls = 1
        record = await directory.refresh(SELLER, 'diff-review')
        calls = 0
        old = asyncio.create_task(directory.refresh(SELLER, 'diff-review'))
        await entered.wait()
        await directory.refresh(SELLER, 'diff-review')
        release.set()
        assert (await old)['state'] == 'online'
        assert directory.get(record['id'])['state'] == 'online'
    finally:
        await directory.close()


async def test_owner_publication_requires_auth_and_a_published_version(seller, directory, monkeypatch):
    class Client(DirectoryClient):
        def __init__(self, origin):
            super().__init__(origin, transport=httpx.ASGITransport(app=directory_app(directory)))
    monkeypatch.setattr('hermes_sell.discovery.DirectoryClient', Client)
    owner = admin_app(seller, 'owner-token', 'http://127.0.0.1:18802', seller_origin=SELLER, directory_origin=CATALOG)
    revision = seller.service('diff-review')['revision']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner), base_url='http://127.0.0.1:18802') as client:
        path = '/admin/listings/' + revision
        assert (await client.post(path)).status_code == 401
        headers = {'Authorization': 'Bearer owner-token'}
        response = await client.post(path, headers=headers)
        assert response.status_code == 200 and response.json()['listed'] is True
        assert (await client.post(path + '/withdraw', headers=headers)).json()['withdrawn'] is True
        assert directory.search()['listings'] == []


@pytest.mark.parametrize('origin', ['http://localhost:8795', 'http://127.0.0.1:80', 'http://127.0.0.1:8795/', 'http://127.0.0.1:8795?x=1', 'https://user@host', 'file:///etc/passwd'])
def test_local_directory_rejects_ambiguous_origins(origin):
    with pytest.raises(Problem):
        exact_origin(origin, local_only=True)
