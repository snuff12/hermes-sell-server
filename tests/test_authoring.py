from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from hermes_sell.adapters import HermesRunsRuntime
from hermes_sell.authoring import draft_with_host, snapshot
from hermes_sell.contracts import Problem, make_revision, validate_schema


def test_snapshot_freezes_source_and_blocks_reference_escape(tmp_path):
    skill = tmp_path / 'skill'
    skill.mkdir()
    (skill / 'SKILL.md').write_text('original')
    (tmp_path / 'outside.txt').write_text('private')
    frozen = snapshot(skill / 'SKILL.md')
    (skill / 'SKILL.md').write_text('changed')
    assert frozen['files']['SKILL.md'] == 'original'
    with pytest.raises(Problem):
        snapshot(skill / 'SKILL.md', ['../outside.txt'])


def test_schema_rejects_remote_refs_and_unbounded_text():
    for schema in [{'$ref': 'https://example.com/schema'}, {'type': 'string'}, {'type': 'object', 'additionalProperties': True}]:
        with pytest.raises(Problem):
            validate_schema(schema)


def test_llm_draft_uses_host_and_validates_output(revision):
    public = revision['public']
    candidate = {k: public[k] for k in ('name', 'description', 'input_schema', 'output_schema', 'examples')}
    class HostLLM:
        def complete_structured(self, **kwargs):
            assert kwargs['purpose'] == 'hermes-sell.service-draft'
            assert 'original' not in kwargs  # No model credentials or override knobs.
            return SimpleNamespace(parsed=candidate)
    result = draft_with_host(HostLLM(), revision['bundle'], candidate['examples'][0]['input'], candidate['examples'][0]['output'])
    assert result['name'] == public['name'] and 'pay_to' not in result
    candidate['input_schema'] = {'$ref': 'https://evil.example'}
    with pytest.raises(Problem):
        draft_with_host(HostLLM(), revision['bundle'], {}, {})


async def test_hermes_transport_preserves_run_key_and_frozen_skill(revision):
    seen = []
    def handler(request):
        import json
        if request.url.path == '/v1/capabilities':
            return httpx.Response(200, json={'object': 'hermes.api_server.capabilities', 'auth': {'required': True},
                'features': {'run_submission': True, 'runs_idempotency': {'supported': True, 'durable': True, 'retention_seconds': 86400}}})
        if request.url.path == '/v1/toolsets':
            return httpx.Response(200, json={'data': [{'name': 'terminal', 'enabled': False}]})
        if request.method == 'POST':
            seen.append(request)
            body = json.loads(request.content)
            assert revision['bundle']['files']['SKILL.md'] in body['instructions']
            return httpx.Response(202, json={'run_id': 'run-fixed'})
        return httpx.Response(200, json={'run_id': 'run-fixed', 'status': 'completed', 'completed': True, 'partial': False,
                                       'interrupted': False, 'output': '{"summary":"done","findings":[]}'})
    runtime = HermesRunsRuntime('http://127.0.0.1:8642', 'test-token', 'credential-generation-1', transport=httpx.MockTransport(handler))
    order = {'input': {'diff': 'example', 'context': ''}, 'run_key': 'stable-key'}
    assert await runtime.start(order, revision) == await runtime.start(order, revision) == 'run-fixed'
    assert seen[0].content == seen[1].content
    assert seen[0].headers['Idempotency-Key'] == 'stable-key'
    assert (await runtime.poll('run-fixed', order, revision))['output']['summary'] == 'done'
    await runtime.close()


@pytest.mark.parametrize('amount', [True, 100000.9, '100000.9', None])
def test_price_cannot_be_silently_coerced_or_truncated(revision, amount):
    spec = deepcopy(revision['public'])
    spec['amount'] = amount
    with pytest.raises(Problem) as invalid:
        make_revision(spec, revision['bundle'])
    assert invalid.value.code == 'invalid_price'
