"""Fresh profile checks share only immutable outcomes inside one collection."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.errors import DomainError
from fmheadendd.sources.manager import SourceManager

DEFAULT = Path(__file__).resolve().parents[2] / 'audio-profiles/default/basic.json'


@pytest.fixture
def profiles(tmp_path):
    root = tmp_path / 'custom'
    root.mkdir()
    data = json.loads(DEFAULT.read_text(encoding='utf-8'))
    for name in ('first', 'second'):
        (root / f'{name}.json').write_text(json.dumps(data), encoding='utf-8')
    return AudioProfileFiles(DEFAULT.parent, root), data


def manager_for(files, now, paths):
    manager = SourceManager(None, profile_files=files, clock=lambda: now[0])
    sources = [SimpleNamespace(id=i, processor_mode='builtin', audio_profile_path=path)
               for i, path in enumerate(paths)]
    for source in sources:
        manager.pipelines[source.id] = SimpleNamespace(mocked=False,
            snapshot=lambda: dict(state='RUNNING', stream_title='Current'),
            pcm_snapshot=lambda: dict(staging={'accepted_bytes': 100}, consumers={}))
    return manager, sources


def observe(manager, sources):
    errors = {}
    statuses = [manager.status(s.id, source=s, profile_errors=errors) for s in sources]
    return statuses, errors


def test_shared_and_distinct_profiles_and_runtime_ownership(profiles):
    files, _ = profiles
    now = [0.0]
    manager, sources = manager_for(files, now, ['custom/first.json'] * 4 + ['custom/second.json'])
    with patch.object(files, 'get', wraps=files.get) as load:
        statuses, errors = observe(manager, sources)
        assert [c.args[0] for c in load.call_args_list] == ['custom/first.json', 'custom/second.json']
        assert errors == {('builtin', 'custom/first.json'): None, ('builtin', 'custom/second.json'): None}
        assert all(s.state == 'RUNNING' for s in statuses)
        statuses[0].pcm.staging.accepted_bytes = 999
        statuses[0].stream_title = 'Changed'
        assert statuses[1].pcm.staging.accepted_bytes == 100 and statuses[1].stream_title == 'Current'
        # A new publication samples runtime again even when validation TTLs hit.
        now[0] = .5
        fresh, _ = observe(manager, sources)
        assert fresh[0].pcm.staging.accepted_bytes == 100 and fresh[0].stream_title == 'Current'
        assert load.call_count == 2
        now[0] = 1.0
        observe(manager, sources)
        assert load.call_count == 4  # No shared dictionary survives a generation.
        (files.roots['custom'] / 'second.json').write_text('{broken', encoding='utf-8')
        now[0] = 2.0
        statuses, _ = observe(manager, sources)
        assert all(s.state == 'RUNNING' for s in statuses[:4])
        assert statuses[4].state == 'ERROR'
        assert load.call_count == 6


def test_ordinary_source_observations_keep_independent_lookup_path(profiles):
    files, _ = profiles
    manager, sources = manager_for(files, [0.0], ['custom/first.json'] * 4)
    with patch.object(files, 'get', wraps=files.get) as load:
        for source in sources:
            assert manager.status(source.id, source=source).state == 'RUNNING'
        assert load.call_count == 4


@pytest.mark.parametrize('failure', ['missing', 'json', 'settings', 'permission', 'path'])
def test_errors_are_shared_and_next_due_generation_observes_repair(profiles, failure):
    files, data = profiles
    now = [0.0]
    manager, sources = manager_for(files, now, ['custom/first.json'] * 4)
    path = files.roots['custom'] / 'first.json'
    if failure == 'missing':
        path.unlink()
    elif failure == 'json':
        path.write_text('{broken', encoding='utf-8')
    elif failure == 'settings':
        invalid = json.loads(json.dumps(data))
        invalid['stages']['input_gain']['implementations']['VOLUME']['gain_db'] = 21
        path.write_text(json.dumps(invalid), encoding='utf-8')
    elif failure == 'path':
        for source in sources:
            source.audio_profile_path = 'custom/../first.json'
    permission = patch('fmheadendd.audio_profile_files.os.open', side_effect=PermissionError(13, 'Denied'))
    with (permission if failure == 'permission' else nullcontext()):
        with pytest.raises(DomainError) as expected:
            files.get(sources[0].audio_profile_path)
        with patch.object(files, 'get', wraps=files.get) as load:
            statuses, errors = observe(manager, sources)
            load.assert_called_once()
        text = f'{expected.value.error}: {expected.value}'
        assert all(s.state == 'ERROR' and s.last_error == text for s in statuses)
        assert list(errors.values()) == [text]
    path.write_text(json.dumps(data), encoding='utf-8')
    for source in sources:
        source.audio_profile_path = 'custom/first.json'
    now[0] = 1.0
    assert all(s.state == 'RUNNING' for s in observe(manager, sources)[0])


def test_external_edit_and_deletion_keep_existing_ttl_boundary(profiles):
    files, data = profiles
    now = [0.0]
    manager, sources = manager_for(files, now, ['custom/first.json'] * 4)
    path = files.roots['custom'] / 'first.json'
    assert all(s.state == 'RUNNING' for s in observe(manager, sources)[0])
    path.write_text('{broken', encoding='utf-8')
    now[0] = .5
    assert all(s.state == 'RUNNING' for s in observe(manager, sources)[0])
    # Audio lifecycle validation is never allowed to consume observation reuse.
    with pytest.raises(DomainError):
        manager.validate_config(sources[0])
    now[0] = 1.0
    assert all(s.state == 'ERROR' for s in observe(manager, sources)[0])
    path.write_text(json.dumps(data), encoding='utf-8')
    now[0] = 2.0
    assert all(s.state == 'RUNNING' for s in observe(manager, sources)[0])
    path.unlink()
    now[0] = 2.5
    assert all(s.state == 'RUNNING' for s in observe(manager, sources)[0])
    now[0] = 3.0
    assert all('PROFILE_NOT_FOUND' in s.last_error for s in observe(manager, sources)[0])


def test_old_source_ttl_hit_does_not_seed_fresh_generation(profiles):
    files, _ = profiles
    now = [0.0]
    manager, sources = manager_for(files, now, ['custom/first.json'] * 4)
    manager.status(sources[0].id, source=sources[0])
    (files.roots['custom'] / 'first.json').write_text('{broken', encoding='utf-8')
    now[0] = .5
    statuses, _ = observe(manager, sources)
    assert statuses[0].state == 'RUNNING'  # Existing source's original TTL.
    assert all(s.state == 'ERROR' for s in statuses[1:])  # Due observations read the file.


def test_concurrent_independent_collections_share_no_cache(profiles):
    files, _ = profiles
    first = manager_for(files, [0.0], ['custom/first.json'] * 4)
    second = manager_for(files, [0.0], ['custom/first.json'] * 4)
    barrier = Barrier(2)
    original = files.get
    def load(path):
        barrier.wait(timeout=5)
        return original(path)
    with patch.object(files, 'get', side_effect=load) as get:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = [f.result(timeout=10) for f in [pool.submit(observe, *first), pool.submit(observe, *second)]]
        assert get.call_count == 2
    assert a[1] == b[1] and a[1] is not b[1]
    assert all(s.state == 'RUNNING' for s in a[0] + b[0])
