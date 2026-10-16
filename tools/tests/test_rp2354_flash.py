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
    def __init__(self, lost=None, boot=0):
        self.s=dict(state=0,boot=boot,inactive=boot^1,target=255,tbyb=0,ready=True,error=0,
            version=(1,1),capacity=0xf0000,chunk=250,accepted=0,committed=0,size=0,
            sha256=bytes(32),build=bytes(16),running_build=b'n'*16,maintenance=False,
            firmware_version='0.8.6b')
        self.calls=[];self.data=bytearray();self.lost=lost;self.pending=False
    def get_info(self): return dict(rp_protocol_version=1,rp_firmware_version='0.8.6b')
    def get_status(self): return {'modulators':[{'tx_state':'OFF'}]*8}
    def flash_update(self,payload):
        sub=F(payload[0]);self.calls.append(sub)
        if sub==F.STATUS and self.pending:
            self.pending=False;self.s['maintenance']=False
        if sub==F.BEGIN:
            self.data.clear()
            self.s.update(state=1,target=self.s['boot']^1,maintenance=True,accepted=0,committed=0,
                          size=struct.unpack_from('<I',payload,1)[0],sha256=payload[5:37])
        elif sub==F.WRITE:
            if self.lost=='before':
                self.lost=None;raise p.HardwareUnavailable('before acceptance')
            off=struct.unpack_from('<I',payload,1)[0];assert off==len(self.data)
            self.data.extend(payload[5:]);accepted=len(self.data)
            self.s.update(accepted=accepted,committed=accepted if accepted==self.s['size'] else accepted//256*256)
            if self.lost=='after':
                self.lost=None;raise p.ProtocolError('CRC mismatch')
            if self.lost=='reset':
                self.lost=None;self.s.update(state=5,accepted=0,committed=0)
                raise p.HardwareUnavailable('reset')
            return dict(accepted=self.s['accepted'],committed=self.s['committed'])
        elif sub==F.VERIFY:
            assert hashlib.sha256(self.data).digest()==self.s['sha256']
            self.s.update(state=2,build=b'n'*16)
        elif sub==F.ACTIVATE:
            self.s.update(state=3,boot=self.s['target'],inactive=self.s['target']^1,tbyb=1)
        elif sub==F.CONFIRM:
            self.s.update(state=4,tbyb=0)
            if self.lost=='partial_confirm':
                self.lost=None;raise p.ProtocolError('partial ACK; no reboot')
            self.pending=True
        if self.lost==sub:
            self.lost=None;raise p.ProtocolError('lost ACK')
        return dict(self.s)


def runner(fake):
    now=[0.0]
    def sleep(n): now[0]+=n
    return Uploader(fake,clock=lambda:now[0],sleep=sleep,progress=lambda s:None)


@pytest.mark.parametrize('boot',[0,1])
@pytest.mark.parametrize('lost',[None,'before','after',F.BEGIN,F.VERIFY,F.ACTIVATE,F.CONFIRM,'partial_confirm'])
def test_complete_and_reconcile(boot,lost):
    fake=Fake(lost,boot)
    result=runner(fake).run(*artifact())
    assert result['state']==4 and result['tbyb']==0 and not result['maintenance']
    assert bytes(fake.data)==artifact()[0]
    assert fake.calls.count(F.WRITE)==(4 if lost=='before' else 3)


def test_interrupted_run_next_invocation_starts_zero():
    fake=Fake('reset')
    with pytest.raises(UpdateError):runner(fake).run(*artifact())
    assert F.ACTIVATE not in fake.calls
    runner(fake).run(*artifact())
    assert fake.calls.count(F.BEGIN)==2 and bytes(fake.data)==artifact()[0]
    runner(fake).run(*artifact()) # same build/version is uploaded again
    assert fake.calls.count(F.BEGIN)==3


def test_trial_never_begins_writes():
    fake=Fake();fake.s['tbyb']=1
    with pytest.raises(UpdateError):runner(fake).run(*artifact())
    assert F.BEGIN not in fake.calls


def test_persistent_crc_errors_are_bounded():
    fake=Fake()
    fake.get_info=Mock(side_effect=p.ProtocolError('CRC'))
    with pytest.raises(UpdateError):runner(fake).run(*artifact())
    assert fake.get_info.call_count==6 and not fake.calls


def test_artifact_and_dry_run(tmp_path,monkeypatch):
    data,meta=artifact();path=tmp_path/'firmware.bin';path.write_bytes(data)
    path.with_suffix('.bin.json').write_text(json.dumps(meta))
    assert load_artifact(path)==(data,meta)
    monkeypatch.setattr(linux.Rp2354Client,'reserve_bus',Mock(side_effect=AssertionError('dry run touched bus')))
    main(['--dry-run',str(path)])
    path.write_bytes(data[:-1]+b'X')
    with pytest.raises(ValueError): load_artifact(path)



def test_frame_boundaries_crc_and_old_format_rejection():
    packet=bytes([F.WRITE])+struct.pack('<I',0)+bytes(250)
    frame=p.request(p.Command.FLASH_UPDATE,255,packet)
    assert len(frame)==260 and frame[2]==255
    with pytest.raises(ValueError):p.request(p.Command.FLASH_UPDATE,255,packet+b'x')
    body=bytes([0,9,2])+struct.pack('<II',250,0)
    response=body+struct.pack('<H',p.crc16(body))
    assert p.parse_flash_write(p.response(p.Command.FLASH_UPDATE,response))['accepted']==250
    with pytest.raises(p.ProtocolError):p.response(p.Command.FLASH_UPDATE,response[:-1]+bytes([response[-1]^1]))
    with pytest.raises(p.ProtocolError):p.parse_flash_status(bytes([1])+bytes(127))
    with pytest.raises(ValueError):p.validate_flash_payload(bytes([F.BEGIN])+bytes(73))


def test_busy_poll_does_not_resend_write(monkeypatch):
    def frame(body):return body+struct.pack('<H',p.crc16(body))
    busy=frame(bytes([4,0]));ok=frame(bytes([0,9,2])+struct.pack('<II',250,0))
    packet=bytes([F.WRITE])+bytes(4)+bytes(250)
    monkeypatch.setattr(linux.os,'write',Mock(return_value=260))
    monkeypatch.setattr(linux.os,'read',Mock(side_effect=[busy[:2],busy[2:],ok[:2],ok[2:]]))
    monkeypatch.setattr(linux.time,'sleep',lambda n:None)
    client=linux.Rp2354Client('/dev/fake',0x62);client._fd=42
    result=client._exchange(p.Command.FLASH_UPDATE,255,packet,decode=p.parse_flash_write)
    assert result['accepted']==250
    linux.os.write.assert_called_once()
