import sys,os,socket,threading,time,struct,json,signal
from collections import deque
import pytest
from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.direct_pcm import DirectHttpFactory,DirectMux
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.worker_recovery import RecoveryPolicy
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.errors import SourcePending
from fmheadendd.tests.test_gstreamer import gst,http_files,require,file_profile
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_local_worker_native import wav
from fmheadendd.tests.test_worker_recovery_native import NativeStallWorker
from fmheadendd.tests.test_audio_worker import until,finish

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native LOCAL direct/PCM1')


class SocketSink:
    def __init__(self,root):
        self.stop=threading.Event();self.records=deque(maxlen=4000);self.sessions=0
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.listener.bind(str(root/'pcm0.sock'));self.listener.listen();self.listener.settimeout(.1)
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        while not self.stop.is_set():
            try:c,_=self.listener.accept()
            except socket.timeout:continue
            self.sessions+=1;c.settimeout(.1)
            with c:
                pending=b''
                while not self.stop.is_set():
                    try:data=c.recv(65536)
                    except socket.timeout:continue
                    if not data:break
                    pending+=data;n=len(pending)//4*4
                    if n:self.records.append((time.monotonic_ns(),pending[:n]));pending=pending[n:]
    def close(self):
        self.stop.set();self.thread.join(2);self.listener.close();assert not self.thread.is_alive()


