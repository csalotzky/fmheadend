"""Source policy/callback lifecycle with injected I/O and time; no real waits."""
from datetime import datetime, timezone, timedelta
from threading import Event, Thread, Barrier
from types import SimpleNamespace
from unittest.mock import Mock
import random

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import InputSourceConfig, PodcastConfig, RtOverride
from fmheadendd.sources.podcast_runtime import PodcastRuntime, PLAYED_BYTES
from fmheadendd.sources.podcast_fetch import FeedResponse
from fmheadendd.sources.podcast_history import PodcastHistory
from fmheadendd.sources.gst_pipeline import GstPipeline
from fmheadendd.sources.manager import SourceManager
from fmheadendd.rds.radiotext import RadioTextService

URL = 'https://feed.example.org/rss'
NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def rss(names=('A', 'B'), show='Example Podcast'):
    return ('<rss version="2.0"><channel><title>'+show+'</title>'+''.join(
        f'<item><guid isPermaLink="false">{n}</guid><title>Episode {n}</title>'
        f'<enclosure type="audio/mpeg" url="https://cdn.example.org/media?id={n}&amp;token=abc"/>'
        '</item>' for n in names)+'</channel></rss>').encode()


def runtime(tmp_path, *, names=('A','B'), mode='RANDOM', show='Example Podcast'):
    db = Database(tmp_path/'source.db')
    source = db.save('sources', InputSourceConfig(name='Podcast', type='PODCAST', config={
        'feeds': [{'url': URL}], 'schedule_mode': mode, 'refresh_interval_minutes': 1}))
    history = PodcastHistory(db, source.id)
    seconds = [0]
    response = [rss(names, show)]
    calls = []
    def fetch(url):
        calls.append(url)
        if isinstance(response[0], Exception):
            raise response[0]
        return FeedResponse(response[0], url)
    p = PodcastRuntime(PodcastConfig.model_validate(source.config), Event(),
        history_factory=lambda: history, fetch_factory=lambda: fetch,
        clock=lambda: seconds[0], now=lambda: NOW+timedelta(seconds=seconds[0]), rng=random.Random(3))
    return SimpleNamespace(p=p, db=db, source=source, history=history, seconds=seconds,
                           response=response, calls=calls)


@pytest.fixture
def rt(tmp_path):
    r = runtime(tmp_path)
    yield r
    r.p.close()
    r.db.close()


def begin(r):
    r.p.io_step(r.history)
    uri = r.p.initial()
    r.p.stream_start()
    return uri


def test_selection_uses_media_item_and_prepared_is_not_active(rt):
    uri = begin(rt)
    assert uri == rt.p.rendering.context.media_item().locator
    assert '?id=' in uri and '&token=abc' in uri and '.mp3' not in uri
    assert rt.p.active is None
    rt.p.pcm(4)
    first = rt.p.active
    player = Mock()
    rt.p.handoff(player)
    assert rt.p.active is first
    rt.p.stream_start()
    assert rt.p.active is first  # stream/preroll alone is not success
    rt.p.pcm(4)
    assert rt.p.active is rt.p.rendering and rt.p.active is not first
    assert rt.p.active.pcm_bytes == 4


@pytest.mark.parametrize('seconds,expected', [(0,0),(59,0),(60,1),(120,1),(181,1)])
def test_actual_pcm_threshold_once(rt, seconds, expected):
    begin(rt)
    episode = rt.p.rendering.context
    rt.seconds[0] = 100000  # wall time alone is irrelevant
    rt.p.pcm(seconds * 192000)
    rt.p._flush_marks(rt.history)
    assert rt.history.snapshot()[episode.key].play_count == expected
    assert PLAYED_BYTES == 11520000


def test_arbitrary_frame_aligned_boundaries_and_new_occurrence(rt):
    begin(rt)
    e = rt.p.rendering.context
    for size in (4, 8192, PLAYED_BYTES-8200):
        rt.p.pcm(size)
    rt.p._flush_marks(rt.history)
    assert rt.history.snapshot()[e.key].play_count == 0
    rt.p.pcm(4)
    rt.p.pcm(PLAYED_BYTES)
    rt.p._flush_marks(rt.history)
    assert rt.history.snapshot()[e.key].play_count == 1
    # Independent future occurrence of the same episode gets a fresh ticket.
    rt.p.prepared = None
    rt.p.scheduler = Mock()
    rt.p.scheduler.choose_next.return_value = e
    rt.p.resume(failed=False); rt.p.stream_start(); rt.p.pcm(PLAYED_BYTES)
    rt.p._flush_marks(rt.history)
    assert rt.history.snapshot()[e.key].play_count == 2


