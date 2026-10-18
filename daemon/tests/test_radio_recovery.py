from fmheadendd.tests.profile_support import profile_store
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable, DomainError
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.radio_browser import station
from fmheadendd.sources.gst_pipeline import GstPipeline, RetryBackoff
from fmheadendd.sources.http_recovery import HttpUrlRecovery
from fmheadendd.sources.manager import SourceManager
from fmheadendd.sources.output import PcmOutput
from fmheadendd.sources.radio_recovery import RadioRecoveryTask

UUID_TEXT='960e57c5-0601-11e8-ae97-52543be04c81'
OLD='https://old.test/live?password=OLD_SECRET'
NEW='https://new.test/live?password=NEW_SECRET'


def entry(url=NEW):
    # Neither a low bitrate, unknown codec nor upstream broken flag vetoes recovery.
    return station(dict(stationuuid=UUID_TEXT,name='Station',url=url,codec='UNKNOWN',bitrate=1,lastcheckok=0))


class FakePipeline:
    mocked=False
    def __init__(self, source, *, output=None, **kwargs):
        self.source=source
        self.output=output or PcmOutput()
        self.radio_url=HttpUrlRecovery(source.config['uri']) if source.config.get('radio_browser_uuid') and kwargs.get('reconnect',True) else None
        self.backoff=RetryBackoff()
        self.running=False
    def start(self): self.running=True
    def stop(self): self.running=False
    def snapshot(self): return dict(state='CONNECTING', reconnect_count=self.backoff.attempt)
    def fail(self):
        self.radio_url.failed()
        self.backoff.next_delay()
        self.radio_url.retry(self.backoff.attempt)


@pytest.fixture
def rig(tmp_path):
    db=Database(tmp_path/'radio.db')
    source=db.save('sources',InputSourceConfig(name='Original name',type='HTTP',
        config=dict(uri=OLD,radio_browser_uuid=UUID_TEXT),input_gain_db=-3))
    clock=[0.0]
    manager=SourceManager(db,profile_files=profile_store(tmp_path),pipeline_factory=FakePipeline,clock=lambda:clock[0])
    manager.acquire(source.id,0)
    manager.acquire(source.id,1)
    yield SimpleNamespace(db=db,source=source,manager=manager,clock=clock,
                          recovery=manager.radio_recovery,pipeline=manager.pipelines[source.id])
    manager.close()
    db.close()


def failed(rig):
    for _ in range(3): rig.pipeline.fail()
    tickets=rig.recovery.collect()
    assert len(tickets)==1
    return tickets[0]


def test_working_transient_and_one_ticket_per_shared_source(rig):
    assert rig.recovery.collect()==[]
    for _ in range(2):
        rig.pipeline.fail()
        assert rig.recovery.collect()==[]
    rig.pipeline.fail()
    assert len(rig.recovery.collect())==1
    assert rig.recovery.collect()==[]
    assert rig.manager.consumers[rig.source.id]=={0,1}
    assert len(rig.manager.pipelines)==1


def test_success_requires_pcm_and_persists_only_uri(rig):
    ticket=failed(rig)
    before=rig.db.get('sources',rig.source.id).model_dump(exclude={'timestamp'})
    rig.recovery.resolved(ticket,entry())
    assert rig.pipeline.radio_url.begin_attempt()==NEW
    rig.recovery.collect()
    assert rig.db.get('sources',rig.source.id).config['uri']==OLD
    rig.pipeline.radio_url.pcm()
    rig.recovery.collect()
    after=rig.db.get('sources',rig.source.id).model_dump(exclude={'timestamp'})
    before['config']['uri']=NEW
    assert before==after
    assert rig.pipeline.source.config['uri']==NEW
    assert rig.pipeline.radio_url.begin_attempt()==NEW
    assert rig.manager.pipelines[rig.source.id] is rig.pipeline
    assert rig.manager.consumers[rig.source.id]=={0,1}
    assert rig.recovery.collect()==[]