@pytest.mark.parametrize('mode',['C','D'])
def test_local_two_transitions_pcm_and_metadata(gst,tmp_path,mode,monkeypatch):
    require(gst,'playbin3','wavparse','volume')
    monkeypatch.setenv('FMHEADEND_TEST_LOCAL_TRACE',str(tmp_path/'frames.jsonl'))
    folder=tmp_path/'tracks';folder.mkdir()
    for i,value in enumerate((5000,10000,15000),1):wav(folder/f'track{i}.wav',value,3)
    class Recording(NativeStallWorker):
        worker_module='fmheadendd.tests.audiod_local_direct_fixture'
        def __init__(self,*a,**kw):super().__init__(*a,**kw);self.metadata=[]
        def _accept(self,channel,message,pcm=b''):
            super()._accept(channel,message,pcm)
            title=self.snapshot().get('title')
            if title and (not self.metadata or self.metadata[-1][1]!=title):self.metadata.append((time.monotonic_ns(),title))
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):return Recording(s,direct_runtime=self.runtime if mode=='D' else None,**kw)
    db=Database(tmp_path/'config.db');m=SourceManager(db,Factory(tmp_path,local=True),profile_files=profile_store(tmp_path))
    profile=m.profile_files.create(file_profile('local direct').model_dump(mode='json'))
    s=db.save('sources',InputSourceConfig(name='local',type='LOCAL',config={'mode':'folder','path':str(folder),'order':'sequential'},use_default_audio_profile=False,audio_profile_id=profile.path))
    sink=SocketSink(tmp_path);mux=DirectMux(m,tmp_path) if mode=='D' else UnixMuxClient(m.output_for,tmp_path)
    def step(predicate):
        def check():
            m.worker_recovery.service()
            if mode=='D':mux.service()
            return predicate()
        return until(check,timeout=22)
    def fourth_track_received():
        if len(p.metadata)<4 or not sink.records:
            return False
        data=sink.records[-1][1]
        left,right=struct.unpack_from('<hh',data,len(data)-4)
        return abs(left-5000)<=2 and abs(right-5000)<=2
    try:
        try:m.acquire(s.id,0)
        except SourcePending:pass
        p=m.pipelines[s.id]
        step(lambda:p._has_pcm)
        try:mux.attach(0,s.id)
        except SourcePending:pass
        # Parent metadata can arrive before the PCM1 writer and receiver run.
        step(fourth_track_received)
        assert [t for _,t in p.metadata[:4]]==['track1','track2','track3','track1']
        assert m.pipelines[s.id] is p and p.process.returncode is None and not m.worker_recovery.attempts
        if mode=='D':assert p._sequences['pcm']==0 and p.output.read(0) is None
        # Classify every complete received stereo frame, including split socket records.
        segments=[];byte_count=0;records=list(sink.records)
        previous_at=records[0][0]
        for at,data in records:
            samples=struct.unpack('<'+'h'*(len(data)//2),data)
            for left,right in zip(samples[::2],samples[1::2]):
                # Independent float->S16 dithering also exists in unchanged PCM1.
                track=min(range(1,4),key=lambda n:abs(left-5000*n))
                assert abs(left-5000*track)<=2
                assert abs(right-5000*track)<=2
                if not segments or segments[-1]['track']!=track:segments.append(dict(track=track,at=at,bytes=0,boundary_gap_s=(at-previous_at)/1e9))
                segments[-1]['bytes']+=4;byte_count+=4
            previous_at=at
        assert [v['track'] for v in segments[:4]]==[1,2,3,1]
        # Middle tracks are complete: no new-transport skip/duplication of track2/3.
        assert segments[1]['bytes']==segments[2]['bytes']==3*192000
        if mode=='D':
            # First-frame kernel send precedes metadata; receiver observation may lag it.
            for (_,title),segment in zip(p.metadata[:3],segments[:3]):assert title==f'track{segment["track"]}'
            frames=[json.loads(l) for l in (tmp_path/'frames.jsonl').read_text().splitlines()]
            assert [f['title'] for f in frames[:4]]==['track1','track2','track3','track1']
            assert any(title=='track1' and at<frames[1]['produced'] for at,title in frames[1]['handoffs'])
            assert any(title=='track2' and at<frames[2]['produced'] for at,title in frames[2]['handoffs'])
            for (at,title),frame in zip(p.metadata[:4],frames[:4]):
                assert frame['title']==title and at>=frame['at']
                assert 0 < frame['produced'] <= frame['at']
                left,right=struct.unpack('<hh',bytes.fromhex(frame['prefix']))
                expected=5000*int(title[-1]);assert abs(left-expected)<=2 and abs(right-expected)<=2
            print('LOCAL_FRAME_ASSOCIATION',json.dumps(frames[:4]))
        gaps=[(b[0]-a[0])/1e9 for a,b in zip(records,records[1:])]
        result=dict(mode=mode,segments=segments[:4],metadata=p.metadata[:4],max_socket_gap=max(gaps),counts=p.control_counts,bytes=byte_count,pid=p.process.pid)
        print('LOCAL_TRANSITIONS',json.dumps(result));(tmp_path/'transitions.json').write_text(json.dumps(result))
        mux.detach(0);m.release(s.id,0);step(lambda:not m.pipelines)
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for p in remaining:finish(p)
        sink.close();db.close()


def test_local_direct_missing_media_wait_and_stop(gst,tmp_path):
    require(gst,'playbin3','wavparse')
    media=tmp_path/'absent';db=Database(tmp_path/'wait.db')
    m=SourceManager(db,DirectHttpFactory(tmp_path,local=True),profile_files=profile_store(tmp_path))
    a=db.save('sources',InputSourceConfig(name='missing',type='LOCAL',config={'mode':'folder','path':str(media)}))
    mux=DirectMux(m,tmp_path);sink=SocketSink(tmp_path);p=None
    def step(predicate, timeout=15):
        def poll():m.worker_recovery.service();mux.service();return predicate()
        return until(poll,timeout=timeout)
    try:
        try:m.acquire(a.id,0)
        except SourcePending:pass
        try:mux.attach(0,a.id)
        except SourcePending:pass
        p=m.pipelines[a.id];step(lambda:p.expected_media_wait)
        pcm_at=p.recovery_observation()[1]
        pcm_timeout=m.worker_recovery.policy.pcm_timeout
        hb=p.control_counts.get('heartbeat',0)
        # Cross the production PCM deadline without making it shorter than the
        # direct worker's heartbeat interval during playback.
        step(lambda:m.clock()-pcm_at>pcm_timeout and p.control_counts.get('heartbeat',0)>=hb+2,
             timeout=pcm_timeout+5)
        assert m.pipelines[a.id] is p and not m.worker_recovery.attempts
        media.mkdir();wav(media/'returned.wav',8000,2)
        step(lambda:p._has_pcm and bool(sink.records) and mux.owner is p)
        assert m.pipelines[a.id] is p and not m.worker_recovery.attempts
        mux.detach(0);m.release(a.id,0);step(lambda:not m.pipelines)
        # STOP during discovery on another missing source is also asynchronous.
        b=db.save('sources',a.model_copy(update={'id':None,'config':{'mode':'folder','path':str(tmp_path/'never')}}))
        try:m.acquire(b.id,0)
        except SourcePending:pass
        pending=m.pipelines[b.id];step(lambda:pending.expected_media_wait)
        before=time.monotonic();m.release(b.id,0);assert time.monotonic()-before<.5
        step(lambda:not m.pipelines);finish(pending)
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for worker in remaining+([p] if p else []):finish(worker)
        sink.close();db.close()


@pytest.mark.parametrize('fault',['crash','native-stall'])
def test_local_direct_recovery_revoke_apply_and_boundary_stop(gst,http_files,tmp_path,fault):
    require(gst,'playbin3','wavparse','souphttpsrc','fmheadendlimiter','fmheadendclipper')
    folder,url=http_files;wav(folder/'sibling.wav',8000,60)
    media=tmp_path/'tracks';media.mkdir()
    for n in range(1,4):wav(media/f'track{n}.wav',5000*n,3)
    class Factory(DirectHttpFactory):
        def __call__(self,s,**kw):
            return (ProcessPipeline(s,**kw) if s.type=='HTTP' else NativeStallWorker(s,direct_runtime=self.runtime,**kw))
    db=Database(tmp_path/'recover.db');m=SourceManager(db,Factory(tmp_path,local=True),profile_files=profile_store(tmp_path))
    a=db.save('sources',InputSourceConfig(name='local',type='LOCAL',config={'mode':'folder','path':str(media),'order':'sequential'}))
    sibling=db.save('sources',InputSourceConfig(name='sibling',type='HTTP',config={'uri':url+'/sibling.wav'}))
    sink=SocketSink(tmp_path);mux=DirectMux(m,tmp_path);owners=[]
    m.worker_recovery.policy=RecoveryPolicy(pcm_timeout=2,buffering_timeout=5)
    def step(predicate):
        def poll():m.worker_recovery.service();mux.service();return predicate()
        return until(poll,timeout=18)
    def attach():
        try:mux.attach(0,a.id)
        except SourcePending:pass
    try:
        for sid,tx in ((a.id,0),(sibling.id,1)):
            try:m.acquire(sid,tx)
            except SourcePending:pass
        attach();step(lambda:len(sink.records)>10)
        old=m.pipelines[a.id];other=m.pipelines[sibling.id];owners.extend((old,other))
        stable=old.output;status=old.recovery_observation()[0];progress=other.recovery_observation()[1]
        os.kill(old.process.pid,signal.SIGKILL if fault=='crash' else signal.SIGUSR1)
        if fault=='native-stall':step(lambda:old.recovery_observation()[0]>status)
        step(lambda:m.pipelines[a.id] is not old)
        current=m.pipelines[a.id];owners.append(current)
        assert current.output is stable and current.generation!=old.generation and old._done.is_set()
        with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
        step(lambda:mux.owner is current and current.direct_applied[1] is not None and current._has_pcm)
        assert m.pipelines[sibling.id] is other and other.recovery_observation()[1]>progress
        # Explicit revoke proves closure, then regrant without recreating the decoder.
        mux.detach(0);step(lambda:mux.owner is None and current.direct_applied[1] is None)
        sessions=sink.sessions;heartbeat=current.control_counts.get('heartbeat',0)
        step(lambda:current.control_counts.get('heartbeat',0)>heartbeat)
        assert sink.sessions==sessions
        attach();step(lambda:sink.sessions>sessions)
        # Native DSP profile apply and gain, without a manual API/lifecycle retry.
        profile=file_profile('direct limiter').model_dump(mode='json')
        profile['stages']['limiter']['selected']='FM_HEADEND_LIMITER'
        profile['stages']['clipper']['selected']='FM_HEADEND_CLIPPER'
        saved=m.profile_files.create(profile)
        changed=a.model_copy(update={'use_default_audio_profile':False,'audio_profile_id':saved.path,'input_gain_db':-6})
        m._restart_audio(changed);step(lambda:m.pipelines[a.id] is not current)
        current=m.pipelines[a.id];owners.append(current)
        step(lambda:current.snapshot().get('title')=='track2' and current._has_pcm)
        assert current.output is stable and current.audio_profile_id==saved.path
        # Feed/source replacement is another ordered generation; STOP at its first boundary.
        changed=changed.model_copy(update={'config':{'mode':'folder','path':str(media),'order':'random'}})
        previous=current;m._restart_audio(changed);step(lambda:m.pipelines[a.id] is not previous)
        current=m.pipelines[a.id];owners.append(current)
        step(lambda:current._has_pcm and current.snapshot().get('title') is not None)
        title=current.snapshot()['title'];step(lambda:current.snapshot().get('title')!=title)
        before=time.monotonic();mux.detach(0);m.release(a.id,0)
        assert time.monotonic()-before<.5
        step(lambda:a.id not in m.pipelines)
        assert other.process.poll() is None
        m.release(sibling.id,1);step(lambda:not m.pipelines)
        print('LOCAL_RECOVERY',fault,'reaped',[(p.process.pid,p.exit_code) for p in owners])
    finally:
        remaining=list(m.pipelines.values());mux.close();m.close()
        for p in owners+remaining:
            finish(p)
            if p.process:
                with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
        sink.close();db.close()
