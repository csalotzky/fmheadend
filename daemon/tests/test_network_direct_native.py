"""Paced loopback UDP/RTP into real direct workers; no hardware or public URLs."""
import json,math,os,signal,socket,struct,sys,time,wave

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import SourcePending
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.direct_pcm import DirectHttpFactory,DirectMux
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_worker import until,finish
from fmheadendd.tests.test_gstreamer import gst,http_files,require,file_profile,free_udp_port
from fmheadendd.tests.test_local_worker_native import wav
from fmheadendd.tests.test_local_direct_native import SocketSink
from fmheadendd.tests.test_direct_multitx_native import Sinks
from fmheadendd.tests.test_worker_recovery_native import NativeStallWorker
from fmheadendd.tests.test_network_worker_native import transport

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native paced direct UDP/RTP')


def paced(gst,mode,folder,port,seconds=180,left=8000,right=-4000,tone=False):
    """Existing supported raw/L16 configuration; wavparse PTS + sync udpsink pace it."""
    require(gst,'wavparse','udpsink')
    path=folder/f'paced-{port}.wav'
    block=(b''.join(struct.pack('<hh',int(left*math.sin(2*math.pi*440*i/48000)),int(right*math.sin(2*math.pi*880*i/48000))) for i in range(48000))
        if tone else struct.pack('<hh',left,right)*48000)
    with wave.open(str(path),'wb') as out:
        out.setparams((2,2,48000,0,'NONE',''))
        for _ in range(seconds):out.writeframes(block)
    values=dict(host='127.0.0.1',port=port)
    if mode=='UDP':
        values.update(format='S16LE',sample_rate=48000,channels=2)
        chain='audio/x-raw,format=S16LE,rate=48000,channels=2'
    else:
        require(gst,'rtpL16pay','rtpL16depay','rtpjitterbuffer')
        values.update(codec='PCM',clock_rate=48000,channels=2,payload_type=96)
        chain='audio/x-raw,format=S16BE,rate=48000,channels=2 ! rtpL16pay pt=96'
    sender=gst.parse_launch('filesrc name=input ! wavparse ! audioconvert ! '+chain+
        f' ! udpsink name=target host=127.0.0.1 port={port} sync=true')
    sender.get_by_name('input').set_property('location',str(path))
    return InputSourceConfig(name=mode,type='UDP' if mode=='UDP' else 'RTP',config=values),sender


def acquire(m,sid,ch):
    try:m.acquire(sid,ch)
    except SourcePending:pass


def attach(mux,sid,ch=0):
    try:mux.attach(ch,sid)
    except SourcePending:pass


def step(m,mux,predicate,timeout=25):
    def poll():
        m.worker_recovery.service()
        if isinstance(mux,DirectMux):mux.service()
        return predicate()
    return until(poll,timeout=timeout)


