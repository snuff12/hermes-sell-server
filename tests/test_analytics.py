from datetime import datetime, timezone
import json

import httpx
import pytest

from conftest import authorization, quote
from hermes_sell.analytics import seller_analytics, public_service_usage, buyer_analytics
from hermes_sell.api import admin_app
from hermes_sell.buyer import TestnetBuyer
from hermes_sell.contracts import canonical
from hermes_sell.directory import publication_key, public_publication
from hermes_sell.vault import AuthorizationVault


async def test_analytics_counts_durable_executions_and_confirmed_payments_once(seller):
    unpaid = quote(seller)
    paid = quote(seller, idem='paid-analytics')
    payload = authorization(paid)
    await seller.admit(paid['id'], 'demo-browser', payload)
    await seller.admit(paid['id'], 'demo-browser', payload)
    await seller.tick()
    await seller.tick()
    report = seller_analytics(seller)
    assert report['summary']['quotes'] == 2
    assert report['summary']['executions'] == report['summary']['paid_requests'] == 1
    assert report['summary']['settled_amount'] == '100000'
    assert len(report['payments']) == 1
    assert report['payments'][0]['order_id'] == paid['id']
    assert 'input' not in canonical(report) and 'authorization' not in canonical(report)


async def test_pending_unknown_and_failed_amounts_are_not_revenue(seller):
    pending = quote(seller)
    await seller.admit(pending['id'], 'demo-browser', authorization(pending))
    seller.transition(pending['id'], 'fixture_unknown', execution='unknown', payment='unknown')
    failed = quote(seller)
    seller.transition(failed['id'], 'fixture_failed', execution='result_ready', payment='failed')
    report = seller_analytics(seller, 'all')
    assert report['summary']['settled_amount'] == '0'
    assert report['summary']['pending_amount'] == '100000'
    assert report['summary']['unknown_orders'] == report['summary']['unpaid_results'] == 1
    assert report['summary']['failed_orders'] == 1


async def test_payments_use_confirmation_day_not_quote_day_and_retries_do_not_double_count(seller):
    order = quote(seller)
    await seller.admit(order['id'], 'demo-browser', authorization(order))
    await seller.tick()
    now = int(datetime(2026, 11, 7, 12, tzinfo=timezone.utc).timestamp())
    quote_time = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    stored = seller.store.order(order['id']); stored['created_at'] = quote_time
    with seller.store.tx() as db:
        db.execute('UPDATE orders SET document=?,created_at=? WHERE id=?', (canonical(stored), quote_time, order['id']))
        db.execute("UPDATE events SET at=? WHERE kind='settled'", (now - 30,))
        db.execute("UPDATE events SET at=? WHERE kind='dispatch_intent'", (quote_time,))
        db.execute("INSERT INTO events(order_id,kind,at) VALUES(?,?,?)", (order['id'], 'settled', now - 10))
    report = seller_analytics(seller, '7d', clock=now)
    assert report['summary']['quotes'] == report['summary']['executions'] == 0
    assert report['summary']['paid_requests'] == 1 and report['summary']['settled_amount'] == '100000'
    assert report['daily'][-1]['date'] == '2026-11-07' and report['daily'][-1]['settled_amount'] == '100000'


async def test_usage_sharing_is_explicit_and_hidden_under_restore_lock(seller):
    service = seller.service('diff-review')
    record = {'revision': service['revision'], 'enabled': True, 'directory_origin': 'http://127.0.0.1:8795'}
    with seller.store.tx() as db:
        db.execute('INSERT INTO meta VALUES(?,?)', (publication_key('diff-review'), canonical(record)))
    assert 'usage' not in public_publication(seller, 'diff-review', 'http://127.0.0.1:8765')
    record['share_usage'] = True
    with seller.store.tx() as db:
        db.execute('UPDATE meta SET value=? WHERE key=?', (canonical(record), publication_key('diff-review')))
    assert public_publication(seller, 'diff-review', 'http://127.0.0.1:8765')['usage']['settled_amount'] == '0'
    seller.store.flag_recovery()
    assert public_service_usage(seller, 'diff-review') is None
    assert seller_analytics(seller)['recovery_required'] is True


async def test_analytics_requires_owner_authentication(seller):
    app = admin_app(seller, 'private-owner-token')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8766') as client:
        assert (await client.get('/admin/analytics')).status_code == 401
        response = await client.get('/admin/analytics?period=wrong', headers={'Authorization': 'Bearer private-owner-token'})
        assert response.status_code == 422
        assert (await client.get('/admin/analytics?period=all', headers={'Authorization': 'Bearer private-owner-token'})).status_code == 200


async def test_buyer_spending_and_budget_are_shared_across_sellers_and_exclude_simulation(tmp_path):
    buyer = TestnetBuyer('http://127.0.0.1:8785', tmp_path / 'buyer.sqlite3', vault=AuthorizationVault(b'b' * 32))
    try:
        for identity, origin, mode, status, amount, reserved in [
            ('paid', 'https://seller-a.example', 'testnet', 'settled', '200000', True),
            ('unknown', 'https://seller-b.example', 'testnet', 'unknown', '300000', True),
            ('simulation', 'http://127.0.0.1:8765', 'simulation', 'settled', '900000', False),
        ]:
            buyer.save({'purchase_id': identity, 'origin': origin, 'mode': mode, 'status': status,
                'order': {'amount': amount, 'payment': 'settled' if status == 'settled' else 'unknown'}, 'budget_reserved': reserved})
        report = buyer_analytics(buyer)
        assert report['summary']['settled_amount'] == '200000'
        assert report['summary']['pending_amount'] == '300000'
        assert report['summary']['budget_committed'] == report['summary']['budget_available'] == '500000'
        assert len(report['sellers']) == 2 and report['summary']['paid_requests'] == 1
    finally:
        await buyer.close()