def test_no_pcm_failed_candidate_restores_old_and_cooldown(rig):
    ticket=failed(rig)
    rig.recovery.resolved(ticket,entry())
    assert rig.pipeline.radio_url.begin_attempt()==NEW
    rig.pipeline.fail()
    rig.recovery.collect()
    assert rig.pipeline.radio_url.begin_attempt()==OLD
    assert rig.db.get('sources',rig.source.id).config['uri']==OLD
    rig.clock[0]=299
    assert rig.recovery.collect()==[]
    rig.clock[0]=301
    assert len(rig.recovery.collect())==1


@pytest.mark.parametrize('result',[None,entry(OLD),entry('ftp://unsupported.test/audio')])
def test_missing_same_and_unsupported_leave_source_unchanged(rig,result):
    ticket=failed(rig)
    rig.recovery.resolved(ticket,result)
    assert rig.pipeline.radio_url.observe()[1] is None
    assert rig.db.get('sources',rig.source.id).config['uri']==OLD
    assert rig.recovery.collect()==[]


def test_original_recovers_while_lookup_inflight(rig):
    ticket=failed(rig)
    rig.pipeline.radio_url.pcm()
    rig.recovery.resolved(ticket,entry())
    assert rig.pipeline.radio_url.observe()[1] is None
    assert rig.db.get('sources',rig.source.id).config['uri']==OLD


@pytest.mark.parametrize('change',['url','uuid','unlink','name','gain','delete','stop','replace'])
@pytest.mark.parametrize('phase',['lookup','trying','pcm'])
def test_concurrent_changes_never_overwritten(rig,change,phase):
    ticket=failed(rig)
    if phase!='lookup':
        rig.recovery.resolved(ticket,entry())
        rig.pipeline.radio_url.begin_attempt()
        if phase=='pcm': rig.pipeline.radio_url.pcm()
    source=rig.db.get('sources',rig.source.id)
    if change=='stop':
        rig.manager.release(source.id,0)
        rig.manager.release(source.id,1)
    elif change=='delete':
        rig.db.delete('sources',source.id)
    elif change=='replace':
        rig.manager._restart_audio(source)
    else:
        update={}
        if change=='url': update['config']=source.config|{'uri':'https://manual.test/new'}
        if change=='uuid': update['config']=source.config|{'radio_browser_uuid':'11111111-1111-1111-1111-111111111111'}
        if change=='unlink': update['config']=source.config|{'radio_browser_uuid':None}
        if change=='name': update['name']='User choice'
        if change=='gain': update['input_gain_db']=-6
        saved=rig.db.save('sources',source.model_copy(update=update),source.id)
        rig.manager.apply_saved_source(source,saved)
    rig.recovery.resolved(ticket,entry())
    rig.recovery.collect()
    assert rig.source.id not in rig.recovery.pending
    if change!='delete':
        saved=rig.db.get('sources',source.id)
        assert saved.config['uri'] != NEW
        if change=='name': assert saved.name=='User choice'
        if change=='gain': assert saved.input_gain_db==-6
    if change=='name' and phase!='lookup':
        assert rig.pipeline.radio_url.cancelled()


def test_atomic_compare_prevents_write_race(rig,monkeypatch):
    ticket=failed(rig)
    rig.recovery.resolved(ticket,entry())
    rig.pipeline.radio_url.begin_attempt()
    rig.pipeline.radio_url.pcm()
    original=rig.db.replace_radio_browser_url
    def concurrent(*args):
        source=rig.db.get('sources',rig.source.id)
        rig.db.save('sources',source.model_copy(update={'name':'Concurrent edit'}),source.id)
        return original(*args)
    monkeypatch.setattr(rig.db,'replace_radio_browser_url',concurrent)
    rig.recovery.collect()
    assert rig.db.get('sources',rig.source.id).name=='Concurrent edit'
    assert rig.db.get('sources',rig.source.id).config['uri']==OLD
    assert rig.pipeline.radio_url.cancelled()