@pytest.mark.parametrize('size', [0, 10*192000, PLAYED_BYTES])
def test_failure_suppresses_zero_partial_and_post_threshold(rt, size):
    begin(rt)
    e = rt.p.rendering.context
    rt.p.pcm(size)
    uri = rt.p.resume(failed=True)
    assert e.key in rt.p.failed and uri != e.media_url
    rt.p._flush_marks(rt.history)
    assert rt.history.snapshot()[e.key].play_count == int(size >= PLAYED_BYTES)


@pytest.mark.parametrize('failed', [False, True])
def test_short_eos_or_failure_never_counts_as_played(rt, failed):
    begin(rt)
    rt.p.pcm(2*192000)
    rt.p.resume(failed=failed)
    rt.p._flush_marks(rt.history)
    assert all(h.play_count == 0 for h in rt.history.snapshot().values())


def test_short_stop_no_played_and_stale_callbacks_inert(rt):
    begin(rt); rt.p.pcm(59*192000)
    rt.p.close()
    rt.p.pcm(PLAYED_BYTES)
    assert rt.p.stream_start() is None
    player = Mock(); rt.p.handoff(player)
    player.set_property.assert_not_called()
    assert rt.p.active is None and rt.p.prepared is None and not rt.p.failed
    assert not rt.p.submitted and rt.p.rendering is None
    assert all(h.play_count == 0 for h in rt.history.snapshot().values())


@pytest.mark.parametrize('mode', ['SMART','RANDOM'])
def test_exhausted_waits_for_scheduled_successful_refresh(tmp_path, mode):
    r = runtime(tmp_path, mode=mode)
    try:
        begin(r)
        attempted = []
        for _ in range(2):
            attempted.append(r.p.rendering.context.key if r.p.rendering else r.p.submitted[0].context.key)
            r.p.resume(failed=True)
        assert len(set(attempted)) == 2
        assert r.p.error == 'PLAYBACK_EXHAUSTED'
        for _ in range(10):
            assert r.p.resume(failed=False) is None
            r.p.io_step(r.history)
        assert len(r.calls) == 1 and not r.p.closed
        r.response[0] = DomainError(503, 'PODCAST_FETCH_FAILED', 'temporary')
        r.seconds[0] = 60; r.p.io_step(r.history)
        assert r.p.resume(failed=False) is None  # failed RSS does not clear failures
        assert len(r.p.catalog_view.feeds[0].episodes) == 2
        r.seconds[0] = 120; r.response[0] = rss(('New',)); r.p.io_step(r.history)
        assert r.p.resume(failed=False).endswith('id=New&token=abc')
        assert not r.p.failed and len(r.calls) == 3
    finally:
        r.p.close(); r.db.close()


def test_initial_failure_recovery_without_runtime_replacement(rt):
    rt.response[0] = DomainError(503, 'PODCAST_FETCH_FAILED', 'temporary')
    rt.p.io_step(rt.history)
    assert rt.p.initial() is None
    assert rt.p.error == 'NO_PLAYABLE_EPISODES'
    rt.seconds[0] = 59; rt.response[0] = rss(); rt.p.io_step(rt.history)
    assert len(rt.calls) == 1
    rt.seconds[0] = 60; rt.p.io_step(rt.history)
    assert rt.p.resume(failed=False) is not None


def test_refresh_does_not_interrupt_active_but_invalidates_unsubmitted(rt):
    begin(rt); rt.p.pcm(4)
    active = rt.p.active
    rt.response[0] = rss(('New',)); rt.seconds[0] = 60
    rt.p.io_step(rt.history)
    assert rt.p.active is active
    player=Mock(); rt.p.handoff(player)
    player.set_property.assert_not_called()  # obsolete prepared URI cannot escape
    rt.p.service(player)
    rt.p.handoff(player)
    assert 'id=New' in player.set_property.call_args.args[1]
    assert rt.p.active is active


