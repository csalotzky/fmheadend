"""Linux integration gate for Phase 4C; local RSS/media, genuine audiod child."""
import os
import sys
import signal
import subprocess
import time
from threading import Event

import pytest

from fmheadendd.database import Database
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.tests.test_gstreamer import gst, require, file_profile, http_files
from fmheadendd.tests.test_audio_worker import until, finish
from fmheadendd.tests.test_local_worker_native import start
from fmheadendd.tests.test_podcast_playback_native import server, wav, source


@pytest.mark.parametrize('mode', ['SMART', 'RANDOM'])
def test_native_mp3_multifeed_transitions(gst, tmp_path, mode):
    from fmheadendd.tests.test_local_worker_native import RecordingWorker, titles
    require(gst, 'playbin3', 'lamemp3enc', 'fmheadendlimiter', 'fmheadendclipper')
    input_file = tmp_path/'input.wav'
    wav(input_file, 2)
    output_file = tmp_path/'episode.mp3'
    subprocess.run(['gst-launch-1.0', '-q', 'filesrc', f'location={input_file}', '!',
        'wavparse', '!', 'audioconvert', '!', 'lamemp3enc', '!', 'filesink',
        f'location={output_file}'], check=True, timeout=30)
    media = output_file.read_bytes()
    with server({'A': media}) as (url_a, _), server({'B': media}) as (url_b, _):
        db = Database(tmp_path/'multifeed.db')
        s = source(db, url_a, mode)
        s = s.model_copy(update={'config': dict(s.config, feeds=[{'url':url_a}, {'url':url_b}]), 'input_gain_db': -6})
        profile = file_profile('podcast')
        raw = profile.model_dump(mode='json')
        raw['stages']['limiter']['selected'] = 'FM_HEADEND_LIMITER'
        raw['stages']['clipper']['selected'] = 'FM_HEADEND_CLIPPER'
        from fmheadendd.models.audio_profile_file import AudioProfileFile
        p = RecordingWorker(s, profile=AudioProfileFile.model_validate(raw), podcast_database_path=str(tmp_path/'multifeed.db'))
        p.output.attach(0)
        try:
            start(p)
            until(lambda: len(titles(p)) >= 4, timeout=25)
            assert set(titles(p)) == {'Episode A', 'Episode B'}
            records = list(p.records)
            assert all(title in ('Episode A', 'Episode B') and len(data)%4 == 0 for title, _, data in records)
            assert p.snapshot()['output_caps'].startswith('audio/x-raw')
            gaps = [(b[1]-a[1])/1e9 for a,b in zip(records,records[1:]) if a[0]!=b[0]]
            print('MP3', mode, 'transitions', len(gaps), 'max PCM boundary interval', max(gaps))
            assert max(gaps) < .5
        finally:
            finish(p); db.close()


@pytest.mark.parametrize('fault', ['crash', 'native-stall'])
def test_native_podcast_recovery(gst, tmp_path, fault, http_files):
    from fmheadendd.errors import SourcePending
    from fmheadendd.models.configuration import InputSourceConfig
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.sources.worker_recovery import RecoveryPolicy
    from fmheadendd.tests.profile_support import profile_store
    from fmheadendd.tests.test_local_worker_native import RecordingWorker
    require(gst, 'playbin3', 'wavparse')
    folder, http_url = http_files
    wav(folder/'sibling.wav', 60)
    with server({'A': wav(tmp_path/'a.wav', 3), 'B': wav(tmp_path/'b.wav', 3)}) as (url, state):
        db = Database(tmp_path/'recover.db')
        manager = SourceManager(db, pipeline_factory=RecordingWorker, profile_files=profile_store(tmp_path))
        manager.worker_recovery.policy = RecoveryPolicy(pcm_timeout=1, buffering_timeout=3)
        podcast = source(db, url)
        sibling_source = db.save('sources', InputSourceConfig(name='local sibling', type='LOCAL',
            config={'mode': 'folder', 'path': str(tmp_path)}))
        http_source = db.save('sources', InputSourceConfig(name='http sibling', type='HTTP',
            config={'uri': http_url+'/sibling.wav'}))
        owners = []
        def step(condition):
            def poll():
                manager.worker_recovery.service()
                return condition()
            until(poll, timeout=20)
        try:
            for sid, tx in ((podcast.id, 0), (podcast.id, 1), (sibling_source.id, 2), (http_source.id, 3)):
                try: manager.acquire(sid, tx)
                except SourcePending: pass
            old, sibling = manager.pipelines[podcast.id], manager.pipelines[sibling_source.id]
            owners.extend((old, sibling))
            http = manager.pipelines[http_source.id]; owners.append(http)
            until(lambda: old.output.read(0) and sibling.output.read(2), timeout=20)
            service, thread = old._podcast_service, old._podcast_service._io_thread
            before = old.recovery_observation()[0]
            os.kill(old.process.pid, signal.SIGKILL if fault == 'crash' else signal.SIGUSR1)
            if fault == 'native-stall':
                until(lambda: old.recovery_observation()[0] > before)
            progress = sibling.recovery_observation()[1]
            http_progress = http.recovery_observation()[1]
            step(lambda: manager.pipelines[podcast.id] is not old)
            new = manager.pipelines[podcast.id]; owners.append(new)
            until(lambda: new.output.read(0) and new.output.read(1), timeout=20)
            assert new.output is old.output and new.generation != old.generation
            assert new._podcast_service is service and service._io_thread is thread
            assert manager.consumers[podcast.id] == {0, 1}
            assert sibling.recovery_observation()[1] > progress
            assert http.process.poll() is None and http.recovery_observation()[1] > http_progress
            with pytest.raises(ChildProcessError): os.waitpid(old.process.pid, os.WNOHANG)
            changed = podcast.model_copy(update={'input_gain_db': -6})
            manager._restart_audio(changed)
            step(lambda: manager.pipelines[podcast.id] is not new)
            latest = manager.pipelines[podcast.id]; owners.append(latest)
            until(lambda: latest.output.read(0), timeout=20)
            assert latest._podcast_service is service
            for sid, tx in ((podcast.id, 0), (podcast.id, 1), (sibling_source.id, 2), (http_source.id, 3)):
                manager.release(sid, tx)
            step(lambda: not manager.pipelines)
            assert not thread.is_alive()
        finally:
            owners.extend(manager.pipelines.values())
            manager.close()
            for p in set(owners): finish(p)
            db.close()


