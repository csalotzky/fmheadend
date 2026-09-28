import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from fmheadendd.app import create_app, argument_parser
from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.config import Settings
from fmheadendd.errors import DomainError
from fmheadendd.models.dsp import neutral_stages

DEFAULT = Path(__file__).resolve().parents[2] / 'audio-profiles/default/basic.json'


@pytest.fixture
def store(tmp_path):
    defaults = tmp_path / 'default'
    defaults.mkdir()
    shutil.copy(DEFAULT, defaults / 'basic.json')
    return AudioProfileFiles(defaults, tmp_path / 'custom',
                             clock=lambda: datetime(2026, 9, 24, 21, 35, 15, tzinfo=timezone.utc))


def document():
    return json.loads(DEFAULT.read_text(encoding='utf-8'))


def test_default_and_list(store):
    item = store.get('default/basic.json')
    assert item.profile.schema_version == 1
    assert item.profile.stages['wideband'].selected == 'OFF'
    assert item.profile.stages['input_gain'].implementations['VOLUME']['gain_db'] == 0
    assert item.profile.stages['lpf'].implementations['CHEBYSHEV'] == dict(cutoff_hz=16000, poles=14, ripple_db=0, type=1)
    assert [x.model_dump() for x in store.list()] == [
        {'path': 'default/basic.json', 'name': 'Basic', 'kind': 'default'}]


@pytest.mark.parametrize('weighting', ['off', '50us', '75us'])
def test_weighting_persists_even_with_limiter_off(store, weighting):
    data = document()
    data['stages']['limiter']['implementations'] = {'LSP_LIMITER': {'fm_limiter_weighting': weighting}}
    item = store.create(data)
    loaded = store.get(item.path)
    assert loaded.profile.stages['limiter'].selected == 'OFF'
    assert loaded.profile.stages['limiter'].implementations['LSP_LIMITER']['fm_limiter_weighting'] == weighting
    store.save(item.path, loaded.profile.model_dump())
    assert store.get(item.path) == loaded


def test_crud_clone_stable_name_and_inactive_settings(store):
    first = store.clone('default/basic.json', 'Vitézy Dávid for president')
    assert first.path == 'custom/vitezy_david_for_president_20260924213515.json'
    data = first.profile.model_dump()
    data['name'] = 'Renamed'
    data['stages']['eq']['selected'] = 'OFF'
    data['stages']['eq']['implementations'] = neutral_stages()['eq']['implementations']
    data['stages']['eq']['implementations']['LSP_EQ']['bands'][0]['gain_db'] = 3
    saved = store.save(first.path, data)
    assert saved.path == first.path and saved.name == 'Renamed'
    cloned = store.clone(saved.path, 'Copy')
    assert cloned.profile.stages['eq'].selected == 'OFF'
    assert cloned.profile.stages['eq'].implementations['LSP_EQ']['bands'][0]['gain_db'] == 3
    assert len(store.list()) == 3
    store.delete(first.path)
    assert len(store.list()) == 2


def test_collision_and_empty_slug(store):
    a = store.clone('default/basic.json', '漢字')
    b = store.clone('default/basic.json', '漢字')
    assert a.path == 'custom/profile_20260924213515.json'
    assert b.path == 'custom/profile_20260924213515_1.json'
    assert store.get(a.path).name == '漢字'


def test_cli_roots():
    args = argument_parser().parse_args(['--audio-profiles-default-dir', 'audio-profiles/default',
                                         '--audio-profiles-custom-dir', 'tmp/profiles'])
    assert args.audio_profiles_default_dir == 'audio-profiles/default'
    assert args.audio_profiles_custom_dir == 'tmp/profiles'


def test_atomic_create_collision_does_not_replace(store, monkeypatch):
    import os
    original = os.link
    calls = []
    def race(source, destination):
        calls.append(destination)
        if len(calls) == 1:
            destination.write_text('another creator owns this file')
        return original(source, destination)
    monkeypatch.setattr('fmheadendd.audio_profile_files.os.link', race)
    created = store.create(document())
    assert created.path.endswith('_1.json')
    assert calls[0].read_text() == 'another creator owns this file'


def test_unknown_implementation_and_gain(store):
    for stage, changes in [('input_gain', {'selected': 'AUTO'}),
                           ('wideband', {'selected': 'LSP'}),
                           ('wideband', {'implementations': {'LSP': {}}})]:
        data = document()
        data['stages'][stage].update(changes)
        with pytest.raises(DomainError):
            store.create(data)
    data = document()
    data['stages']['input_gain']['implementations']['VOLUME']['gain_db'] = 21
    with pytest.raises(DomainError):
        store.create(data)


@pytest.mark.parametrize('change', [
    {'schema_version': 2}, {'schema_version': True}, {'schema_version': '1'},
    {'unknown': 1}, {'name': ''}, {'stages': {}},
])
def test_document_validation(store, change):
    with pytest.raises(DomainError) as error:
        store.create({**document(), **change})
    assert error.value.error == 'INVALID_PROFILE'