def test_already_submitted_handoff_survives_refresh(rt):
    begin(rt); rt.p.pcm(4)
    rt.p.handoff(Mock()); submitted = rt.p.submitted[0]
    rt.response[0] = rss(('New',)); rt.seconds[0] = 60; rt.p.io_step(rt.history)
    rt.p.stream_start(); rt.p.pcm(4)
    assert rt.p.active is submitted


def test_pcm_threshold_and_transition_are_serialized(rt):
    begin(rt); rt.p.pcm(PLAYED_BYTES-4)
    old = rt.p.rendering
    gate = Barrier(3)
    def pcm():
        gate.wait(); rt.p.pcm(4)
    threads=[Thread(target=pcm) for _ in range(2)]
    for t in threads: t.start()
    gate.wait()
    for t in threads: t.join(2)
    rt.p._flush_marks(rt.history)
    assert old.marked and rt.history.snapshot()[old.context.key].play_count == 1


def test_refresh_completion_after_stop_cannot_resurrect(rt):
    entered, release = Event(), Event()
    def fetch(url):
        entered.set(); assert release.wait(2)
        return FeedResponse(rss(), url)
    rt.p.fetch_factory = lambda: fetch
    history=Mock(); history.snapshot.return_value={}
    thread=Thread(target=lambda:rt.p.io_step(history)); thread.start()
    assert entered.wait(2)
    rt.p.close(); release.set(); thread.join(2)
    assert not thread.is_alive() and rt.p.closed
    assert not rt.p.catalog_view.feeds and rt.p._candidate() is None


def gst_metadata(r):
    p=GstPipeline(r.source)
    p._local=r.p
    p.Gst=SimpleNamespace(EventType=SimpleNamespace(STREAM_START='start',TAG='tag'),
                          PadProbeReturn=SimpleNamespace(OK='OK'))
    return p


def event(p, kind, **values):
    tags=Mock()
    tags.get_string.side_effect=lambda k:(k in values,values.get(k))
    tags.get_uint.side_effect=lambda k:(k in values,values.get(k,0))
    info=Mock(); info.get_event.return_value=SimpleNamespace(type=kind,parse_tag=lambda:tags)
    p._local_event(None,info)


def test_rss_metadata_first_pcm_technical_tags_and_existing_rds(rt):
    rt.p.io_step(rt.history); rt.p.initial()
    p=gst_metadata(rt)
    event(p,'start')
    expected=rt.p.rendering.context
    event(p,'tag',title='Stale embedded title',artist='Wrong artist',**{'audio-codec':'MP3','bitrate':64000})
    assert p.snapshot()['stream_title'] is None
    rt.p.pcm(4); p._publish_local_metadata()
    assert p.snapshot()['artist']=='Example Podcast'
    assert p.snapshot()['title']==expected.title
    assert p.snapshot()['stream_title']==f'Example Podcast - {expected.title}'
    assert p.snapshot()['codec']=='MP3' and p.snapshot()['bitrate_bps']==64000
    first=p.snapshot()['stream_title']
    rt.p.handoff(Mock())
    assert p.snapshot()['stream_title']==first
    event(p,'tag',title='Late old title'); event(p,'start')
    assert p.snapshot()['stream_title']==first
    rt.p.pcm(4); p._publish_local_metadata()
    assert p.snapshot()['title']==rt.p.active.context.title != expected.title
    sources=SourceManager(rt.db); sources.pipelines[rt.source.id]=p
    for tx in (0,1):
        m=rt.db.get('modulators',tx)
        rt.db.save('modulators',m.model_copy(update=dict(input_source_id=rt.source.id,
            rds_metadata_to_rt=tx==0,rds_rt='Fallback')),tx)
    radio=RadioTextService(rt.db,sources)
    assert radio.effective(0).text==p.snapshot()['stream_title']
    assert radio.effective(1).text=='Fallback'
    radio.set(0,RtOverride(text='Override')); assert radio.effective(0).text=='Override'
    radio.clear(0); assert radio.effective(0).text==p.snapshot()['stream_title']
    p._clear_local_metadata(); assert radio.effective(0).text=='Fallback'


