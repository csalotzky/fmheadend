#!/usr/bin/env python3
"""Fresh, bounded-retry V1 I2C OTA upload; no daemon or bus configuration changes."""
import argparse
import errno
import logging
import struct
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.rp2354 import protocol as p
from daemon.rp2354.linux_client import Rp2354Client
from firmware.fmheadend_rp2354.tools.rp2354_artifact import load_artifact
F = p.FlashCommand
TRANSIENT = (p.ProtocolError, p.HardwareUnavailable, p.InternalError, p.Busy)
log = logging.getLogger('rp2354.ota')


class UpdateError(RuntimeError): pass


class Uploader:
    def __init__(self, client, clock=time.monotonic, sleep=time.sleep, progress=print):
        self.client, self.clock, self.sleep, self.progress = client, clock, sleep, progress
        self.recoveries = 0
        self.reboot_nacks = 0
        self.reboot_timeouts = 0
        self._reboot_deadline = 0
        self._pending_reboot_nacks = 0
        self._last_counted_error = 0

    def reboot_window(self, deadline=0, *, verified=False):
        # Reclassify only after the expected boot is actually observed. Failed
        # or timed-out boots leave their ENXIO events in the client's error total.
        if verified:
            self.reboot_nacks += self._pending_reboot_nacks
        self._pending_reboot_nacks = 0
        self._reboot_deadline = deadline

    @property
    def i2c_errors(self):
        return self.client.i2c_errors - self.reboot_nacks + self.reboot_timeouts

    def uncertain(self, operation, exc):
        if isinstance(exc, p.IncompatibleProtocol):
            raise exc
        self.recoveries += 1
        count = getattr(self.client, 'i2c_errors', 0)
        cause = exc.__cause__
        if (count > self._last_counted_error and self.clock() < self._reboot_deadline and
                isinstance(cause, OSError) and cause.errno == errno.ENXIO and
                getattr(exc, 'stage', None) in ('write', 'header', 'payload')):
            self._pending_reboot_nacks += 1
            log.warning('%s: ENXIO during acknowledged reboot; awaiting boot verification', operation)
        self._last_counted_error = count
        log.warning('%s uncertain: %s; reconciling/retrying', operation, exc)
        self.sleep(max(0.02, getattr(self.client, 'retry_delay', 0)))

    def retry(self, operation, call):
        for attempt in range(6):
            try:
                return call()
            except TRANSIENT as exc:
                self.uncertain(operation, exc)
                if attempt == 5:
                    raise UpdateError(f'{operation}: retry limit reached') from exc

    def status(self):
        return self.retry('STATUS', lambda: self.client.flash_update(bytes([F.STATUS])))

    def check_rf_off(self):
        health = self.retry('GET_STATUS', self.client.get_status)
        if any(tx['tx_state'] == 'ON' for tx in health['modulators']):
            raise UpdateError('Unexpected RF ON')

    @staticmethod
    def identity(s, meta, target, verified=False):
        if (s['target'] != target or s['size'] != meta['size'] or
                s['sha256'].hex() != meta['sha256'] or
                (verified and s['build'].hex() != meta['build_id'])):
            raise UpdateError('Image/target mismatch; stopping')

    def finalized(self, meta, target, deadline):
        """Reconcile lost CONFIRM ACK; normal firmware must return after buy."""
        sends = 0
        while self.clock() < deadline:
            try:
                s = self.client.flash_update(bytes([F.STATUS]))
                info = self.client.get_info()
                self.identity(s, meta, target, True)
                if (s['boot'] != target or s['running_build'].hex() != meta['build_id'] or
                        s['version'] != tuple(meta['rom_version']) or not s['ready'] or
                        info['rp_protocol_version'] != 1 or info['rp_firmware_version'] != meta['firmware_version']):
                    raise UpdateError('New firmware identity/health not established')
                if s['state'] == 6 or s['error'] not in (0, 5):
                    raise UpdateError('Finalization failed; no success assumed')
                if s['state'] == 4 and s['tbyb'] == 0 and not s['maintenance'] and not s['error']:
                    self.reboot_window(verified=True)
                    self.check_rf_off()
                    return s
                self.reboot_window()
                if s['state'] in (3, 4) and sends < 6:
                    sends += 1
                    ack = self.client.flash_update(bytes([F.CONFIRM]))
                    if ack['state'] == 4 and ack['tbyb'] == 0 and ack['maintenance'] and not ack['error']:
                        self.reboot_window(deadline)
            except TRANSIENT as exc:
                self.uncertain('CONFIRM/normal boot', exc)
            self.sleep(0.05)
        self.reboot_timeouts += 1
        self.reboot_window()
        raise UpdateError('Finalized normal firmware not observed before deadline')

    def run(self, data, meta):
        started = self.clock()
        info = self.retry('GET_INFO', self.client.get_info)
        old = self.status()
        # An interrupted trial must roll back before its fallback may be erased.
        # Do not buy an unobserved trial merely to make a new BEGIN possible.
        deadline = self.clock() + 20
        while old['tbyb'] == 1 and self.clock() < deadline:
            self.sleep(0.25)
            old = self.status()
        if (info['rp_protocol_version'] != 1 or old['boot'] not in (0, 1) or
                old['inactive'] != (old['boot'] ^ 1) or old['tbyb'] != 0 or not old['ready']):
            raise UpdateError('Require valid A/B layout and healthy bought firmware; trial still pending')
        # A successful buy with lost ACK can need its normal reboot. Complete
        # that checked operation, then still perform the requested fresh upload.
        if old['target'] == old['boot'] and old['state'] in (3, 4) and old['maintenance']:
            previous = dict(size=old['size'], sha256=old['sha256'].hex(),
                            build_id=old['build'].hex(), rom_version=old['version'],
                            firmware_version=old['firmware_version'])
            self.finalized(previous, old['boot'], self.clock()+10)
            old = self.status()
        if old['capacity'] != 0xf0000 or old['chunk'] != p.FLASH_CHUNK:
            raise UpdateError('Unsupported OTA receiver geometry')
        target = old['inactive']
        self.progress(f"Slot {old['boot']} -> {target}; {len(data)} bytes; chunks {p.FLASH_CHUNK}")
        packet = bytes([F.BEGIN]) + struct.pack('<I', len(data)) + bytes.fromhex(meta['sha256'])
        # A damaged BEGIN ACK can still mean successful erase. Reconcile the
        # empty receiver before retrying; no image data has been sent yet.
        for attempt in range(6):
            try:
                current = self.client.flash_update(packet)
            except TRANSIENT as exc:
                self.uncertain('BEGIN', exc)
                current = self.status()
            if (current['state'] == 1 and current['target'] == target and
                    current['size'] == len(data) and current['sha256'].hex() == meta['sha256'] and
                    not current['accepted'] and not current['committed'] and not current['error']):
                break
        else:
            raise UpdateError('Fresh BEGIN retry limit reached')
        self.identity(current, meta, target)
        if current['state'] != 1 or current['accepted'] or current['committed'] or current['error']:
            raise UpdateError('Fresh BEGIN not established')
        offset, last_progress = 0, self.clock()
        while offset < len(data):
            block = data[offset:offset+p.FLASH_CHUNK]
            packet = bytes([F.WRITE]) + struct.pack('<I', offset) + block
            for attempt in range(6):
                try:
                    ack = self.client.flash_update(packet)
                except TRANSIENT as exc:
                    self.uncertain('WRITE', exc)
                    ack = self.status()
                    self.identity(ack, meta, target)
                    if ack['state'] != 1 or ack['error']:
                        raise UpdateError('Receiver interrupted; rerun uploader for a fresh BEGIN')
                    if ack['accepted'] == offset:
                        continue
                if ack['accepted'] != offset + len(block):
                    raise UpdateError('Unexpected WRITE offset; no blind replay')
                durable = ack['accepted'] if ack['accepted'] == len(data) else ack['accepted']//256*256
                if ack['committed'] != durable:
                    raise UpdateError('Unexpected durable WRITE offset')
                offset = ack['accepted']
                break
            else:
                raise UpdateError('WRITE retry limit reached')
            if self.clock()-last_progress >= 1 or offset == len(data):
                elapsed = max(self.clock()-started, 0.001)
                self.progress(f'{offset}/{len(data)} bytes; {offset/elapsed/1024:.1f} KiB/s; {elapsed:.1f}s')
                last_progress = self.clock()
        for attempt in range(6):
            try:
                verified = self.client.flash_update(bytes([F.VERIFY]))
            except TRANSIENT as exc:
                self.uncertain('VERIFY', exc)
                verified = self.status()
            self.identity(verified, meta, target)
            if verified['state'] == 2 and verified['committed'] == len(data) and not verified['error']:
                self.identity(verified, meta, target, True)
                break
            if verified['state'] != 1 or verified['error']:
                raise UpdateError('Flash SHA256/image verification failed')
        else:
            raise UpdateError('VERIFY retry limit reached')
        # Reissue ACTIVATE only after observing the original slot with the same
        # verified image. A complete ACK, including final STOP, gates reboot.
        deadline, sends = self.clock()+12, 0
        while self.clock() < deadline:
            try:
                s = self.client.flash_update(bytes([F.STATUS]))
                self.identity(s, meta, target, True)
                if s['boot'] == target:
                    if (s['state'] != 3 or s['tbyb'] != 1 or not s['ready'] or s['error'] or
                            s['running_build'].hex() != meta['build_id'] or s['version'] != tuple(meta['rom_version'])):
                        raise UpdateError('Trial identity/health check failed; allow rollback')
                    self.reboot_window(verified=True)
                    self.check_rf_off()
                    result = self.finalized(meta, target, self.clock()+10)
                    elapsed = self.clock()-started
                    self.progress(f"Confirmed slot {target}, build {meta['build_id']}; {elapsed:.1f}s; "
                                  f"reconciliations {self.recoveries}; RF off")
                    return result
                if s['boot'] != old['boot'] or s['state'] not in (2, 3) or s['error']:
                    raise UpdateError('Activation/rollback state unexpected')
                self.reboot_window()
                if sends < 6:
                    sends += 1
                    ack = self.client.flash_update(bytes([F.ACTIVATE]))
                    if ack['state'] == 3 and not ack['error']:
                        self.reboot_window(deadline)
            except TRANSIENT as exc:
                self.uncertain('ACTIVATE/trial boot', exc)
            self.sleep(0.05)
        self.reboot_timeouts += 1
        self.reboot_window()
        raise UpdateError('Trial not observed; update not confirmed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path, nargs='?')
    parser.add_argument('--device', default='/dev/i2c-1')
    parser.add_argument('--address', type=lambda s: int(s, 0), default=0x62)
    parser.add_argument('--dry-run', action='store_true', help='Validate locally; no I2C access')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--log', type=Path, default=Path('rp2354-upload.log'))
    args = parser.parse_args(argv)
    logging.basicConfig(filename=args.log, level=logging.WARNING,
                        format='%(asctime)s %(name)s %(message)s')
    if args.status and args.dry_run:
        parser.error('--status requires I2C access')
    if not args.status:
        if args.image is None:
            parser.error('Image required')
        data, meta = load_artifact(args.image)
        if args.dry_run:
            print(f"Validated {len(data)} bytes; ROM {meta['rom_version']}; no I2C access")
            return
    client = Rp2354Client(args.device, args.address, timeout_ms=1000)
    uploader = Uploader(client)
    try:
        client.reserve_bus()
        if args.status:
            print({k: v.hex() if isinstance(v, bytes) else v for k, v in uploader.status().items()})
        else:
            uploader.run(data, meta)
    finally:
        print(f'I2C errors: {uploader.i2c_errors}')
        print(f'Reboot NACKs: {uploader.reboot_nacks}; diagnostics: {args.log}')
        client.close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, UpdateError, p.DependencyUnavailable, *TRANSIENT, p.RpResponseError) as exc:
        print(f'Update failed: {exc}', file=sys.stderr)
        sys.exit(1)