@pytest.mark.parametrize('field,value', [('poles', 3), ('cutoff_hz', 24000),
    ('cutoff_hz', float('nan')), ('type', 'yes'), ('ripple_db', -1), ('unknown', 1)])
def test_settings_invalid_before_replace(store, field, value):
    item = store.create(document())
    path = store.roots['custom'] / item.path.split('/')[1]
    before = path.read_bytes()
    data = document()
    data['stages']['hpf']['implementations']['CHEBYSHEV'][field] = value
    with pytest.raises(DomainError):
        store.save(item.path, data)
    assert path.read_bytes() == before


def test_default_reference_protection(store):
    for action in (lambda: store.save('default/basic.json', document()),
                   lambda: store.delete('default/basic.json')):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.status == 403
    item = store.create(document())
    store.is_referenced = lambda path: path == item.path
    with pytest.raises(DomainError) as error:
        store.delete(item.path)
    assert error.value.error == 'PROFILE_IN_USE'
    assert store.get(item.path)


@pytest.mark.parametrize('path', ['/etc/passwd', '../secret.json', 'custom/../basic.json',
    'custom/a/b.json', 'custom\\x.json', 'C:/secret.json', 'default//basic.json', 'other/a.json'])
def test_paths(store, path):
    for action in (lambda: store.get(path), lambda: store.save(path, document()), lambda: store.delete(path)):
        with pytest.raises(DomainError):
            action()


def test_missing_invalid_and_repair(store):
    with pytest.raises(DomainError) as error:
        store.get('custom/missing.json')
    assert error.value.error == 'PROFILE_NOT_FOUND'
    for action in (lambda: store.save('custom/missing.json', document()),
                   lambda: store.delete('custom/missing.json')):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.error == 'PROFILE_NOT_FOUND'
    store.roots['custom'].mkdir()
    path = store.roots['custom'] / 'bad.json'
    for raw in ('{bad', '{"schema_version":9}', 'x' * 65537):
        path.write_text(raw)
        with pytest.raises(DomainError) as error:
            store.get('custom/bad.json')
        assert error.value.error == 'INVALID_PROFILE'
        with pytest.raises(DomainError):
            store.list()
    store.save('custom/bad.json', document())
    assert store.get('custom/bad.json')
    (store.roots['custom'] / 'directory.json').mkdir()
    with pytest.raises(DomainError) as error:
        store.get('custom/directory.json')
    assert error.value.error == 'INVALID_PROFILE'


def test_atomic_replace_failure_preserves_original(store, monkeypatch):
    item = store.create(document())
    path = store.roots['custom'] / item.path.split('/')[1]
    before = path.read_bytes()
    def fail(source, target):
        assert Path(source).parent == target.parent
        assert json.loads(Path(source).read_text())['name'] == 'New'
        raise OSError('simulated replace failure')
    monkeypatch.setattr('fmheadendd.audio_profile_files.os.replace', fail)
    with pytest.raises(DomainError):
        store.save(item.path, {**document(), 'name': 'New'})
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_symlink_escape(store, tmp_path):
    outside = tmp_path / 'outside.json'
    outside.write_text(json.dumps(document()))
    store.roots['custom'].mkdir()
    link = store.roots['custom'] / 'link.json'
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f'Platform cannot create symlinks: {exc}')
    for action in (lambda: store.get('custom/link.json'), lambda: store.delete('custom/link.json'),
                   lambda: store.save('custom/link.json', document()), store.list):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.error == 'INVALID_PROFILE_PATH'
    assert outside.exists()
    root_link = tmp_path / 'root_link'
    root_link.symlink_to(store.roots['custom'], target_is_directory=True)
    other = AudioProfileFiles(store.roots['default'], root_link)
    with pytest.raises(DomainError):
        other.create(document())


def test_file_api(store, tmp_path):
    settings = Settings(tmp_path / 'db', audio_profiles_default_dir=store.roots['default'],
                        audio_profiles_custom_dir=store.roots['custom'])
    base = '/api/v1/audio-profiles/files'
    with TestClient(create_app(settings)) as api:
        assert api.get(base).json()[0]['path'] == 'default/basic.json'
        assert api.get(base + '/default/basic.json').status_code == 200
        created = api.post(base + '/custom', json=document())
        assert created.status_code == 201
        path = created.json()['path']
        assert api.put(base + '/' + path, json={**document(), 'name': 'New'}).json()['path'] == path
        assert api.post(base + '/' + path + '/clone', json={'name': 'Copy'}).status_code == 201
        assert api.post(base + '/default/basic.json/clone', json={'name': 'Default copy'}).status_code == 201
        assert api.put(base + '/default/basic.json', json=document()).status_code == 405
        assert api.delete(base + '/default/basic.json').status_code == 405
        invalid = copy.deepcopy(document())
        invalid['stages']['hpf']['implementations']['CHEBYSHEV']['limiter_enabled'] = 'yes'
        assert api.post(base + '/custom', json=invalid).status_code == 422
        assert api.delete(base + '/' + path).status_code == 204
        assert api.get(base + '/' + path).status_code == 404
        assert api.get('/api/v1/audio-profiles').status_code == 404
