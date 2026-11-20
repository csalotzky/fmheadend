"""Native observations and bounded HTTP retry evidence; no native watchdog enforcement."""
from contextlib import contextmanager
import math
import time
import threading
from functools import wraps
from .diagnostic_events import log

LANES = ('source', 'control', 'handoff')
LIMITS = {'build':30, 'state':10, 'teardown':10, 'uri':10, 'handoff':10, 'resume':10, 'buffering':10, 'stop':10}


def observed(lane, kind):
    def decorate(function):
        @wraps(function)
        def call(self,*args,**kwargs):
            with self._observe(lane,kind):
                return function(self,*args,**kwargs)
        return call
    return decorate


class NativeHealth:
    def __init__(self, clock=time.monotonic):
        self.clock=clock; self.generation=''; self.attempt=0
        self.slots=[() for _ in LANES]; self.sequences=[0]*len(LANES)
        self.stats=[{} for _ in LANES]; self.overflow=[0]*len(LANES)
        self.progress=(clock(), 'initial', 0.0); self.next_checkpoint=0
        self.bus=(0, 0.0)
        self.owners={}
        self.probe=None
        self.http_retry=None

    def receive_probe(self, value):
        if (not isinstance(value,dict) or set(value)!={'id','attempt','owner','mode'}
                or type(value['id']) is not int or not 1<=value['id']<2**63
                or type(value['attempt']) is not int or not 0<=value['attempt']<2**63
                or value['owner']!=self.generation or value['mode']!='progress'):
            return False
        old=self.probe
        if old and value['id']<=old[0]:
            return value['id']==old[0] and value['attempt']==old[1]
        if old and old[5]=='pending':return False
        if value['attempt']>self.attempt:return False
        # Receipt is control-thread ACK only. Completion belongs to checkpoint().
        self.probe=(value['id'],value['attempt'],self.generation,self.clock(),0.,
                    'pending' if value['attempt']==self.attempt else 'stale_attempt')
        return True

    def invalidate_probe(self):
        self.http_retry=None
        if self.probe:
            p=self.probe;self.probe=(*p[:4],0.,'cancelled')

    def checkpoint(self, phase='service', due=0.0, force=False):
        if phase == 'finished':self.http_retry=None
        now=self.clock()
        if force or now>=self.next_checkpoint:
            self.progress=(now,phase,due); self.next_checkpoint=now+1
            p=self.probe
            if p and p[5]=='pending' and phase!='finished':
                outcome='checkpoint' if p[1]==self.attempt else 'advanced_attempt'
                completed=self.clock()  # Read after receipt: do not publish a pre-ACK checkpoint time.
                if self.probe is p:self.probe=(*p[:4],completed,outcome)

    def begin_attempt(self):
        self.attempt+=1; self.checkpoint('attempt',force=True)

    @contextmanager
    def operation(self, lane, kind):
        if lane not in LANES or kind not in LIMITS:
            yield
            return
        owner=threading.get_ident()
        if self.owners.setdefault(lane,owner)!=owner:
            self.overflow[LANES.index(lane)]+=1
            yield
            return
        index=LANES.index(lane); stack=self.slots[index]
        if len(stack)>=4:
            self.overflow[index]+=1
            yield  # Observation never changes the native operation's outcome.
            return
        self.sequences[index]+=1
        token=(self.sequences[index], self.attempt, kind, self.clock(), 0.0, 'active')
        self.slots[index]=stack+(token,)
        result=['ok']
        try:
            yield result
        except BaseException:
            result[0]='error';raise
        finally:
            end=self.clock(); elapsed=end-token[3]
            # A lane has one owning thread; nested operations restore their outer token.
            self.slots[index]=stack
            stats=self.stats[index].copy(); previous=stats.get(kind,(0,0.,0.,0.,0,0,0))
            n,total,minimum,maximum,under1,under10,over10=previous
            stats[kind]=(n+1,total+elapsed,min(minimum,elapsed) if n else elapsed,max(maximum,elapsed),
                         under1+(elapsed<1),under10+(1<=elapsed<=10),over10+(elapsed>10))
            self.stats[index]=stats
            # Last completion independent from the still-active outer stack.
            setattr(self,'last_'+lane,(token[0],token[1],kind,token[3],end,result[0]))
            if not stack:
                self.owners.pop(lane,None)

    def snapshot(self):
        retry=self.http_retry  # Read before attempt: source publication may advance concurrently.
        return dict(version=3,http_retry=retry,generation=self.generation,attempt=self.attempt,probe=self.probe,
                    progress=self.progress,bus=self.bus,slots=list(self.slots),overflow=list(self.overflow),
                    completed=[getattr(self,'last_'+lane,None) for lane in LANES],stats=list(self.stats))


def accept(parent, value, now=None):
    from .source_liveness import accept_http_retry
    now=time.monotonic() if now is None else now
    valid=_accept(parent,value,now)
    accept_http_retry(parent,value,now,bool(valid))
    return valid


