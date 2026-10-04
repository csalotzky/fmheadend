"""Podcast uses generic source CRUD/probe, with no network during persistence."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable, DomainError
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.podcast_fetch import FeedFetcher, FeedResponse

P = '/api/v1/sources'
URL = 'https://offline.example.org/feed'
RSS = b'''<rss version="2.0"><channel><title>Test podcast</title><item><guid>1</guid>
<title>Episode</title><enclosure url="https://cdn.example.org/download?id=1" type="audio/mpeg"/>
</item></channel></rss>'''


def body(**config):
    return dict(name='Podcast', type='PODCAST', config={'feeds': [{'url': URL}]} | config)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(FeedFetcher, '__call__', Mock(side_effect=AssertionError('unexpected network')))
    factory = Mock(side_effect=AssertionError('unexpected GStreamer'))
    factory.mocked = True
    factory.__name__ = 'NoPlayback'
    with TestClient(create_app(Settings(tmp_path/'podcast.db'), pipeline_factory=factory)) as client:
        yield client
    factory.assert_not_called()


@pytest.mark.parametrize('mode', ['SMART', 'RANDOM'])
def test_crud_offline_config_and_defaults(api, mode):
    result = api.post(P, json=body(schedule_mode=mode))
    assert result.status_code == 201, result.text
    saved = result.json()
    cfg = saved['config']
    assert cfg == {'feeds': [{'url': URL, 'max_age_days': None, 'max_episodes': None}],
        'schedule_mode': mode, 'refresh_interval_minutes': 30, 'replay_cooldown_hours': 24}
    sid = saved['id']
    assert api.get(P+f'/{sid}').json() == saved
    assert api.get(P).json() == [saved]
    updated = dict(feeds=[dict(url=URL, max_age_days=14, max_episodes=5),
                          dict(url='http://localhost/another', max_age_days=None, max_episodes=0)],
                   schedule_mode='RANDOM', refresh_interval_minutes=60, replay_cooldown_hours=0)
    assert api.patch(P+f'/{sid}', json={'config': updated}).status_code == 200
    assert api.get(P+f'/{sid}').json()['config'] == updated
    assert api.delete(P+f'/{sid}').status_code == 204


@pytest.mark.parametrize('config', [
    {}, {'feeds': []}, {'feeds': [{'url': 'file:///tmp/feed'}]},
    {'feeds': [{'url': 'ftp://example.org/feed'}]}, {'feeds': [{'url': 'relative'}]},
    {'feeds': [{'url': 'https://user:secret@example.org/feed'}]},
    {'feeds': [{'url': 'https://example.org:bad/feed'}]},
    {'feeds': [{'url': URL, 'max_age_days': -1}]}, {'feeds': [{'url': URL, 'max_episodes': -1}]},
    {'feeds': [{'url': URL, 'max_episodes': True}]},
    {'feeds': [{'url': URL}, {'url': URL}]},
    {'feeds': [{'url': URL}], 'schedule_mode': 'sequential'},
    {'feeds': [{'url': URL}], 'refresh_interval_minutes': 0},
    {'feeds': [{'url': URL}], 'replay_cooldown_hours': -1},
    {'feeds': [{'url': URL}], 'current_episode': 'one'},
])
def test_invalid_configuration(api, config):
    saved = api.post(P, json=body()).json()
    assert api.post(P, json=dict(body(), config=config)).status_code == 422
    assert api.patch(P+f"/{saved['id']}", json={'config': config}).status_code == 422
    assert api.get(P+f"/{saved['id']}").json() == saved


def test_restart_preserves_only_config(tmp_path):
    settings = Settings(tmp_path/'reload.db')
    with TestClient(create_app(settings)) as api:
        saved = api.post(P, json=body()).json()
    with TestClient(create_app(settings)) as api:
        assert api.get(P).json() == [saved]
        assert not api.app.state.services.sources.pipelines


@pytest.mark.parametrize('failure', ['none', 'partial', 'all', 'empty'])
@pytest.mark.parametrize('saved', [False, True])
def test_probe_counts_no_playback_no_history(api, monkeypatch, failure, saved):
    other = 'https://example.net/feed'
    cfg = body(feeds=[dict(url=URL), dict(url=other)])
    sid = api.post(P, json=cfg).json()['id']
    def fetch(self, url):
        if failure == 'all' or (failure == 'partial' and url == other):
            raise DomainError(503, 'PODCAST_FETCH_TIMEOUT', 'url?secret-token')
        return FeedResponse(RSS if failure != 'empty' else b'<rss version="2.0"><channel/></rss>', url)
    monkeypatch.setattr(FeedFetcher, '__call__', fetch)
    result = api.post(P+f'/{sid}/test') if saved else api.post(P+'/test', json=cfg)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['ok'] == (failure in ('none', 'partial'))
    assert data['mocked'] is False and data['codec'] is None
    expected = {'none': 2, 'partial': 1, 'all': 0, 'empty': 0}[failure]
    assert data['podcast']['feeds_attempted'] == 2
    assert data['podcast']['eligible_episodes'] == expected
    assert data['podcast']['discovered_episodes'] == expected
    assert data['podcast']['feeds_succeeded'] == (2 if failure == 'empty' else expected)
    assert 'secret-token' not in result.text
    assert not api.app.state.services.sources.pipelines


def test_probe_history_is_unchanged_and_playback_explicitly_unavailable(tmp_path, monkeypatch):
    db = Database(tmp_path/'history.db')
    source = db.save('sources', InputSourceConfig.model_validate(body()))
    monkeypatch.setattr(FeedFetcher, '__call__', lambda self, url: FeedResponse(RSS, url))
    manager = SourceManager(db)
    assert manager.test(source).ok
    assert not db.connection.execute('SELECT * FROM podcast_history').fetchall()
    with pytest.raises(DependencyUnavailable, match='PODCAST_PLAYBACK_NOT_IMPLEMENTED'):
        manager.acquire(source.id, 0)
    assert not manager.pipelines
    db.close()
