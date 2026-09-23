"""Quota math, interleaving and RT format independent of hardware."""
from collections import Counter
from fractions import Fraction
from itertools import groupby

import pytest

from fmheadendd.models.configuration import ModulatorConfig
from fmheadendd.rds.allocator import GroupAllocator, quotas
from fmheadendd.rds.characters import rt_bytes
from fmheadendd.rds.scheduler import Scheduler, GROUP_SECONDS


@pytest.mark.parametrize('rt,ecc,expected', [
    (True, True, ('0.60','0.38','0.02')),
    (True, False, ('0.60','0.40','0')),
    (False, True, ('0.98','0','0.02')),
    (False, False, ('1','0','0')),
])
def test_derived_shares_and_streaming_distribution(rt, ecc, expected):
    shares=quotas(rt_enabled=rt,ecc_enabled=ecc)
    assert shares == dict(zip(('ps','rt','ecc'), map(Fraction,expected)))
    allocator=GroupAllocator(shares)
    counts=Counter(); sequence=[]
    for n in range(1,10004):  # Deliberately not a 100-group multiple.
        name=allocator.next(); counts[name]+=1; sequence.append(name)
        for key,share in shares.items():
            assert abs(counts[key]-n*share) <= 1
    if rt:
        assert max(len(list(run)) for _,run in groupby(sequence)) <= 2
    if ecc:
        indices=[i for i,name in enumerate(sequence) if name=='ecc']
        assert max(b-a for a,b in zip(indices,indices[1:])) <= 51
    fresh=GroupAllocator(shares)
    assert sequence == [fresh.next() for _ in sequence]


@pytest.mark.parametrize('rt,ecc,extra', [
    (True,True,{'future_a':.03,'future_b':.05}),
    (True,False,{'future_a':.10}),
    (False,True,{'future_a':.20,'future_b':.18}),
    (False,False,{'future_a':Fraction(1,7),'future_b':.12}),
])
def test_extensions_take_only_surplus_without_fixed_batch(rt,ecc,extra):
    shares=quotas(rt_enabled=rt,ecc_enabled=ecc,additional=extra)
    assert sum(shares.values())==1 and shares['ps']>=Fraction(3,5)
    assert shares['rt']>=Fraction(3,10) if rt else shares['rt']==0
    assert shares['ecc']==(Fraction(1,50) if ecc else 0)
    for name,value in extra.items(): assert shares[name]==Fraction(str(value))
    allocator=GroupAllocator(shares); counts=Counter(allocator.next() for _ in range(20003))
    for key,share in shares.items(): assert abs(counts[key]-20003*share)<2


@pytest.mark.parametrize('rt,ecc,extra', [
    (True,True,{'extra':.080001}), (True,False,{'extra':.100001}),
    (False,True,{'extra':.380001}), (False,False,{'extra':.400001}),
    (True,True,{'a':.05,'b':.04}),
])
def test_infeasible_quotas_are_rejected(rt,ecc,extra):
    with pytest.raises(ValueError,match='Infeasible'):
        quotas(rt_enabled=rt,ecc_enabled=ecc,additional=extra)


@pytest.mark.parametrize('extra', [{'ps':.01},{'rt':0},{'ecc':0},{'x':-.1},
    {'x':1.1},{'x':True},{'x':float('nan')},{'x':float('inf')},{'':.01}])
def test_invalid_quota_rejected(extra):
    with pytest.raises(ValueError): quotas(rt_enabled=True,ecc_enabled=True,additional=extra)


@pytest.mark.parametrize('termination', ['CR','PAD'])
@pytest.mark.parametrize('text', ['', 'Hi', 'ABCD', 'X'*63, 'Y'*64])
def test_rt_enabled_is_independent_of_content_and_termination(termination,text):
    s=Scheduler();s.reset(ModulatorConfig(rds_rt_enabled=True,rds_rt_termination=termination),text,0)
    groups=[s._next(i*GROUP_SECONDS) for i in range(200)]
    rt=[g for g in groups if g[1]>>12==2]
    data=rt_bytes(text,termination)
    count=len(data)//4
    assert len(rt)==80
    assert [g[1]&15 for g in rt]==[i%count for i in range(len(rt))]
    actual=b''.join(g[2].to_bytes(2,'big')+g[3].to_bytes(2,'big') for g in rt[:count])
    assert actual==data
    if termination=='PAD':
        assert count==16 and data==text.encode().ljust(64,b' ') and b'\r' not in data
    else:
        assert data== (text[:63].encode()+b'\r').ljust(count*4,b' ')
    s.reset(ModulatorConfig(rds_rt_enabled=False,rds_rt_termination=termination),text,20)
    assert all(s._next(20+i*GROUP_SECONDS)[1]>>12==0 for i in range(200))


def test_batch_size_and_text_changes_do_not_restart_allocation():
    def run(size):
        s=Scheduler();s.reset(ModulatorConfig(rds_ecc=224),'Original',0)
        groups=[]
        while len(groups)<1003:
            batch,s=s.prepare(min(size,1003-len(groups)),s.cursor,10)
            groups.extend(batch)
        return groups
    assert run(1)==run(7)==run(31)
    s=Scheduler();s.reset(ModulatorConfig(),'A',0)
    _,s=s.prepare(7,0)
    old=dict(s.allocator.credits)
    assert s.update_rt('B',1) and s.text_ab==1 and s.rt_segment==0
    assert s.allocator.credits==old
    s.reset(ModulatorConfig(rds_rt_enabled=False),'B',2)
    s.reset(ModulatorConfig(rds_rt_enabled=True),'B',3)
    assert s.text_ab==1  # Enabling alone is not a text change.


def test_rt_format_validation():
    with pytest.raises(ValueError): rt_bytes('Hi','invalid')
    with pytest.raises(ValueError): ModulatorConfig(rds_rt_termination='invalid')
    with pytest.raises(ValueError): ModulatorConfig(rds_rt_enabled='false')


def test_unimplemented_type_cannot_silently_emit_rt():
    s=Scheduler();s.reset(ModulatorConfig(),'text',0)
    s.allocator=GroupAllocator({'future':Fraction(1)})
    with pytest.raises(ValueError,match='No RDS encoder'):
        s._next(0)
