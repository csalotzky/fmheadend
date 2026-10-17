"""Local configuration persistence and discovery-only probes; no media playback."""
import json
import sqlite3
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database, SCHEMA_VERSION
from fmheadendd.models.configuration import InputSourceConfig, LocalConfig
from fmheadendd.sources.local_media import LocalMediaMode, PlaybackOrder, FolderResolver, M3uResolver

P = '/api/v1/sources'


def body(**values):
    return dict(name='Local media', type='LOCAL', config=dict(mode='folder', path='/mnt/offline/music') | values)


@pytest.fixture
def api(tmp_path, monkeypatch):
    # Any accidental save-time resolution or player construction fails the test.
    monkeypatch.setattr(FolderResolver, 'resolve', Mock(side_effect=AssertionError('folder scan')))
    monkeypatch.setattr(M3uResolver, 'resolve', Mock(side_effect=AssertionError('playlist read')))
    monkeypatch.setattr(LocalConfig, 'to_core', Mock(side_effect=AssertionError('premature conversion')))
    factory = Mock(side_effect=AssertionError('pipeline construction'))
    factory.mocked = True
    factory.__name__ = "NoPlaybackFactory"
    with TestClient(create_app(Settings(tmp_path / 'local.db'), pipeline_factory=factory)) as client:
        yield client
    factory.assert_not_called()


@pytest.mark.parametrize('mode,path', [('folder', '/mnt/nas/offline'),
    ('folder', '/media/unplugged/music'), ('playlist', '/srv/missing/night.m3u')])
@pytest.mark.parametrize('order', ['sequential', 'random'])
@pytest.mark.parametrize('recursive', [False, True])
def test_create_read_list_delete_local(api, mode, path, order, recursive):
    config = dict(mode=mode, path=path, order=order, recursive=recursive)
    response = api.post(P, json=body(**config))
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved['type'] == 'LOCAL' and saved['config'] == config
    assert api.get(P+f"/{saved['id']}").json() == saved
    assert api.get(P).json() == [saved]
    assert not api.app.state.services.sources.pipelines
    assert api.delete(P+f"/{saved['id']}").status_code == 204
    assert api.get(P).json() == []


def test_defaults_path_spelling_and_idle_update(api):
    path = '/srv/music-link//album/../playlist.m3u'
    saved = api.post(P, json=body(mode='playlist', path=path)).json()
    assert saved['config'] == dict(mode='playlist', path=path, order='sequential', recursive=False)
    sid = saved['id']
    for config in [dict(mode='folder', path='/media/new', order='random', recursive=True),
                   dict(mode='playlist', path='/srv/next.m3u8', recursive=True),
                   dict(mode='folder', path='/mnt/final', order='sequential', recursive=False)]:
        result = api.patch(P+f'/{sid}', json={'config': config})
        assert result.status_code == 200, result.text
        assert result.json()['saved'] and result.json()['applied']
        expected = dict(order='sequential', recursive=False) | config
        assert api.get(P+f'/{sid}').json()['config'] == expected
    # Existing config replacement semantics: a config patch is not a deep merge.
    assert api.patch(P+f'/{sid}', json={'config': {'order': 'random'}}).status_code == 422
    assert api.get(P+f'/{sid}').json()['config'] == expected


@pytest.mark.parametrize('config', [
    {}, {'mode': 'folder'}, {'path': '/mnt/music'},
    {'mode': 'other', 'path': '/mnt/music'},
    {'mode': 'folder', 'path': ''}, {'mode': 'folder', 'path': 'relative/music'},
    {'mode': 'folder', 'path': 'file:///mnt/music'},
    {'mode': 'folder', 'path': 'https://example.org/music'},
    {'mode': 'folder', 'path': '/mnt/invalid\x00path'},
    {'mode': 'folder', 'path': 123},
    {'mode': 'folder', 'path': '/mnt/music', 'order': 'shuffle'},
    {'mode': 'folder', 'path': '/mnt/music', 'recursive': 'false'},
    {'mode': 'folder', 'path': '/mnt/music', 'recursive': 1},
    {'mode': 'folder', 'path': '/mnt/music', 'queue': []},
    {'mode': 'folder', 'path': '/mnt/music', 'current_track': 'song'},
])
def test_invalid_creation_and_update_are_rejected(api, config):
    saved = api.post(P, json=body()).json()
    assert api.post(P, json=dict(body(), config=config)).status_code == 422
    assert api.patch(P+f"/{saved['id']}", json={'config': config}).status_code == 422
    assert api.get(P+f"/{saved['id']}").json() == saved


@pytest.mark.parametrize('mode', ['folder', 'playlist'])
def test_restart_and_database_reload_preserve_config_only(tmp_path, mode):
    path = tmp_path / 'restart.db'
    config = dict(mode=mode, path='/mnt/offline/symlink-name', order='random', recursive=True)
    with TestClient(create_app(Settings(path))) as api:
        saved = api.post(P, json=body(**config)).json()
    db = Database(path)
    try:
        assert SCHEMA_VERSION == db.connection.execute('PRAGMA user_version').fetchone()[0] == 1
        assert db.get('sources', saved['id']).model_dump(mode='json') == saved
        raw = json.loads(db.connection.execute('SELECT data FROM sources WHERE id=?', (saved['id'],)).fetchone()[0])
        assert raw['config'] == config
        assert set(raw) == set(saved) - {'id', 'rds_profile', 'rds_profile_id'}
    finally:
        db.close()
    with TestClient(create_app(Settings(path))) as api:
        assert api.get(P).json() == [saved]
        assert not api.app.state.services.sources.pipelines


