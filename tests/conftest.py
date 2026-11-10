import json
from pathlib import Path
import time
import uuid

import pytest

from hermes_sell.adapters import SimulationPayments, SimulationRuntime
from hermes_sell.authoring import from_files
from hermes_sell.engine import Engine
from hermes_sell.storage import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def revision():
    return from_files(ROOT / 'examples/diff-review/service.json', ROOT / 'examples/diff-review/SKILL.md')


@pytest.fixture
async def seller(tmp_path, revision):
    engine = Engine(Store(tmp_path / 'seller.sqlite3'), SimulationRuntime(), SimulationPayments())
    engine.put_revision(revision)
    report = await engine.test_revision(revision['public']['revision'])
    engine.publish(revision['public']['revision'], report['id'], True)
    engine.runtime.starts = 0
    return engine


def quote(seller, *, owner='demo-browser', idem=None, value=None):
    service = seller.service('diff-review')
    return seller.create_order(owner, service['slug'], service['revision'], value or service['examples'][0]['input'], idem or str(uuid.uuid4()), 'http://testserver')


def authorization(order, *, payer='demo-browser', nonce=None):
    return {'mode': 'simulation', 'payer': payer, 'order_digest': order['order_digest'],
            'nonce': nonce or uuid.uuid4().hex, 'valid_before': int(time.time()) + 180}
