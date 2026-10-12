import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from fmheadendd.app import create_app, argument_parser
from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.config import Settings
from fmheadendd.errors import DomainError
from fmheadendd.models.dsp import neutral_stages
from fmheadendd.models.audio_profile_file import AudioProfileFile, ProfileFileResult
from fmheadendd.models.dsp import IMPLEMENTATIONS, STAGES
from pydantic import TypeAdapter, ValidationError

DEFAULT = Path(__file__).resolve().parents[2] / 'audio-profiles/default/basic_filtering.json'


@pytest.fixture
def store(tmp_path):
    defaults = tmp_path / 'default'
    defaults.mkdir()
    (defaults / 'test_filters.json').write_text(json.dumps(document()), encoding='utf-8')
    return AudioProfileFiles(defaults, tmp_path / 'custom',
                             clock=lambda: datetime(2026, 9, 24, 21, 35, 15, tzinfo=timezone.utc))


def document():
    stages = neutral_stages()
    for key, (_, choices) in STAGES.items():
        if 'OFF' in choices and key not in ('hpf', 'lpf'):
            stages[key]['selected'] = 'OFF'
    stages['lpf']['implementations']['CHEBYSHEV'] = dict(cutoff_hz=16000., poles=14, ripple_db=0., type=1)
    return dict(schema_version=1, name='Test filters', description='Filter test profile', stages=stages)


@pytest.mark.parametrize('value', ['', 'Gentle compression / Normal loudness', 'Árvíztűrő'])
def test_description_validation_and_serialization(value):
    data = {**document(), 'description': value}
    parsed = AudioProfileFile.model_validate(data)
    assert parsed.schema_version == 1
    assert parsed.description == value
    assert json.loads(parsed.model_dump_json())['description'] == value
    assert AudioProfileFile.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize('value', [None, 1, 1.5, True, [], {}])
def test_description_rejects_non_strings(store, value):
    with pytest.raises(DomainError) as error:
        store.create({**document(), 'description': value})
    assert error.value.error == 'INVALID_PROFILE'


def test_description_required_in_documents_and_catalogue(store):
    data = document()
    del data['description']
    with pytest.raises(ValidationError) as error:
        AudioProfileFile.model_validate(data)
    assert error.value.errors()[0]['loc'] == ('description',)
    (store.roots['default'] / 'invalid.json').write_text(json.dumps(data), encoding='utf-8')
    for operation in (lambda: store.get('default/invalid.json'), store.list):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.error == 'INVALID_PROFILE'


def test_description_api_roundtrip_and_validation(store, tmp_path):
    settings = Settings(tmp_path / 'db', audio_profiles_default_dir=store.roots['default'],
                        audio_profiles_custom_dir=store.roots['custom'])
    base = '/api/v1/audio-profiles/files'
    with TestClient(create_app(settings)) as api:
        template = api.get(base + '/dsp').json()['template']
        assert template['schema_version'] == 1
        assert isinstance(template['description'], str)
        AudioProfileFile.model_validate({**template, 'name': 'New profile'})
        data = document()
        created = api.post(base + '/custom', json=data)
        assert created.status_code == 201
        path = created.json()['path']
        for result in (created.json(), api.get(base + '/' + path).json()):
            assert result['description'] == result['profile']['description'] == data['description']
        data['description'] = 'Updated description'
        saved = api.put(base + '/' + path, json=data)
        assert saved.status_code == 200
        assert saved.json()['description'] == saved.json()['profile']['description'] == data['description']
        listed = next(row for row in api.get(base).json() if row['path'] == path)
        assert listed['description'] == data['description']
        for original in (path, 'default/test_filters.json'):
            before = api.get(base + '/' + original).json()['profile']
            cloned = api.post(base + '/' + original + '/clone', json={'name': 'Copy'})
            assert cloned.status_code == 201
            assert cloned.json()['profile'] == {**before, 'name': 'Copy'}
            assert cloned.json()['description'] == before['description']
        missing = dict(data)
        del missing['description']
        for invalid in (missing, {**data, 'description': 42}):
            assert api.post(base + '/custom', json=invalid).status_code == 422
            assert api.put(base + '/' + path, json=invalid).status_code == 422
        assert api.get(base + '/' + path).json()['profile']['description'] == data['description']


