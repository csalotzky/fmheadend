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
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.tests.test_worker_recovery_native import NativeStallWorker
from fmheadendd.tests.test_gstreamer import gst,http_files,require,file_profile
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_worker import source,until,finish

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native Linux eight-output workers')


class Sinks:
    def __init__(self,root):
        self.stop=threading.Event(); self.blocked=[threading.Event() for _ in range(8)]
        self.disconnect=[threading.Event() for _ in range(8)]
        self.counts=[0]*8; self.samples=[bytearray() for _ in range(8)]
        self.sessions=[0]*8; self.threads=[]; self.listeners=[]
        for ch in range(8):
            s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            s.bind(str(root/f'pcm{ch}.sock')); s.listen(); s.settimeout(.1)
            self.listeners.append(s)
            t=threading.Thread(target=self.run,args=(ch,s),daemon=True); t.start(); self.threads.append(t)

    def run(self,ch,listener):
        while not self.stop.is_set():
            try:c,_=listener.accept()
            except socket.timeout:continue
            c.settimeout(.1); c.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
            self.sessions[ch]+=1
            with c:
                while not self.stop.is_set():
                    if self.disconnect[ch].is_set():
                        self.disconnect[ch].clear()
                        break
                    if self.blocked[ch].is_set():self.stop.wait(.02); continue
                    try:data=c.recv(65536)
                    except socket.timeout:continue
                    if not data:break
                    self.counts[ch]+=len(data)
                    if len(self.samples[ch])<4096:self.samples[ch].extend(data[:4096-len(self.samples[ch])])

    def close(self):
        self.stop.set()
        for t in self.threads:t.join(2)
        for s in self.listeners:s.close()
        assert not any(t.is_alive() for t in self.threads)


@pytest.mark.parametrize('fault',['crash','native-stall'])
def test_native_eight_output_recovery_and_independent_consumers(gst,http_files,tmp_path,fault):
    require(gst,'souphttpsrc','wavparse','volume')
    folder,url=http_files
    with wave.open(str(folder/'multi.wav'),'wb') as wav:
        wav.setparams((2,2,48000,0,'NONE','')); wav.writeframes(struct.pack('<hh',8000,-4000)*48000*90)
    sinks=Sinks(tmp_path)
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):return NativeStallWorker(s,direct_runtime=self.runtime,**kw)
    db=Database(tmp_path/'multi.db'); m=SourceManager(db,Factory(tmp_path),profile_files=profile_store(tmp_path))
    profile=m.profile_files.create(file_profile('eight',gain_db=-6).model_dump(mode='json'))
    a=db.save('sources',source().model_copy(update={'config':{'uri':url+'/multi.wav'},
        'use_default_audio_profile':False,'audio_profile_id':profile.path}))
    mux=DirectMux(m,tmp_path); retired=[]
    m.worker_recovery.policy=RecoveryPolicy(pcm_timeout=5,buffering_timeout=10)
    def step(predicate):
        def check():m.worker_recovery.service(); mux.service(); return predicate()
        return until(check,timeout=18)
    def attach(ch):
        try:m.acquire(a.id,ch)
        except SourcePending:pass
        try:mux.attach(ch,a.id)
        except SourcePending:pass
    try:
        for ch in range(8):attach(ch)
        step(lambda:all(n>30000 for n in sinks.counts))
        old=m.pipelines[a.id]; stable=old.output
        assert len(m.pipelines)==1 and m.consumers[a.id]==set(range(8))
        assert old._sequences['pcm']==0 and all(old.output.read(ch) is None for ch in range(8))
        # Observe an unchanged native stream; no periodic state/metadata trees.
        step(lambda:len(old.direct_applied_channels)==8 and old.probe_result().ok and
            old.control_counts.get('heartbeat',0)>=3)
        before_counts=dict(old.control_counts)
        until(lambda:old.control_counts.get('heartbeat',0)>=before_counts.get('heartbeat',0)+2,timeout=4)
        delta={k:old.control_counts.get(k,0)-before_counts.get(k,0) for k in ('status','direct_progress','direct_state','heartbeat')}
        print('UNCHANGED_CONTROL',delta)
        assert delta['status']==delta['direct_progress']==delta['direct_state']==0
        os.kill(old.process.pid,signal.SIGUSR2)
        step(lambda:old.snapshot().get('stream_title')=='event-title')
        assert old.control_counts['direct_state']>before_counts.get('direct_state',0)
        print('METADATA_CONTROL',old.control_counts)
        old.request_state()
        state_count=old.control_counts['direct_state']
        step(lambda:old.control_counts['direct_state']>state_count)
        for sample in sinks.samples:
            values=struct.unpack('<'+'h'*(len(sample)//2),sample)
            assert all(abs(v-8000*10**(-6/20))<=2 for v in values[::2])
            assert all(abs(v+4000*10**(-6/20))<=2 for v in values[1::2])
        # Stop reading just TX3; force native socket backpressure.
        sinks.blocked[3].set(); before=list(sinks.counts)
        step(lambda:old.direct_progress.get('writer',{}).get('3',{}).get('stale_disconnects',0)>0)
        step(lambda:all(sinks.counts[ch]>before[ch]+30000 for ch in range(8) if ch!=3))
        assert all(old.direct_progress['writer'][str(ch)]['stale_disconnects']==0 for ch in range(8) if ch!=3)
        sinks.blocked[3].clear()
        step(lambda:sinks.counts[3]>before[3]+30000)
        # Revoke one pending/reconnecting output, leave seven intact.
        mux.detach(3); m.release(a.id,3)
        step(lambda:old.direct_applied_channels.get(3,(0,1))[1] is None)
        sessions=sinks.sessions[3]; before=list(sinks.counts)
        step(lambda:all(sinks.counts[ch]>before[ch]+30000 for ch in range(8) if ch!=3))
        assert sinks.sessions[3]==sessions
        attach(3); before=sinks.counts[3]; step(lambda:sinks.counts[3]>before+30000)
        # Desired returns to the same owner before the revoke has completed.
        mux.detach(2); attach(2)
        step(lambda:mux.channels[2].revoking is None and
            old.direct_applied_channels.get(2,(0,None))[1] is not None)
        before=sinks.counts[2]; step(lambda:sinks.counts[2]>before+30000)
        status_at=old.recovery_observation()[0]
        os.kill(old.process.pid,signal.SIGKILL if fault=='crash' else signal.SIGUSR1)
        if fault=='native-stall':step(lambda:old.recovery_observation()[0]>status_at)
        step(lambda:m.pipelines[a.id] is not old); retired.append(old)
        with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
        assert m.pipelines[a.id].output is stable and m.pipelines[a.id].generation!=old.generation
        before=list(sinks.counts); step(lambda:all(n>b+30000 for n,b in zip(sinks.counts,before)))
        current=m.pipelines[a.id]
        changed=m.profile_files.create(file_profile('eight apply',gain_db=-3).model_dump(mode='json'))
        m._restart_audio(a.model_copy(update={'audio_profile_id':changed.path}))
        step(lambda:m.pipelines[a.id] is not current); retired.append(current)
        before=list(sinks.counts); step(lambda:all(n>b+30000 for n,b in zip(sinks.counts,before)))
        for ch in range(8):mux.detach(ch); m.release(a.id,ch)
        step(lambda:not m.pipelines)
    finally:
        remaining=list(m.pipelines.values()); mux.close(); m.close()
        for p in retired+remaining:finish(p)
        sinks.close(); db.close()
