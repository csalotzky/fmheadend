import hashlib
import json
import struct
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fmheadendd.rp2354.artifact import inspect_image, load_artifact
from fmheadendd.rp2354.update import Uploader, UpdateError, main, F
from fmheadendd.rp2354 import protocol as p
from fmheadendd.rp2354 import linux_client as linux


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


@pytest.mark.parametrize('offset,value', [
    (0, 0), (4, 0x20000001), (256, 0), (260, 0x10210142),
    (264, 0), (272, 0), (276, 0xffffffff), (280, 0),
    (384, 0), (432, 0), (512, 0), (524, 0),
])
def test_rejects_structurally_invalid_images_even_with_a_manifest(tmp_path, offset, value):
    data, meta = artifact()
    broken = bytearray(data)
    struct.pack_into('<I', broken, offset, value)
    path = tmp_path / 'firmware.bin'
    path.write_bytes(broken)
    meta['sha256'] = hashlib.sha256(broken).hexdigest()
    Path(str(path) + '.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        load_artifact(path)


@pytest.mark.parametrize('field,value', [
    ('format', 2), ('image_type', 'other'), ('runtime_base', 0x10004000),
    ('tbyb', False), ('size', 533), ('sha256', '00' * 32),
    ('build_id', '00' * 16), ('firmware_version', 'other'), ('rom_version', [1, 0]),
])
def test_rejects_inconsistent_manifest_fields(tmp_path, field, value):
    data, meta = artifact()
    path = tmp_path / 'firmware.bin'
    path.write_bytes(data)
    meta[field] = value
    Path(str(path) + '.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='manifest'):
        load_artifact(path)


def test_sha256_detects_payload_tampering(tmp_path):
    data, meta = artifact()
    broken = bytearray(data)
    broken[100] ^= 1  # Outside all structural metadata.
    path = tmp_path / 'firmware.bin'
    path.write_bytes(broken)
    Path(str(path) + '.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='manifest'):
        load_artifact(path)


@pytest.mark.parametrize('size', [0, 511, 0xf0001])
def test_rejects_images_outside_slot_bounds(size):
    with pytest.raises(ValueError, match='slot'):
        inspect_image(bytes(size))


def test_missing_or_invalid_manifest_is_rejected(tmp_path):
    data, _ = artifact()
    path = tmp_path / 'firmware.bin'
    path.write_bytes(data)
    with pytest.raises(FileNotFoundError):
        load_artifact(path)
    Path(str(path) + '.json').write_text('{broken')
    with pytest.raises(ValueError):
        load_artifact(path)


def test_trial_identity_mismatch_is_never_confirmed():
    fake = Fake()
    fake.s['running_build'] = b'x' * 16
    with pytest.raises(UpdateError, match='Trial identity'):
        runner(fake).run(*artifact())
    assert F.CONFIRM not in fake.calls


def test_unexpected_rf_on_is_never_confirmed():
    fake = Fake()
    fake.get_status = lambda: {'modulators': [{'tx_state': 'ON'}]}
    with pytest.raises(UpdateError, match='RF ON'):
        runner(fake).run(*artifact())
    assert F.CONFIRM not in fake.calls


@pytest.fixture(scope='module')
def installed_updater(tmp_path_factory):
    root = tmp_path_factory.mktemp('installed-controller-update')
    checkout = Path(__file__).resolve().parents[2]
    # Build from only the Python package: no tools/ or Controller checkout.
    source = root / 'package-source'
    shutil.copytree(checkout / 'daemon', source,
                    ignore=shutil.ignore_patterns('tests', '__pycache__', '*.egg-info', 'build'))
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-index', '--no-deps', '--no-build-isolation',
                    '--wheel-dir', str(root), str(source)], check=True, capture_output=True, text=True)
    environment = root / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], check=True)
    scripts = environment / ('Scripts' if os.name == 'nt' else 'bin')
    python = scripts / ('python.exe' if os.name == 'nt' else 'python')
    wheel = next(root.glob('fmheadendd-*.whl'))
    subprocess.run([sys.executable, '-m', 'pip', '--python', str(python), 'install',
                    '--no-index', '--no-deps', str(wheel)], check=True, capture_output=True, text=True)
    launcher = scripts / ('fmheadend-controller-update.exe' if os.name == 'nt' else 'fmheadend-controller-update')
    wrapper = root / 'rp2354_flash.py'
    shutil.copy2(checkout / 'tools/rp2354_flash.py', wrapper)
    return root, python, launcher, wrapper


def test_installed_cli_and_wrapper_without_source_repositories(installed_updater):
    root, python, launcher, wrapper = installed_updater
    # Isolated Python cannot import the source tree; reject firmware imports too.
    probe = '''import importlib.abc, pathlib, sys
class NoController(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('firmware', 'daemon', 'rp2354_artifact'):
            raise AssertionError('Source-tree import: ' + fullname)
sys.meta_path.insert(0, NoController())
from fmheadendd.rp2354 import update
assert pathlib.Path(update.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix))
'''
    subprocess.run([str(python), '-I', '-c', probe], cwd=root, check=True, capture_output=True, text=True)
    for command in ([str(launcher)], [str(python), '-I', str(wrapper)]):
        result = subprocess.run([*command, '--help'], cwd=root, check=True, capture_output=True, text=True)
        assert '--device' in result.stdout and '--dry-run' in result.stdout and '--status' in result.stdout


def test_installed_cli_validates_downloaded_pair_before_bus_access(installed_updater):
    root, _, launcher, _ = installed_updater
    data, meta = artifact()
    path = root / 'downloaded.bin'
    path.write_bytes(data)
    manifest = Path(str(path) + '.json')
    manifest.write_text(json.dumps(meta))
    command = [str(launcher), '--dry-run', '--device', str(root / 'nonexistent-device'), str(path)]
    result = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
    assert 'Validated' in result.stdout and 'no I2C access' in result.stdout
    meta['build_id'] = '00' * 16
    manifest.write_text(json.dumps(meta))
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert result.returncode == 1 and 'manifest' in result.stderr



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
