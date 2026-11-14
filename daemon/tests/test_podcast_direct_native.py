"""Loopback RSS, actual audiod/GStreamer, direct sockets and disposable SQLite v1."""
import json,os,signal,struct,sys,time
from datetime import datetime

import pytest

from fmheadendd.database import Database
from fmheadendd.errors import SourcePending
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.direct_pcm import DirectHttpFactory,DirectMux
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.podcast_history import PodcastHistory
from fmheadendd.mux.unix_client import UnixMuxClient
from fmheadendd.tests.profile_support import profile_store
from fmheadendd.tests.test_audio_worker import until,finish
from fmheadendd.tests.test_gstreamer import gst,http_files,require,file_profile
from fmheadendd.tests.test_local_worker_native import wav
from fmheadendd.tests.test_local_direct_native import SocketSink
from fmheadendd.tests.test_podcast_playback_native import server,source
from fmheadendd.tests.test_worker_recovery_native import NativeStallWorker

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native PODCAST direct PCM')


def media_bytes(path,value=8000,seconds=3):
    wav(path,value,seconds)
    return path.read_bytes()


class Recording(ProcessPipeline):
    worker_module='fmheadendd.tests.audiod_podcast_direct_fixture'
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.metadata=[];self.played=[]
    def _accept(self,channel,message,pcm=b''):
        super()._accept(channel,message,pcm)
        if message['generation']!=self.generation:return
        if message['kind']=='podcast_played':self.played.append(message['payload'])
        title=self.snapshot().get('title')
        if title and (not self.metadata or title!=self.metadata[-1][1]):self.metadata.append((time.monotonic_ns(),title))


