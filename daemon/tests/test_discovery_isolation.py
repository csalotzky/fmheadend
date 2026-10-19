"""Blocked read-only I/O must not own control or PCM delivery resources."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from fmheadendd.app import create_app
from fmheadendd.control import DiscoveryExecutor
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.podcast_fetch import FeedFetcher, FeedResponse
from fmheadendd.tests.profile_support import profile_settings


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.001)


class PcmSink:
    """Mock transport; exercise the real mux writer without Unix/hardware I/O."""
    def __init__(self):
        self.data = bytearray()

    def send(self, data):
        self.data.extend(data)
        return len(data)

    def close(self):
        pass


@pytest.mark.parametrize('operation', ['podcast', 'filesystem'])
def test_blocked_discovery_preserves_maintenance_and_pcm(tmp_path, monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    app = create_app(profile_settings(tmp_path / 'config.db'))

    def blocked(*args):
        entered.set()
        assert release.wait(5), 'test did not release discovery'
        if operation == 'podcast':
            return FeedResponse(b'<rss><channel/></rss>', 'https://example.org/feed')
        return {'entries': []}

    async def run():
        async with app.router.lifespan_context(app):
            ticks = []
            original = app.state.services.rds.tick

            def tick():
                original()
                ticks.append(True)

            monkeypatch.setattr(app.state.services.rds, 'tick', tick)
            monkeypatch.setattr(FeedFetcher, '__call__', blocked)
            monkeypatch.setattr(app.state.services.filesystem, 'browse', blocked)
            # Actual media drain loop and mux writer; only native decoding and
            # the socket are replaced. Inject synthetic stereo PCM at appsink's
            # staging boundary, with no live GStreamer pipeline or network.
            pipeline = GstPipeline(InputSourceConfig(name='active', type='HTTP',
                config={'uri': 'http://localhost/live'}))
            pipeline.Gst = SimpleNamespace(State=SimpleNamespace(PLAYING=1),
                StateChangeReturn=SimpleNamespace(FAILURE=0))
            pipeline._pipeline = SimpleNamespace(set_state=lambda state: 1)
            pipeline._convert = SimpleNamespace(get_static_pad=lambda name:
                SimpleNamespace(get_current_caps=lambda: None))
            pipeline._build = lambda: None
            pipeline._bus = SimpleNamespace(timed_pop=lambda timeout: None)
            pipeline.output.attach(0)
            sink = PcmSink()
            mux = UnixMuxClient(lambda source_id: pipeline.output)
            monkeypatch.setattr(mux, '_connect', lambda channel: sink)
            mux.attach(0, 1)
            failures = []

            def stream():
                try:
                    pipeline._attempt()
                except BaseException as exc:
                    failures.append(exc)

            worker = threading.Thread(target=stream)
            worker.start()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
                if operation == 'podcast':
                    job = asyncio.create_task(api.post('/api/v1/sources/test', json=dict(
                        name='probe', type='PODCAST', config={'feeds': [{'url': 'https://example.org/feed'}]})))
                else:
                    job = asyncio.create_task(api.get('/api/v1/filesystem/browse', params={'path': '/mnt', 'mode': 'folder'}))
                try:
                    await until(entered.is_set)
                    baseline = len(ticks)
                    expected = bytearray()
                    while len(ticks) < baseline + 2 or len(expected) < 512 * 16:
                        block = (len(expected) // 512).to_bytes(4, 'little') * 128
                        expected.extend(block)
                        assert pipeline.staging.put(block)
                        await until(lambda: len(sink.data) == len(expected))
                    assert not job.done()
                    assert bytes(sink.data) == bytes(expected)
                    assert pipeline.staging.snapshot()['dropped_blocks'] == 0
                    output = pipeline.output.snapshot()[0]
                    assert output['accepted_bytes'] == len(expected)
                    assert output['buffered_bytes'] == output['dropped_blocks'] == 0
                    assert mux.pcm_snapshot()[0] == dict(disconnects=0, stale_disconnects=0, discarded_pending_bytes=0)
                    assert await app.state.control.call(lambda: 'control progresses') == 'control progresses'
                    busy = await api.get('/api/v1/filesystem/locations')
                    assert busy.status_code == 503 and busy.json()['error'] == 'DISCOVERY_BUSY'
                finally:
                    release.set()
                    pipeline._stop.set()
                    pipeline._audio_ready.set()
                    worker.join(2)
                    mux.close()
                response = await job
                assert response.status_code == 200, response.text
                assert not worker.is_alive() and not failures

    asyncio.run(run())


def test_saved_probe_snapshot_survives_edit_and_delete(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def fetch(self, url):
        seen.append(url)
        entered.set()
        assert release.wait(5)
        return FeedResponse(b'<rss><channel/></rss>', url)

    monkeypatch.setattr(FeedFetcher, '__call__', fetch)
    app = create_app(profile_settings(tmp_path / 'config.db'))

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
                created = await api.post('/api/v1/sources', json=dict(name='old', type='PODCAST',
                    config={'feeds': [{'url': 'https://example.org/old'}]}))
                assert created.status_code == 201
                url = f"/api/v1/sources/{created.json()['id']}"
                job = asyncio.create_task(api.post(url + '/test'))
                try:
                    await until(entered.is_set)
                    assert (await api.patch(url, json={'name': 'new',
                        'config': {'feeds': [{'url': 'https://example.org/new'}]}})).status_code == 200
                    assert (await api.delete(url)).status_code == 204
                finally:
                    release.set()
                assert (await job).status_code == 200
                assert (await api.get(url)).status_code == 404
                assert seen == ['https://example.org/old']
                assert await app.state.control.call(lambda: app.state.services.database.list('sources')) == []
                assert not app.state.services.sources.pipelines

    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_timeout_or_cancellation_retains_slot_and_shutdown_drains(cancel, caplog):
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(5)
        raise RuntimeError('late probe failure')

    async def run():
        executor = DiscoveryExecutor(timeout=.02)
        job = asyncio.create_task(executor.call(blocked))
        await until(entered.is_set)
        try:
            if cancel:
                job.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await job
            else:
                with pytest.raises(DomainError) as error:
                    await job
                assert error.value.error == 'DISCOVERY_TIMEOUT'
            with pytest.raises(DomainError) as error:
                await executor.call(lambda: None)
            assert error.value.error == 'DISCOVERY_BUSY'
            closing = asyncio.create_task(executor.close())
            await until(lambda: executor.closing)
            assert not closing.done()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='late probe failure'):
            await closing
        assert executor.pending is None
        assert executor.executor._shutdown

    asyncio.run(run())
    assert 'late probe failure' in caplog.text


def test_shutdown_cleans_live_services_before_draining_discovery(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    app = create_app(profile_settings(tmp_path / 'config.db'))

    def blocked():
        entered.set()
        assert release.wait(5)

    async def run():
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        cleaned = []
        original = app.state.services.sources.close

        def close():
            original()
            cleaned.append(True)

        monkeypatch.setattr(app.state.services.sources, 'close', close)
        job = asyncio.create_task(app.state.discovery.call(blocked))
        await until(entered.is_set)
        shutdown = asyncio.create_task(context.__aexit__(None, None, None))
        try:
            await until(lambda: cleaned)
            assert app.state.discovery.closing
            assert not shutdown.done()
            with pytest.raises(DomainError):
                await app.state.discovery.call(lambda: None)
        finally:
            release.set()
        await job
        await shutdown
        assert app.state.discovery.executor._shutdown

    asyncio.run(run())
