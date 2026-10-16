import hashlib
import json
import struct
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from firmware.fmheadend_rp2354.tools.rp2354_artifact import inspect_image, load_artifact
from tools.rp2354_flash import Uploader, UpdateError, main, F
from daemon.rp2354 import protocol as p
from daemon.rp2354 import linux_client as linux


def artifact():
    data=bytearray(532)
    struct.pack_into('<II',data,0,0x20080000,0x10000021)
    struct.pack_into('<7I',data,256,0xffffded3,0x90210142,0x248,0x00010001,0x3ff,256,0xab123579)
    struct.pack_into('<5I',data,512,0xffffded3,0x1fe,0x1ff,(-256)&0xffffffff,0xab123579)
    data[384:400]=b'FMHEADEND-OTA-V1'
    data[400:416]=b'n'*16
    data[416:432]=b'0.8.6b'+bytes(10)
    struct.pack_into('<HH',data,432,1,1)
    return bytes(data),inspect_image(data)


class Fake:
    def __init__(self,lost=None,bad_boot=False,rollback=False,boot=0,no_normal=False,buy_failure=False):
        self.s=dict(state=0,boot=0,inactive=1,target=255,tbyb=0,ready=True,error=0,
            version=(1,0),capacity=0xf0000,chunk=128,accepted=0,committed=0,size=0,
            session=bytes(16),sha256=bytes(32),build=bytes(16),running_build=b'o'*16,maintenance=False)
        self.s.update(boot=boot,inactive=boot^1)
        self.no_normal=no_normal;self.normal_pending=False;self.normal_boots=0;self.buy_failure=buy_failure
        self.calls=[];self.data=bytearray();self.lost=lost;self.bad_boot=bad_boot;self.rollback=rollback;self.health=0
    def get_info(self): return dict(rp_protocol_version=1,rp_firmware_version='0.8.6b')
    def get_status(self): self.health+=1;return {'modulators':[{'tx_state':'OFF'} for _ in range(8)]}
    def flash_update(self,payload):
        sub=F(payload[0]);self.calls.append(sub)
        if sub==F.STATUS and self.normal_pending:
            self.normal_pending=False;self.normal_boots+=1;self.s['maintenance']=False
        if sub==F.BEGIN:
            self.s.update(state=1,target=self.s['boot']^1,maintenance=True,session=payload[1:17],size=struct.unpack_from('<I',payload,17)[0],
                sha256=payload[21:53],build=payload[53:69])
        elif sub==F.WRITE:
            if self.lost=='before':
                self.lost=None;raise p.HardwareUnavailable('lost before acceptance')
            off=struct.unpack_from('<I',payload,17)[0]
            assert off==len(self.data)
            self.data.extend(payload[22:]);accepted=len(self.data)
            self.s.update(accepted=accepted,committed=accepted if accepted==self.s['size'] else accepted//256*256)
            if self.lost=='after':
                self.lost=None;raise p.ProtocolError('lost ACK')
            if self.lost=='reset':
                self.lost=None;self.s.update(state=5,accepted=0,committed=0)
                raise p.HardwareUnavailable('reset')
        elif sub==F.VERIFY:
            assert hashlib.sha256(self.data).digest()==self.s['sha256'];self.s['state']=2
            if self.lost=='verify':
                self.lost=None;raise p.ProtocolError('lost VERIFY ACK')
        elif sub==F.ACTIVATE:
            answer=dict(self.s,state=3)
            self.s.update(state=3)
            if not self.rollback:
                self.s.update(boot=self.s['target'],inactive=self.s['target']^1,tbyb=1,version=(1,1),running_build=b'x'*16 if self.bad_boot else self.s['build'])
            return answer
        elif sub==F.CONFIRM:
            assert self.health==1 and payload[1:]==self.s['session']
            if self.buy_failure:
                self.s.update(state=6,tbyb=0,error=10) # ROM clears pending even on failure.
                raise p.InternalError(p.Command.FLASH_UPDATE,p.Status.INTERNAL_ERROR)
            self.s.update(state=4,tbyb=0)
            if self.lost=='confirm_partial':
                self.lost=None;raise p.ProtocolError('CONFIRM payload/CRC not fully consumed; no reboot')
            self.normal_pending=not self.no_normal
            if self.lost=='confirm':
                self.lost=None;raise p.ProtocolError('lost CONFIRM ACK')
        elif sub==F.ABORT:
            assert payload[1:]==self.s['session']
            self.s.update(state=7)
            if self.lost=='abort_partial':
                self.lost=None;raise p.ProtocolError('ABORT ACK partial')
            self.normal_pending=not self.no_normal
            if self.lost=='abort':
                self.lost=None;raise p.ProtocolError('ABORT ACK lost after STOP')
        return dict(self.s)


def runner(fake):
    now=[0.0]
    def sleep(n): now[0]+=n
    return Uploader(fake,clock=lambda:now[0],sleep=sleep,progress=lambda s:None)


@pytest.mark.parametrize('lost',[None,'before','after','verify','confirm','confirm_partial'])
@pytest.mark.parametrize('boot',[0,1])
def test_upload_and_reconcile(lost,boot):
    data,meta=artifact();fake=Fake(lost,boot=boot)
    final=runner(fake).run(data,meta)
    assert final['state']==4 and not final['maintenance'] and final['boot']==boot^1 and final['tbyb']==0
    assert fake.data==data and fake.calls.count(F.CONFIRM)==(2 if lost=='confirm_partial' else 1)
    assert fake.normal_boots==1 and fake.health==2
    if lost in ('before','after'):
        first=fake.calls.index(F.WRITE)
        assert fake.calls[first+1]==F.STATUS


@pytest.mark.parametrize('mode',['no_normal','buy_failure'])
def test_bought_flag_or_ack_alone_does_not_prove_recovery(mode):
    fake=Fake(**{mode:True})
    with pytest.raises(UpdateError):runner(fake).run(*artifact())
    assert not fake.normal_boots


@pytest.mark.parametrize('key,value',[
    ('running_build',b'x'*16),('boot',0),('version',(1,0)),('session',b'x'*16),
    ('sha256',b'x'*32),('ready',False),('state',3),('error',10),('tbyb',1)])
def test_final_firmware_identity_and_health_required(key,value):
    fake=Fake();original=fake.flash_update
    def call(packet):
        result=original(packet)
        if fake.normal_boots:result[key]=value
        return result
    fake.flash_update=call
    with pytest.raises(UpdateError):runner(fake).run(*artifact())


def test_final_rf_must_remain_off():
    fake=Fake();original=fake.get_status
    def status():
        result=original()
        if fake.normal_boots:result['modulators'][0]['tx_state']='ON'
        return result
    fake.get_status=status
    with pytest.raises(UpdateError,match='RF ON'):runner(fake).run(*artifact())


@pytest.mark.parametrize('lost',[None,'abort','abort_partial'])
@pytest.mark.parametrize('ram_only',[False,True])
def test_abort_waits_for_original_firmware_recovery(lost,ram_only):
    fake=Fake(lost);session=b'a'*16
    fake.s.update(session=session,target=1,state=6 if ram_only else 1,maintenance=True,error=9 if ram_only else 0)
    original=fake.flash_update
    def call(packet):
        result=original(packet)
        if fake.normal_boots:
            result['error']=0
            if ram_only:result.update(state=0,session=bytes(16),target=255)
        return result
    fake.flash_update=call
    result=runner(fake).abort(session)
    assert not result['maintenance'] and result['boot']==0 and fake.normal_boots==1
    assert F.CONFIRM not in fake.calls


def test_abort_no_reboot_is_not_success():
    fake=Fake(no_normal=True);session=b'a'*16
    fake.s.update(session=session,target=1,state=6,maintenance=True)
    with pytest.raises(UpdateError):runner(fake).abort(session)


@pytest.mark.parametrize('maintenance,state',[(False,4),(True,3),(True,4)])
def test_restart_uploader_after_buy(maintenance,state):
    data,meta=artifact();fake=Fake()
    fake.s.update(state=state,boot=1,inactive=0,target=1,tbyb=0,version=(1,1),size=len(data),
        session=b's'*16,sha256=bytes.fromhex(meta['sha256']),build=bytes.fromhex(meta['build_id']),
        running_build=bytes.fromhex(meta['build_id']),maintenance=maintenance)
    result=runner(fake).run(data,meta,resume=True)
    assert result['state']==4 and not result['maintenance']
    assert F.BEGIN not in fake.calls and F.ACTIVATE not in fake.calls
    assert fake.calls.count(F.CONFIRM)==int(maintenance)


def test_restart_uploader_does_not_recover_different_hash():
    data,meta=artifact();fake=Fake()
    fake.s.update(state=4,boot=1,inactive=0,target=1,tbyb=0,version=(1,1),size=len(data),
        build=bytes.fromhex(meta['build_id']),running_build=bytes.fromhex(meta['build_id']),maintenance=True)
    with pytest.raises(UpdateError,match='mismatch'):runner(fake).run(data,meta,resume=True)
    assert F.CONFIRM not in fake.calls


@pytest.mark.parametrize('boot',[0,1])
@pytest.mark.parametrize('old_version',[(1,1),(2,0)])
def test_equal_or_older_version_really_writes_inactive_slot(boot,old_version):
    data,meta=artifact();fake=Fake(boot=boot)
    fake.s.update(version=old_version,running_build=bytes.fromhex(meta['build_id']),
                  state=4,target=boot)
    result=runner(fake).run(data,meta)
    assert fake.data==data and F.BEGIN in fake.calls and F.WRITE in fake.calls
    assert result['boot']==boot^1 and result['state']==4


def test_resume_without_existing_session_does_not_write():
    fake=Fake()
    with pytest.raises(UpdateError,match='No matching'):
        runner(fake).run(*artifact(),resume=True)
    assert F.BEGIN not in fake.calls


@pytest.mark.parametrize('boot',[0,1])
def test_resume_activation_intent_does_not_rewrite(boot):
    data,meta=artifact();fake=Fake(boot=boot)
    fake.s.update(state=3,target=boot^1,maintenance=True,session=b's'*16,
                  size=len(data),accepted=len(data),committed=len(data),
                  sha256=bytes.fromhex(meta['sha256']),build=bytes.fromhex(meta['build_id']))
    result=runner(fake).run(data,meta,resume=True)
    assert result['state']==4 and result['boot']==boot^1
    assert F.BEGIN not in fake.calls and F.WRITE not in fake.calls
    assert fake.calls.count(F.ACTIVATE)==1


@pytest.mark.parametrize('mode',['bad_boot','rollback'])
def test_no_confirm_on_bad_boot(mode):
    fake=Fake(**{mode:True})
    with pytest.raises(UpdateError): runner(fake).run(*artifact())
    assert F.CONFIRM not in fake.calls


def test_reset_is_not_replayed():
    fake=Fake('reset')
    with pytest.raises(UpdateError): runner(fake).run(*artifact())
    assert fake.calls.count(F.WRITE)==1 and F.ACTIVATE not in fake.calls


def test_unexpected_session_stops():
    fake=Fake();original=fake.flash_update
    def call(packet):
        result=original(packet)
        if packet[0]==F.WRITE: result['session']=b'bad'*5+b'!'
        return result
    fake.flash_update=call
    with pytest.raises(UpdateError): runner(fake).run(*artifact())
    assert F.VERIFY not in fake.calls


def test_artifact_and_dry_run(tmp_path,monkeypatch):
    data,meta=artifact();path=tmp_path/'firmware.bin';path.write_bytes(data)
    path.with_suffix('.bin.json').write_text(json.dumps(meta))
    assert load_artifact(path)==(data,meta)
    monkeypatch.setattr(linux.Rp2354Client,'reserve_bus',Mock(side_effect=AssertionError('dry run touched bus')))
    main(['--dry-run',str(path)])
    path.write_bytes(data[:-1]+b'X')
    with pytest.raises(ValueError): load_artifact(path)


@pytest.mark.parametrize('sub',list(F))
def test_frame_crc(sub):
    if sub==F.BEGIN:
        data,meta=artifact()
        payload=bytes([sub])+bytes(16)+struct.pack('<I',len(data))+bytes.fromhex(meta['sha256'])+bytes.fromhex(meta['build_id'])+struct.pack('<HHB',1,1,1)
    elif sub==F.WRITE: payload=bytes([sub])+bytes(16)+struct.pack('<IB',0,128)+bytes(128)
    else: payload=bytes([sub])+(b'' if sub==F.STATUS else bytes(16))
    frame=p.request(p.Command.FLASH_UPDATE,255,payload)
    assert frame[:3]==bytes([240,255,len(payload)])
    assert int.from_bytes(frame[-2:],'little')==p.crc16(frame[:-2])
    with pytest.raises(ValueError): p.request(p.Command.FLASH_UPDATE,0,payload)
    body=bytes([0,128])+bytes(128)
    response=body+struct.pack('<H',p.crc16(body))
    assert p.response(p.Command.FLASH_UPDATE,response)==bytes(128)
    with pytest.raises(p.ProtocolError): p.response(p.Command.FLASH_UPDATE,response[:-1]+bytes([response[-1]^1]))


def test_unknown_and_oversized():
    for payload in [b'',b'\xff',bytes([F.WRITE])+bytes(16)+struct.pack('<IB',0,129)+bytes(129)]:
        with pytest.raises(ValueError):p.request(p.Command.FLASH_UPDATE,255,payload)


def test_bus_lock_survives_ambiguous_exchange(monkeypatch):
    os=SimpleNamespace(open=Mock(return_value=42),close=Mock(),write=Mock(return_value=6),read=Mock(return_value=b''),O_RDWR=2,O_CLOEXEC=0)
    def ioctl(fd,op,value,*args):
        if op==linux.I2C_FUNCS:value[0]=1
    locks=SimpleNamespace(flock=Mock(),ioctl=ioctl,LOCK_EX=2,LOCK_NB=4)
    monkeypatch.setattr(linux,'os',os);monkeypatch.setattr(linux,'fcntl',locks)
    monkeypatch.setattr(linux,'sys',SimpleNamespace(platform='linux'))
    monkeypatch.setattr(linux.time,'sleep',lambda n:None)
    client=linux.Rp2354Client('/dev/fake',0x62)
    client.reserve_bus()
    with pytest.raises(p.ProtocolError):client._exchange(p.Command.FLASH_UPDATE,255,bytes([F.STATUS]))
    assert client._fd==42;os.close.assert_not_called()
    client.close();os.close.assert_called_once_with(42)
    locks.flock.side_effect=BlockingIOError()
    with pytest.raises(p.HardwareUnavailable,match='locked'):linux.Rp2354Client('/dev/fake',0x62).reserve_bus()


@pytest.mark.parametrize('offset,value',[(2,2),(3,3),(4,2),(5,11),(6,2),(7,2),(126,2),(127,1)])
def test_status_rejects_unknown_flags(offset,value):
    status=bytearray(128);status[0]=1;status[104:108]=b'test'
    status[offset]=value
    with pytest.raises(p.ProtocolError):p.parse_flash_status(bytes(status))
