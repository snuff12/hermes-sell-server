import asyncio
from copy import deepcopy
import time

import pytest

from hermes_sell.contracts import Problem, make_revision
from hermes_sell.engine import Engine
from hermes_sell.adapters import SimulationPayments, SimulationRuntime
from hermes_sell.storage import Store
from conftest import authorization, quote


async def test_result_requires_settlement_and_retries_do_not_rerun(seller):
    order = quote(seller)
    payload = authorization(order)
    accepted = await seller.admit(order['id'], 'demo-browser', payload)
    assert accepted['payment'] == 'verified' and 'result' not in accepted
    await seller.tick()
    result = await seller.admit(order['id'], 'demo-browser', payload)
    assert result['payment'] == 'settled' and result['result']
    assert seller.runtime.starts == seller.payments.settlements == 1
    with pytest.raises(Problem) as conflict:
        await seller.admit(order['id'], 'demo-browser', authorization(order))
    assert conflict.value.code == 'authorization_conflict'


async def test_parallel_duplicate_quote_and_admission(seller):
    orders = await asyncio.gather(*(asyncio.to_thread(quote, seller, idem='repeat-request') for _ in range(8)))
    assert len({order['id'] for order in orders}) == 1
    payload = authorization(orders[0])
    await asyncio.gather(*(seller.admit(orders[0]['id'], 'demo-browser', payload) for _ in range(8)))
    await seller.tick()
    assert seller.runtime.starts == 1 and seller.payments.settlements == 1


async def test_idempotency_conflict_and_wallet_ownership(seller):
    order = quote(seller, idem='fixed-request')
    with pytest.raises(Problem) as conflict:
        quote(seller, idem='fixed-request', value={'diff': 'other', 'context': ''})
    assert conflict.value.status == 409
    with pytest.raises(Problem) as hidden:
        seller.owned(order['id'], 'demo-other')
    assert hidden.value.status == 404
    with pytest.raises(Problem):
        await seller.admit(order['id'], 'demo-browser', authorization(order, payer='demo-other'))


async def test_nonce_reuse_and_short_lived_authorization_rejected(seller):
    first = quote(seller)
    payload = authorization(first, nonce='a' * 32)
    await seller.admit(first['id'], 'demo-browser', payload)
    await seller.tick()
    second = quote(seller)
    with pytest.raises(Problem) as reused:
        await seller.admit(second['id'], 'demo-browser', authorization(second, nonce='a' * 32))
    assert reused.value.code == 'nonce_reused'
    expired = authorization(second)
    expired['valid_before'] = int(time.time()) + 119
    with pytest.raises(Problem) as short:
        await seller.admit(second['id'], 'demo-browser', expired)
    assert short.value.code == 'authorization_expiry'
    assert seller.runtime.starts == 1


async def test_invalid_output_never_settles(seller):
    async def invalid(*args):
        return {'status': 'completed', 'output': {'unexpected': 'value'}}
    seller.runtime.poll = invalid
    order = quote(seller)
    await seller.admit(order['id'], 'demo-browser', authorization(order))
    await seller.tick()
    stored = seller.store.order(order['id'])
    assert stored['execution'] == 'failed' and stored['payment'] == 'failed'
    assert seller.payments.settlements == 0


async def test_unknown_settlement_keeps_result_private_and_freezes_retry(seller):
    async def uncertain(*args):
        raise TimeoutError('chain response lost')
    seller.payments.settle = uncertain
    order = quote(seller)
    payload = authorization(order)
    await seller.admit(order['id'], 'demo-browser', payload)
    await seller.tick()
    unknown = seller.view(seller.store.order(order['id']))
    assert unknown['payment'] == 'unknown' and 'result' not in unknown
    seller.recover_interrupted()
    await seller.tick()
    assert seller.runtime.starts == 1
    assert (await seller.admit(order['id'], 'demo-browser', payload))['payment'] == 'unknown'


@pytest.mark.parametrize('execution,payment,expected_exec,expected_pay', [
    ('dispatching', 'verified', 'unknown', 'verified'),
    ('result_ready', 'settling', 'result_ready', 'unknown'),
])
async def test_process_restart_never_assumes_side_effect_failure(seller, execution, payment, expected_exec, expected_pay):
    order = quote(seller)
    await seller.admit(order['id'], 'demo-browser', authorization(order))
    seller.transition(order['id'], 'crash_fixture', execution=execution, payment=payment)
    restarted = Engine(Store(seller.store.path), SimulationRuntime(), SimulationPayments())
    restarted.recover_interrupted()
    await restarted.tick()
    stored = restarted.store.order(order['id'])
    assert (stored['execution'], stored['payment']) == (expected_exec, expected_pay)
    assert restarted.runtime.starts == restarted.payments.settlements == 0


async def test_restore_recovery_lock_blocks_new_side_effects(seller):
    order = quote(seller)
    await seller.admit(order['id'], 'demo-browser', authorization(order))
    seller.store.flag_recovery()
    await seller.tick()
    with pytest.raises(Problem) as blocked:
        quote(seller)
    assert blocked.value.code == 'recovery_required'
    assert seller.runtime.starts == 0
    assert seller.owned(order['id'], 'demo-browser')
    with pytest.raises(Problem) as owner_test_blocked:
        await seller.test_revision(order['revision'])
    assert owner_test_blocked.value.code == 'recovery_required'
    assert seller.runtime.starts == 0


async def test_immutable_revision_keeps_existing_order_contract(seller, revision):
    first = quote(seller)
    spec = deepcopy(revision['public'])
    spec['amount'] = '200000'
    spec['name'] = 'A different service name'
    updated = make_revision(spec, revision['bundle'])
    seller.put_revision(updated)
    with pytest.raises(Problem) as needs_test:
        seller.publish(updated['public']['revision'], 'missing', True)
    assert needs_test.value.code == 'test_required'
    report = await seller.test_revision(updated['public']['revision'])
    seller.publish(updated['public']['revision'], report['id'], True)
    assert quote(seller)['amount'] == '200000'
    assert seller.owned(first['id'], 'demo-browser')['amount'] == '100000'
    assert 'bundle' not in seller.service('diff-review')
    # Old stored orders contain no embedded contract; resolve their exact revision.
    legacy = seller.owned(first['id'], 'demo-browser')
    assert 'service_contract' not in legacy
    restored = seller.view(legacy)
    assert restored['service_contract'] == revision['public']
    assert restored['service_contract']['name'] != seller.service('diff-review')['name']
    assert 'bundle' not in restored['service_contract']
    assert 'execution_digest' not in restored['service_contract']


async def test_non_finite_runtime_output_fails_order_without_crashing_worker(seller):
    original_poll = seller.runtime.poll

    async def invalid_output(*args):
        return {'status': 'completed', 'output': {'summary': float('nan'), 'findings': []}}

    seller.runtime.poll = invalid_output
    first = quote(seller)
    await seller.admit(first['id'], 'demo-browser', authorization(first))
    await seller.tick()
    assert seller.store.order(first['id'])['execution'] == 'failed'
    assert seller.payments.settlements == 0
    seller.runtime.poll = original_poll
    second = quote(seller)
    await seller.admit(second['id'], 'demo-browser', authorization(second))
    await seller.tick()
    assert seller.store.order(second['id'])['payment'] == 'settled'
