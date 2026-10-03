"""M5 exact information words, fake-time policy, and high-level mock integration."""
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.configuration import ModulatorConfig, ModulatorPatch, InputSourceConfig, PsEntry, RtOverride
from fmheadendd.models.runtime_status import ModulatorStatus
from fmheadendd.rp2354.client import MockRp2354Client
from fmheadendd.rp2354.protocol import Busy, Command, Status, RESPONSE_WAIT_MS
from fmheadendd.rds.characters import convert, ps_bytes, rt_bytes
from fmheadendd.rds.encoder import group_0a, group_1a, group_2a, af_bytes
from fmheadendd.rds.scheduler import Scheduler, GROUP_SECONDS, LOOKAHEAD_SECONDS
from fmheadendd.rds.refill import RdsRefill, POLL_SECONDS
from fmheadendd.rds.radiotext import RadioTextService
from fmheadendd.sources.manager import SourceManager


class Clock:
    value = 0.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


def config(**values):
    return ModulatorConfig(rds_enabled=True, rds_pi=0x1234, rds_pty=10, rds_tp=True,
                           rds_ecc=0xe0, rds_ps=[PsEntry(text='RADIO FM')], **values)


@pytest.mark.parametrize('segment,b,d', [(0,0x548,0x5241),(1,0x549,0x4449),(2,0x54a,0x4f20),(3,0x54f,0x464d)])
def test_exact_0a_all_segments(segment,b,d):
    assert group_0a(0x1234,10,True,b'RADIO FM',segment) == (0x1234,b,0xe0cd,d)


def test_pi_pty_tp_and_mono_bits():
    assert group_0a(0xffff,31,False,b'ABCDEFGH',3,False) == (0xffff,0x3eb,0xe0cd,0x4748)
    assert group_1a(0xabcd,31,True,0xe1) == (0xabcd,0x17e0,0x00e1,0)
    assert group_1a(0x1234,10,True,0xe0) == (0x1234,0x1540,0x00e0,0)
    assert group_2a(0x1234,10,True,b'ABCD',0,1) == (0x1234,0x2550,0x4142,0x4344)


@pytest.mark.parametrize('frequencies,expected', [([],b'\xe0\xcd'),([87600],b'\xe1\x01'),
    ([87600,107900],b'\xe2\x01\xcc\xcd'),([101100,98700,87600],b'\xe3\x88\x70\x01')])
