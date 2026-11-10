from copy import deepcopy
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import time
import sqlite3

import pytest

from hermes_sell import runtime_container


def container_info():
    return {'State': {'Running': True}, 'Image': 'sha256:verified-image',
        'Config': {'User': '10000:10000', 'Labels': {'org.hermes-sell.policy': 'text-json-no-tools-v1'},
                   'Entrypoint': ['/opt/hermes/.venv/bin/python', '-I', '-B', '/opt/hermes-sell/entrypoint.py']},
        'HostConfig': {'ReadonlyRootfs': True, 'Privileged': False, 'CapDrop': ['ALL'],
            'SecurityOpt': ['no-new-privileges:true'], 'NetworkMode': 'bridge', 'Binds': None,
            'PortBindings': {'8642/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18790'}]}},
        'Mounts': [
            {'Type': 'volume', 'Destination': '/state', 'Name': 'hermes-sell-runtime-state', 'RW': True},
            {'Type': 'volume', 'Destination': '/run/secrets', 'Name': 'hermes-sell-runtime-secrets', 'RW': False},
        ]}


@pytest.mark.parametrize('failure', ['bind', 'root', 'host_network', 'public_port', 'writable_secrets', 'changed_config', 'tools'])
def test_container_inspection_rejects_isolation_drift(monkeypatch, failure):
    info = container_info()
    proof = {'policy_sha256': runtime_container.POLICY_SHA256, 'effective_toolsets': [], 'uid': 10000,
             'credential_provider': 'openai-codex', 'has_refresh_token': False, 'personal_mounts': False,
             'credential_generation': 'generation'}
    if failure == 'bind':
        info['Mounts'][0]['Type'] = 'bind'
    elif failure == 'root':
        info['Config']['User'] = '0:0'
    elif failure == 'host_network':
        info['HostConfig']['NetworkMode'] = 'host'
    elif failure == 'public_port':
        info['HostConfig']['PortBindings']['8642/tcp'][0]['HostIp'] = '0.0.0.0'
    elif failure == 'writable_secrets':
        info['Mounts'][1]['RW'] = True
    elif failure == 'changed_config':
        proof['policy_sha256'] = 'different'
    else:
        proof['effective_toolsets'] = ['terminal']
    monkeypatch.setattr(runtime_container, 'docker_json', lambda context, *args: [deepcopy(info)] if args[0] == 'inspect' else proof)
    with pytest.raises(RuntimeError):
        runtime_container.inspect_runtime()


def runtime_script():
    path = Path(__file__).resolve().parents[1] / 'scripts/hermes_runtime.py'
    spec = importlib.util.spec_from_file_location('runtime_script', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_access_token_export_excludes_refresh_and_unrelated_credentials(tmp_path):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': int(time.time()) + 3600}).encode()).decode().rstrip('=')
    token = 'header.' + payload + '.signature'
    original = {'version': 1, 'providers': {'other': {'token': 'unrelated-secret'}},
                'credential_pool': {'openai-codex': [{'access_token': token, 'refresh_token': 'rotating-secret',
                                                     'base_url': 'https://chatgpt.com/backend-api/codex'}]}}
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps(original))
    path.chmod(0o600)
    before = path.read_bytes()
    exported, expiry = runtime_script().access_only_auth(path)
    assert expiry > time.time()
    assert exported['providers'] == {}
    assert exported['credential_pool']['openai-codex'][0]['source'].startswith('manual:')
    assert 'refresh_token' not in json.dumps(exported) and 'unrelated-secret' not in json.dumps(exported)
    assert path.read_bytes() == before


def test_runtime_token_is_private_and_stable_across_restart(tmp_path):
    script = runtime_script()
    path = tmp_path / 'runtime-token'
    token = script.runtime_token(path)
    assert len(token) >= 32 and path.stat().st_mode & 0o777 == 0o600
    assert script.runtime_token(path) == token


@pytest.fixture
def admission_ledger(tmp_path):
    token = 'dedicated-test-runtime-token'
    token_file = tmp_path / 'runtime-token'
    token_file.write_text(token)
    path = tmp_path / 'runs_idempotency.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE run_idempotency (scope TEXT, idempotency_key TEXT, fingerprint TEXT, run_id TEXT)')
        connection.execute('INSERT INTO run_idempotency VALUES (?, ?, ?, ?)', (
            hashlib.sha256(('default\0' + token).encode()).hexdigest(), 'sell-fixed', 'a' * 64, 'run-fixed'))
    return path, token_file, 'sell-fixed', 'a' * 64, hashlib.sha256(token.encode()).hexdigest()


def test_read_only_admission_lookup_preserves_ledger_and_matches_exact_identity(admission_ledger):
    path, token_file, run_key, fingerprint, generation = admission_ledger
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert runtime_container.read_admission(*admission_ledger) == 'run-fixed'
    assert runtime_container.read_admission(path, token_file, 'sell-missing', fingerprint, generation) is None
    with pytest.raises(RuntimeError, match='different request'):
        runtime_container.read_admission(path, token_file, run_key, 'b' * 64, generation)
    with pytest.raises(RuntimeError, match='generation'):
        runtime_container.read_admission(path, token_file, run_key, fingerprint, 'c' * 64)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_admission_lookup_does_not_create_a_missing_ledger(admission_ledger):
    path, token_file, run_key, fingerprint, generation = admission_ledger
    missing = path.parent / 'missing.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        runtime_container.read_admission(missing, token_file, run_key, fingerprint, generation)
    assert not missing.exists()


def test_admission_lookup_checks_container_before_reading(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_container, 'inspect_runtime', lambda *args: {'credential_generation': 'c' * 64})
    monkeypatch.setattr(runtime_container, 'docker_json', lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match='generation'):
        runtime_container.lookup_run_id('sell-fixed', 'a' * 64, 'b' * 64)
    assert calls == []


def test_admission_lookup_sends_no_bearer_and_validates_run_id(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_container, 'inspect_runtime', lambda *args: {'credential_generation': 'b' * 64})
    def read(*args):
        calls.append(args)
        return 'run-fixed'
    monkeypatch.setattr(runtime_container, 'docker_json', read)
    assert runtime_container.lookup_run_id('sell-fixed', 'a' * 64, 'b' * 64) == 'run-fixed'
    assert calls[0][-3:] == ('sell-fixed', 'a' * 64, 'b' * 64)
    assert 'PRAGMA query_only=ON' in calls[0][-4] and '?mode=ro' in calls[0][-4]
    monkeypatch.setattr(runtime_container, 'docker_json', lambda *args: '../other-run')
    with pytest.raises(RuntimeError, match='invalid admission'):
        runtime_container.lookup_run_id('sell-fixed', 'a' * 64, 'b' * 64)
