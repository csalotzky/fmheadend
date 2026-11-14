import os,signal,struct,sys,wave
import pytest
from fmheadendd.database import Database
from fmheadendd.errors import SourcePending
from fmheadendd.sources.direct_pcm import DirectHttpFactory,DirectMux
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.tests.test_direct_multitx_native import Sinks
from fmheadendd.tests.test_worker_recovery_native import NativeStallWorker
from fmheadendd.tests.test_gstreamer import gst,http_files,require,file_profile
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_worker import source,until,finish

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Four native Linux HTTP workers')


@pytest.mark.parametrize('fault',['crash','native-stall'])
def test_four_independent_workers(gst,http_files,tmp_path,fault):
    require(gst,'souphttpsrc','wavparse','volume')
    folder,url=http_files
    for idx in range(4):
        with wave.open(str(folder/f'four{idx}.wav'),'wb') as wav:
            wav.setparams((2,2,48000,0,'NONE',''))
            wav.writeframes(struct.pack('<hh',8000+1000*idx,-4000-500*idx)*48000*120)
    sinks=Sinks(tmp_path)
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):return NativeStallWorker(s,direct_runtime=self.runtime,**kw)
    db=Database(tmp_path/'four.db');m=SourceManager(db,Factory(tmp_path),profile_files=profile_store(tmp_path))
    profile=m.profile_files.create(file_profile('four',gain_db=-6).model_dump(mode='json'))
    configs=[db.save('sources',source(f'worker{i}').model_copy(update={
        'config':{'uri':url+f'/four{i}.wav'},'use_default_audio_profile':False,
        'audio_profile_id':profile.path})) for i in range(4)]
    mux=DirectMux(m,tmp_path);retired=[]
    def step(predicate):
        def check():m.worker_recovery.service();mux.service();return predicate()
        return until(check,timeout=25)
    def attach(sid,ch):
        try:m.acquire(sid,ch)
        except SourcePending:pass
        try:mux.attach(ch,sid)
        except SourcePending:pass
    def samples(ch,idx,db_gain=-6):
        vals=struct.unpack('<'+'h'*(len(sinks.samples[ch])//2),sinks.samples[ch])
        assert vals
        assert all(abs(v-(8000+1000*idx)*10**(db_gain/20))<=2 for v in vals[::2])
        assert all(abs(v-(-4000-500*idx)*10**(db_gain/20))<=2 for v in vals[1::2])
    try:
        for ch,s in enumerate(configs):attach(s.id,ch)
        step(lambda:all(sinks.counts[ch]>40000 for ch in range(4)))
        workers=[m.pipelines[s.id] for s in configs]
        assert len(m.pipelines)==len({p.process.pid for p in workers})==4
        for ch,p in enumerate(workers):
            assert p._sequences['pcm']==0 and p.output.read(ch) is None
            samples(ch,ch)
        step(lambda:all(p.probe_result().ok and p.control_counts.get('heartbeat',0)>=3 for p in workers))
        counts=[dict(p.control_counts) for p in workers]
        step(lambda:all(p.control_counts.get('heartbeat',0)>=b.get('heartbeat',0)+2 for p,b in zip(workers,counts)))
        deltas=[{k:p.control_counts.get(k,0)-b.get(k,0) for k in ('status','direct_progress','direct_state','heartbeat')}
                for p,b in zip(workers,counts)]
        print('FOUR_STEADY_CONTROL',deltas)
        assert all(d['status']==d['direct_state']==d['direct_progress']==0 and d['heartbeat']>=2 for d in deltas)
        # One native failure: all other process identities and PCM keep going.
        old=workers[1];stable=old.output;before=list(sinks.counts)
        m.worker_recovery.policy=RecoveryPolicy(pcm_timeout=5,buffering_timeout=10,backoff=.05)
        status_at=old.recovery_observation()[0]
        os.kill(old.process.pid,signal.SIGKILL if fault=='crash' else signal.SIGUSR1)
        if fault=='native-stall':step(lambda:old.recovery_observation()[0]>status_at)
        step(lambda:m.pipelines[configs[1].id] is not old);retired.append(old)
        with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
        new=m.pipelines[configs[1].id];assert new.output is stable and new.generation!=old.generation
        step(lambda:all(sinks.counts[ch]>before[ch]+30000 for ch in range(4)))
        assert all(m.pipelines[configs[ch].id] is workers[ch] for ch in (0,2,3))
        # Live cross-worker transfer: source 2 temporarily owns TX0 as well.
        previous=sinks.sessions[0];attach(configs[2].id,0)
        step(lambda:mux.channels[0].owner is workers[2] and sinks.sessions[0]>previous)
        assert workers[0].direct_applied[1] is None
        sinks.samples[0].clear();step(lambda:len(sinks.samples[0])==4096);samples(0,2)
        previous=sinks.sessions[0];attach(configs[0].id,0)
        step(lambda:mux.channels[0].owner is workers[0] and sinks.sessions[0]>previous)
        m.release(configs[2].id,0)
        sinks.samples[0].clear();step(lambda:len(sinks.samples[0])==4096);samples(0,0)
        # Endpoint closes just one socket; same worker/lease reconnects.
        previous=sinks.sessions[2];other_sessions=[sinks.sessions[ch] for ch in (0,1,3)]
        sinks.disconnect[2].set()
        step(lambda:sinks.sessions[2]>previous)
        assert m.pipelines[configs[2].id] is workers[2]
        assert [sinks.sessions[ch] for ch in (0,1,3)]==other_sessions
        # Apply one profile; only this worker is replaced.
        prior=m.pipelines[configs[1].id]
        gain=m.profile_files.create(file_profile('four apply',gain_db=-3).model_dump(mode='json'))
        m._restart_audio(configs[1].model_copy(update={'audio_profile_id':gain.path}))
        step(lambda:m.pipelines[configs[1].id] is not prior);retired.append(prior)
        before=sinks.counts[1];step(lambda:sinks.counts[1]>before+30000)
        # Last-consumer STOP of one source leaves the other three alive.
        stopped=m.pipelines[configs[3].id];mux.detach(3);m.release(configs[3].id,3)
        step(lambda:configs[3].id not in m.pipelines);retired.append(stopped)
        before=list(sinks.counts);step(lambda:all(sinks.counts[ch]>before[ch]+30000 for ch in range(3)))
        assert len(m.pipelines)==3
        for ch in range(3):mux.detach(ch);m.release(configs[ch].id,ch)
        step(lambda:not m.pipelines)
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for p in retired+remaining:finish(p)
        sinks.close();db.close()
