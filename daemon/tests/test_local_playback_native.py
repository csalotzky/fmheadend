"""Isolated synthetic media -> existing appsink/fanout; never mux/RP/TX hardware."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import struct
import threading
import time
import wave

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline, load_gst
from fmheadendd.sources.manager import SourceManager

pytestmark = pytest.mark.gstreamer


@pytest.fixture(autouse=True)
def gst():
    try:
        gst = load_gst()
    except DependencyUnavailable as exc:
        pytest.skip(str(exc))
    for name in ('playbin3', 'wavparse', 'appsink'):
        if not gst.ElementFactory.find(name): pytest.skip('Missing '+name)
    return gst


def tone(path, amplitude, duration=.4):
    # Distinct 1 kHz stereo square tones, generated without external media/tools.
    frames = [struct.pack('<hh', *([amplitude if (i // 24) % 2 else -amplitude] * 2))
              for i in range(int(48000 * duration))]
    with wave.open(str(path), 'wb') as f:
        f.setparams((2, 2, 48000, 0, 'NONE', ''))
        f.writeframes(b''.join(frames))


class TracedPipeline(GstPipeline):
    def __init__(self, *args, **kwargs):
        self.handoffs = self.fallbacks = self.builds = 0
        self.callback_times = []
        self.disable_handoff = False
        super().__init__(*args, **kwargs)
    def _build(self):
        self.builds += 1
        super()._build()
    def _local_handoff(self, player):
        if not self.disable_handoff:
            self.handoffs += 1
            super()._local_handoff(player)
    def _local_resume(self, **kwargs):
        self.fallbacks += 1
        super()._local_resume(**kwargs)
    def _sample(self, sink):
        result = super()._sample(sink)
        self.callback_times.append(time.monotonic())
        return result


def config(path, mode='folder', order='sequential'):
    return InputSourceConfig(name='isolated LOCAL', type='LOCAL',
                             config=dict(mode=mode, path=str(path), order=order))


def collect(p, needed, seconds=8):
    seen = []
    total = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        block = p.output.read(0)
        if block:
            total += len(block)
            amplitude = abs(struct.unpack('<h', block[:2])[0])
            if not seen or seen[-1] != amplitude: seen.append(amplitude)
            if needed(seen): break
        if p.fatal_error: break
        time.sleep(.002)
    print(dict(seen=seen, pcm_bytes=total, handoffs=p.handoffs, fallbacks=p.fallbacks,
               builds=p.builds, max_callback_gap=max((b-a for a,b in zip(p.callback_times,p.callback_times[1:])), default=0),
               error=p.snapshot()['last_error']))
    assert needed(seen), (seen, p.snapshot())
    return seen


@pytest.mark.parametrize('mode,order', [('folder','sequential'), ('folder','random'), ('playlist','sequential')])
def test_native_cycles_handoff_and_stable_output(tmp_path, mode, order):
    for i in range(3): tone(tmp_path / f'{i} space \u00e9 #.wav', 1000*(i+1))
    path = tmp_path
    if mode == 'playlist':
        path = tmp_path / 'list.m3u8'
        path.write_text('\n'.join(p.name for p in sorted(tmp_path.glob('*.wav'))), encoding='utf-8-sig')
    p = TracedPipeline(config(path, mode, order), timeout_s=3)
    p.output.attach(0)
    try:
        p.start(); native, sink, output = p._pipeline, p.sink, p.output
        seen = collect(p, lambda seen: len(seen) >= 4)
        if order == 'sequential': assert seen[:4] == [1000,2000,3000,1000]
        else: assert set(seen[:3]) == {1000,2000,3000}
        assert p.handoffs >= 3 and p.builds == 1
        # Short files can finish prefetch before the worker replenishes the next
        # slot. Allow the required EOS fallback, but prove it did not account
        # for every observed transition (native handoff actually carried PCM).
        assert p.fallbacks < len(seen) - 1
        assert p._pipeline is native and p.sink is sink and p.output is output
        assert p.snapshot()['output_caps'] and p.snapshot()['title'] is None
    finally:
        p.stop()
    assert not p._thread.is_alive() and p._local is None


def test_native_eos_fallback(tmp_path):
    tone(tmp_path/'a.wav',1000); tone(tmp_path/'b.wav',2000)
    p = TracedPipeline(config(tmp_path), timeout_s=3); p.disable_handoff = True
    p.output.attach(0)
    try:
        p.start()
        assert collect(p, lambda s: len(s)>=3)[:3] == [1000,2000,1000]
        assert p.handoffs == 0 and p.fallbacks >= 2 and p.builds == 1
    finally: p.stop()


def test_native_bad_items_and_exhaustion(tmp_path):
    (tmp_path/'a.wav').write_bytes(b'not audio')
    tone(tmp_path/'b.wav',2000)
    playlist = tmp_path/'list.m3u'
    playlist.write_text('missing.wav\na.wav\nb.wav\n')
    p = TracedPipeline(config(playlist,'playlist'), timeout_s=2); p.output.attach(0)
    try:
        p.start(); collect(p, lambda s: 2000 in s)
        assert not p.fatal_error
    finally: p.stop()
    playlist.write_text('missing.wav\na.wav\n')
    p = TracedPipeline(config(playlist,'playlist'), timeout_s=2)
    try:
        try: p.start()
        except DependencyUnavailable: pass
        assert p._finished.wait(5)
        assert p.snapshot()['state']=='ERROR' and p.fatal_error == 'PLAYBACK_EXHAUSTED'
        assert p.fallbacks <= 3
    finally: p.stop()


def test_native_empty(tmp_path):
    p = TracedPipeline(config(tmp_path), timeout_s=2)
    with pytest.raises(DependencyUnavailable, match='NO_PLAYABLE_TRACKS'): p.start()
    p.stop()


def test_native_shared_consumers_restart_and_active_probe(tmp_path):
    tone(tmp_path/'a.wav',1000,1); tone(tmp_path/'b.wav',2000,1)
    db=Database(tmp_path/'source.db'); manager=SourceManager(db,pipeline_factory=TracedPipeline)
    source=db.save('sources',config(tmp_path))
    try:
        manager.acquire(source.id,0); p=manager.pipelines[source.id]
        assert p._ready.wait(3)
        manager.acquire(source.id,1)
        assert manager.pipelines[source.id] is p
        assert manager.test(source).ok and manager.pipelines[source.id] is p
        p.output.clear()
        end=time.monotonic()+2
        left=right=None
        while time.monotonic()<end:
            left=left or p.output.read(0);right=right or p.output.read(1)
            if left and right: break
            time.sleep(.002)
        assert left == right and left
        manager.release(source.id,0)
        assert manager.pipelines[source.id] is p and p._thread.is_alive()
        manager.release(source.id,1)
        assert not p._thread.is_alive() and p._local is None
        tone(tmp_path/'0.wav',3000,1)
        manager.acquire(source.id,0); new=manager.pipelines[source.id]
        assert new is not p
        assert collect(new,lambda s:bool(s))[0]==3000
    finally: manager.close();db.close()


def test_native_mixed_http_playlist_and_probe(tmp_path):
    tone(tmp_path/'a.wav',1000);tone(tmp_path/'b.wav',2000)
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever);thread.start()
    playlist=tmp_path/'remote.m3u'
    playlist.write_text(f'a.wav\nhttp://127.0.0.1:{server.server_port}/b.wav?token=notlogged\n')
    p=TracedPipeline(config(playlist,'playlist'),timeout_s=3);p.output.attach(0)
    try:
        p.start(); assert collect(p,lambda s:len(s)>=3)[:3]==[1000,2000,1000]
        manager=SourceManager(None,pipeline_factory=TracedPipeline)
        assert manager.test(config(playlist,'playlist')).ok
        assert not manager.pipelines
    finally:
        p.stop();server.shutdown();server.server_close();thread.join()


def test_native_stop_at_track_handoff(tmp_path):
    tone(tmp_path/'a.wav',1000,.3);tone(tmp_path/'b.wav',2000,.3)
    for _ in range(5):
        p=TracedPipeline(config(tmp_path),timeout_s=3);p.output.attach(0)
        try:
            p.start();collect(p,lambda s:len(s)>=2)
        finally: p.stop()
        assert not p._thread.is_alive()

def test_native_cycle_refresh_and_gain(tmp_path):
    from fmheadendd.tests.test_gstreamer import file_profile
    tone(tmp_path/'a.wav',4000,1)
    source=config(tmp_path).model_copy(update=dict(processor_mode='builtin', audio_profile_path='custom/test.json', input_gain_db=-6))
    p=TracedPipeline(source,profile=file_profile('LOCAL native',gain_db=-6),timeout_s=3)
    p.output.attach(0)
    try:
        p.start()
        collect(p,lambda s:bool(s))
        # Newly resolved next cycles must discover this item without restarting.
        tone(tmp_path/'b.wav',8000,1)
        seen=collect(p,lambda s:any(1950 <= value <= 2050 for value in s),seconds=6)
        assert all(950 <= value <= 1050 or 1950 <= value <= 2050 for value in seen)
        assert p.builds == 1
    finally: p.stop()

@pytest.mark.parametrize('suffix', ['.m3u', '.m3u8'])
def test_native_vlc_encoded_playlist(tmp_path, suffix):
    from urllib.parse import quote
    from fmheadendd.sources.local_media import M3uResolver
    media = tmp_path / 'ZENE' / '2025 11 November' / 'P\u00e9terfy - Sz\u00e1z f\u00e9ny\u00e9v.wav'
    media.parent.mkdir(parents=True)
    tone(media, 16423, 1)
    entry = quote(media.relative_to(tmp_path).as_posix())
    playlist = tmp_path / ('vlc'+suffix)
    comment = '#EXTINF:1,P\u00e9terfy Bori - Sz\u00e1z f\u00e9ny\u00e9v\n'
    playlist.write_bytes(b'\xef\xbb\xbf#EXTM3U\n' + comment.encode('latin-1' if suffix == '.m3u' else 'utf-8') + entry.encode('ascii') + b'\n')
    assert M3uResolver().resolve(playlist)[0].locator == media
    p = TracedPipeline(config(playlist, 'playlist'), timeout_s=3)
    p.output.attach(0)
    try:
        p.start()
        assert p._ready.wait(3), p.snapshot()
        assert p._pipeline.get_property('uri') == media.as_uri()
        assert collect(p, lambda seen: bool(seen)) == [16423]
        assert p.snapshot()['output_caps'] and not p.fatal_error
    finally:
        p.stop()
    assert not p._thread.is_alive()
