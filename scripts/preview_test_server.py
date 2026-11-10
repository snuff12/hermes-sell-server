"""Disposable seller ledger for browser integration tests."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uvicorn
from hermes_sell.cli import serve, engine, ROOT
from hermes_sell.authoring import from_files
from hermes_sell.contracts import make_revision
from hermes_sell.directory import ServiceDirectory, directory_app


async def run(state):
    second = engine(state / 'second/seller.sqlite3')
    original = from_files(ROOT / 'examples/diff-review/service.json', ROOT / 'examples/diff-review/SKILL.md')
    document = make_revision({**original['public'], 'name': 'River code review', 'amount': '200000'}, original['bundle'])
    second.put_revision(document)
    report = await second.test_revision(document['public']['revision'])
    second.publish(document['public']['revision'], report['id'], True)
    catalog = ServiceDirectory(state / 'directory.sqlite3', 'http://127.0.0.1:8879',
                               ['http://127.0.0.1:8875', 'http://127.0.0.1:8877'])
    app = directory_app(catalog, ROOT / 'web/dist', owner_origin='http://127.0.0.1:8876')
    directory_server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8879,
                                                   proxy_headers=False, log_level='warning'))
    await asyncio.gather(
        serve(SimpleNamespace(state=state / 'first', port=8875, admin_port=8876, directory_origin='http://127.0.0.1:8879')),
        serve(SimpleNamespace(state=state / 'second', port=8877, admin_port=8878, directory_origin='http://127.0.0.1:8879')),
        directory_server.serve(),
    )

with TemporaryDirectory(prefix="hermes-sell-e2e-") as state:
    asyncio.run(run(Path(state)))