def test_storage_failure_keeps_audio_and_retries_guarded_write(rig,monkeypatch,caplog):
    ticket=failed(rig)
    rig.recovery.resolved(ticket,entry())
    rig.pipeline.radio_url.begin_attempt()
    rig.pipeline.radio_url.pcm()
    original=rig.db.replace_radio_browser_url
    calls=[]
    def failing(*args):
        calls.append(args)
        raise sqlite3.OperationalError('secret details')
    monkeypatch.setattr(rig.db,'replace_radio_browser_url',failing)
    rig.recovery.collect()
    assert rig.pipeline.running and rig.pipeline.radio_url.observe()[1]['state']=='pcm'
    assert rig.db.get('sources',rig.source.id).config['uri']==OLD
    assert 'persistence failed' in caplog.text
    assert 'SECRET' not in caplog.text and 'secret details' not in caplog.text
    rig.clock[0]=29
    rig.recovery.collect()
    assert len(calls)==1
    monkeypatch.setattr(rig.db,'replace_radio_browser_url',original)
    rig.clock[0]=30
    rig.recovery.collect()
    assert rig.db.get('sources',rig.source.id).config['uri']==NEW


def test_unlinked_and_one_shot_probes_have_no_recovery_state():
    ordinary=InputSourceConfig(name='Ordinary',type='HTTP',config={'uri':OLD})
    assert GstPipeline(ordinary).radio_url is None
    linked=InputSourceConfig(name='Linked',type='HTTP',config={'uri':OLD,'radio_browser_uuid':UUID_TEXT})
    assert GstPipeline(linked,reconnect=False).radio_url is None


def test_actual_worker_retry_hook_uses_existing_backoff(monkeypatch):
    source=InputSourceConfig(name='Linked',type='HTTP',config={'uri':OLD,'radio_browser_uuid':UUID_TEXT})
    pipeline=GstPipeline(source)
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.load_gst',lambda:object())
    def failure(): raise DependencyUnavailable('temporary')
    monkeypatch.setattr(pipeline,'_attempt',failure)
    observations=[]
    class Stop:
        def is_set(self): return False
        def wait(self,delay):
            observations.append((delay,pipeline.radio_url.observe()[0]))
            return len(observations)==3
    pipeline._stop=Stop()
    pipeline._run()
    assert observations==[(1,False),(2,False),(5,True)]


def test_api_outage_and_control_busy_handoff(rig):
    ticket=failed(rig)
    class Control:
        calls=0
        async def call(self,fn):
            self.calls+=1
            if self.calls==1: raise DomainError(503,'CONTROL_BUSY','Busy')
            return fn()
    class Client:
        async def lookup(self,uuid):
            assert uuid==UUID_TEXT
            raise DomainError(503,'RADIO_BROWSER_UNAVAILABLE','Unavailable')
    async def run():
        task=RadioRecoveryTask(Control(),rig.recovery,Client())
        await task.lookup(ticket)
    asyncio.run(run())
    assert rig.source.id not in rig.recovery.pending
    assert rig.db.get('sources',rig.source.id).config['uri']==OLD
    assert rig.recovery.collect()==[]


def test_lost_lookup_handoff_expires_with_cooldown(rig):
    ticket = failed(rig)
    rig.clock[0] = 61
    assert rig.recovery.collect() == []
    assert not rig.recovery.pending
    rig.recovery.resolved(ticket, entry())
    assert rig.pipeline.radio_url.observe()[1] is None


def test_commit_racing_old_url_retry_cancels_that_attempt():
    state = HttpUrlRecovery(OLD)
    state.retry(3)
    token = object()
    assert state.offer(token, NEW)
    assert state.begin_attempt() == NEW
    state.pcm()
    # Control observed PCM, but the stream failed while the guarded write ran.
    state.failed()
    assert state.begin_attempt() == OLD
    state.accept(token)
    assert state.cancelled()
    assert state.begin_attempt() == NEW
    assert not state.cancelled()
