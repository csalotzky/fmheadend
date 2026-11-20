import copy,json,threading
from types import SimpleNamespace
import pytest
from fmheadendd.sources.native_health import NativeHealth,accept
from fmheadendd.diagnostics import RecentLogs


def setup_health():
    now=[100.]; h=NativeHealth(lambda:now[0]);h.generation='g'
    return now,h,SimpleNamespace(generation='g',source_id=14)


def test_nested_operation_identity_and_slow_completion(caplog):
    caplog.set_level('WARNING')
    now,h,p=setup_health();h.begin_attempt()
    with RecentLogs() as logs:
        with h.operation('source','teardown'):
            original=h.snapshot()['slots'][0][0]
            with h.operation('source','state'):
                now[0]+=11;accept(p,h.snapshot(),now[0])
                assert len(logs.snapshot())==2
                assert h.snapshot()['slots'][0][0]==original
            assert h.snapshot()['slots'][0]==[original] or h.snapshot()['slots'][0]==(original,)
            now[0]+=1;accept(p,h.snapshot(),now[0]);assert len(logs.snapshot())==2
        value=h.snapshot();assert not value['slots'][0]
        assert value['completed'][0][-1]=='ok' and value['stats'][0]['teardown'][3]==12
        assert len(json.dumps(value))<4096


def test_backoff_exempts_loop_but_not_active_operation():
    now,h,p=setup_health();h.checkpoint('backoff',130,True)
    with RecentLogs() as logs:
        now[0]=129;accept(p,h.snapshot(),now[0]);assert not logs.snapshot()
        now[0]=136;accept(p,h.snapshot(),now[0]);assert 'source_suspect' in logs.snapshot()[0]['message']
        now[0]=141;accept(p,h.snapshot(),now[0]);assert 'source_unresponsive' in logs.snapshot()[0]['message']


@pytest.mark.parametrize('change',[
 lambda s:s.update(generation='stale'),lambda s:s.update(version=True),
 lambda s:s.update(slots=[[[1,0,'state',float('nan'),0,'active']],[],[]]),
 lambda s:s.update(stats=[{'state':[0]*100}, {},{}]),lambda s:s.update(attempt=-1),
 lambda s:s.update(progress=(10**400,'service',0)),
])
def test_malformed_health_is_ignored_without_failure(change):
    now,h,p=setup_health();value=h.snapshot();change(value);accept(p,value,now[0]);assert not hasattr(p,'native_health')


def test_same_operation_cannot_rearm_and_storage_bounded():
    now,h,p=setup_health()
    with h.operation('source','state'):
        accept(p,h.snapshot(),now[0]);previous=p.native_health
        value=copy.deepcopy(h.snapshot());value['slots'][0]=[tuple([1,0,'state',99.,0.,'active'])]
        accept(p,value,now[0]);assert p.native_health is previous
    for i in range(10000):
        with h.operation('source','state'):pass
    assert len(h.stats[0])==1 and len(h.slots)==3


def test_concurrent_lane_does_not_overwrite_active_owner():
    now,h,p=setup_health();entered=threading.Event();release=threading.Event()
    def run():
        with h.operation('handoff','handoff'):entered.set();release.wait(2)
    t=threading.Thread(target=run);t.start();assert entered.wait(1)
    old=h.slots[2]
    try:
        with h.operation('handoff','handoff'):assert h.slots[2]==old
        assert h.overflow[2]==1
    finally:release.set();t.join(2)
    assert not h.slots[2]
