import os
import signal
import socket
import struct
import sys
import threading
import wave

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import SourcePending
from fmheadendd.sources.direct_pcm import DirectMux, DirectHttpFactory
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.tests.test_worker_recovery_native import NativeStallWorker
from fmheadendd.tests.test_gstreamer import gst, http_files, require, file_profile
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_worker import source, until, finish

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Native Linux direct PCM')


@pytest.mark.parametrize('fault', ['crash', 'native-stall'])
def test_native_direct_recovery_switch_apply(gst, http_files, tmp_path, fault):
    require(gst, 'souphttpsrc', 'wavparse', 'volume')
    folder,url=http_files
    with wave.open(str(folder/'direct.wav'),'wb') as out:
        out.setparams((2,2,48000,0,'NONE',''))
        out.writeframes(struct.pack('<hh',8000,-4000)*48000*60)
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(str(tmp_path/'pcm0.sock')); listener.listen(); listener.settimeout(.1)
    stopped=threading.Event(); counts=[0]; samples=bytearray(); sessions=[]
    def receive():
        while not stopped.is_set():
            try:c,_=listener.accept()
            except socket.timeout:continue
            c.settimeout(.1); sessions.append(c)
            with c:
                while not stopped.is_set():
                    try:data=c.recv(65536)
                    except socket.timeout:continue
                    if not data:break
                    counts[0]+=len(data)
                    if len(samples)<4096:samples.extend(data[:4096-len(samples)])
    thread=threading.Thread(target=receive,daemon=True); thread.start()
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kwargs):
            return (ProcessPipeline(s,**kwargs) if s.name=='sibling' else
                    NativeStallWorker(s,direct_runtime=self.runtime,**kwargs))
    db=Database(tmp_path/'config.db')
    m=SourceManager(db,Factory(tmp_path),profile_files=profile_store(tmp_path))
    saved=m.profile_files.create(file_profile('direct gain',gain_db=-6).model_dump(mode='json'))
    specs=[db.save('sources',source(n).model_copy(update={
        'config':{'uri':url+'/direct.wav'},'use_default_audio_profile':False,
        'audio_profile_id':saved.path})) for n in ('victim','next','sibling')]
    a,b,sibling=specs; mux=DirectMux(m,tmp_path); retired=[]
    m.worker_recovery.policy=RecoveryPolicy(pcm_timeout=5,buffering_timeout=10)
    def step(predicate):
        def check():
            m.worker_recovery.service(); mux.service()
            return predicate()
        try:
            return until(check,timeout=15)
        except BaseException:
            print({sid:dict(status=p.snapshot(), progress=p.direct_progress,
                applied=p.direct_applied, intent=p._direct_intent,
                exit=p.exit_code) for sid,p in m.pipelines.items()})
            raise
    def attach(sid):
        try:m.acquire(sid,0)
        except SourcePending:pass
        try:mux.attach(0,sid)
        except SourcePending:pass
    try:
        attach(a.id)
        try:m.acquire(sibling.id,1)
        except SourcePending:pass
        step(lambda:counts[0]>20000)
        p=m.pipelines[a.id]; stable=p.output
        assert p._sequences['pcm']==0 and not p.output.read(0)
        values=struct.unpack('<'+'h'*(len(samples)//2),samples)
        assert all(abs(v-8000*10**(-6/20))<=2 for v in values[::2])
        assert all(abs(v+4000*10**(-6/20))<=2 for v in values[1::2])
        old_status=p.recovery_observation()[0]
        os.kill(p.process.pid,signal.SIGKILL if fault=='crash' else signal.SIGUSR1)
        if fault=='native-stall':
            step(lambda:p.recovery_observation()[0]>old_status)
        step(lambda:m.pipelines[a.id] is not p)
        retired.append(p)
        with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
        current=m.pipelines[a.id]; assert current.output is stable
        before=counts[0]; step(lambda:counts[0]>before+20000)
        assert until(lambda:m.pipelines[sibling.id].output.read(1))
        # Profile-only apply, same parent output and same socket destination.
        updated=m.profile_files.create(file_profile('direct changed gain',gain_db=-3).model_dump(mode='json'))
        changed=a.model_copy(update={'audio_profile_id':updated.path})
        m._restart_audio(changed)
        step(lambda:m.pipelines[a.id] is not current)
        retired.append(current)
        before=counts[0]; step(lambda:counts[0]>before+20000)
        # Ordered cross-source transfer while old source is still alive.
        attach(b.id)
        step(lambda:mux.owner is m.pipelines[b.id] and mux.owner.direct_applied[1] is not None)
        assert m.pipelines[a.id].direct_applied[1] is None
        before=counts[0]; step(lambda:counts[0]>before+20000)
        mux.detach(0)
        for s,tx in ((a,0),(b,0),(sibling,1)):m.release(s.id,tx)
        step(lambda:not m.pipelines)
    finally:
        remaining=list(m.pipelines.values()); mux.close(); m.close()
        for p in retired+remaining:finish(p)
        db.close(); stopped.set(); thread.join(2); listener.close()
    assert not thread.is_alive()
