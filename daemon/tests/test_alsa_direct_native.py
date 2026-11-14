"""Native alsasrc/null direct transport. Null is not physical capture hardware."""
import json,os,signal,struct,sys,time

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import SourcePending,DependencyUnavailable
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.direct_pcm import DirectHttpFactory,DirectMux
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_worker import until,finish
from fmheadendd.tests.test_gstreamer import gst,http_files,require,file_profile
from fmheadendd.tests.test_direct_multitx_native import Sinks
from fmheadendd.tests.test_local_worker_native import wav
from fmheadendd.tests.test_worker_recovery_native import NativeStallWorker

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native ALSA direct PCM')


def acquire(manager,sid,tx):
    try:manager.acquire(sid,tx)
    except SourcePending:pass


def attach(mux,sid):
    try:mux.attach(0,sid)
    except SourcePending:pass


def step(manager,mux,predicate,timeout=32):
    def poll():
        manager.worker_recovery.service()
        if isinstance(mux,DirectMux):mux.service()
        return predicate()
    return until(poll,timeout=timeout)


@pytest.mark.parametrize('mode',['C','D'])
def test_native_alsa_null_direct_pcm_and_reporting(gst,tmp_path,mode):
    require(gst,'alsasrc','fmheadendlimiter','fmheadendclipper')
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):return ProcessPipeline(s,direct_runtime=self.runtime if mode=='D' else None,**kw)
    db=Database(tmp_path/'capture.db');m=SourceManager(db,Factory(tmp_path,alsa=True),profile_files=profile_store(tmp_path))
    profile=file_profile('ALSA direct').model_dump(mode='json')
    profile['stages']['limiter']['selected']='FM_HEADEND_LIMITER'
    profile['stages']['clipper']['selected']='FM_HEADEND_CLIPPER'
    saved=m.profile_files.create(profile)
    a=db.save('sources',InputSourceConfig(name='null capture',type='ALSA',config={'device':'null','channel_selection':'right'},
        input_gain_db=-6,use_default_audio_profile=False,audio_profile_id=saved.path))
    sinks=Sinks(tmp_path);mux=DirectMux(m,tmp_path) if mode=='D' else UnixMuxClient(m.output_for,tmp_path);p=None
    try:
        acquire(m,a.id,0);attach(mux,a.id);p=m.pipelines[a.id]
        step(m,mux,lambda:sinks.counts[0]>40000 and p.snapshot().get('output_caps') and p.control_counts.get('heartbeat',0)>=3)
        caps=p.snapshot()['output_caps']
        assert 'S16LE' in caps and '48000' in caps and 'channels=(int)2' in caps and 'interleaved' in caps
        assert p.snapshot()['codec']=='PCM' and p.source.config==a.config
        samples=bytes(sinks.samples[0]);assert len(samples)%4==0
        values=struct.unpack('<'+'h'*(len(samples)//2),samples)
        assert max(abs(v) for v in values)<=2  # Real null silence, allow existing S16 dither.
        assert all(n==0 for n in sinks.counts[1:])
        before=dict(p.control_counts);start=time.monotonic();count=sinks.counts[0];pcm_at=p._last_pcm
        step(m,mux,lambda:p.control_counts.get('heartbeat',0)>=before['heartbeat']+3,timeout=6)
        dt=time.monotonic()-start;rate=(sinks.counts[0]-count)/dt
        delta={k:p.control_counts.get(k,0)-before.get(k,0) for k in ('status','direct_state','direct_progress','heartbeat')}
        print('ALSA_NULL_PCM',json.dumps(dict(mode=mode,pid=p.process.pid,caps=caps,seconds=dt,byte_rate=rate,
            control_delta=delta,pcm_sequence=p._sequences['pcm'],sample_max=max(abs(v) for v in values))))
        assert 192000*.9<=rate<=192000*1.1
        assert delta['status']==delta['direct_state']==delta['direct_progress']==0
        assert p._last_pcm>pcm_at and not p.fatal_error and not m.worker_recovery.attempts
        if mode=='D':assert p._sequences['pcm']==0 and p.output.read(0) is None
        mux.detach(0);m.release(a.id,0);step(m,mux,lambda:not m.pipelines)
        with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for worker in remaining+([p] if p else []):finish(worker)
        sinks.close();db.close()


def test_native_alsa_direct_mux_unavailable_reconnect_revoke(gst,tmp_path):
    require(gst,'alsasrc')
    db=Database(tmp_path/'reconnect.db');m=SourceManager(db,DirectHttpFactory(tmp_path,alsa=True),profile_files=profile_store(tmp_path))
    a=db.save('sources',InputSourceConfig(name='null',type='ALSA',config={'device':'null'}))
    mux=DirectMux(m,tmp_path);sinks=None;p=None
    try:
        acquire(m,a.id,0);attach(mux,a.id);p=m.pipelines[a.id]
        step(m,mux,lambda:p.direct_progress.get('bytes',0)>192000)
        before=p.direct_progress['bytes'];pid=p.process.pid
        step(m,mux,lambda:p.direct_progress.get('bytes',0)>before+192000*2)
        assert not p.fatal_error and not m.worker_recovery.attempts
        sinks=Sinks(tmp_path);step(m,mux,lambda:sinks.counts[0]>30000)
        # Actual mux endpoint goes away; capture progress must stay independent.
        sinks.close();sinks=None
        for ch in range(8):(tmp_path/f'pcm{ch}.sock').unlink()
        before=p.direct_progress['bytes'];step(m,mux,lambda:p.direct_progress.get('bytes',0)>before+192000*2)
        assert p.process.pid==pid and not p.fatal_error and not m.worker_recovery.attempts
        sinks=Sinks(tmp_path);step(m,mux,lambda:sinks.counts[0]>30000)
        mux.detach(0);step(m,mux,lambda:mux.owner is None and p.direct_applied[1] is None)
        sessions=sinks.sessions[0];hb=p.control_counts.get('heartbeat',0)
        step(m,mux,lambda:p.control_counts.get('heartbeat',0)>hb);assert sinks.sessions[0]==sessions
        attach(mux,a.id);step(m,mux,lambda:sinks.sessions[0]>sessions)
        assert p.process.pid==pid
        mux.detach(0);m.release(a.id,0);step(m,mux,lambda:not m.pipelines)
        print('ALSA_MUX_RECONNECT',pid,'same worker, revoked/regranted')
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for worker in remaining+([p] if p else []):finish(worker)
        if sinks:sinks.close()
        db.close()


def test_native_alsa_direct_missing_device_error_and_source_apply(gst,tmp_path):
    require(gst,'alsasrc')
    db=Database(tmp_path/'missing.db');m=SourceManager(db,DirectHttpFactory(tmp_path,alsa=True),profile_files=profile_store(tmp_path))
    a=db.save('sources',InputSourceConfig(name='missing',type='ALSA',config={'device':'hw:CARD=FMHEADEND_MISSING_CAPTURE,DEV=0'}))
    mux=DirectMux(m,tmp_path);sinks=Sinks(tmp_path);owners=[]
    try:
        try:acquire(m,a.id,0)
        except DependencyUnavailable:pass
        attach(mux,a.id);old=m.pipelines[a.id];owners.append(old)
        step(m,mux,lambda:old.snapshot().get('last_error'))
        error=old.snapshot()['last_error'];assert 'ALSA capture' in error and 'FMHEADEND_MISSING_CAPTURE' in error
        assert not old._has_pcm and sinks.counts[0]==0
        assert old.snapshot()['error_scope']=='SOURCE'
        # Correct the source configuration, not global ALSA configuration.
        changed=a.model_copy(update={'config':dict(a.config,device='null')})
        m._restart_audio(changed);step(m,mux,lambda:m.pipelines[a.id] is not old)
        current=m.pipelines[a.id];owners.append(current)
        step(m,mux,lambda:current._has_pcm and sinks.counts[0]>30000)
        assert current.output is old.output and current.source.config['device']=='null'
        with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
        mux.detach(0);m.release(a.id,0);step(m,mux,lambda:not m.pipelines)
        print('ALSA_DEVICE_ERROR',error)
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for p in owners+remaining:finish(p)
        sinks.close();db.close()


@pytest.mark.parametrize('fault',['crash','native-stall'])
def test_native_alsa_direct_recovery_sibling_apply_stop(gst,tmp_path,http_files,fault):
    require(gst,'alsasrc','souphttpsrc','wavparse','fmheadendlimiter','fmheadendclipper')
    folder,url=http_files;wav(folder/'sibling.wav',8000,120)
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):return NativeStallWorker(s,direct_runtime=self.runtime,**kw) if s.type=='ALSA' else ProcessPipeline(s,**kw)
    db=Database(tmp_path/'recovery.db');m=SourceManager(db,Factory(tmp_path,alsa=True),profile_files=profile_store(tmp_path))
    a=db.save('sources',InputSourceConfig(name='capture',type='ALSA',config={'device':'null','channel_selection':'stereo'}))
    b=db.save('sources',InputSourceConfig(name='sibling',type='HTTP',config={'uri':url+'/sibling.wav'}))
    sinks=Sinks(tmp_path);mux=DirectMux(m,tmp_path);owners=[]
    try:
        acquire(m,a.id,0);acquire(m,b.id,1);attach(mux,a.id)
        old=m.pipelines[a.id];other=m.pipelines[b.id];owners.extend((old,other))
        step(m,mux,lambda:old._has_pcm and other._has_pcm and sinks.counts[0]>30000)
        stable=old.output;status=old.recovery_observation()[0];progress=other.recovery_observation()[1];sessions=sinks.sessions[0]
        fault_at=time.monotonic();os.kill(old.process.pid,signal.SIGKILL if fault=='crash' else signal.SIGUSR1)
        if fault=='native-stall':
            step(m,mux,lambda:old.recovery_observation()[0]>status)
            before=time.monotonic();m.status(a.id);assert time.monotonic()-before<.5
        step(m,mux,lambda:m.pipelines[a.id] is not old)
        current=m.pipelines[a.id];owners.append(current)
        assert old._done.is_set() and current.output is stable and current.generation!=old.generation
        with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
        step(m,mux,lambda:current._has_pcm and sinks.sessions[0]>sessions)
        recovered=time.monotonic()-fault_at
        assert m.pipelines[b.id] is other and other.process.poll() is None and other.recovery_observation()[1]>progress
        profile=file_profile('ALSA apply').model_dump(mode='json')
        profile['stages']['limiter']['selected']='FM_HEADEND_LIMITER'
        profile['stages']['clipper']['selected']='FM_HEADEND_CLIPPER'
        saved=m.profile_files.create(profile)
        changed=a.model_copy(update={'input_gain_db':-6,'use_default_audio_profile':False,'audio_profile_id':saved.path})
        m._restart_audio(changed);step(m,mux,lambda:m.pipelines[a.id] is not current)
        current=m.pipelines[a.id];owners.append(current);step(m,mux,lambda:current._has_pcm)
        assert current.output is stable and current.audio_profile_id==saved.path
        changed=changed.model_copy(update={'config':dict(changed.config,channel_selection='mono')})
        previous=current;m._restart_audio(changed);step(m,mux,lambda:m.pipelines[a.id] is not previous)
        current=m.pipelines[a.id];owners.append(current);step(m,mux,lambda:current._has_pcm)
        assert current.source.config['channel_selection']=='mono'
        before=time.monotonic();mux.detach(0);m.release(a.id,0);stop_seconds=time.monotonic()-before;assert stop_seconds<.5
        step(m,mux,lambda:a.id not in m.pipelines);assert other.process.poll() is None
        m.release(b.id,1);step(m,mux,lambda:not m.pipelines)
        print('ALSA_RECOVERY',json.dumps(dict(fault=fault,recovery_seconds=recovered,stop_seconds=stop_seconds,processes=[(p.process.pid,p.exit_code) for p in owners])))
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for p in owners+remaining:
            finish(p)
            with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
        sinks.close();db.close()