def test_existing_types_mix_and_type_changes_use_normal_api(api):
    http = dict(name='HTTP', type='HTTP', config={'uri': 'https://example.org/live.m3u8'})
    old = api.post(P, json=http).json()
    local = api.post(P, json=body()).json()
    assert [r['type'] for r in api.get(P).json()] == ['HTTP', 'LOCAL']
    sid = old['id']
    result = api.patch(P+f'/{sid}', json={'type': 'LOCAL', 'config': body()['config']})
    assert result.status_code == 200 and result.json()['type'] == 'LOCAL'
    result = api.patch(P+f'/{sid}', json={'type': 'HTTP', 'config': http['config']})
    assert result.status_code == 200 and result.json()['type'] == 'HTTP'
    # Shared source references remain ordinary foreign keys, with deletion protection.
    for tx in (0, 2):
        assert api.patch(f'/api/v1/modulators/{tx}', json={'input_source_id': local['id']}).status_code == 200
    assert api.delete(P+f"/{local['id']}").status_code == 409


def test_core_conversion_reuses_enums_without_filesystem_access(tmp_path):
    path = str(tmp_path / 'unmounted' / 'symlink')
    config = LocalConfig(mode='playlist', path=path, recursive=True, order='random')
    core = config.to_core()
    assert core.mode is LocalMediaMode.PLAYLIST
    assert core.order is PlaybackOrder.RANDOM
    assert str(core.path) == path and core.recursive is True
    source = InputSourceConfig.model_validate(body(path=path))
    assert source.config['path'] == path


@pytest.mark.parametrize('mode,recursive,count', [
    ('folder', False, 0), ('folder', False, 1), ('folder', False, 2),
    ('folder', True, 3), ('playlist', False, 0), ('playlist', False, 3),
])
def test_local_probe_counts_without_playback_or_persistence(tmp_path, mode, recursive, count):
    factory = Mock(side_effect=AssertionError('pipeline construction'))
    factory.mocked = False
    factory.__name__ = 'NoPlaybackFactory'
    media = tmp_path / 'media'
    media.mkdir()
    if mode == 'folder':
        for name in ['a.MP3', 'b.wav'][:min(count, 2)]:
            (media / name).write_bytes(b'not decodable audio')
        (media / 'ignored.txt').write_text('ignored')
        (media / '.hidden.mp3').write_text('ignored')
        (media / 'sub').mkdir()
        (media / 'sub' / 'c.flac').write_text('candidate')
        path = media
    else:
        path = media / 'test.m3u'
        path.write_text('#EXTM3U\n' + ('missing.mp3\nmissing.mp3\nhttps://invalid.example/a\n' if count else ''))
    before_files = {p: p.read_bytes() for p in media.rglob('*') if p.is_file()}
    with TestClient(create_app(Settings(tmp_path / 'local.db'), pipeline_factory=factory)) as api:
        draft = body(mode=mode, path=str(path), recursive=recursive)
        saved = api.post(P, json=draft).json()
        with sqlite3.connect(tmp_path / 'local.db') as snapshot:
            before = '\n'.join(snapshot.iterdump())
        for response in (api.post(P+'/test', json=draft), api.post(P+f"/{saved['id']}/test")):
            assert response.status_code == 200
            result = response.json()
            assert result['ok'] == bool(count) and result['mocked'] is False
            assert result['local_file_count' if mode == 'folder' else 'playlist_entry_count'] == count
            assert result['codec'] is None and result['output_caps'] is None
            if not count:
                assert result['message'] == ('No supported audio files found.' if mode == 'folder'
                                             else 'Playlist contains no entries.')
        with sqlite3.connect(tmp_path / 'local.db') as snapshot:
            assert '\n'.join(snapshot.iterdump()) == before
        assert not api.app.state.services.sources.pipelines
    assert {p: p.read_bytes() for p in before_files} == before_files
    factory.assert_not_called()


@pytest.mark.parametrize('mode,contents,code', [
    ('folder', None, 'FOLDER_UNREADABLE'),
    ('playlist', None, 'PLAYLIST_UNREADABLE'),
    ('playlist', 'ftp://example.org/a.mp3', 'INVALID_PLAYLIST_ENTRY'),
])
def test_local_probe_resolution_errors(tmp_path, mode, contents, code):
    from fmheadendd.sources.manager import SourceManager
    path = tmp_path / 'missing.m3u'
    if contents is not None:
        path.write_text(contents)
    result = SourceManager(None).test(InputSourceConfig.model_validate(body(mode=mode, path=str(path))))
    assert not result.ok and not result.mocked
    assert code in result.message
