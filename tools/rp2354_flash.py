#!/usr/bin/env python3
"""Standalone V1 I2C updater: no HTTP backend, bus speed changes or daemon control."""
import argparse
import struct
import sys
import time
import uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from daemon.rp2354 import protocol as p
from daemon.rp2354.linux_client import Rp2354Client
from tools.rp2354_artifact import load_artifact
F=p.FlashCommand


class UpdateError(RuntimeError): pass


class Uploader:
    def __init__(self,client,clock=time.monotonic,sleep=time.sleep,progress=print):
        self.client,self.clock,self.sleep,self.progress=client,clock,sleep,progress

    def status(self):
        deadline=self.clock()+2
        while True:
            try: return self.client.flash_update(bytes([F.STATUS]))
            except (p.ProtocolError,p.HardwareUnavailable,p.InternalError):
                if self.clock()>=deadline: raise
                self.sleep(0.05)

    def check_rf_off(self):
        # A normal reboot initializes the MCU with RF off. External QN faults
        # may still be reported as ERROR; they must not result in RF ON.
        health=self.client.get_status()
        if any(tx['tx_state']=='ON' for tx in health['modulators']):
            raise UpdateError('Unexpected RF ON; normal recovery not established')

    @staticmethod
    def identity(s,session,meta,target):
        if (s['session']!=session or s['size']!=meta['size'] or s['target']!=target or
                s['sha256'].hex()!=meta['sha256'] or s['build'].hex()!=meta['build_id']):
            raise UpdateError('Session/image/target mismatch; no further writes or CONFIRM')

    def wait_normal(self, expected, info, command, session):
        """Observe normal operation, not merely a bought flag or a reset gap.

        Reissue only after a matching STATUS: a partially read ACK may have
        prevented reset. CONFIRM/ABORT are idempotent for this exact session.
        """
        deadline=self.clock()+10
        retries=2
        while self.clock()<deadline:
            self.sleep(0.05)
            try:
                current_info=self.client.get_info()
                s=self.client.flash_update(bytes([F.STATUS]))
                if (s['boot']!=expected['boot'] or s['running_build']!=expected['running_build'] or
                    s['version']!=expected['version'] or current_info!=info or s['tbyb']!=0):
                    raise UpdateError('Unexpected firmware after normal reboot; success not proven')
                if command==F.CONFIRM:
                    for key in ('session','target','size','sha256','build'):
                        if s[key]!=expected[key]: raise UpdateError('Confirmation identity changed after reboot')
                    if s['state']==6 or s['error']==10:
                        raise UpdateError('ROM confirmation failed; cleared pending bit is not success')
                if not s['maintenance']:
                    if not s['ready'] or s['error'] or (command==F.CONFIRM and s['state']!=4):
                        raise UpdateError('Firmware returned without healthy finalized state')
                    self.check_rf_off()
                    return s
                if s['session']!=session:
                    raise UpdateError('Recovery session changed; no replay')
                if retries:
                    retries-=1
                    self.client.flash_update(bytes([command])+session)
            except (p.ProtocolError,p.HardwareUnavailable,p.InternalError):
                pass
        raise UpdateError('Normal operation not observed after ACK; inspect STATUS, no success assumed')

    def abort(self, session):
        info=self.client.get_info()
        before=self.status()
        if before['session']!=session or before['tbyb']!=0 or before['boot'] not in (0,1):
            raise UpdateError('ABORT session/boot mismatch')
        try:
            if self.client.flash_update(bytes([F.ABORT])+session)['state']!=7:
                raise UpdateError('Abort not established')
        except (p.ProtocolError,p.HardwareUnavailable,p.InternalError):
            pass
        result=self.wait_normal(before,info,F.ABORT,session)
        self.progress('ABORT recovery complete; original firmware running normally, RF remains off')
        return result

    def run(self,data,meta):
        info=self.client.get_info()
        old=self.status()
        if (info['rp_protocol_version']!=1 or old['boot'] not in (0,1) or
            old['inactive']!=(old['boot']^1) or old['tbyb']!=0 or not old['ready']):
            raise UpdateError('Require valid A/B layout, no pending trial and successful MCU initialization')
        # A previous uploader may have died after buy, before consuming the
        # CONFIRM ACK. Re-running the identical artifact must finish the normal
        # reboot lifecycle, without beginning another update or buying again.
        if (old['state'] in (3,4) and old['boot']==old['target'] and
                old['running_build'].hex()==meta['build_id'] and old['version']==tuple(meta['rom_version'])):
            self.identity(old,old['session'],meta,old['boot'])
            if info['rp_firmware_version']!=meta['firmware_version'] or old['error'] not in (0,5):
                raise UpdateError('Finalization recovery identity/health check failed')
            self.check_rf_off()
            result=self.wait_normal(old,info,F.CONFIRM,old['session'])
            self.progress('Previous buy finalized; matching firmware running normally, RF off')
            return result
        if tuple(meta['rom_version'])<=old['version']:
            raise UpdateError('ROM image version must increase; downgrades unsupported')
        if old['capacity']!=0xf0000 or old['chunk']!=128:
            raise UpdateError('Unsupported receiver geometry')
        session=uuid.uuid4().bytes
        if old['state'] in (1,2,5) and old['sha256'].hex()==meta['sha256'] and old['build'].hex()==meta['build_id']:
            session=old['session']
        target=old['inactive']
        self.progress(f"Active slot {old['boot']}; target {target}; session {session.hex()}")
        packet=bytes([F.BEGIN])+session+struct.pack('<I',len(data))+bytes.fromhex(meta['sha256'])+bytes.fromhex(meta['build_id'])+struct.pack('<HHB',*meta['rom_version'],1)
        try: current=self.client.flash_update(packet)
        except (p.ProtocolError,p.HardwareUnavailable,p.InternalError): current=self.status()
        self.identity(current,session,meta,target)
        if current['state'] not in (1,2): raise UpdateError('BEGIN not established; inspect STATUS')
        offset=current['accepted']
        while offset<len(data):
            block=data[offset:offset+128]
            packet=bytes([F.WRITE])+session+struct.pack('<IB',offset,len(block))+block
            try: ack=self.client.flash_update(packet)
            except (p.ProtocolError,p.HardwareUnavailable,p.InternalError):
                ack=self.status()  # Never replay an uncertain write before reconciliation.
                self.identity(ack,session,meta,target)
                if ack['state']!=1: raise UpdateError('Upload interrupted; restart from BEGIN')
                if ack['accepted']==offset: ack=self.client.flash_update(packet)
            self.identity(ack,session,meta,target)
            if ack['state']!=1 or ack['accepted']!=offset+len(block): raise UpdateError('Unexpected WRITE offset')
            expected=ack['accepted'] if ack['accepted']==len(data) else ack['accepted']//256*256
            if ack['committed']!=expected: raise UpdateError('Unexpected durable WRITE offset')
            offset=ack['accepted']
            self.progress(f"Accepted {offset}/{len(data)}; committed {ack['committed']}")
        verified=self.client.flash_update(bytes([F.VERIFY])+session)
        self.identity(verified,session,meta,target)
        if verified['state']!=2 or verified['committed']!=len(data): raise UpdateError('VERIFY failed')
        self.progress('Flash SHA256 matches: '+meta['sha256'])
        try:
            activated=self.client.flash_update(bytes([F.ACTIVATE])+session)
            self.identity(activated,session,meta,target)
            if activated['state']!=3: raise UpdateError('ACTIVATE failed')
        except (p.ProtocolError,p.HardwareUnavailable,p.InternalError):
            self.progress('ACTIVATE ACK uncertain; observe boot, do not replay')
        deadline=self.clock()+12
        while self.clock()<deadline:
            try:
                info=self.client.get_info()
                s=self.client.flash_update(bytes([F.STATUS]))
                if s['boot']==target:
                    self.identity(s,session,meta,target)
                    if (s['state']!=3 or s['tbyb']!=1 or not s['ready'] or s['error'] or
                        s['running_build'].hex()!=meta['build_id'] or s['version']!=tuple(meta['rom_version']) or
                        info['rp_protocol_version']!=1 or info['rp_firmware_version']!=meta['firmware_version']):
                        raise UpdateError('Trial identity/health check failed; allow ROM rollback')
                    self.check_rf_off()  # Existing V1 CRC and payload validation.
                    if self.clock()+3>=deadline: raise UpdateError('Trial deadline too close; allow rollback')
                    try: self.client.flash_update(bytes([F.CONFIRM])+session)
                    except (p.ProtocolError,p.HardwareUnavailable,p.InternalError): pass
                    confirmed=self.wait_normal(s,info,F.CONFIRM,session)
                    self.progress(f"Confirmed slot {target}, build {meta['build_id']}; normal operation verified, RF off")
                    return confirmed
            except (p.ProtocolError,p.HardwareUnavailable,p.InternalError): pass
            self.sleep(0.05)
        rollback_deadline=self.clock()+8
        while self.clock()<rollback_deadline:
            try:
                s=self.client.flash_update(bytes([F.STATUS]))
                if s['boot']==old['boot'] and s['running_build']==old['running_build']:
                    raise UpdateError('Old firmware returned or activation never occurred; update not confirmed')
            except (p.ProtocolError,p.HardwareUnavailable,p.InternalError): pass
            self.sleep(0.1)
        raise UpdateError('Trial not confirmed; rollback expected, final hardware state unavailable')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image',type=Path,nargs='?')
    parser.add_argument('--device',default='/dev/i2c-1')
    parser.add_argument('--address',type=lambda s:int(s,0),default=0x62)
    parser.add_argument('--dry-run',action='store_true',help='Validate locally; no I2C access')
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--status',action='store_true',help='Read update/boot status only')
    parser.add_argument('--abort-session',help='Abort the specified 16-byte hex session instead of uploading')
    args=parser.parse_args(argv)
    if args.status and args.dry_run: parser.error('--status accesses I2C; it cannot be combined with --dry-run')
    if args.status:
        client=Rp2354Client(args.device,args.address,timeout_ms=1000)
        try:
            client.reserve_bus()
            status=Uploader(client).status()
            print({k:v.hex() if isinstance(v,bytes) else v for k,v in status.items()})
        finally: client.close()
        return
    if args.abort_session:
        session=bytes.fromhex(args.abort_session)
        if len(session)!=16: parser.error('Session requires 16 bytes')
    else:
        if args.image is None: parser.error('Image required')
        data,meta=load_artifact(args.image)
        print(f"Validated {meta['size']} bytes; ROM {meta['rom_version']}; SHA256 {meta['sha256']}")
    if args.dry_run:
        print('No I2C access. Plan: inactive slot only; BEGIN/WRITE/VERIFY/ACTIVATE; checked CONFIRM.')
        return
    if not args.yes and input('Daemon must be stopped manually; RF/audio stop during maintenance. Type UPDATE: ')!='UPDATE':
        raise UpdateError('Cancelled')
    client=Rp2354Client(args.device,args.address,timeout_ms=1000)
    try:
        client.reserve_bus()
        if args.abort_session:
            Uploader(client).abort(session)
        else: Uploader(client).run(data,meta)
    finally: client.close()


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,UpdateError,p.DependencyUnavailable) as exc:
        print(f'Update failed: {exc}',file=sys.stderr)
        sys.exit(1)
