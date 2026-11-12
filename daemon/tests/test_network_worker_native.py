"""Real loopback senders and audiod receivers; no public streams or RF."""
import os
import signal
import struct
import sys
import time
import socket
import pytest

from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.tests.test_gstreamer import gst, require, free_udp_port, file_profile, http_files
from fmheadendd.tests.test_audio_worker import until, finish
from fmheadendd.tests.test_local_worker_native import start, RecordingWorker, wav

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native Linux UDP/RTP workers')


def test_native_malformed_rtp_then_valid_sender(gst):
    port=free_udp_port()
    s,sender=transport(gst,'PCM',port)
    p=ProcessPipeline(s,profile=file_profile('network'))
    p.output.attach(0)
    try:
        start(p)
        until(lambda:p._prepared,timeout=10)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            for _ in range(10):sock.sendto(b'not an RTP packet',('127.0.0.1',port))
        assert p.output.read(0) is None
        sender.set_state(gst.State.PLAYING)
        assert until(lambda:p.output.read(0),timeout=15)
    finally:
        sender.set_state(gst.State.NULL);finish(p)


def transport(gst,mode,port):
    values=dict(host='127.0.0.1',port=port)
    if mode=='UDP':
        values.update(format='S16LE',sample_rate=44100,channels=1)
        chain='audio/x-raw,format=S16LE,rate=44100,channels=1'
    else:
        chains={
            'PCM':('rtpL16pay','audioconvert ! audio/x-raw,format=S16BE,rate=44100,channels=1 ! rtpL16pay pt=96'),
            'OPUS':('opusenc','audioconvert ! audioresample ! opusenc ! rtpopuspay pt=96'),
            'MP3':('lamemp3enc','audioconvert ! lamemp3enc ! mpegaudioparse ! rtpmpapay pt=96'),
            'AAC':('avenc_aac','audioconvert ! audio/x-raw,rate=48000,channels=2 ! avenc_aac ! aacparse ! rtpmp4gpay pt=96')}
        require(gst,chains[mode][0]);chain=chains[mode][1]
        values.update(codec=mode,payload_type=96)
        if mode=='PCM':values.update(clock_rate=44100,channels=1)
        if mode=='AAC':values.update(clock_rate=48000,channels=2,codec_data='1190')
    sender=gst.parse_launch('audiotestsrc is-live=true volume=0.25 ! '+chain+
        f' ! udpsink host=127.0.0.1 port={port} sync=true')
    return InputSourceConfig(name=mode,type='UDP' if mode=='UDP' else 'RTP',config=values),sender


@pytest.mark.parametrize('mode',['UDP','PCM','OPUS','MP3','AAC'])
def test_native_payload_pcm_and_gain(gst,mode):
    s,sender=transport(gst,mode,free_udp_port())
    p=ProcessPipeline(s.model_copy(update={'input_gain_db':-6}),profile=file_profile('network'))
    p.output.attach(0)
    try:
        start(p);sender.set_state(gst.State.PLAYING)
        data=until(lambda:p.output.read(0),timeout=15)
        assert len(data)%4==0 and p.process.pid!=os.getpid()
        caps=until(lambda:p.snapshot().get('output_caps'),timeout=5)
        assert 'S16LE' in caps and '48000' in caps and 'channels=(int)2' in caps
        if mode in ('UDP','PCM'):
            samples=struct.unpack('<'+'h'*(len(data)//2),data)
            assert 3500<max(abs(v) for v in samples)<4600
    finally:
        sender.set_state(gst.State.NULL);finish(p)
    with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)


@pytest.mark.parametrize('mode',['UDP','PCM'])
@pytest.mark.parametrize('fault',['crash','native-stall'])
def test_native_idle_resume_recovery_rebind(gst,http_files,tmp_path,mode,fault):
    from fmheadendd.database import Database
    from fmheadendd.sources.manager import SourceManager
    from fmheadendd.sources.worker_recovery import RecoveryPolicy
    from fmheadendd.errors import SourcePending
    from fmheadendd.tests.profile_support import profile_store
    s,sender=transport(gst,mode,free_udp_port())
    folder,url=http_files;wav(folder/'sibling.wav',seconds=60)
    db=Database(tmp_path/'network.db')
    m=SourceManager(db,pipeline_factory=RecordingWorker,profile_files=profile_store(tmp_path))
    a=db.save('sources',s)
    b=db.save('sources',InputSourceConfig(name='sibling',type='HTTP',config={'uri':url+'/sibling.wav'}))
    owners=[]
    def step(condition):
        def poll():m.worker_recovery.service();return condition()
        until(poll,timeout=20)
    def idle(seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            m.worker_recovery.service();assert m.pipelines[a.id] is old
            time.sleep(.05)
    try:
        for sid,tx in ((a.id,0),(a.id,1),(b.id,2)):
            try:m.acquire(sid,tx)
            except SourcePending:pass
        old,sibling=m.pipelines[a.id],m.pipelines[b.id];owners.extend((old,sibling))
        idle(2)  # No sender yet, status remains responsive.
        sender.set_state(gst.State.PLAYING)
        until(lambda:old.output.read(0) and old.output.read(1),timeout=15)
        sender.set_state(gst.State.PAUSED);idle(2)
        sender.set_state(gst.State.PLAYING)
        until(lambda:old.output.read(0),timeout=15)
        m.worker_recovery.policy=RecoveryPolicy(network_idle_timeout=3,backoff=.05)
        before=old.recovery_observation()[0]
        os.kill(old.process.pid,signal.SIGKILL if fault=='crash' else signal.SIGUSR1)
        if fault=='native-stall':until(lambda:old.recovery_observation()[0]>before)
        sibling_progress=sibling.recovery_observation()[1]
        step(lambda:m.pipelines[a.id] is not old)
        new=m.pipelines[a.id];owners.append(new)
        assert new.output is old.output and new.generation!=old.generation
        with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
        until(lambda:new.output.read(0) and new.output.read(1),timeout=15) # Same UDP port!
        assert sibling.recovery_observation()[1]>sibling_progress
        m._restart_audio(a.model_copy(update={'input_gain_db':-6}))
        step(lambda:m.pipelines[a.id] is not new)
        current=m.pipelines[a.id];owners.append(current)
        until(lambda:current.output.read(0),timeout=15)
        for sid,tx in ((a.id,0),(a.id,1),(b.id,2)):m.release(sid,tx)
        step(lambda:not m.pipelines)
    finally:
        sender.set_state(gst.State.NULL);m.close()
        for p in owners:finish(p)
        db.close()