@pytest.mark.parametrize('mode,backend',[('UDP','C'),('UDP','D'),('PCM','C'),('PCM','D'),('OPUS','D'),('MP3','D'),('AAC','D')])
def test_native_paced_network_pcm(gst,tmp_path,mode,backend):
    if mode in ('UDP','PCM'):spec,sender=paced(gst,mode,tmp_path,free_udp_port(),seconds=40)
    else:
        required={'OPUS':('opusenc','rtpopuspay','rtpopusdepay','opusdec'),
            'MP3':('lamemp3enc','mpegaudioparse','rtpmpapay','rtpmpadepay','mpg123audiodec'),
            'AAC':('avenc_aac','aacparse','rtpmp4gpay','rtpmp4gdepay','avdec_aac')}
        require(gst,*required[mode]);spec,sender=transport(gst,mode,free_udp_port())
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):return Observed(s,direct_runtime=self.runtime if backend=='D' else None,**kw)
    class Observed(ProcessPipeline):
        def __init__(self,*args,**kw):super().__init__(*args,**kw);self.status_events=[];self.metadata_events=[]
        def _accept(self,channel,message,pcm=b''):
            super()._accept(channel,message,pcm)
            if message['kind']=='status':self.status_events.append(message['payload']['status'])
            if message['kind']=='direct_state' and message['payload']['metadata'] is not None:
                self.metadata_events.append(message['payload']['metadata'])
    db=Database(tmp_path/'network.db');m=SourceManager(db,Factory(tmp_path,network=True),profile_files=profile_store(tmp_path))
    profile=m.profile_files.create(file_profile('network samples').model_dump(mode='json'))
    a=db.save('sources',spec.model_copy(update={'input_gain_db':-6,'use_default_audio_profile':False,'audio_profile_id':profile.path}))
    sink=SocketSink(tmp_path);mux=DirectMux(m,tmp_path) if backend=='D' else UnixMuxClient(m.output_for,tmp_path);p=None
    def received():return sum(len(data) for _,data in list(sink.records))
    try:
        acquire(m,a.id,0);attach(mux,a.id);p=m.pipelines[a.id]
        step(m,mux,lambda:p._prepared)  # No sender yet: no fake PCM generation.
        assert not sink.records
        sender.set_state(gst.State.PLAYING)
        step(m,mux,lambda:received()>50000 and p.snapshot().get('output_caps') and p.control_counts.get('heartbeat',0)>=3)
        caps=p.snapshot()['output_caps'];assert 'S16LE' in caps and '48000' in caps and 'channels=(int)2' in caps and 'interleaved' in caps
        records=list(sink.records);data=b''.join(d for _,d in records[-10:]);assert data and len(data)%4==0
        samples=struct.unpack('<'+'h'*(len(data)//2),data)
        if mode in ('UDP','PCM'):
            assert max(abs(v-8000*10**(-6/20)) for v in samples[::2])<=3
            assert max(abs(v+4000*10**(-6/20)) for v in samples[1::2])<=3
        else:assert 100<math.sqrt(sum(v*v for v in samples)/len(samples))<10000
        before=dict(p.control_counts);count=received();at=time.monotonic()
        step(m,mux,lambda:p.control_counts.get('heartbeat',0)>=before['heartbeat']+3,timeout=6)
        dt=time.monotonic()-at;rate=(received()-count)/dt
        delta={k:p.control_counts.get(k,0)-before.get(k,0) for k in ('status','direct_state','direct_progress','heartbeat')}
        print('NETWORK_PCM',json.dumps(dict(mode=mode,backend=backend,pid=p.process.pid,caps=caps,byte_rate=rate,seconds=dt,
            control_delta=delta,pcm_sequence=p._sequences['pcm'],sample_rms=math.sqrt(sum(v*v for v in samples)/len(samples)))))
        assert 192000*.9<=rate<=192000*1.1
        status_diffs=[{k:(old.get(k),new.get(k)) for k in new if old.get(k)!=new.get(k)} for old,new in zip(p.status_events,p.status_events[1:])]
        print('NETWORK_STATE_CHANGES',mode,json.dumps(status_diffs))
        if mode in ('UDP','PCM','OPUS'):assert delta['status']==delta['direct_state']==delta['direct_progress']==0
        else:
            # VBR codec tags can legitimately change. Every event must then
            # contain an actual change, rather than repeating the same tree.
            assert all(status_diffs) and delta['direct_progress']==0
            assert all(old!=new for old,new in zip(p.metadata_events,p.metadata_events[1:]))
        if backend=='D':assert p._sequences['pcm']==0 and p.output.read(0) is None
        if backend=='D' and mode in ('UDP','PCM'):
            sender.set_state(gst.State.PAUSED);hb=p.control_counts['heartbeat']
            step(m,mux,lambda:p.control_counts['heartbeat']>=hb+2)
            frozen=p._last_pcm;count=received();hb=p.control_counts['heartbeat'];status=dict(p.control_counts)
            step(m,mux,lambda:p.control_counts['heartbeat']>=hb+1)
            assert p._last_pcm==frozen and received()==count and m.pipelines[a.id] is p
            assert not m.worker_recovery.attempts and p.control_counts['status']==status['status']
            sender.set_state(gst.State.PLAYING);step(m,mux,lambda:received()>count+30000)
            assert m.pipelines[a.id] is p
            print('NETWORK_PAUSE_RESUME',mode,'same PID',p.process.pid)
        sender.set_state(gst.State.NULL);mux.detach(0);m.release(a.id,0);step(m,mux,lambda:not m.pipelines)
        with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
    finally:
        sender.set_state(gst.State.NULL);remaining=list(m.pipelines.values());mux.close();m.close()
        for worker in remaining+([p] if p else []):finish(worker)
        sink.close();db.close()


def test_native_network_default_timeout_recovery_and_lifecycle(gst,tmp_path,http_files):
    """One real 90 s window: UDP starvation and responsive-IPC RTP callback stall."""
    require(gst,'souphttpsrc','wavparse','fmheadendlimiter','fmheadendclipper')
    udp,udp_sender=paced(gst,'UDP',tmp_path,free_udp_port(),seconds=240)
    rtp,rtp_sender=paced(gst,'PCM',tmp_path,free_udp_port(),seconds=240,left=6000,right=-3000)
    folder,url=http_files;wav(folder/'sibling.wav',12000,240)
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):
            cls=NativeStallWorker if s.type in ('UDP','RTP') else ProcessPipeline
            return cls(s,direct_runtime=self.runtime,**kw)
    db=Database(tmp_path/'lifecycle.db');m=SourceManager(db,Factory(tmp_path,network=True),profile_files=profile_store(tmp_path))
    profile=m.profile_files.create(file_profile('network lifecycle').model_dump(mode='json'))
    specs=[udp,rtp,InputSourceConfig(name='http sibling',type='HTTP',config={'uri':url+'/sibling.wav'})]
    a,b,c=[db.save('sources',s.model_copy(update={'use_default_audio_profile':False,'audio_profile_id':profile.path})) for s in specs]
    mux=DirectMux(m,tmp_path);sinks=Sinks(tmp_path);owners=[]
    def service(predicate,timeout=30):return step(m,mux,predicate,timeout)
    try:
        for ch,s in enumerate((a,b,c)):acquire(m,s.id,ch);attach(mux,s.id,ch)
        udp_sender.set_state(gst.State.PLAYING);rtp_sender.set_state(gst.State.PLAYING)
        service(lambda:all(sinks.counts[ch]>30000 for ch in range(3)))
        first=m.pipelines[a.id];victim=m.pipelines[b.id];sibling=m.pipelines[c.id];owners.extend((first,victim,sibling))
        outputs={s.id:m.pipelines[s.id].output for s in (a,b,c)}
        for ch,(left,right) in enumerate(((8000,-4000),(6000,-3000),(12000,12000))):
            vals=struct.unpack('<'+'h'*(len(sinks.samples[ch])//2),bytes(sinks.samples[ch]))
            assert max(abs(v-left) for v in vals[::2])<=3 and max(abs(v-right) for v in vals[1::2])<=3
        # Genuine SIGKILL and rebind on the same UDP port.
        os.kill(first.process.pid,signal.SIGKILL);service(lambda:m.pipelines[a.id] is not first)
        current=m.pipelines[a.id];owners.append(current)
        assert first._done.is_set() and current.output is outputs[a.id]
        with pytest.raises(ChildProcessError):os.waitpid(first.process.pid,os.WNOHANG)
        service(lambda:current._has_pcm)
        # One long default-policy observation covers absence and native stall.
        assert m.worker_recovery.policy==RecoveryPolicy()
        udp_sender.set_state(gst.State.PAUSED)
        before_status=victim.recovery_observation()[0];fault_at=time.monotonic()
        os.kill(victim.process.pid,signal.SIGUSR1)
        service(lambda:victim.recovery_observation()[0]>before_status)
        siblings_before=sinks.counts[2];latest=[time.monotonic()]
        def recovered():
            assert m.pipelines[c.id] is sibling and sibling.process.poll() is None
            assert time.monotonic()-sibling._last_pcm<2.5
            latest[0]=time.monotonic()
            return m.pipelines[a.id] is not current and m.pipelines[b.id] is not victim
        service(recovered,timeout=110)
        assert time.monotonic()-fault_at>=89 and sinks.counts[2]>siblings_before
        for old,s in ((current,a),(victim,b)):
            assert old._done.is_set()
            with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
            assert m.pipelines[s.id].generation!=old.generation and m.pipelines[s.id].output is outputs[s.id]
        udp_current=m.pipelines[a.id];rtp_current=m.pipelines[b.id];owners.extend((udp_current,rtp_current))
        udp_sender.set_state(gst.State.PLAYING);service(lambda:udp_current._has_pcm and rtp_current._has_pcm)
        print('NETWORK_DEFAULT_RECOVERY',json.dumps(dict(seconds=time.monotonic()-fault_at,udp_old=current.process.pid,rtp_old=victim.process.pid,
            udp_new=udp_current.process.pid,rtp_new=rtp_current.process.pid,http_pid=sibling.process.pid)))
        # Profile apply and source-port replacement use the existing async owner path.
        raw=file_profile('network limiter').model_dump(mode='json');raw['stages']['limiter']['selected']='FM_HEADEND_LIMITER';raw['stages']['clipper']['selected']='FM_HEADEND_CLIPPER'
        saved=m.profile_files.create(raw);changed=a.model_copy(update={'input_gain_db':-6,'audio_profile_id':saved.path})
        m._restart_audio(changed);service(lambda:m.pipelines[a.id] is not udp_current)
        current=m.pipelines[a.id];owners.append(current);service(lambda:current._has_pcm)
        new_port=free_udp_port();changed=changed.model_copy(update={'config':dict(changed.config,port=new_port)})
        previous=current;m._restart_audio(changed);service(lambda:m.pipelines[a.id] is not previous)
        current=m.pipelines[a.id];owners.append(current);udp_sender.get_by_name('target').set_property('port',new_port);service(lambda:current._has_pcm)
        # Independent lease revoke/regrant while source ownership remains held.
        mux.detach(0);service(lambda:mux.channels[0].owner is None and current.direct_applied[1] is None)
        sessions=sinks.sessions[0];hb=current.control_counts['heartbeat'];service(lambda:current.control_counts['heartbeat']>hb)
        assert sinks.sessions[0]==sessions
        attach(mux,a.id);service(lambda:sinks.sessions[0]>sessions)
        # An isolated mux restart closes all endpoints, not the source processes.
        ids={sid:p.process.pid for sid,p in m.pipelines.items()};progress=current.direct_progress['bytes']
        sinks.close();sinks=None
        for ch in range(8):(tmp_path/f'pcm{ch}.sock').unlink()
        service(lambda:current.direct_progress['bytes']>progress+192000)
        assert {sid:p.process.pid for sid,p in m.pipelines.items()}==ids
        sinks=Sinks(tmp_path);service(lambda:all(sinks.counts[ch]>30000 for ch in range(3)))
        assert all(p._sequences['pcm']==0 for p in m.pipelines.values())
        before=time.monotonic()
        for ch,s in enumerate((a,b,c)):mux.detach(ch);m.release(s.id,ch)
        assert time.monotonic()-before<.5
        service(lambda:not m.pipelines)
        # Actual sockets are free after all receiving children were reaped.
        for port in (a.config['port'],b.config['port'],new_port):
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:probe.bind(('127.0.0.1',port))
        print('NETWORK_LIFECYCLE_REAP',[(p.process.pid,p.exit_code) for p in owners])
    finally:
        udp_sender.set_state(gst.State.NULL);rtp_sender.set_state(gst.State.NULL)
        remaining=list(m.pipelines.values());mux.close();m.close()
        for p in owners+remaining:
            finish(p)
            if p.process:
                with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
        if sinks:sinks.close()
        db.close()
