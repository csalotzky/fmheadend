"""Synthetic RSS/HTTP -> the shared native player, never mux/RP/RF."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import struct
import time
import wave

import pytest

from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import load_gst
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.podcast_history import PodcastHistory
from fmheadendd.sources.podcast_runtime import PodcastRuntime
from fmheadendd.errors import DependencyUnavailable
from test_local_playback_native import TracedPipeline

pytestmark=pytest.mark.gstreamer


@pytest.fixture(autouse=True)
def gst():
    try: return load_gst()
    except DependencyUnavailable as exc: pytest.skip(str(exc))


def wav(path, seconds=1):
    block=b''.join(struct.pack('<hh',1000 if n%48<24 else -1000,1000 if n%48<24 else -1000)
                   for n in range(48000))
    with wave.open(str(path),'wb') as f:
        f.setparams((2,2,48000,0,'NONE',''))
        for _ in range(seconds): f.writeframes(block)
    return path.read_bytes()


@contextmanager
def server(media):
    state={'names': list(media), 'fail_feed':False, 'bad':set(), 'requests':[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            state['requests'].append(self.path)
            if self.path=='/rss':
                if state['fail_feed']:
                    self.send_error(503); return
                body=('<rss version="2.0"><channel><title>Example Podcast</title>'+''.join(
                    f'<item><guid isPermaLink="false">{n}</guid><title>Episode {n}</title>'
                    f'<enclosure type="audio/mpeg" url="http://127.0.0.1:{self.server.server_port}/media?id={n}"/>'
                    '</item>' for n in state['names'])+'</channel></rss>').encode()
            else:
                key=self.path.split('id=')[-1]
                if key in state['bad'] or key not in media:
                    self.send_error(404); return
                body=media[key]
            self.send_response(200); self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=http.serve_forever);thread.start()
    try: yield f'http://127.0.0.1:{http.server_port}/rss',state
    finally: http.shutdown();http.server_close();thread.join()


def source(db,url,mode='RANDOM'):
    return db.save('sources',InputSourceConfig(name='Native Podcast',type='PODCAST',config={
        'feeds':[{'url':url}],'schedule_mode':mode,'refresh_interval_minutes':1}))


def wait_for(predicate, seconds=8):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.005)
    assert predicate()


@pytest.mark.parametrize('fallback',[False,True])
def test_native_http_transitions_rss_identity_and_shared_cleanup(tmp_path,gst,fallback):
    if not all(gst.ElementFactory.find(n) for n in ('flacenc','taginject')):
        pytest.skip('Synthetic tag fixtures need flacenc/taginject')
    path=tmp_path/'tagged.flac'
    generator=gst.parse_launch('audiotestsrc num-buffers=100 samplesperbuffer=480 ! audio/x-raw,rate=48000,channels=2 ! taginject tags="title=WrongTitle,artist=WrongArtist" ! flacenc ! filesink name=out')
    generator.get_by_name('out').set_property('location',str(path))
    try:
        generator.set_state(gst.State.PLAYING)
        msg=generator.get_bus().timed_pop_filtered(5*gst.SECOND,gst.MessageType.EOS|gst.MessageType.ERROR)
        assert msg and msg.type==gst.MessageType.EOS
    finally: generator.set_state(gst.State.NULL)
    with server({'12':path.read_bytes(),'13':wav(tmp_path/'b.wav')}) as (url,state):
        db=Database(tmp_path/'native.db')
        def factory(s,**kwargs):
            p=TracedPipeline(s,timeout_s=3,**kwargs);p.disable_handoff=fallback;return p
        factory.mocked=False
        manager=SourceManager(db,pipeline_factory=factory)
        s=source(db,url)
        try:
            manager.acquire(s.id,0); p=manager.pipelines[s.id]
            manager.acquire(s.id,1)
            assert manager.pipelines[s.id] is p
            wait_for(lambda:len(p.metadata_records)>=3)
            runtime=p._local
            print('Podcast transitions',p.metadata_records,'handoffs',p.handoff_metadata)
            for uri,title,artist,text in p.metadata_records:
                assert uri.startswith(url.replace('/rss','/media?id='))
                n=uri.split('id=')[-1]
                assert (title,artist,text)==(f'Episode {n}','Example Podcast',f'Example Podcast - Episode {n}')
            assert {'Episode 12','Episode 13'} <= {r[1] for r in p.metadata_records}
            assert p.builds==1 and p.snapshot()['output_caps']
            if fallback: assert p.fallbacks>=2
            else: assert p.handoffs>=2 and any(a==b and a for a,b in p.handoff_metadata)
            assert all(h.play_count==0 for h in PodcastHistory(db,s.id).snapshot().values())
            manager.release(s.id,0)
            assert p._thread.is_alive() and runtime._io_thread.is_alive()
            manager.release(s.id,1)
            assert not p._thread.is_alive() and not runtime._io_thread.is_alive()
            assert p.snapshot()['stream_title'] is None and runtime.active is None
            manager.acquire(s.id,0)
            assert manager.pipelines[s.id] is not p
            assert manager.pipelines[s.id]._local.scheduler is not runtime.scheduler
        finally: manager.close();db.close()


def test_native_real_decoded_pcm_threshold_accelerated(tmp_path):
    with server({'long':wav(tmp_path/'long.wav',61)}) as (url,state):
        db=Database(tmp_path/'threshold.db');s=source(db,url)
        class Fast(TracedPipeline):
            def _build(self):
                super()._build(); self.sink.set_property('sync',False)
        p=Fast(s,podcast_database_path=str(tmp_path/'threshold.db'),timeout_s=3)
        p.disable_handoff=True
        # Keep one actual occurrence: do not advance after its EOS.
        original=p._local_resume
        def resume(**kw):
            if p._local.active and p._local.active.marked:
                p._stop.set(); return
            original(**kw)
        p._local_resume=resume
        try:
            p.start()
            wait_for(lambda:sum(h.play_count for h in PodcastHistory(db,s.id).snapshot().values())==1)
            assert sum(h.play_count for h in PodcastHistory(db,s.id).snapshot().values())==1
            assert p.callback_times
        finally: p.stop();db.close()


def test_native_initial_failure_exhaustion_and_refresh_recovery(tmp_path,gst):
    from gi.repository import GLib
    with server({'A':wav(tmp_path/'a.wav',2)}) as (url,state):
        state['fail_feed']=True
        db=Database(tmp_path/'recovery.db');s=source(db,url,'SMART')
        tick=[0.0]
        def runtime_factory(*a,**kw): return PodcastRuntime(*a,clock=lambda:tick[0],**kw)
        p=TracedPipeline(s,podcast_database_path=str(tmp_path/'recovery.db'),
                         podcast_runtime_factory=runtime_factory,timeout_s=2)
        p.disable_handoff=True
        try:
            p.start(); r=p._local
            wait_for(lambda:r.catalog_view.refreshed_at is not None)
            assert p._thread.is_alive() and r._io_thread.is_alive()
            assert p.snapshot()['stream_title'] is None
            state['fail_feed']=False;state['bad'].add('A')
            tick[0]=61;r._io_ready.set()
            wait_for(lambda: r.error=='PLAYBACK_EXHAUSTED')
            failed_requests=state['requests'].count('/media?id=A')
            assert failed_requests<=2 and not p.fatal_error
            state['bad'].clear();tick[0]=122;r._io_ready.set()
            wait_for(lambda:p.snapshot()['stream_title']=='Example Podcast - Episode A')
            assert p._local is r and p.builds==1
            # A real bus error after some PCM is still a failed sub-threshold occurrence.
            p._bus.post(gst.Message.new_error(p.sink, GLib.Error('test failure'), 'synthetic failure'))
            wait_for(lambda:r.error=='PLAYBACK_EXHAUSTED')
            assert p.snapshot()['stream_title'] is None
            assert all(h.play_count==0 for h in PodcastHistory(db,s.id).snapshot().values())
            assert p._thread.is_alive() and r._io_thread.is_alive()
        finally:
            p.stop();db.close()
        assert not r._io_thread.is_alive()
