import importlib.util
import json
from pathlib import Path

import pytest


def exporter():
    path = Path(__file__).resolve().parents[1] / 'scripts/build_catalog_plugin.py'
    spec = importlib.util.spec_from_file_location('catalog_exporter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_is_standalone_source_and_excludes_developer_credentials(tmp_path):
    destination = tmp_path / 'hermes-sell'
    report = exporter().build(destination)
    assert not report['developer_auth_helpers_included'] and not report['published']
    assert 'desktop/plugin.js' in report['files']
    assert not any(name.startswith(('scripts/', 'deployment/', '.state/')) for name in report['files'])
    assert not any('auth.json' in (destination / name).read_text() for name in report['files'] if name.endswith('.py'))
    spec = importlib.util.spec_from_file_location('standalone_sell_candidate', destination / '__init__.py',
                                                submodule_search_locations=[str(destination)])
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    tools = {}
    class Context:
        def get_config(self, key, default=None): return default
        def register_tool(self, **kwargs): tools[kwargs['name']] = kwargs
        def register_skill(self, *args): assert Path(args[1]).is_relative_to(destination)
    module.register(Context())
    assert set(tools) == {'service_search', 'service_draft', 'service_inspect', 'service_quote', 'service_purchase', 'service_status'}
    with pytest.raises(ValueError, match='never overwritten'):
        exporter().build(destination)


async def test_missing_buyer_setup_fails_cleanly_without_reading_hermes_storage(tmp_path):
    from hermes_sell.plugin import register
    tools = {}
    class Context:
        def get_config(self, key, default=None): return default
        def register_tool(self, **kwargs): tools[kwargs['name']] = kwargs
        def register_skill(self, *args): pass
    register(Context())
    result = json.loads(await tools['service_quote']['handler']({'slug': 'diff-review', 'input': {}}))
    assert result['error']['code'] == 'buyer_setup_required'