def test_af_method_a_exact(frequencies,expected):
    assert af_bytes(frequencies) == expected
    for pair in range(len(expected)//2):
        assert group_0a(1,0,False,b'12345678',0,af=frequencies,af_pair=pair)[2] == int.from_bytes(expected[pair*2:pair*2+2],'big')


def test_af_maximum_and_order():
    frequencies = list(range(90000,87500,-100))
    assert af_bytes(frequencies) == bytes([249,*range(25,0,-1)])
    assert len(af_bytes(frequencies)) == 26


@pytest.mark.parametrize('values', [[87500],[108000],[87601],[87650],[87600.0],[True],[87600]*26])
def test_af_invalid(values):
    with pytest.raises(ValueError): af_bytes(values)
    with pytest.raises(ValidationError): ModulatorConfig(rds_af_khz=values)


@pytest.mark.parametrize('ecc', [-1,256,True,224.0,'E0'])
def test_ecc_validation(ecc):
    with pytest.raises(ValidationError): ModulatorConfig(rds_ecc=ecc)
    with pytest.raises(ValueError): group_1a(1,0,False,ecc)


def test_ecc_nullable_and_boundaries():
    assert ModulatorConfig().rds_ecc is None
    assert ModulatorConfig(rds_ecc=0).rds_ecc == 0
    assert ModulatorConfig(rds_ecc=255).rds_ecc == 255


def test_character_policy_and_padding():
    assert ps_bytes('FM') == b'FM      '
    assert ps_bytes('Caf\u00e9') == b'Caf\x82    '
    assert convert('\u0150r\u00fclt \u00df\u2014\U0001f600',64) == b'Or\x99lt \x8d-?'
    assert convert('$^`~\n',64) == b'\xab????'
    assert convert('Cafe\u0301',64) == convert('Caf\u00e9',64)
    assert rt_bytes('Hi') == b'Hi\r '
    assert rt_bytes('ABCD') == b'ABCD\r   '
    assert rt_bytes('') == b'\r   '
    assert rt_bytes('x'*64) == b'x'*63 + b'\r'
    assert rt_bytes('x'*64, 'PAD') == b'x'*64
    assert len(ps_bytes('\u00df'*8)) == 8


def test_exact_rt_segments_and_maximum():
    data = rt_bytes('ABCDEFG')
    assert group_2a(0x1234,0,False,data,0,0) == (0x1234,0x2000,0x4142,0x4344)
    assert group_2a(0x1234,0,False,data,1,0) == (0x1234,0x2001,0x4546,0x470d)
    assert group_2a(0xabcd,31,True,b'X'*64,15,1) == (0xabcd,0x27ff,0x5858,0x5858)
    with pytest.raises(ValueError): group_2a(1,0,False,data,2,0)


def test_prepare_is_speculative_and_ab_tracks_only_content():
    s=Scheduler();s.reset(config(),'First',0)
    groups,candidate=s.prepare(6,0)
    assert s.ps_segment == s.rt_segment == 0
    assert candidate != s and s.text_ab == 0
    s=candidate
    assert not s.update_rt('First',1) and s.text_ab == 0
    assert s.update_rt('Second',1) and s.text_ab == 1 and s.rt_segment == 0
    groups,s=s.prepare(31,1,3)
    rts=[g for g in groups if g[1]>>12 == 2]
    assert rts[0][1]&15 == 0 and all(g[1]&16 for g in rts)
    assert not s.update_rt('Second',5) and s.text_ab == 1
    assert s.update_rt('Third',5) and s.text_ab == 0


def test_dynamic_ps_duration_and_segment_restart():
    c=config();c.rds_ps=[PsEntry(text='ONE',duration_s=2),PsEntry(text='TWO',duration_s=3)]
    s=Scheduler();s.reset(c,'text',0)
    _,s=s.prepare(31,0,1)
    _,before=s.prepare(1,1.99)
    assert before.ps_item == 0
    groups,after=s.prepare(6,2)
    ps=[g for g in groups if g[1]>>12 == 0]
    assert after.ps_item == 1 and ps[0][1]&3 == 0 and ps[0][3] == 0x5457
    _,still=after.prepare(1,4.99)
    assert still.ps_item == 1
    _,switched=after.prepare(6,5)
    assert switched.ps_item == 0


def test_quota_fairness_ecc_spacing_and_static_ps():
    s=Scheduler();s.reset(config(),'X'*64,0)
    counts=Counter();ecc_times=[];ps_addresses=[]
    for i in range(700):
        now=i*GROUP_SECONDS
        groups,s=s.prepare(1,now,0.001)
        for g in groups:
            kind=g[1]>>12;counts[kind]+=1
            if kind==1: ecc_times.append(now)
            if kind==0: ps_addresses.append(g[1]&3)
    assert counts[0]>200 and counts[2]>200 and counts[1]>=12
    assert max(b-a for a,b in zip(ecc_times,ecc_times[1:])) < 5.2
    assert ps_addresses == [i%4 for i in range(len(ps_addresses))]
    assert s.ps_item == 0


def test_no_ecc_invented_and_af_cycles_independently():
    c=config(rds_af_khz=[87600,87700,87800,87900]);c.rds_ecc=None
    s=Scheduler();s.reset(c,'rt',0)
    groups,s=s.prepare(31,0,3)
    assert all(g[1]>>12 != 1 for g in groups)
    ps=[g for g in groups if g[1]>>12==0]
    assert [g[2] for g in ps[:6]] == [0xe401,0x0203,0x04cd]*2


@pytest.fixture
def rig(tmp_path):
    clock=Clock();db=Database(tmp_path/'rds.db');sources=SourceManager(db)
    src=db.save('sources',InputSourceConfig(name='shared',type='HTTP',config={'uri':'https://example.org/live'}))
    for i in range(8):
        c=config(rds_rt='Fallback',rds_metadata_to_rt=True,input_source_id=src.id)
        db.save('modulators',c,i)
    manager=SimpleNamespace(active_sources={i:src.id for i in range(8)},statuses={i:ModulatorStatus(id=i) for i in range(8)})
    rp=MockRp2354Client(clock=clock);rt=RadioTextService(db,sources,clock)
    refill=RdsRefill(db,manager,rt,rp,clock)
    yield SimpleNamespace(clock=clock,db=db,sources=sources,src=src,manager=manager,rp=rp,rt=rt,refill=refill)
    db.close()


def test_shared_metadata_precedence_and_ttl(rig):
    r=rig
    # Existing source abstraction's status method supplies shared metadata.
    r.sources.status=Mock(return_value=SimpleNamespace(stream_title='Metadata'))
    assert r.rt.effective(0).text == r.rt.effective(7).text == 'Metadata'
    r.rt.set(0,RtOverride(text='Override',ttl_s=1))
    assert r.rt.effective(0).origin=='override' and r.rt.effective(7).origin=='metadata'
    r.refill.tick();ab=r.refill.channels[0].scheduler.text_ab
    r.clock.advance(1.01);r.refill.tick()
    assert r.rt.effective(0).text=='Metadata'
    assert r.refill.channels[0].scheduler.text_ab != ab
    r.rt.set(0,RtOverride(text='Metadata',ttl_s=1));r.clock.advance(.2)
    same=r.refill.channels[0].scheduler.text_ab;r.refill.tick()
    assert r.refill.channels[0].scheduler.text_ab==same
    r.sources.status.return_value.stream_title=None;r.clock.advance(1.1);r.refill.tick()
    assert r.rt.effective(0).text=='Fallback'
    assert r.db.get('modulators',0).rds_rt=='Fallback'


def test_all_eight_states_and_fifo_query_only(rig):
    r=rig;r.rp.get_status=Mock(side_effect=AssertionError('Full status is not a refill query'))
    r.refill.tick()
    assert {c for c,g in r.rp.rds_history}==set(range(8))
    assert sum(call[0]=='GET_RDS_FIFO_FREE' for call in r.rp.rds_calls)==1
    assert all(row.rds_groups_accepted>0 for row in r.manager.statuses.values())
    r.rt.set(0,RtOverride(text='new',ttl_s=10));r.clock.advance(.2);r.refill.tick()
    assert r.refill.channels[0].scheduler.text_ab==1
    assert all(r.refill.channels[i].scheduler.text_ab==0 for i in range(1,8))


@pytest.mark.parametrize('jitter', [0.0, 0.1, 0.25])
@pytest.mark.parametrize('old_horizon', [False, True])
def test_lookahead_survives_serialized_status_gap(rig, jitter, old_horizon):
    """Real scheduler/refill, fake continuous 1187.5/104 groups/s consumption.

    No refill runs while the one executor is occupied by GET_STATUS. Include
    a full refill phase and explicit scheduling jitter, repeatedly on all TXs.
    The old 0.5 s horizon is a negative control, not a reduced test drain rate.
    """
    r = rig
    assert LOOKAHEAD_SECONDS == r.refill.horizon == 2.0
    assert POLL_SECONDS == 0.1 and RESPONSE_WAIT_MS[Command.GET_STATUS] == 850
    if old_horizon:
        r.refill.horizon = 0.5
    missed = [0] * 8
    drain = r.rp._drain_rds

    def counted_drain():
        for channel, fifo in enumerate(r.rp.rds_fifos):
            due = int((r.clock() - r.rp._rds_times[channel]) / GROUP_SECONDS)
            missed[channel] += max(0, due - len(fifo))
        drain()

    r.rp._drain_rds = counted_drain
    r.refill.tick()
    assert [len(fifo) for fifo in r.rp.rds_fifos] == [6 if old_horizon else 23] * 8
    gap = RESPONSE_WAIT_MS[Command.GET_STATUS] / 1000 + POLL_SECONDS + jitter
    for _ in range(30):
        r.clock.advance(gap)  # Executor unavailable: RP continues consuming.
        r.rp.get_status()     # Finish that status transaction, then resume refill.
        if not old_horizon:
            assert all(r.rp.rds_fifos)  # Never ran out, even before replenishment.
        r.refill.tick()
        assert all(0 < len(fifo) <= 24 < 64 for fifo in r.rp.rds_fifos)
    if old_horizon:
        assert all(n > 0 for n in missed)
    else:
        assert missed == [0] * 8


@pytest.mark.parametrize('boundary', ['stop', 'disable', 'configuration', 'recovery'])
def test_two_second_queue_discarded_at_lifecycle_boundary(rig, boundary):
    r = rig
    r.manager.active_sources = {0: r.src.id}
    r.refill.tick()
    assert len(r.rp.rds_fifos[0]) == 23
    c = r.db.get('modulators', 0)
    if boundary == 'stop':
        r.manager.active_sources.clear()
        r.refill.stop(0)
        assert not r.rp.rds_fifos[0]
        r.manager.active_sources = {0: r.src.id}
    elif boundary == 'disable':
        c.rds_enabled = False
        r.db.save('modulators', c, 0)
        r.clock.advance(POLL_SECONDS)
        r.refill.tick()
        assert not r.rp.rds_fifos[0]
        c.rds_enabled = True
    elif boundary == 'recovery':
        r.refill.suspend()
        r.rp.clear_rds(0)  # Existing M6A resynchronization boundary.
        assert not r.rp.rds_fifos[0]
        r.refill.suspended = False
    # Content change identifies any stale pre-boundary words unambiguously.
    c.rds_pi = 0xabcd
    r.db.save('modulators', c, 0)
    r.clock.advance(POLL_SECONDS + .001)
    r.refill.tick()
    assert len(r.rp.rds_fifos[0]) == 23
    assert all(group[0] == 0xabcd for group in r.rp.rds_fifos[0])
    calls = list(r.rp.rds_calls)
    pushes = [i for i, call in enumerate(calls) if call[0] == 'RDS_PUSH']
    assert any(call[0] == 'RDS_CLEAR' for call in calls[pushes[0]+1:pushes[-1]])


def test_zero_partial_and_maximum_multiple_batches(rig):
    r=rig;r.manager.active_sources={0:r.src.id};r.rp.rds_capacity=0
    r.refill.tick();assert not r.rp.rds_history
    r.rp.rds_capacity=2;r.clock.advance(.2);r.refill.tick()
    assert r.manager.statuses[0].rds_groups_accepted==2
    r.refill.stop(0);r.rp.rds_capacity=64;r.refill.horizon=4;r.clock.advance(.2);r.refill.tick()
    sizes=[c[2] for c in r.rp.rds_calls if c[0]=='RDS_PUSH']
    assert 31 in sizes and all(n<=31 for n in sizes)
    assert sum(sizes[-2:])>31


def test_busy_preserves_exact_batch_and_commits_only_on_ack(rig):
    r=rig;r.manager.active_sources={0:r.src.id}
    push=r.rp.push_rds;r.rp.push_rds=Mock(side_effect=Busy(Command.RDS_PUSH,Status.BUSY))
    r.refill.tick();state=r.refill.channels[0]
    pending=state.pending[0];assert state.scheduler.ps_segment==0 and state.scheduler.rt_segment==0
    assert r.manager.statuses[0].rds_groups_accepted==0
    r.rp.push_rds.side_effect=push;r.clock.advance(.11);r.refill.tick()
    assert r.rp.push_rds.call_args_list[1].args[1]==pending
    assert [g for c,g in r.rp.rds_history][:len(pending)]==list(pending)
    assert r.manager.statuses[0].rds_groups_accepted>=len(pending)


def test_ambiguous_failure_clears_before_restart(rig):
    r=rig;r.manager.active_sources={0:r.src.id};real_push=r.rp.push_rds
    def lost_ack(channel,groups):
        real_push(channel,groups)
        raise DependencyUnavailable('ACK lost; outcome unknown')
    r.rp.push_rds=Mock(side_effect=lost_ack)
    r.refill.tick();assert r.refill.channels[0].needs_clear
    assert r.manager.statuses[0].rds_groups_accepted==0
    assert r.manager.statuses[0].rds_last_error
    r.rp.push_rds.side_effect=real_push;r.clock.advance(1.1);r.refill.tick()
    calls=list(r.rp.rds_calls);pushes=[i for i,c in enumerate(calls) if c[0]=='RDS_PUSH']
    assert any(c[0]=='RDS_CLEAR' for c in calls[pushes[0]+1:pushes[1]])
    assert r.manager.statuses[0].rds_last_error is None


def test_disable_clear_and_clean_reenable(rig):
    r=rig;r.refill.tick()
    c=r.db.get('modulators',0);c.rds_enabled=False;r.db.save('modulators',c,0)
    r.clock.advance(.2);r.refill.tick()
    assert not r.refill.channels[0].active and r.refill.channels[0].pending is None
    assert not r.rp.rds_fifos[0]
    c.rds_enabled=True;r.db.save('modulators',c,0);r.clock.advance(.2);r.refill.tick()
    ps=[g for g in r.rp.rds_fifos[0] if g[1]>>12==0]
    rt=[g for g in r.rp.rds_fifos[0] if g[1]>>12==2]
    assert ps[0][1]&3==0 and rt[0][1]&15==0


def test_capacity_failure_preserves_config_and_can_retry(rig):
    r=rig;original=r.rp.get_rds_fifo_free
    r.rp.get_rds_fifo_free=Mock(side_effect=DependencyUnavailable('bus down'))
    r.refill.tick();assert r.manager.statuses[0].rds_last_error=='bus down'
    assert r.db.get('modulators',0).rds_enabled
    r.rp.get_rds_fifo_free=original;r.clock.advance(1.1);r.refill.tick()
    assert r.manager.statuses[0].rds_groups_accepted>0


def test_ambiguous_push_clear_and_immediate_full_lookahead(rig):
    r = rig
    r.manager.active_sources = {0:r.src.id}
    original = r.rp.push_rds
    uncertain = []
    def lost_ack(channel, groups):
        original(channel, groups)
        uncertain.append(tuple(groups))
        raise DependencyUnavailable('lost ACK after acceptance')
    r.rp.push_rds = Mock(side_effect=lost_ack)
    r.refill.tick()
    assert len(r.rp.rds_fifos[0]) == 23
    assert r.refill.channels[0].pending is None and r.refill.channels[0].needs_clear
    r.clock.advance(.249); r.refill.tick()
    assert r.rp.push_rds.call_count == 1
    r.rp.push_rds.side_effect = original
    r.clock.advance(.001)
    r.refill.next_poll = r.clock() + 100
    r.refill.tick()  # No extra 1-second or 100-ms wait after transport backoff.
    assert not r.refill.channels[0].needs_clear
    assert len(r.rp.rds_fifos[0]) == 23
    assert r.manager.statuses[0].rds_groups_accepted == 23
    calls = list(r.rp.rds_calls)
    pushes = [i for i,c in enumerate(calls) if c[0] == 'RDS_PUSH']
    assert any(c[0] == 'RDS_CLEAR' for c in calls[pushes[0]+1:pushes[1]])


def test_not_sent_push_keeps_exact_pending_batch_without_clear(rig):
    from fmheadendd.rp2354.protocol import HardwareUnavailable, Outcome
    r = rig; r.manager.active_sources = {0:r.src.id}
    error = HardwareUnavailable('open failed')
    error.command, error.outcome = Command.RDS_PUSH, Outcome.NOT_SENT
    original = r.rp.push_rds
    r.rp.push_rds = Mock(side_effect=error)
    r.refill.tick()
    pending = r.refill.channels[0].pending
    assert pending and not r.refill.channels[0].needs_clear
    clears = len([c for c in r.rp.rds_calls if c[0] == 'RDS_CLEAR'])
    r.rp.push_rds.side_effect = original
    r.clock.advance(.25); r.refill.tick()
    assert r.rp.push_rds.call_args_list[1].args[1] == pending[0]
    assert len([c for c in r.rp.rds_calls if c[0] == 'RDS_CLEAR']) == clears


def test_failed_clear_fences_one_channel_healthy_channels_continue(rig):
    from fmheadendd.rp2354.protocol import QnError
    r = rig; r.refill.tick()
    original = r.rp.clear_rds
    r.refill.fence(0)
    def failed_clear(channel):
        if channel == 0: raise QnError(Command.RDS_CLEAR, Status.QN_ERROR)
        return original(channel)
    r.rp.clear_rds = Mock(side_effect=failed_clear)
    before = [r.manager.statuses[i].rds_groups_accepted for i in range(8)]
    r.clock.advance(.2); r.refill.tick()
    after = [r.manager.statuses[i].rds_groups_accepted for i in range(8)]
    assert after[0] == before[0] and r.refill.channels[0].needs_clear
    assert all(a > b for a,b in zip(after[1:],before[1:]))
    r.rp.clear_rds.side_effect = original
    r.clock.advance(1.01); r.refill.tick()
    assert not r.refill.channels[0].needs_clear
    assert r.manager.statuses[0].rds_groups_accepted > before[0]


def test_capacity_failure_preserves_every_pending_scheduler(rig):
    r = rig; r.refill.tick()
    schedulers = [c.scheduler for c in r.refill.channels]
    pending = [c.scheduler.prepare(31,r.clock(),2) for c in r.refill.channels]
    for c,pending_batch in zip(r.refill.channels,pending): c.pending = pending_batch
    original = r.rp.get_rds_fifo_free
    r.rp.get_rds_fifo_free = Mock(side_effect=DependencyUnavailable('capacity unavailable'))
    r.clock.advance(.2); r.refill.tick()
    assert all(c.scheduler is s for c,s in zip(r.refill.channels,schedulers))
    assert all(c.pending is p for c,p in zip(r.refill.channels,pending))
    assert not any(c.needs_clear for c in r.refill.channels)
    r.rp.get_rds_fifo_free = original


def test_current_database_and_api_ecc(tmp_path):
    path = tmp_path / 'current.db'
    with TestClient(create_app(Settings(path))) as api:
        assert all(row['rds_ecc'] is None for row in api.get('/api/v1/modulators').json())
        assert api.patch('/api/v1/modulators/0',json={'rds_ecc':224,'rds_af_khz':[98100,101100]}).status_code==200
        assert api.patch('/api/v1/modulators/0',json={'rds_ecc':256}).status_code==422
        assert api.get('/api/v1/modulators/0').json()['rds_ecc']==224
        assert api.get('/openapi.json').json()['components']['schemas']['Modulator']['properties']['rds_ecc']
    db=Database(path);assert db.get('modulators',0).rds_ecc==224;db.close()


def test_clear_failure_blocks_new_push_until_success(rig):
    r=rig;r.manager.active_sources={0:r.src.id};r.refill.tick()
    clear=r.rp.clear_rds;r.rp.clear_rds=Mock(side_effect=DependencyUnavailable('clear failed'))
    c=r.db.get('modulators',0);c.rds_pi=0xabcd;r.db.save('modulators',c,0)
    accepted=r.manager.statuses[0].rds_groups_accepted
    r.clock.advance(.2);r.refill.tick()
    assert r.manager.statuses[0].rds_groups_accepted==accepted
    r.rp.clear_rds=clear;r.clock.advance(1.1);r.refill.tick()
    assert all(g[0]==0xabcd for g in r.rp.rds_fifos[0])


def test_busy_retry_waits_for_sufficient_fresh_capacity(rig):
    r=rig;r.manager.active_sources={0:r.src.id};push=r.rp.push_rds
    r.rp.push_rds=Mock(side_effect=Busy(Command.RDS_PUSH,Status.BUSY));r.refill.tick()
    pending=r.refill.channels[0].pending[0]
    query=r.rp.get_rds_fifo_free;r.rp.get_rds_fifo_free=Mock(return_value=[1]*8)
    r.clock.advance(.2);r.refill.tick()
    assert r.rp.push_rds.call_count==1 and r.refill.channels[0].pending[0]==pending
    r.rp.get_rds_fifo_free=query;r.rp.push_rds.side_effect=push;r.clock.advance(.2);r.refill.tick()
    assert r.rp.push_rds.call_args_list[1].args[1]==pending


def test_mock_fifo_consumption_and_capacity_are_bounded():
    clock=Clock();rp=MockRp2354Client(clock=clock,rds_capacity=2)
    rp.push_rds(0,[(1,2,3,4)]*2)
    with pytest.raises(Busy): rp.push_rds(0,[(1,2,3,4)])
    assert rp.get_rds_fifo_free()[0]==0
    clock.advance(GROUP_SECONDS);assert rp.get_rds_fifo_free()[0]==1
    clock.advance(GROUP_SECONDS);assert rp.get_rds_fifo_free()[0]==2
    rp.clear_rds(0);assert not rp.rds_fifos[0]


def test_mock_end_to_end_smoke():
    from fmheadendd.rds.smoke import run
    result=run()
    assert set(result['group_types'])=={'0A','1A','2A'}
    assert result['fifo_queries']>0 and result['push_calls']>0
    assert result['after_ttl']['text']=='Persistent fallback'
    assert result['physical_transmission_tested'] is False


def test_app_uses_existing_rds_control_path(tmp_path):
    clock=Clock();rp=MockRp2354Client(clock=clock)
    app=create_app(Settings(tmp_path/'api.db'),rp=rp,clock=clock)
    with TestClient(app) as api:
        assert api.patch('/api/v1/modulators/0',json={'audio_mode':'ANALOG','frequency_khz':98100,
            'rds_enabled':True,'rds_ecc':224,'rds_pi':4660,'rds_rt':'API text'}).status_code==200
        assert api.post('/api/v1/modulators/0/start').status_code==200
        # Execute the normal maintenance service on its owning control worker.
        clock.advance(.2)
        app.state.control.executor.submit(app.state.services.rds.tick).result()
        assert rp.rds_history
        assert api.post('/api/v1/modulators/0/stop').status_code==200
        assert not rp.rds_fifos[0]


def test_rt_change_preserves_ps_duration_and_quota_credits(rig):
    r=rig;r.manager.active_sources={0:r.src.id}
    c=r.db.get('modulators',0);c.rds_ps=[PsEntry(text='ONE',duration_s=2),PsEntry(text='TWO',duration_s=2)]
    r.db.save('modulators',c,0);r.refill.tick()
    expected=deepcopy(r.refill.channels[0].scheduler.allocator)
    before=r.manager.statuses[0].rds_groups_accepted
    r.clock.advance(1);r.rt.set(0,RtOverride(text='changed',ttl_s=10));r.refill.tick()
    state=r.refill.channels[0].scheduler
    for _ in range(r.manager.statuses[0].rds_groups_accepted-before): expected.next()
    assert state.ps_since==0 and state.allocator.credits==expected.credits
    r.clock.advance(1.1);r.refill.tick()
    assert r.refill.channels[0].scheduler.ps_item==1


def test_basic_rds_rates_over_ten_minutes():
    s=Scheduler();s.reset(config(),'X'*64,0)
    counts=Counter();times={0:[],1:[],2:[]}
    duration=600
    for i in range(int(duration/GROUP_SECONDS)):
        now=i*GROUP_SECONDS
        groups,s=s.prepare(1,now,0.001)
        kind=groups[0][1]>>12
        counts[kind]+=1;times[kind].append(now)
    total=sum(counts.values())
    for kind,share in ((0,.60),(1,.02),(2,.38)):
        assert abs(counts[kind]-total*share) < 1
    assert max(b-a for a,b in zip(times[0],times[0][1:])) < 3*GROUP_SECONDS + 1e-8
    assert max(b-a for a,b in zip(times[1],times[1][1:])) < 52*GROUP_SECONDS
    assert max(b-a for a,b in zip(times[2],times[2][1:])) < 4*GROUP_SECONDS + 1e-8


def test_rt_change_does_not_override_quota():
    def run(changed):
        s=Scheduler();s.reset(config(),'A'*64,0)
        if changed:
            assert s.update_rt('B'*64,0)
        kinds=[];ps_times=[]
        for i in range(26):
            now=i*GROUP_SECONDS
            groups,s=s.prepare(1,now,0.001)
            kind=groups[0][1]>>12;kinds.append(kind)
            if kind==0: ps_times.append(now)
        return kinds,ps_times,s
    baseline,_,_=run(False)
    changed,ps_times,s=run(True)
    assert changed==baseline
    assert changed.count(0)>=15
    assert max(b-a for a,b in zip(ps_times,ps_times[1:])) < 0.36
    assert s.text_ab == 1


@pytest.mark.parametrize('clear_fails', [False, True])
def test_disabling_rt_fences_queued_and_busy_rt_only_for_target(rig,clear_fails):
    r=rig;r.refill.tick()
    assert any(g[1]>>12==2 for g in r.rp.rds_fifos[0])
    push=r.rp.push_rds
    def busy(channel,groups):
        if channel==0: raise Busy(Command.RDS_PUSH,Status.BUSY)
        push(channel,groups)
    r.rp.push_rds=busy;r.clock.advance(.2);r.refill.tick()
    assert r.refill.channels[0].pending is not None
    peers=[s.scheduler for s in r.refill.channels[1:]]
    c=r.db.get('modulators',0);c.rds_rt_enabled=False;r.db.save('modulators',c,0)
    clear=r.rp.clear_rds
    if clear_fails:
        def fail(channel):
            if channel==0: raise DependencyUnavailable('uncertain CLEAR')
            clear(channel)
        r.rp.clear_rds=fail
    r.rp.push_rds=Mock(side_effect=push);r.rp.rds_calls.clear()
    r.clock.advance(.2);r.refill.tick()
    if clear_fails:
        assert r.refill.channels[0].needs_clear and r.refill.channels[0].pending is None
        assert all(call.args[0]!=0 for call in r.rp.push_rds.call_args_list)
        r.rp.clear_rds=clear;r.clock.advance(.3);r.refill.tick()
    assert not r.refill.channels[0].needs_clear
    assert r.rp.rds_fifos[0] and all(g[1]>>12!=2 for g in r.rp.rds_fifos[0])
    assert all(c[1]==0 for c in r.rp.rds_calls if c[0]=='RDS_CLEAR')
    assert all(r.refill.channels[i].scheduler.cursor>=peers[i-1].cursor for i in range(1,8))
    assert all(any(g[1]>>12==2 for g in r.rp.rds_fifos[i]) for i in range(1,8))
    # Metadata while disabled neither enables RT nor clears PS/ECC queues.
    r.rp.rds_calls.clear();r.rt.set(0,RtOverride(text='Changed while disabled'))
    r.clock.advance(.2);r.refill.tick()
    assert not any(c[0]=='RDS_CLEAR' for c in r.rp.rds_calls)
    assert all(g[1]>>12!=2 for g in r.rp.rds_fifos[0])
    c.rds_rt_enabled=True;r.db.save('modulators',c,0)
    r.clock.advance(.2);r.refill.tick()
    assert any(g[1]>>12==2 for g in r.rp.rds_fifos[0])
    assert [c[1] for c in r.rp.rds_calls if c[0]=='RDS_CLEAR']==[0]
    assert r.refill.channels[0].scheduler.text_ab==1


def test_rt_options_persist_through_api_and_default_existing_records(tmp_path):
    path=tmp_path/'rt-options.db'
    with TestClient(create_app(Settings(path))) as api:
        row=api.get('/api/v1/modulators/0').json()
        assert row['rds_rt_enabled'] is True and row['rds_rt_termination']=='CR'
        assert api.patch('/api/v1/modulators/0',json={'rds_rt_enabled':False,'rds_rt_termination':'PAD'}).status_code==200
        assert api.patch('/api/v1/modulators/0',json={'rds_rt_termination':'bad'}).status_code==422
    db=Database(path)
    assert not db.get('modulators',0).rds_rt_enabled
    assert db.get('modulators',0).rds_rt_termination=='PAD'
    db.close()
    assert ModulatorConfig.model_validate({'rds_rt':'existing text'}).rds_rt_enabled


def test_unicode_selected_rt_reaches_fifo_without_mutating_sources_or_config(rig):
    r = rig
    c = r.db.get('modulators', 0)
    c.rds_rt = '\u00e9\u00f6\u0151\u0171'
    r.db.save('modulators', c, 0)
    metadata = '\u0151\u0171\u00c1\u00c9'
    r.sources.status = Mock(return_value=SimpleNamespace(stream_title=metadata))
    r.refill.tick()
    groups = [g for channel, g in r.rp.rds_history
              if channel == 0 and g[1] >> 12 == 2 and g[1] & 15 == 0]
    assert groups[0][2:] == (0xa7b7, 0xc0c2)
    assert r.rt.effective(0).text == metadata
    r.rt.set(0, RtOverride(text='\u00f6\u00fc\u00d6\u00dc', ttl_s=1))
    r.rp.rds_history.clear()  # Bounded mock history: inspect only this update.
    r.clock.advance(.2); r.refill.tick()
    groups = [g for channel, g in r.rp.rds_history
              if channel == 0 and g[1] >> 12 == 2 and g[1] & 15 == 0]
    assert groups[0][2:] == (0x9799, 0xd7d9)
    assert r.refill.channels[0].scheduler.text_ab == 1
    r.sources.status.return_value.stream_title = None
    r.rp.rds_history.clear()  # Bounded mock history: inspect only this update.
    r.clock.advance(1); r.refill.tick()
    groups = [g for channel, g in r.rp.rds_history
              if channel == 0 and g[1] >> 12 == 2 and g[1] & 15 == 0]
    assert groups[0][2:] == (0x8297, 0xa7b7)
    assert r.refill.channels[0].scheduler.text_ab == 0
    assert r.db.get('modulators', 0).rds_rt == c.rds_rt


def test_api_and_database_keep_original_unicode(tmp_path):
    path = tmp_path / 'unicode.db'
    text = 'Cafe\u0301 \u0151\u0171 \U0001f600'
    ps = '\u00e1\u00e9\u0151\u0171'
    with TestClient(create_app(Settings(path))) as api:
        saved = api.patch('/api/v1/modulators/0', json={
            'rds_rt': text, 'rds_ps': [{'text': ps, 'duration_s': 5}]})
        assert saved.status_code == 200
        assert saved.json()['rds_rt'] == text
        assert saved.json()['rds_ps'][0]['text'] == ps
        assert api.put('/api/v1/modulators/0/rds/rt', json={'text': text}).json()['text'] == text
        assert api.get('/api/v1/status').json()['modulators'][0]['rds_rt_effective'] == text
    db = Database(path)
    try:
        assert db.get('modulators', 0).rds_rt == text
        assert db.get('modulators', 0).rds_ps[0].text == ps
    finally:
        db.close()