def _accept(parent, value, now=None):
    """Store/log shadow observations only. Never call _fail/stop/recovery."""
    now=time.monotonic() if now is None else now
    fields={'version','generation','attempt','progress','bus','slots','overflow','completed','stats'}
    if not isinstance(value,dict) or set(value) not in (fields,fields|{'probe'},fields|{'probe','http_retry'}):
        return
    def number(x):return type(x) in (int,float) and math.isfinite(x) and 0<=x<=now
    if type(value['version']) is not int or value['version'] not in (1,2,3) or value['generation']!=parent.generation or type(value['attempt']) is not int or not 0<=value['attempt']<2**63:
        return
    if (value['version']>=2) != ('probe' in value):return
    if (value['version']==3) != ('http_retry' in value):return
    try:
        probe=value.get('probe')
        if probe is not None:
            ident,target,owner,received,completed,outcome=probe
            if (type(ident) is not int or not 1<=ident<2**63 or type(target) is not int or not 0<=target<=value['attempt']
                    or owner!=parent.generation or not number(received)
                    or outcome not in ('pending','checkpoint','advanced_attempt','cancelled','stale_attempt')
                    or (outcome in ('checkpoint','advanced_attempt') and (not number(completed) or completed<received))
                    or (outcome in ('pending','cancelled','stale_attempt') and completed!=0)):return
        at,phase,due=value['progress']
        if not number(at) or phase not in ('initial','attempt','service','backoff','finished') or type(due) not in (int,float) or not math.isfinite(due) or not 0<=due<=now+31:return
        if len(value['bus'])!=2 or type(value['bus'][0]) is not int or not 0<=value['bus'][0]<2**63 or not number(value['bus'][1]):return
        if any(not isinstance(value[k],(list,tuple)) or len(value[k])!=3 for k in ('slots','overflow','completed','stats')):return
        for stack in value['slots']:
            if not isinstance(stack,(list,tuple)) or len(stack)>4:return
            for seq,attempt,kind,start,end,result in stack:
                if type(seq) is not int or not 1<=seq<2**63 or type(attempt) is not int or not 0<=attempt<=value['attempt'] or kind not in LIMITS or not number(start) or end!=0 or result!='active':return
        for item in value['completed']:
            if item is not None:
                seq,attempt,kind,start,end,result=item
                if type(seq) is not int or not 1<=seq<2**63 or type(attempt) is not int or not 0<=attempt<=value['attempt'] or kind not in LIMITS or not number(start) or not number(end) or end<start or result not in ('ok','error','success','async','failure','no-preroll','unknown'):return
        if any(type(n) is not int or not 0<=n<2**63 for n in value['overflow']):return
        for stats in value['stats']:
            if not isinstance(stats,dict) or len(stats)>len(LIMITS):return
            for kind,items in stats.items():
                if kind not in LIMITS or len(items)!=7 or any(type(x) not in (int,float) or not math.isfinite(x) or not 0<=x<=1e18 for x in items):return
    except (TypeError,ValueError,OverflowError):return
    previous=getattr(parent,'native_health',None)
    if previous and (value['attempt']<previous['attempt'] or at<previous['progress'][0]):return
    if previous:
        for old_stack,new_stack in zip(previous['slots'],value['slots']):
            for old in old_stack:
                for new in new_stack:
                    if old[0]==new[0] and old!=new:return
    parent.native_health=value
    requested=getattr(parent,'_source_probe',None)
    if probe is not None and requested and probe[0]==requested['id'] and probe[1]==requested['attempt']:
        parent._source_probe_report=probe
    observations=[]
    for lane,stack in zip(LANES,value['slots']):
        for seq,attempt,kind,start,end,result in stack:
            if now-start>LIMITS[kind]:observations.append((lane,kind,seq,start,now-start))
    if not any(value['slots']) and phase!='finished':
        age=now-(max(at,due) if phase=='backoff' else at)
        if age>5:
            response=getattr(parent,'_shadow_response',None)
            if response is None or response[0]!=at:
                response=(at,now+5);parent._shadow_response=response
            observations.append(('source','source_unresponsive' if now>=response[1] else 'source_suspect',0,at,age))
        else:parent._shadow_response=None
    else:parent._shadow_response=None
    # Fixed category cooldowns, not an ever-growing operation-id history.
    times=getattr(parent,'_shadow_log_times',{})
    for lane,kind,seq,start,age in observations:
        key=(lane,kind)
        if now>=times.get(key,0):
            times[key]=now+300
            log.warning('source=%s generation=%s category=shadow_native_overdue lane=%s kind=%s operation=%s attempt=%s start=%.6f elapsed_s=%.3f observation_only=true',
                        parent.source_id,parent.generation,lane,kind,seq,value['attempt'],start,age)
    parent._shadow_log_times=times
    return True
