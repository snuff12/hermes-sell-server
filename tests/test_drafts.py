from copy import deepcopy
import json

import pytest

from hermes_sell.authoring import validate_draft
from hermes_sell.contracts import Problem
from hermes_sell.drafts import DraftStore


def candidate(revision):
    return {key: revision['public'][key] for key in ('name', 'description', 'input_schema', 'output_schema', 'examples')}


def test_drafts_are_immutable_private_and_integrity_checked(tmp_path, revision):
    store = DraftStore(tmp_path / 'drafts')
    record = store.save(revision['bundle'], candidate(revision))
    assert store.save(revision['bundle'], candidate(revision)) == record
    path = store.directory / (record['draft_id'] + '.json')
    assert path.stat().st_mode & 0o777 == 0o600
    damaged = deepcopy(record)
    damaged['bundle']['files']['SKILL.md'] = 'replacement'
    path.write_text(json.dumps(damaged))
    with pytest.raises(Problem, match='integrity'):
        store.get(record['draft_id'])


def test_draft_import_rejects_paths_and_symlinks(tmp_path, revision):
    store = DraftStore(tmp_path / 'drafts')
    with pytest.raises(Problem) as invalid:
        store.get('../private')
    assert invalid.value.code == 'invalid_draft_id'
    record = store.save(revision['bundle'], candidate(revision))
    path = store.directory / (record['draft_id'] + '.json')
    target = tmp_path / 'outside.json'
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(Problem) as invalid:
        store.get(record['draft_id'])
    assert invalid.value.code == 'invalid_draft'


def test_model_cannot_silently_replace_supplied_examples(revision):
    draft = candidate(revision)
    supplied = deepcopy(draft['examples'][0])
    draft['examples'][0]['input']['diff'] = 'different input'
    with pytest.raises(Problem) as invalid:
        validate_draft(draft, input_example=supplied['input'], output_example=supplied['output'])
    assert invalid.value.code == 'example_changed'