def test_default_and_list(store):
    item = store.get('default/test_filters.json')
    assert item.profile.schema_version == 1
    assert item.profile.stages['wideband'].selected == 'OFF'
    assert item.profile.stages['input_gain'].implementations['VOLUME']['gain_db'] == 0
    assert item.profile.stages['lpf'].implementations['CHEBYSHEV'] == dict(cutoff_hz=16000, poles=14, ripple_db=0, type=1)
    assert [x.model_dump() for x in store.list()] == [
        {'path': 'default/test_filters.json', 'name': 'Test filters', 'kind': 'default', 'description': document()['description']}]


@pytest.mark.parametrize('weighting', [0, 50, 75])
def test_weighting_persists_even_with_limiter_off(store, weighting):
    data = document()
    data['stages']['limiter']['implementations'] = {
        'FM_HEADEND_LIMITER': {'weighting_us': weighting, 'true_peak_detection': True}}
    data['stages']['clipper']['implementations']['FM_HEADEND_CLIPPER'].update(
        clipping_mode='soft', soft_knee_db=4.5)
    item = store.create(data)
    loaded = store.get(item.path)
    assert loaded.profile.stages['limiter'].selected == 'OFF'
    assert loaded.profile.stages['limiter'].implementations['FM_HEADEND_LIMITER']['weighting_us'] == weighting
    assert loaded.profile.stages['limiter'].implementations['FM_HEADEND_LIMITER']['true_peak_detection'] is True
    assert loaded.profile.stages['clipper'].implementations['FM_HEADEND_CLIPPER']['clipping_mode'] == 'soft'
    assert loaded.profile.stages['clipper'].implementations['FM_HEADEND_CLIPPER']['soft_knee_db'] == 4.5
    store.save(item.path, loaded.profile.model_dump())
    assert store.get(item.path) == loaded


def test_crud_clone_stable_name_and_inactive_settings(store):
    first = store.clone('default/test_filters.json', 'Vitézy Dávid for president')
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
    a = store.clone('default/test_filters.json', '漢字')
    b = store.clone('default/test_filters.json', '漢字')
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
    for action in (lambda: store.save('default/test_filters.json', document()),
                   lambda: store.delete('default/test_filters.json')):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.status == 403
    item = store.create(document())
    store.is_referenced = lambda path: path == item.path
    with pytest.raises(DomainError) as error:
        store.delete(item.path)
    assert error.value.error == 'PROFILE_IN_USE'
    assert store.get(item.path)


@pytest.mark.parametrize('path', ['/etc/passwd', '../secret.json', 'custom/../basic_filtering.json',
    'custom/a/b.json', 'custom\\x.json', 'C:/secret.json', 'default//basic_filtering.json', 'other/a.json'])
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
        assert api.get(base).json()[0]['path'] == 'default/test_filters.json'
        assert api.get(base + '/default/test_filters.json').status_code == 200
        created = api.post(base + '/custom', json=document())
        assert created.status_code == 201
        path = created.json()['path']
        assert api.put(base + '/' + path, json={**document(), 'name': 'New'}).json()['path'] == path
        assert api.post(base + '/' + path + '/clone', json={'name': 'Copy'}).status_code == 201
        assert api.post(base + '/default/test_filters.json/clone', json={'name': 'Default copy'}).status_code == 201
        assert api.put(base + '/default/test_filters.json', json=document()).status_code == 405
        assert api.delete(base + '/default/test_filters.json').status_code == 405
        invalid = copy.deepcopy(document())
        invalid['stages']['hpf']['implementations']['CHEBYSHEV']['limiter_enabled'] = 'yes'
        assert api.post(base + '/custom', json=invalid).status_code == 422
        assert api.delete(base + '/' + path).status_code == 204
        assert api.get(base + '/' + path).status_code == 404
        assert api.get('/api/v1/audio-profiles').status_code == 404


@pytest.mark.parametrize('path', sorted(DEFAULT.parent.glob('*.json')), ids=lambda path: path.name)
def test_bundled_profiles_current_structure(path):
    from fmheadendd.models.audio_profile_file import AudioProfileFile
    from fmheadendd.models.dsp import STAGES
    data = json.loads(path.read_text(encoding='utf-8'))
    parsed = AudioProfileFile.model_validate(data)
    assert list(parsed.stages) == list(STAGES)
    assert parsed.description == data['description']
    assert parsed.model_dump() == data