def test_native_podcast_real_threshold(gst, tmp_path):
    from fmheadendd.sources.podcast_history import PodcastHistory
    require(gst, 'playbin3', 'wavparse')
    with server({'A': wav(tmp_path/'long.wav', 72)}) as (url, state):
        db = Database(tmp_path/'history.db')
        s = source(db, url)
        p = ProcessPipeline(s, profile=file_profile('podcast'), podcast_database_path=str(tmp_path/'history.db'))
        p.output.attach(0)
        try:
            start(p)
            history = PodcastHistory(db, s.id)
            until(lambda: any(h.play_count == 1 for h in history.snapshot().values()), timeout=85)
            assert sum(h.play_count for h in history.snapshot().values()) == 1
            assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 1
            p._podcast_service.request_snapshot()
            until(lambda: any(h.play_count == 1 for h in p._podcast_service.snapshot().history.values()))
            assert sum(h.play_count for h in history.snapshot().values()) == 1
            assert p.process.poll() is None and p.output.read(0)
        finally:
            finish(p)
            db.close()

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Native Linux PODCAST worker')


def test_native_empty_wait_refresh_and_source_apply(gst, tmp_path):
    from fmheadendd.errors import SourcePending
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.sources.worker_recovery import RecoveryPolicy
    from fmheadendd.tests.profile_support import profile_store
    require(gst, 'playbin3', 'wavparse')
    advance_feed_clock = Event()

    class ClockedPipeline(ProcessPipeline):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            service = self._podcast_service
            interval = service.config.refresh_interval_minutes * 60
            service.clock = lambda: time.monotonic() + (interval if advance_feed_clock.is_set() else 0)

    with server({'A': wav(tmp_path/'a.wav', 3)}) as (url, state):
        state['names'] = []
        db = Database(tmp_path/'empty.db')
        m = SourceManager(db, pipeline_factory=ClockedPipeline, profile_files=profile_store(tmp_path))
        m.worker_recovery.policy = RecoveryPolicy(pcm_timeout=.5, buffering_timeout=2)
        s = source(db, url)
        owners = []
        try:
            try: m.acquire(s.id, 0)
            except SourcePending: pass
            p = m.pipelines[s.id]; owners.append(p)
            until(lambda: p.expected_media_wait, timeout=15)
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                m.worker_recovery.service()
                assert m.pipelines[s.id] is p and not m.worker_recovery.pending
                time.sleep(.05)
            service = p._podcast_service
            until(lambda: service._next_refresh > time.monotonic())
            state['names'] = ['A']
            advance_feed_clock.set()
            service.request_snapshot()
            until(lambda: p.output.read(0), timeout=15)
            assert m.pipelines[s.id] is p
            changed = s.model_copy(update={'config': dict(s.config, schedule_mode='SMART')})
            m._restart_audio(changed)
            def applied():
                m.worker_recovery.service()
                return m.pipelines[s.id] is not p
            until(applied, timeout=15)
            new = m.pipelines[s.id]; owners.append(new)
            until(lambda: new.output.read(0), timeout=15)
            assert new._podcast_service is not service and not service._io_thread.is_alive()
        finally:
            m.close()
            for p in owners: finish(p)
            db.close()


@pytest.mark.parametrize('mode', ['SMART', 'RANDOM'])
def test_native_podcast_child_pcm_and_reap(gst, tmp_path, mode):
    require(gst, 'playbin3', 'wavparse')
    with server({'A': wav(tmp_path/'a.wav', 2), 'B': wav(tmp_path/'b.wav', 2)}) as (url, state):
        db = Database(tmp_path/'podcast.db')
        s = source(db, url, mode)
        p = ProcessPipeline(s, profile=file_profile('podcast'),
            podcast_database_path=str(tmp_path/'podcast.db'))
        p.output.attach(0)
        try:
            start(p)
            until(lambda: p.output.read(0) is not None, timeout=20)
            assert p.process.pid != os.getpid()
            assert p.snapshot()['title'] in ('Episode A', 'Episode B')
            assert p.snapshot()['artist'] == 'Example Podcast'
            assert p._podcast_service._io_thread.is_alive()
            assert state['requests'].count('/rss') == 1
        finally:
            finish(p)
            db.close()
        assert not p._podcast_service._io_thread.is_alive()
        with pytest.raises(ChildProcessError):
            os.waitpid(p.process.pid, os.WNOHANG)