def test_missing_show_and_clean_eos_metadata(tmp_path):
    r=runtime(tmp_path,show='')
    try:
        r.p.io_step(r.history); r.p.initial(); p=gst_metadata(r)
        event(p,'start'); r.p.pcm(4); p._publish_local_metadata()
        assert p.snapshot()['artist'] is None
        assert p.snapshot()['stream_title']==p.snapshot()['title']
        p._pipeline=Mock(); p._bus=Mock(); p.Gst.State=SimpleNamespace(NULL='NULL',PLAYING='PLAYING')
        p._local_resume(failed=False)
        assert p.snapshot()['stream_title'] is None
        event(p,'start'); r.p.pcm(4); p._publish_local_metadata()
        assert p.snapshot()['stream_title']==r.p.active.context.title
    finally:
        r.p.close(); r.db.close()


def test_smart_unsubmitted_choice_reconsiders_legitimate_play_history(tmp_path):
    r=runtime(tmp_path,mode='SMART')
    try:
        begin(r)
        old=r.p.rendering
        assert r.p.prepared.context.key==old.context.key
        r.p.pcm(PLAYED_BYTES)
        r.p.service(Mock())
        assert r.p.prepared.context.key!=old.context.key
        r.p._flush_marks(r.history)
    finally: r.p.close();r.db.close()


def test_stale_source_error_and_queued_history_survive_transition(rt):
    begin(rt)
    old=rt.p.rendering; element=object();rt.p.bind_source(element)
    rt.p.pcm(PLAYED_BYTES)
    rt.p.handoff(Mock()); rt.p.stream_start(); rt.p.pcm(4)
    assert rt.p.stale_error(element)
    assert not rt.p.stale_error(object())  # unassociated errors are not guessed away
    rt.p._flush_marks(rt.history)
    assert rt.history.snapshot()[old.context.key].play_count==1
    assert rt.history.snapshot()[rt.p.active.context.key].play_count==0


def test_coherent_selection_while_refresh_is_blocked(rt):
    begin(rt);rt.p.pcm(4);active=rt.p.active
    entered,release=Event(),Event()
    def fetch(url):
        entered.set();assert release.wait(2)
        return FeedResponse(rss(('New',)),url)
    rt.p.fetch_factory=lambda:fetch;rt.seconds[0]=60
    thread=Thread(target=lambda:rt.p.io_step(Mock()))
    thread.start();assert entered.wait(2)
    try:
        assert rt.p._candidate().context.title in ('Episode A','Episode B')
        assert rt.p.active is active
    finally: release.set();thread.join(2)
    assert rt.p._candidate().context.title=='Episode New'
    assert rt.p.active is active


def test_shared_manager_one_accounting_owner_and_restart_history(rt):
    from fmheadendd.sources.output import PcmOutput
    constructed=[]
    class Pipeline:
        mocked=False
        def __init__(self,source,**kwargs):
            assert kwargs['podcast_database_path']==str(rt.db.connection.execute('PRAGMA database_list').fetchone()[2])
            self.output=kwargs.get('output') or PcmOutput()
            self.policy=PodcastRuntime(PodcastConfig.model_validate(source.config),Event(),
                history_factory=lambda:rt.history,fetch_factory=lambda:lambda u:FeedResponse(rss(),u),now=lambda:NOW)
            self.policy.history_state=rt.history.snapshot()
            constructed.append(self)
        def start(self):
            self.policy.io_step(rt.history);self.policy.initial();self.policy.stream_start()
        def stop(self):
            self.policy._flush_marks(rt.history);self.policy.close()
        def snapshot(self):return dict(state='RUNNING')
    manager=SourceManager(rt.db,pipeline_factory=Pipeline)
    try:
        manager.acquire(rt.source.id,0);manager.acquire(rt.source.id,1)
        assert len(constructed)==1
        p=constructed[0];e=p.policy.rendering.context
        p.policy.pcm(PLAYED_BYTES);p.policy._flush_marks(rt.history)
        assert rt.history.snapshot()[e.key].play_count==1
        manager.release(rt.source.id,0)
        assert not p.policy.closed
        manager.release(rt.source.id,1)
        assert p.policy.closed and not manager.pipelines
        manager.acquire(rt.source.id,0)
        new=constructed[-1]
        assert new is not p and not new.policy.failed
        assert new.policy.history_state[e.key].play_count==1
        assert new.policy.scheduler is not p.policy.scheduler
    finally:manager.close()
