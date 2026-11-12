"""Real alsasrc/null is synthetic ALSA capture, never evidence of physical hardware."""
import os
import signal
import sys
import struct

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import SourcePending
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.tests.test_gstreamer import gst, require, file_profile, http_files
from fmheadendd.tests.test_audio_worker import until, finish
from fmheadendd.tests.test_local_worker_native import RecordingWorker, start, wav
from fmheadendd.tests.profile_support import profile_store

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux ALSA worker')


def test_native_unavailable_device_reports_error(gst):
    from fmheadendd.errors import DependencyUnavailable
    require(gst, 'alsasrc')
    p = ProcessPipeline(InputSourceConfig(name='missing capture', type='ALSA',
        config={'device':'hw:CARD=FMHEADEND_MISSING_CAPTURE,DEV=0'}), timeout_s=1)
    try:
        try: p.start()
        except DependencyUnavailable: pass
        until(lambda: p.snapshot().get('last_error'), timeout=10)
        assert not p._has_pcm
        assert p.snapshot()['state'] in ('ERROR', 'CONNECTING')
    finally: finish(p)


@pytest.mark.parametrize('selection', ['stereo', 'mono', 'left', 'right'])
def test_native_alsasrc_null_pcm_dsp(gst, selection):
    require(gst, 'alsasrc', 'fmheadendlimiter', 'fmheadendclipper')
    profile = file_profile('alsa')
    raw = profile.model_dump(mode='json')
    raw['stages']['limiter']['selected'] = 'FM_HEADEND_LIMITER'
    raw['stages']['clipper']['selected'] = 'FM_HEADEND_CLIPPER'
    profile = type(profile).model_validate(raw)
    p = ProcessPipeline(InputSourceConfig(name='null capture', type='ALSA', input_gain_db=-6,
        config={'device':'null', 'channel_selection':selection}), profile=profile)
    p.output.attach(0)
    try:
        start(p)
        data = until(lambda: p.output.read(0), timeout=15)
        assert p.process.pid != os.getpid() and len(data)%4 == 0
        samples = struct.unpack('<'+'h'*(len(data)//2), data)
        assert max(abs(value) for value in samples) <= 2  # null silence, allow dithering
        caps = p.snapshot()['output_caps']
        assert 'S16LE' in caps and '48000' in caps and 'channels=(int)2' in caps
        before = p.recovery_observation()[1]
        until(lambda: p.recovery_observation()[1] > before)
        assert p.snapshot()['codec'] == 'PCM'
    finally: finish(p)
    with pytest.raises(ChildProcessError): os.waitpid(p.process.pid, os.WNOHANG)


@pytest.mark.parametrize('fault', ['crash', 'native-stall'])
def test_native_capture_recovery(gst, tmp_path, http_files, fault):
    require(gst, 'alsasrc', 'wavparse', 'souphttpsrc')
    folder, url = http_files
    wav(folder/'sibling.wav', seconds=60)
    db = Database(tmp_path/'capture.db')
    m = SourceManager(db, pipeline_factory=RecordingWorker, profile_files=profile_store(tmp_path))
    m.worker_recovery.policy = RecoveryPolicy(pcm_timeout=1, buffering_timeout=3, backoff=.05)
    a = db.save('sources', InputSourceConfig(name='capture', type='ALSA', config={'device':'null'}))
    b = db.save('sources', InputSourceConfig(name='http', type='HTTP', config={'uri':url+'/sibling.wav'}))
    owners = []
    def step(condition):
        def poll():
            m.worker_recovery.service()
            return condition()
        until(poll, timeout=20)
    try:
        for sid,tx in ((a.id,0),(a.id,1),(b.id,2)):
            try: m.acquire(sid,tx)
            except SourcePending: pass
        old,sibling = m.pipelines[a.id],m.pipelines[b.id]; owners.extend((old,sibling))
        until(lambda: old.output.read(0) and sibling.output.read(2), timeout=15)
        before = old.recovery_observation()[0]
        os.kill(old.process.pid, signal.SIGKILL if fault == 'crash' else signal.SIGUSR1)
        if fault == 'native-stall': until(lambda: old.recovery_observation()[0] > before)
        progress = sibling.recovery_observation()[1]
        step(lambda: m.pipelines[a.id] is not old)
        new=m.pipelines[a.id]; owners.append(new)
        assert new.output is old.output and new.generation != old.generation
        assert m.consumers[a.id] == {0,1}
        with pytest.raises(ChildProcessError): os.waitpid(old.process.pid, os.WNOHANG)
        until(lambda: new.output.read(0) and new.output.read(1), timeout=15)
        assert sibling.recovery_observation()[1] > progress
        m._restart_audio(a.model_copy(update={'input_gain_db':-6}))
        step(lambda: m.pipelines[a.id] is not new)
        current=m.pipelines[a.id]; owners.append(current)
        until(lambda: current.output.read(0), timeout=15)
        for sid,tx in ((a.id,0),(a.id,1),(b.id,2)): m.release(sid,tx)
        step(lambda: not m.pipelines)
    finally:
        m.close()
        for p in owners: finish(p)
        db.close()
