"""Real LOCAL child decoding, serialized metadata, recovery and media return."""
from collections import deque
import os
from pathlib import Path
import signal
import math
import subprocess
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
from fmheadendd.tests.test_gstreamer import gst, require, http_files, file_profile
from fmheadendd.tests.test_audio_worker import until, finish

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Native Linux LOCAL worker')


class RecordingWorker(ProcessPipeline):
    worker_module = 'fmheadendd.tests.audiod_native_stall_fixture'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = deque(maxlen=2000)

    def _accept(self, channel, message, pcm=b''):
        super()._accept(channel, message, pcm)
        if channel == 'pcm' and message['generation'] == self.generation:
            # Observe actual wire identity/content together, not asynchronously
            # sampled public metadata versus an independently buffered consumer.
            payload = message['payload']
            self.records.append((payload['status'].get('title'), payload['produced_ns'], pcm))


def wav(path, value=8000, seconds=1):
    with wave.open(str(path), 'wb') as out:
        out.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        out.writeframes(struct.pack('<hh', value, value) * int(48000 * seconds))


def start(p):
    try:
        p.start()
    except SourcePending:
        pass


def titles(p):
    result = []
    for title, _, _ in list(p.records):
        if title and (not result or title != result[-1]):
            result.append(title)
    return result


