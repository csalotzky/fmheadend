"""Real LOCAL callback-held URI lock blocks teardown; parent must KILL/reap."""
import json
import os
import signal
import sys
import time
import pytest
from fmheadendd.database import Database
from fmheadendd.errors import SourcePending
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.direct_pcm import DirectHttpFactory,DirectMux
from fmheadendd.sources.process_pipeline import ProcessPipeline
from fmheadendd.sources.manager import SourceManager
from fmheadendd.tests.test_audio_worker import until,finish
from fmheadendd.tests.test_direct_multitx_native import Sinks
from fmheadendd.tests.test_gstreamer import gst,http_files,require
from fmheadendd.tests.test_local_worker_native import wav
from fmheadendd.tests.profile_support import profile_store

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Native LOCAL teardown containment')


class BlockedLocal(ProcessPipeline):
    worker_module='fmheadendd.tests.audiod_local_teardown_fixture'


def test_local_uri_lock_teardown_kill_reap_restart(gst,http_files,tmp_path,monkeypatch):
    require(gst,'playbin3','wavparse','souphttpsrc','fmheadendlimiter','fmheadendclipper')
    trace=tmp_path/'trace';trace.mkdir();monkeypatch.setenv('FMHEADEND_TEST_TEARDOWN_DIR',str(trace))
    media=tmp_path/'tracks';media.mkdir();wav(media/'track.wav',7000,60)
    folder,url=http_files;wav(folder/'sibling.wav',9000,60)
    class Factory(DirectHttpFactory):
        def __call__(self,source,**kw):
            return (BlockedLocal if source.type=='LOCAL' else ProcessPipeline)(source,direct_runtime=self.runtime,**kw)
    db=Database(tmp_path/'test.db');m=SourceManager(db,Factory(tmp_path,local=True),profile_files=profile_store(tmp_path))
    local=db.save('sources',InputSourceConfig(name='victim',type='LOCAL',config={'mode':'folder','path':str(media)}))
    sibling=db.save('sources',InputSourceConfig(name='sibling',type='HTTP',config={'uri':url+'/sibling.wav'}))
    sinks=Sinks(tmp_path);mux=DirectMux(m,tmp_path);owners=[]
    def service():m.worker_recovery.service();mux.service()
    def acquire(sid,ch):
        try:m.acquire(sid,ch)
        except SourcePending:pass
        try:mux.attach(ch,sid)
        except SourcePending:pass
    def records(pid):
        p=trace/str(pid)
        return [json.loads(s) for s in p.read_text().split('\n')[:-1]] if p.exists() else []
    try:
        acquire(local.id,0);acquire(sibling.id,1)
        old=m.pipelines[local.id];other=m.pipelines[sibling.id];owners.extend((old,other))
        def initial():service();return sinks.counts[0]>19200 and sinks.counts[1]>19200
        until(initial,timeout=15)
        os.kill(old.process.pid,signal.SIGUSR1)
        until(lambda:any(x['stage']=='streaming-callback-holds-uri-lock' for x in records(old.process.pid)),timeout=3)
        sibling_bytes=sinks.counts[1];last=sibling_bytes;last_at=time.monotonic();gaps=[];max_control=0
        def step(predicate,timeout=12):
            def poll():
                nonlocal last,last_at,max_control
                t=time.monotonic();service();max_control=max(max_control,time.monotonic()-t)
                if sinks.counts[1]>last:
                    gaps.append(time.monotonic()-last_at);last=sinks.counts[1];last_at=time.monotonic()
                return predicate()
            return until(poll,timeout=timeout)
        began=time.monotonic();mux.detach(0);m.release(local.id,0);stop_elapsed=time.monotonic()-began
        assert stop_elapsed<.5
        assert m.pipelines[local.id] is old and not old._done.is_set()
        with pytest.raises(SourcePending):m.acquire(local.id,0)
        step(lambda:old._done.is_set(),timeout=5)
        reap_elapsed=time.monotonic()-began
        events=records(old.process.pid)
        assert any(x['stage']=='armed-term-ignore' for x in events)
        assert any(x['stage']=='native-stop-entered' for x in events)
        assert any(x['stage']=='lock-wait' and x['caller']=='_run' for x in events)
        assert old.exit_code==-signal.SIGKILL
        with pytest.raises(ChildProcessError):os.waitpid(old.process.pid,os.WNOHANG)
        step(lambda:local.id not in m.pipelines)
        assert sinks.counts[1]>sibling_bytes and m.pipelines[sibling.id] is other and other.process.poll() is None
        acquire(local.id,0);new=m.pipelines[local.id];owners.append(new)
        assert new.generation!=old.generation and new.process is not old.process
        before=sinks.counts[0]
        step(lambda:mux.channels[0].owner is new and new._has_pcm and sinks.counts[0]>before+19200)
        assert max_control<.5 and gaps and max(gaps)<1.0
        evidence=dict(old_pid=old.process.pid,new_pid=new.process.pid,sibling_pid=other.process.pid,
            old_generation=old.generation,new_generation=new.generation,exit_code=old.exit_code,
            stop_s=stop_elapsed,reap_s=reap_elapsed,max_control_s=max_control,max_sibling_progress_gap_s=max(gaps),
            events=events,sibling_bytes_during_recovery=sinks.counts[1]-sibling_bytes)
        print('LOCAL_TEARDOWN_EVIDENCE',json.dumps(evidence))
    finally:
        mux.close();m.close()
        for p in owners:
            finish(p)
            if p.process:
                with pytest.raises(ChildProcessError):os.waitpid(p.process.pid,os.WNOHANG)
        sinks.close()
        for ch in range(8):
            path=tmp_path/f'pcm{ch}.sock'
            if path.exists():path.unlink()
        assert not list(tmp_path.glob('pcm*.sock'))
        db.close()