def test_loader_preserves_result_normalization_and_validation_contract(store):
    # Include every saved implementation, including disabled stages, and let
    # validation fill defaults and restore deliberately reversed stage order.
    data = dict(schema_version=1, description="Test profile", name='Defaults', stages={
        key: dict(selected=choices[0], implementations={name: {} for name in choices if name != 'OFF'})
        for key, (_, choices) in reversed(list(STAGES.items()))})
    path = store.roots['default'] / 'all.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    expected = ProfileFileResult(description="Test profile", name=data['name'], path='default/all.json', kind='default',
                                profile=store.validate(copy.deepcopy(data)))
    once = store.validate(copy.deepcopy(data))
    # The second pass is not redundant for default-filled settings: default
    # integers in float fields become floats on revalidation. Do not bypass it
    # without preserving this existing normalization/serialization contract.
    assert type(once.stages['input_gain'].implementations['VOLUME']['gain_db']) is int
    assert type(expected.profile.stages['input_gain'].implementations['VOLUME']['gain_db']) is float
    eq = IMPLEMENTATIONS['LSP_EQ'][0]
    with patch.object(eq, 'model_validate', wraps=eq.model_validate) as validate_eq:
        actual = store.get('default/all.json')
        assert validate_eq.call_count == 2
    assert type(actual) is ProfileFileResult
    assert actual.model_fields_set == expected.model_fields_set
    assert actual.model_dump() == expected.model_dump()
    assert actual.model_dump_json() == expected.model_dump_json()
    assert list(actual.profile.stages) == list(STAGES)
    assert TypeAdapter(ProfileFileResult).validate_python(actual).model_dump() == expected.model_dump()
    assert [x.model_dump() for x in store.list()] == [
        actual.model_dump(exclude={'profile'}),
        store.get('default/test_filters.json').model_dump(exclude={'profile'})]
    # No caller or subsequent load shares the loaded document's mutable settings.
    actual.profile.stages['input_gain'].implementations['VOLUME']['gain_db'] = 12
    assert store.get('default/all.json').profile.stages['input_gain'].implementations['VOLUME']['gain_db'] == 0


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'name': ''}, {'unknown': 1}, {'stages': {}},
    {'stages': {**document()['stages'], 'input_gain': {
        'selected': 'VOLUME', 'implementations': {'VOLUME': {'gain_db': 21}}}}},
])
def test_loader_keeps_exact_validation_errors_and_external_models_stay_strict(store, change):
    data = {**document(), **change}
    (store.roots['default'] / 'bad.json').write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(DomainError) as direct:
        store.validate(copy.deepcopy(data))
    with pytest.raises(DomainError) as loaded:
        store.get('default/bad.json')
    assert (loaded.value.status, loaded.value.error, str(loaded.value)) == (
        direct.value.status, direct.value.error, str(direct.value))
    with pytest.raises(ValidationError):
        ProfileFileResult(description="Test profile", name='Bad', path='default/bad.json', kind='default', profile=data)


def test_external_mutated_or_constructed_profile_is_not_trusted_by_result_model(store):
    profile = store.validate(document())
    profile.stages['input_gain'].implementations['VOLUME']['gain_db'] = 21
    unvalidated = AudioProfileFile.model_construct(schema_version=1, description="Test profile", name='Invalid', stages={})
    for supplied in (profile, unvalidated):
        with pytest.raises(ValidationError):
            ProfileFileResult(description="Test profile", name='Invalid', path='custom/bad.json', kind='custom', profile=supplied)


def test_tracked_profiles_contain_fm_peak_defaults():
    for path in DEFAULT.parent.glob('*.json'):
        data = json.loads(path.read_text(encoding='utf-8'))
        limiter = data['stages']['limiter']['implementations']['FM_HEADEND_LIMITER']
        clipper = data['stages']['clipper']['implementations']['FM_HEADEND_CLIPPER']
        assert limiter['true_peak_detection'] is False, path
        assert clipper['clipping_mode'] == 'hard', path
        assert clipper['soft_knee_db'] == 2., path
        assert AudioProfileFile.model_validate(data).model_dump() == data
