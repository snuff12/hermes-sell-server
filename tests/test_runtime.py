from copy import deepcopy

import httpx
import pytest

from hermes_sell.adapters import HermesRunsRuntime
from hermes_sell.contracts import digest


def capabilities():
    return {'object': 'hermes.api_server.capabilities', 'auth': {'required': True},
            'features': {'run_submission': True, 'runs_idempotency': {
                'supported': True, 'durable': True, 'retention_seconds': 86400}}}


@pytest.mark.parametrize('url', ['https://example.com', 'http://127.0.0.1/other', 'http://user:pass@localhost',
                               'http://127.0.0.1?token=value'])
def test_runtime_rejects_nonlocal_or_ambiguous_origins(url):
    with pytest.raises(ValueError):
        HermesRunsRuntime(url, 'secret', 'generation-1')


@pytest.mark.parametrize('failure', ['memory', 'short_retention', 'unauthenticated', 'tools'])
async def test_preflight_refuses_unsafe_host_before_admission(revision, failure):
    seen = []
    capability = capabilities()
    if failure == 'memory':
        capability['features']['runs_idempotency']['durable'] = False
    if failure == 'short_retention':
        capability['features']['runs_idempotency']['retention_seconds'] = 60
    if failure == 'unauthenticated':
        capability['auth']['required'] = False
    def handle(request):
        seen.append(request.method)
        return httpx.Response(200, json=capability if request.url.path.endswith('capabilities') else {
            'data': [{'name': 'terminal', 'enabled': failure == 'tools'}]})
    runtime = HermesRunsRuntime('http://127.0.0.1:8642', 'secret', 'generation-1', transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(RuntimeError):
            await runtime.start({'run_key': 'stable-run-key', 'input': {}}, revision)
        assert 'POST' not in seen
    finally:
        await runtime.close()


async def test_runtime_rotation_or_body_change_cannot_reuse_admission(revision):
    seen = []
    runtime = HermesRunsRuntime('http://127.0.0.1:8642', 'secret', 'generation-1',
                               transport=httpx.MockTransport(lambda request: seen.append(request)))
    try:
        order = {'run_key': 'stable-run-key', 'input': {}}
        order['runtime_binding'] = runtime.execution_identity(order, revision)
        runtime.credential_generation = 'generation-2'
        with pytest.raises(RuntimeError, match='changed'):
            await runtime.start(order, revision)
        runtime.credential_generation = 'generation-1'
        changed = deepcopy(revision)
        changed['bundle']['files']['SKILL.md'] += ' changed'
        with pytest.raises(RuntimeError, match='changed'):
            await runtime.start(order, changed)
        assert not seen
    finally:
        await runtime.close()


@pytest.mark.parametrize(('document', 'expected'), [
    ({'status': 'completed', 'output': '{}'}, 'unknown'),
    ({'status': 'completed', 'completed': True, 'partial': True, 'interrupted': False, 'output': '{}'}, 'failed'),
    ({'status': 'waiting_for_approval'}, 'unknown'),
    ({'status': 'waiting_for_input'}, 'unknown'),
    ({'status': 'interrupted'}, 'interrupted'),
])
async def test_poll_never_releases_an_unconfirmed_result(revision, document, expected):
    document = {'run_id': 'run-fixed', **document}
    runtime = HermesRunsRuntime('http://127.0.0.1:8642', 'secret', 'generation-1',
                               transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document)))
    try:
        result = await runtime.poll('run-fixed', {'input': {}}, revision)
        assert result['status'] == expected and 'output' not in result
    finally:
        await runtime.close()


async def test_poll_rejects_wrong_identity_and_does_not_resubmit(revision):
    seen = []
    def handle(request):
        seen.append(request.method)
        return httpx.Response(200, json={'run_id': 'other-run', 'status': 'completed'})
    runtime = HermesRunsRuntime('http://127.0.0.1:8642', 'secret', 'generation-1', transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(RuntimeError, match='identity'):
            await runtime.poll('run-fixed', {'input': {}}, revision)
        assert seen == ['GET']
    finally:
        await runtime.close()


async def test_v023_canonical_admission_projection_confirms_completion(revision):
    document = {'object': 'hermes.run', 'run_id': 'run-fixed', 'status': 'completed',
                'admission_id': 'a' * 32, 'execution_generation': 1, 'session_id': 'run-fixed',
                'pending_controls': [], 'output': '{"summary":"reviewed","findings":[]}'}
    runtime = HermesRunsRuntime('http://127.0.0.1:8642', 'secret', 'generation-1',
                               transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document)))
    try:
        result = await runtime.poll('run-fixed', {'input': {}}, revision)
        assert result['status'] == 'completed' and result['output']['summary'] == 'reviewed'
        document['pending_controls'] = [{'kind': 'approval'}]
        assert (await runtime.poll('run-fixed', {'input': {}}, revision))['status'] == 'unknown'
    finally:
        await runtime.close()


async def test_read_only_admission_lookup_binds_original_scope_key_and_full_body(revision):
    lookups, requests = [], []

    def lookup(*args):
        lookups.append(args)
        return "run-recovered"

    runtime = HermesRunsRuntime("http://127.0.0.1:8642", "secret", "generation-1", admission_lookup=lookup,
                               transport=httpx.MockTransport(lambda request: requests.append(request)))
    order = {"input": {"diff": "review this", "context": ""}, "run_key": "original-run-key"}
    order["runtime_binding"] = runtime.execution_identity(order, revision)
    try:
        assert await runtime.find_run(order, revision) == "run-recovered"
        assert lookups == [("original-run-key", digest({"body": runtime.body(order, revision), "gateway_session_key": ""}),
                            "generation-1")]
        assert not requests
        runtime.credential_generation = "generation-2"
        with pytest.raises(RuntimeError, match="binding"):
            await runtime.find_run(order, revision)
        assert len(lookups) == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize("found", [None, "../different-run"])
async def test_missing_or_invalid_lookup_never_submits_a_run(revision, found):
    requests = []
    runtime = HermesRunsRuntime("http://127.0.0.1:8642", "secret", "generation-1", admission_lookup=lambda *args: found,
                               transport=httpx.MockTransport(lambda request: requests.append(request)))
    order = {"input": {}, "run_key": "original-run-key"}
    order["runtime_binding"] = runtime.execution_identity(order, revision)
    try:
        if found is None:
            assert await runtime.find_run(order, revision) is None
        else:
            with pytest.raises(RuntimeError, match="identity"):
                await runtime.find_run(order, revision)
        assert not requests
    finally:
        await runtime.close()


async def test_missing_binding_requires_explicit_locked_snapshot_lookup(revision):
    lookups = []

    def lookup(*args):
        lookups.append(args)
        return "run-recovered"

    runtime = HermesRunsRuntime("http://127.0.0.1:8642", "secret", "generation-1", admission_lookup=lookup)
    try:
        order = {"input": {}, "run_key": "original-run-key"}
        with pytest.raises(RuntimeError, match="binding"):
            await runtime.find_run(order, revision)
        assert not lookups
        assert await runtime.find_run(order, revision, allow_unbound=True) == "run-recovered"
        order["runtime_binding"] = {"origin": "different"}
        with pytest.raises(RuntimeError, match="binding"):
            await runtime.find_run(order, revision, allow_unbound=True)
        assert len(lookups) == 1
    finally:
        await runtime.close()
