import os
import signal
import struct
import sys
import time
import wave

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import SourcePending
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_gstreamer import http_files, gst, require
from fmheadendd.tests.test_audio_worker import until, finish

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Native Linux workers')


class NativeStallWorker(ProcessPipeline):
    worker_module = 'fmheadendd.tests.audiod_native_stall_fixture'


@pytest.mark.parametrize('fault', ['crash', 'native-stall'])
def test_native_recovery_shared_pcm_sibling_apply_stop(gst, http_files, tmp_path, fault):
    require(gst, 'souphttpsrc', 'wavparse', 'audiocheblimit', 'fmheadendlimiter', 'fmheadendclipper')
    folder, url = http_files
    with wave.open(str(folder / 'tone.wav'), 'wb') as wav:
        wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack('<hh', 8000, 8000) * 48000 * 60)
    db = Database(tmp_path / 'native-recovery.db')
    m = SourceManager(db, pipeline_factory=NativeStallWorker, profile_files=profile_store(tmp_path))
    m.worker_recovery.policy = RecoveryPolicy(pcm_timeout=1, buffering_timeout=3,
                                               status_timeout=2)
    a, b = [db.save('sources', InputSourceConfig(name=n, type='HTTP',
        config={'uri': url + '/tone.wav'})) for n in ('victim', 'sibling')]
    retired = []
    def step(predicate):
        def run():
            m.worker_recovery.service()
            return predicate()
        return until(run, timeout=12)
    try:
        for sid, tx in ((a.id, 0), (a.id, 1), (b.id, 2)):
            try:
                m.acquire(sid, tx)
            except SourcePending:
                pass
        old, sibling = m.pipelines[a.id], m.pipelines[b.id]
        for p, tx in ((old, 0), (old, 1), (sibling, 2)):
            assert until(lambda: p.output.read(tx), timeout=12)
        before = old.recovery_observation()[0]
        os.kill(old.process.pid, signal.SIGKILL if fault == 'crash' else signal.SIGUSR1)
        if fault == 'native-stall':
            # Control reports continue while the actual Gst streaming callback blocks.
            until(lambda: old.recovery_observation()[0] > before)
        step(lambda: m.pipelines[a.id] is not old)
        retired.append(old)
        new = m.pipelines[a.id]
        assert old._done.is_set()
        with pytest.raises(ChildProcessError):
            os.waitpid(old.process.pid, os.WNOHANG)
        assert new.output is old.output and new.generation != old.generation
        assert m.consumers[a.id] == {0, 1}
        for p, tx in ((new, 0), (new, 1), (sibling, 2)):
            assert until(lambda: p.output.read(tx), timeout=12)
        assert m.pipelines[b.id] is sibling and sibling.process.poll() is None
        changed = a.model_copy(update={'input_gain_db': -6})
        m._restart_audio(changed)
        step(lambda: m.pipelines[a.id] is not new)
        retired.append(new)
        assert m.pipelines[a.id].source.input_gain_db == -6
        assert until(lambda: m.pipelines[a.id].output.read(0), timeout=12)
        from fmheadendd.tests.test_gstreamer import file_profile
        profile = file_profile('recovery-dsp').model_dump(mode='json')
        profile['stages']['limiter']['selected'] = 'FM_HEADEND_LIMITER'
        profile['stages']['clipper']['selected'] = 'FM_HEADEND_CLIPPER'
        saved = m.profile_files.create(profile)
        previous = m.pipelines[a.id]
        changed = changed.model_copy(update={'use_default_audio_profile': False, 'audio_profile_id': saved.path})
        m._restart_audio(changed)
        step(lambda: m.pipelines[a.id] is not previous)
        retired.append(previous)
        assert until(lambda: m.pipelines[a.id].output.read(0), timeout=12)
        assert m.pipelines[a.id].audio_profile_id == saved.path
        retired.extend(m.pipelines.values())
        for sid, tx in ((a.id, 0), (a.id, 1), (b.id, 2)):
            m.release(sid, tx)
        step(lambda: not m.pipelines)
    finally:
        remaining = list(m.pipelines.values())
        m.close()
        for p in retired + remaining:
            finish(p)
            with pytest.raises(ChildProcessError):
                os.waitpid(p.process.pid, os.WNOHANG)
        db.close()