@pytest.mark.parametrize('order', ['sequential', 'random'])
def test_native_local_cycles_metadata_and_gapless(gst, tmp_path, order):
    require(gst, 'playbin3', 'wavparse')
    for name, value in [('track2', 8000), ('track10', 12000), ('track20', 16000)]:
        wav(tmp_path / (name + '.wav'), value, 1)
    p = RecordingWorker(InputSourceConfig(name='local', type='LOCAL', input_gain_db=-6,
        config={'mode': 'folder', 'path': str(tmp_path), 'order': order}), profile=file_profile('local'))
    p.output.attach(1)
    try:
        start(p)
        until(lambda: len(titles(p)) >= 7, timeout=18)
        sequence = titles(p)
        if order == 'sequential':
            assert sequence[:7] == ['track2', 'track10', 'track20', 'track2', 'track10', 'track20', 'track2']
        else:
            assert set(sequence[:3]) == set(sequence[3:6]) == {'track2', 'track10', 'track20'}
            assert sequence[2] != sequence[3]
        records = list(p.records)
        assert p.process.pid != os.getpid() and p.output.read(1)
        expected = {'track2': 8000, 'track10': 12000, 'track20': 16000}
        for title, _, data in records:
            assert len(data) % 4 == 0 and title in expected
            samples = struct.unpack('<' + 'h' * (len(data) // 2), data)
            target = expected[title] * 10 ** (-6 / 20)
            assert max(abs(v - target) for v in samples) <= 3
        gaps = [(b[1] - a[1]) / 1e9 for a, b in zip(records, records[1:]) if a[0] != b[0]]
        print(f'LOCAL {order}: transitions={len(gaps)}, max boundary PCM interval={max(gaps):.6f}s')
        assert max(gaps) < 0.15  # Measure discontinuities; not proof of sample-perfect RF output.
    finally:
        finish(p)
        with pytest.raises(ChildProcessError):
            os.waitpid(p.process.pid, os.WNOHANG)


def test_native_missing_media_return_refresh_and_m3u(gst, tmp_path):
    require(gst, 'playbin3', 'wavparse')
    usb = tmp_path / 'usb'
    db = Database(tmp_path / 'local.db')
    m = SourceManager(db, pipeline_factory=RecordingWorker, profile_files=profile_store(tmp_path))
    m.worker_recovery.policy = RecoveryPolicy(pcm_timeout=0.5, buffering_timeout=3, backoff=0)
    source = db.save('sources', InputSourceConfig(name='usb', type='LOCAL',
        config={'mode': 'playlist', 'path': str(usb / 'list.m3u')}))
    p = None
    try:
        try:
            m.acquire(source.id, 0)
        except SourcePending:
            pass
        p = m.pipelines[source.id]
        until(lambda: p.expected_media_wait, timeout=8)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            m.worker_recovery.service()
            assert m.pipelines[source.id] is p and not p.fatal_error
            time.sleep(0.02)
        usb.mkdir(); wav(usb / 'one.wav', seconds=1)
        (usb / 'list.m3u').write_text('one.wav\n')
        until(lambda: 'one' in titles(p), timeout=12)
        wav(usb / 'two.wav', seconds=1)
        (usb / 'list.m3u').write_text('two.wav\none.wav\n')
        until(lambda: 'two' in titles(p), timeout=12)
        removed = tmp_path / 'unplugged'
        usb.rename(removed)
        until(lambda: p.expected_media_wait, timeout=12)
        assert not p.fatal_error and m.pipelines[source.id] is p
        removed.rename(usb)
        count = len(p.records)
        until(lambda: len(p.records) > count + 10, timeout=12)
        assert m.pipelines[source.id] is p and not m.worker_recovery.attempts
    finally:
        m.close()
        if p:
            finish(p)
        db.close()


@pytest.mark.parametrize('fault', ['crash', 'native-stall'])
def test_native_local_recovery_sibling_and_apply(gst, http_files, tmp_path, fault):
    require(gst, 'playbin3', 'wavparse', 'souphttpsrc', 'fmheadendlimiter', 'fmheadendclipper')
    folder, url = http_files
    wav(folder / 'http.wav', seconds=60)
    media = tmp_path / 'tracks'; media.mkdir()
    wav(media / 'local.wav', seconds=2)
    db = Database(tmp_path / 'recover.db')
    m = SourceManager(db, pipeline_factory=RecordingWorker, profile_files=profile_store(tmp_path))
    m.worker_recovery.policy = RecoveryPolicy(pcm_timeout=1, buffering_timeout=3, backoff=0.05)
    local = db.save('sources', InputSourceConfig(name='local', type='LOCAL', config={'mode': 'folder', 'path': str(media)}))
    http = db.save('sources', InputSourceConfig(name='http', type='HTTP', config={'uri': url + '/http.wav'}))
    owners = []
    def step(predicate):
        def poll():
            m.worker_recovery.service()
            return predicate()
        until(poll, timeout=15)
    try:
        for sid, tx in ((local.id, 0), (local.id, 1), (http.id, 2)):
            try:
                m.acquire(sid, tx)
            except SourcePending:
                pass
        old, sibling = m.pipelines[local.id], m.pipelines[http.id]
        owners += [old, sibling]
        until(lambda: old.output.read(0) and sibling.output.read(2), timeout=12)
        before = old.recovery_observation()[0]
        os.kill(old.process.pid, signal.SIGKILL if fault == 'crash' else signal.SIGUSR1)
        if fault == 'native-stall':
            until(lambda: old.recovery_observation()[0] > before)
        sibling_progress = sibling.recovery_observation()[1]
        step(lambda: m.pipelines[local.id] is not old)
        new = m.pipelines[local.id]; owners.append(new)
        assert new.output is old.output and new.generation != old.generation
        assert old._done.is_set() and m.consumers[local.id] == {0, 1}
        with pytest.raises(ChildProcessError):
            os.waitpid(old.process.pid, os.WNOHANG)
        assert sibling.process.poll() is None and sibling.recovery_observation()[1] > sibling_progress
        until(lambda: new.output.read(0) and new.output.read(1), timeout=12)
        profile = file_profile('local-dsp').model_dump(mode='json')
        profile['stages']['limiter']['selected'] = 'FM_HEADEND_LIMITER'
        profile['stages']['clipper']['selected'] = 'FM_HEADEND_CLIPPER'
        saved = m.profile_files.create(profile)
        changed = local.model_copy(update={'input_gain_db': -6, 'use_default_audio_profile': False, 'audio_profile_id': saved.path})
        m._restart_audio(changed)
        step(lambda: m.pipelines[local.id] is not new)
        current = m.pipelines[local.id]; owners.append(current)
        assert current.output is old.output and current.audio_profile_id == saved.path
        until(lambda: current.output.read(0), timeout=12)
        for sid, tx in ((local.id, 0), (local.id, 1), (http.id, 2)):
            m.release(sid, tx)
        step(lambda: not m.pipelines)
    finally:
        owners += list(m.pipelines.values())
        m.close()
        for p in set(owners):
            finish(p)
            with pytest.raises(ChildProcessError):
                os.waitpid(p.process.pid, os.WNOHANG)
        db.close()


@pytest.mark.parametrize('suffix,encoder,decoder', [
    ('mp3', 'lamemp3enc', 'mpg123audiodec'), ('flac', 'flacenc', 'flacdec')])
def test_native_compressed_local_in_standard_audiod(gst, tmp_path, suffix, encoder, decoder):
    require(gst, 'playbin3', 'wavparse', encoder, decoder)
    original = tmp_path / 'original.wav'
    with wave.open(str(original), 'wb') as out:
        out.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        out.writeframes(b''.join(struct.pack('<hh', value, value) for value in
            (int(8000 * math.sin(2 * math.pi * 700 * i / 48000)) for i in range(96000))))
    media = tmp_path / 'media'; media.mkdir()
    encoded = media / ('encoded.' + suffix)
    subprocess.run(['gst-launch-1.0', '-q', 'filesrc', f'location={original}', '!',
        'wavparse', '!', 'audioconvert', '!', encoder, '!', 'filesink', f'location={encoded}'],
        check=True, timeout=15)
    p = ProcessPipeline(InputSourceConfig(name='compressed', type='LOCAL',
        config={'mode': 'folder', 'path': str(media)}), profile=file_profile('codec'))
    p.output.attach(0)
    try:
        start(p)
        data = until(lambda: p.output.read(0), timeout=10)
        assert len(data) % 4 == 0 and any(data)
        until(lambda: p.snapshot()['title'] == 'encoded')
        until(lambda: p.snapshot().get('output_caps'))  # Cached caps follow the first staging drain.
        p.check_startup()
        assert '48000' in p.snapshot()['output_caps']
        assert p.process.pid != os.getpid()
    finally:
        finish(p)
        with pytest.raises(ChildProcessError):
            os.waitpid(p.process.pid, os.WNOHANG)
