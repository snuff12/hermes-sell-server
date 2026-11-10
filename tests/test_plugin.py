import json
from hermes_sell.plugin import register


def test_plugin_registers_tools_without_exposing_approval(tmp_path):
    tools = {}
    class Context:
        def register_tool(self, **kwargs):
            tools[kwargs['name']] = kwargs
        def register_skill(self, *args):
            pass
    register(Context(), data_dir=tmp_path)
    assert set(tools) == {'service_search', 'service_draft', 'service_inspect', 'service_quote', 'service_purchase', 'service_status'}
    assert list(tools['service_purchase']['schema']['parameters']['properties']) == ['purchase_id']
    assert 'approved' not in json.dumps([tool['schema']['parameters'] for tool in tools.values()])
    assert all(tool['is_async'] is True for tool in tools.values())


async def test_draft_tool_saves_private_bundle_for_owner_import(tmp_path, revision):
    from types import SimpleNamespace
    from hermes_sell.drafts import DraftStore
    tools = {}
    public = revision['public']
    candidate = {key: public[key] for key in ('name', 'description', 'input_schema', 'output_schema', 'examples')}
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'SKILL.md').write_text('Original private instructions.')
    (source / 'reference.txt').write_text('Private reference.')
    class LLM:
        async def acomplete_structured(self, **kwargs):
            assert kwargs['purpose'] == 'hermes-sell.service-draft'
            assert 'provider' not in kwargs and 'model' not in kwargs
            return SimpleNamespace(parsed=candidate)
    class Context:
        llm = LLM()
        def register_tool(self, **kwargs):
            tools[kwargs['name']] = kwargs
        def register_skill(self, *args):
            pass
        def get_config(self, key, default=None):
            return str(tmp_path / 'drafts') if key == 'draft_dir' else default
    register(Context(), data_dir=tmp_path)
    result = json.loads(await tools['service_draft']['handler']({
        'skill_path': str(source / 'SKILL.md'), 'references': ['reference.txt'],
        'input_example': candidate['examples'][0]['input'], 'output_example': candidate['examples'][0]['output'],
    }))
    assert result['publication'] == 'owner_review_required'
    assert 'bundle' not in result and 'Private reference.' not in json.dumps(result)
    saved = DraftStore(tmp_path / 'drafts').get(result['draft_id'])
    assert saved['bundle']['files']['reference.txt'] == 'Private reference.'
    assert saved['draft'] == candidate


async def test_buyer_tool_uses_pinned_hermes_get_config_signature(tmp_path, monkeypatch):
    # Hermes v0.22.7 hermes_cli/plugins.py:266 requires key, default=None.
    tools, calls = {}, []
    class Context:
        def register_tool(self, **kwargs):
            tools[kwargs['name']] = kwargs
        def register_skill(self, *args):
            pass
        def get_config(self, key, default=None):
            calls.append((key, default))
            return 'http://127.0.0.1:8765' if key == 'buyer_origin' else default
    class Client:
        def __init__(self, origin, path):
            assert origin == 'http://127.0.0.1:8765'
        async def connect(self):
            pass
        async def inspect(self, slug):
            return {'slug': slug}
        async def close(self):
            pass
    monkeypatch.setattr('hermes_sell.plugin.Buyer', Client)
    register(Context(), data_dir=tmp_path)
    result = json.loads(await tools['service_inspect']['handler']({'slug': 'diff-review'}))
    assert result['result']['slug'] == 'diff-review'
    assert calls == [('role', 'both'), ('buyer_origin', 'http://127.0.0.1:8765'), ('buyer_mode', 'simulation')]


def test_plugin_redacts_nested_buyer_authorizations():
    from hermes_sell.plugin import redact_buyer_result
    result = redact_buyer_result({'purchase_id': 'purchase', 'order': {'authorization': {'signature': 'secret'}},
                                  'session_token': 'secret', 'result': [{'summary': 'public', 'token': 'secret'}]})
    assert result == {'purchase_id': 'purchase', 'order': {}, 'result': [{'summary': 'public'}]}


async def test_buyer_role_cannot_read_skill_files(tmp_path, monkeypatch):
    tools = {}
    class Context:
        def register_tool(self, **kwargs):
            tools[kwargs['name']] = kwargs
        def register_skill(self, *args):
            pass
        def get_config(self, key, default=None):
            return 'buyer' if key == 'role' else default
    def forbidden(*args):
        raise AssertionError('The buyer must not read a skill file')
    monkeypatch.setattr('hermes_sell.plugin.snapshot', forbidden)
    register(Context(), data_dir=tmp_path)
    result = json.loads(await tools['service_draft']['handler']({'skill_path': '/private/SKILL.md'}))
    assert result['error']['code'] == 'role_denied'
    assert tools['service_draft']['check_fn']() is False
    assert tools['service_status']['check_fn']() is True