@pytest.mark.parametrize('mode',['C','D'])
def test_native_podcast_direct_transitions(gst,tmp_path,monkeypatch,mode):
    require(gst,'playbin3','wavparse','souphttpsrc')
    trace=tmp_path/'trace.jsonl';monkeypatch.setenv('FMHEADEND_TEST_LOCAL_TRACE',str(trace))
    media={name:media_bytes(tmp_path/f'{name}.wav',5000*n,3) for n,name in enumerate(('A','B','C'),1)}
    with server(media) as (url,state):
        db=Database(tmp_path/'episodes.db')
        class Factory(DirectHttpFactory):
            def __call__(self,s,**kw):return Recording(s,direct_runtime=self.runtime if mode=='D' else None,**kw)
        m=SourceManager(db,Factory(tmp_path,podcast=True),profile_files=profile_store(tmp_path))
        profile=m.profile_files.create(file_profile('podcast samples').model_dump(mode='json'))
        s=source(db,url).model_copy(update={'use_default_audio_profile':False,'audio_profile_id':profile.path})
        s=db.save('sources',s,s.id)
        sink=SocketSink(tmp_path);mux=DirectMux(m,tmp_path) if mode=='D' else UnixMuxClient(m.output_for,tmp_path)
        def step(predicate):
            def poll():
                m.worker_recovery.service()
                if mode=='D':mux.service()
                return predicate()
            return until(poll,timeout=25)
        try:
            try:m.acquire(s.id,0)
            except SourcePending:pass
            p=m.pipelines[s.id]
            try:mux.attach(0,s.id)
            except SourcePending:pass
            step(lambda:len(p.metadata)>=4)
            assert [t for _,t in p.metadata[:3]]==['Episode A','Episode B','Episode C']
            assert p.metadata[3][1]!='Episode C'  # Existing RANDOM bag cycle, no immediate repeat.
            assert m.pipelines[s.id] is p and not m.worker_recovery.attempts
            assert p.snapshot()['artist']=='Example Podcast' and not p.fatal_error
            if mode=='D':assert p._sequences['pcm']==0 and p.output.read(0) is None
            records=list(sink.records);segments=[];previous=records[0][0]
            for at,data in records:
                values=struct.unpack('<'+'h'*(len(data)//2),data)
                for left,right in zip(values[::2],values[1::2]):
                    track=min(range(1,4),key=lambda n:abs(left-5000*n))
                    assert abs(left-5000*track)<=2 and abs(right-5000*track)<=2
                    if not segments or segments[-1]['track']!=track:segments.append(dict(track=track,at=at,bytes=0,gap_s=(at-previous)/1e9))
                    segments[-1]['bytes']+=4
                previous=at
            assert [x['track'] for x in segments[:3]]==[1,2,3]
            assert segments[1]['bytes']==segments[2]['bytes']==3*192000
            rows=[json.loads(l) for l in trace.read_text().splitlines()]
            assert [r['count'] for r in rows if r['kind']=='build']==[1]
            frames=[r for r in rows if r['kind']=='frame']
            if mode=='D':
                assert [f['title'] for f in frames[:3]]==['Episode A','Episode B','Episode C']
                for n,((at,title),f) in enumerate(zip(p.metadata[:3],frames[:3]),1):
                    assert f['title']==title and 0<f['produced']<=f['at']<=at
                    left,right=struct.unpack('<hh',bytes.fromhex(f['prefix']))
                    assert abs(left-5000*n)<=2 and abs(right-5000*n)<=2
                for i in (1,2):assert any(title==p.metadata[i-1][1] and at<frames[i]['produced'] for at,title in frames[i]['handoffs'])
            assert state['requests'].count('/rss')==1 and not p.played
            result=dict(mode=mode,pid=p.process.pid,segments=segments[:4],metadata=p.metadata[:4],frames=frames[:4],counts=p.control_counts,
                max_gap_s=max((b[0]-a[0])/1e9 for a,b in zip(records,records[1:])))
            print('PODCAST_TRANSITIONS',json.dumps(result))
            mux.detach(0);m.release(s.id,0);step(lambda:not m.pipelines)
            assert not p._podcast_service._io_thread.is_alive()
            with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
        finally:
            remaining=list(m.pipelines.values());mux.close();m.close()
            for p in remaining:finish(p)
            sink.close();db.close()


def test_native_podcast_direct_real_history_threshold(gst,tmp_path,monkeypatch):
    require(gst,'playbin3','wavparse')
    trace=tmp_path/'history-trace.jsonl';monkeypatch.setenv('FMHEADEND_TEST_LOCAL_TRACE',str(trace))
    with server({'A':media_bytes(tmp_path/'long.wav',8000,72)}) as (url,state):
        db=Database(tmp_path/'history.db');s=source(db,url,'SMART')
        p=Recording(s,profile=file_profile('history'),podcast_database_path=str(tmp_path/'history.db'),direct_runtime=tmp_path)
        sink=SocketSink(tmp_path);p.set_output_lease(1,1);started=time.monotonic()
        try:
            try:p.start()
            except SourcePending:pass
            h=PodcastHistory(db,s.id)
            until(lambda:sum(v.play_count for v in h.snapshot().values())==1,timeout=85)
            until(lambda:trace.exists() and '"kind": "history"' in trace.read_text())
            assert len(p.played)==1 and p.control_counts['podcast_played']==1
            event=p.played[0];key=(event['episode']['feed_id'],event['episode']['episode_id'])
            stored=h.snapshot()[key]
            assert stored.play_count==1 and stored.last_played_at==datetime.fromisoformat(event['when'])
            assert event['episode']['title']=='Episode A'
            rows=[json.loads(l) for l in trace.read_text().splitlines()];marks=[r for r in rows if r['kind']=='history']
            assert len(marks)==1 and marks[0]['before']<11520000<=marks[0]['bytes'] and marks[0]['count']==1
            assert marks[0]['before']+marks[0]['size']==marks[0]['bytes']
            assert marks[0]['episode_id']==key[1]
            p._podcast_service.request_snapshot();revision=p._podcast_service.snapshot().revision
            until(lambda:p._podcast_service.snapshot().revision>revision)
            before=p.control_counts.get('heartbeat',0);until(lambda:p.control_counts.get('heartbeat',0)>=before+2,timeout=5)
            assert h.snapshot()[key].play_count==1 and len(p.played)==1
            assert db.connection.execute('PRAGMA user_version').fetchone()[0]==1
            assert p._sequences['pcm']==0 and p.output.read(0) is None and sink.records
            print('PODCAST_HISTORY',json.dumps(dict(elapsed=time.monotonic()-started,mark=marks[0],event=event,history_count=stored.play_count,
                last_played_at=stored.last_played_at.isoformat(),schema=1,counts=p.control_counts)))
        finally:
            finish(p);sink.close();db.close()
        assert not p._podcast_service._io_thread.is_alive()
        with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)


@pytest.mark.parametrize('fault',['crash','native-stall'])
def test_native_podcast_direct_recovery_apply_stop(gst,tmp_path,http_files,fault):
    require(gst,'playbin3','wavparse','fmheadendlimiter','fmheadendclipper')
    folder,http_url=http_files;wav(folder/'sibling.wav',8000,120)
    with server({name:media_bytes(tmp_path/f'{name}.wav',5000*n,3) for n,name in enumerate(('A','B','C'),1)}) as (url,state):
        db=Database(tmp_path/'recovery.db')
        class Factory(DirectHttpFactory):
            def __call__(self,s,**kw):
                return NativeStallWorker(s,direct_runtime=self.runtime,**kw) if s.type=='PODCAST' else ProcessPipeline(s,**kw)
        m=SourceManager(db,Factory(tmp_path,podcast=True),profile_files=profile_store(tmp_path))
        a=source(db,url)
        b=db.save('sources',InputSourceConfig(name='sibling',type='HTTP',config={'uri':http_url+'/sibling.wav'}))
        sink=SocketSink(tmp_path);mux=DirectMux(m,tmp_path);owners=[]
        # Use the unchanged production recovery policy/deadlines.
        def step(predicate):
            def poll():m.worker_recovery.service();mux.service();return predicate()
            return until(poll,timeout=32)
        try:
            for sid,tx in ((a.id,0),(b.id,1)):
                try:m.acquire(sid,tx)
                except SourcePending:pass
            try:mux.attach(0,a.id)
            except SourcePending:pass
            old=m.pipelines[a.id];other=m.pipelines[b.id];owners.extend((old,other))
            step(lambda:old._has_pcm and len(sink.records)>10)
            stable=old.output;service=old._podcast_service;thread=service._io_thread
            parser=service._parser;executor=parser._executor
            status=old.recovery_observation()[0];progress=other.recovery_observation()[1]
            sessions=sink.sessions
            fault_at=time.monotonic();os.kill(old.process.pid,signal.SIGKILL if fault=='crash' else signal.SIGUSR1)
            if fault=='native-stall':
                step(lambda:old.recovery_observation()[0]>status)
                before=time.monotonic();m.status(a.id);assert time.monotonic()-before<.5
            step(lambda:m.pipelines[a.id] is not old)
            new=m.pipelines[a.id];owners.append(new)
            assert old._done.is_set() and new.output is stable and new.generation!=old.generation
            with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
            step(lambda:new._has_pcm and sink.sessions>sessions)
            assert new._podcast_service is service and service._io_thread is thread
            assert service._parser is parser and parser._executor is executor
            assert m.pipelines[b.id] is other and other.process.poll() is None and other.recovery_observation()[1]>progress
            mux.detach(0);step(lambda:mux.owner is None and new.direct_applied[1] is None)
            sessions=sink.sessions;hb=new.control_counts.get('heartbeat',0)
            step(lambda:new.control_counts.get('heartbeat',0)>hb);assert sink.sessions==sessions
            try:mux.attach(0,a.id)
            except SourcePending:pass
            step(lambda:sink.sessions>sessions)
            profile=file_profile('podcast direct limiter').model_dump(mode='json')
            profile['stages']['limiter']['selected']='FM_HEADEND_LIMITER'
            profile['stages']['clipper']['selected']='FM_HEADEND_CLIPPER'
            saved=m.profile_files.create(profile)
            changed=a.model_copy(update={'use_default_audio_profile':False,'audio_profile_id':saved.path,'input_gain_db':-6})
            m._restart_audio(changed);step(lambda:m.pipelines[a.id] is not new)
            current=m.pipelines[a.id];owners.append(current);step(lambda:current._has_pcm)
            assert current.output is stable and current._podcast_service is service and service._io_thread is thread
            assert parser._executor is executor
            changed=changed.model_copy(update={'config':dict(changed.config,refresh_interval_minutes=2)})
            m._restart_audio(changed);step(lambda:m.pipelines[a.id] is not current)
            current=m.pipelines[a.id];owners.append(current);step(lambda:current._has_pcm and current.snapshot().get('title'))
            assert current._podcast_service is not service and not thread.is_alive()
            assert current._podcast_service._parser is parser and parser._executor is executor
            title=current.snapshot()['title'];step(lambda:current.snapshot().get('title')!=title)
            before=time.monotonic();mux.detach(0);m.release(a.id,0);stop_seconds=time.monotonic()-before
            assert stop_seconds<.5
            step(lambda:a.id not in m.pipelines)
            assert not current._podcast_service._io_thread.is_alive() and other.process.poll() is None
            m.release(b.id,1);step(lambda:not m.pipelines)
            print('PODCAST_RECOVERY',json.dumps(dict(fault=fault,elapsed=time.monotonic()-fault_at,stop_seconds=stop_seconds,
                processes=[(p.process.pid,p.exit_code) for p in owners],rss_requests=state['requests'].count('/rss'))))
        finally:
            remaining=list(m.pipelines.values());mux.close();m.close()
            for p in owners+remaining:
                finish(p)
                if p.process:
                    with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
            sink.close();db.close()


def test_native_podcast_direct_feed_wait_and_return(gst,tmp_path):
    require(gst,'playbin3','wavparse')
    with server({'A':media_bytes(tmp_path/'a.wav')}) as (url,state):
        state['fail_feed']=True
        db=Database(tmp_path/'wait.db');m=SourceManager(db,DirectHttpFactory(tmp_path,podcast=True),profile_files=profile_store(tmp_path))
        a=source(db,url);sink=SocketSink(tmp_path);mux=DirectMux(m,tmp_path);p=None
        def step(predicate):
            def poll():m.worker_recovery.service();mux.service();return predicate()
            return until(poll,timeout=25)
        try:
            try:m.acquire(a.id,0)
            except SourcePending:pass
            try:mux.attach(0,a.id)
            except SourcePending:pass
            p=m.pipelines[a.id];step(lambda:p.expected_media_wait)
            hb=p.control_counts.get('heartbeat',0);step(lambda:p.control_counts.get('heartbeat',0)>=hb+2)
            assert not m.worker_recovery.attempts and m.pipelines[a.id] is p
            state['fail_feed']=False;p._podcast_service._next_refresh=0;p._podcast_service._io_ready.set()
            step(lambda:p._has_pcm and bool(sink.records))
            assert m.pipelines[a.id] is p
            mux.detach(0);m.release(a.id,0);step(lambda:not m.pipelines)
            assert not p._podcast_service._io_thread.is_alive()
        finally:
            remaining=list(m.pipelines.values());mux.close();m.close()
            for worker in remaining+([p] if p else []):finish(worker)
            sink.close();db.close()
