"""Non-interactive full RP2354 factory provisioning over SWD."""
import argparse
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rp2354_artifact import load_artifact

FLASH_SIZE = 0x200000
FACTORY_OFFSET = 0x4000


def tcl_path(path):
    text = Path(path).resolve().as_posix()
    if any(c in text for c in '{}\n\r'):
        raise ValueError('Unsupported Tcl path characters')
    return '{' + text + '}'


def verify_readback(readback, partitions, factory):
    expected = bytearray(b'\xff' * FLASH_SIZE)
    expected[:len(partitions)] = partitions
    expected[FACTORY_OFFSET:FACTORY_OFFSET + len(factory)] = factory
    if readback != expected:
        offset = next((i for i, (a, b) in enumerate(zip(readback, expected)) if a != b), min(len(readback), len(expected)))
        raise ValueError(f'Flash readback mismatch at offset 0x{offset:06x}; provisioning FAILED')


class OpenOcd:
    """One hardware connection; Python controls each stage over local Tcl RPC."""
    def __init__(self, executable, log):
        self.executable, self.log = executable, log

    def __enter__(self):
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        self.output = self.log.open('w')
        self.process = subprocess.Popen([
            str(self.executable), '-s', str(self.executable.parent / 'scripts'),
            '-c', f'bindto 127.0.0.1; tcl_port {port}; telnet_port disabled; gdb_port disabled',
            '-f', 'interface/cmsis-dap.cfg', '-f', 'target/rp2350.cfg',
            '-c', 'adapter speed 1000; init; reset init'],
            stdout=self.output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while self.process.poll() is None and time.monotonic() < deadline:
                try:
                    self.connection = socket.create_connection(('127.0.0.1', port), timeout=1)
                    self.connection.settimeout(120)
                    return self
                except (ConnectionRefusedError, TimeoutError):
                    time.sleep(0.1)  # Local server startup only; no repeated SWD connection.
            raise RuntimeError(f'OpenOCD startup failed; see {self.log}. No automatic hardware retry.')
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def command(self, command):
        wrapped = 'if {[catch {' + command + '} result]} {return "ERROR:$result"}; return "OK:$result"'
        self.connection.sendall(wrapped.encode() + b'\x1a')
        result = bytearray()
        while not result.endswith(b'\x1a'):
            part = self.connection.recv(65536)
            if not part:
                raise RuntimeError('OpenOCD disconnected before acknowledging command')
            result.extend(part)
        response = result[:-1].decode()
        if not response.startswith('OK:'):
            raise RuntimeError(response)
        return response[3:]

    def __exit__(self, *_):
        if hasattr(self, 'connection'):
            self.connection.close()
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.output.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    build = args.build_dir.resolve()
    sdk = Path.home() / '.pico-sdk'
    factory_path = build / 'factory/fmheadend_rp2354_tx8.bin'
    factory, meta = load_artifact(factory_path, factory=True)
    if meta['rom_version'] != [1, 0]:
        raise ValueError('Factory provisioning requires ROM 1.0, non-TBYB')
    partitions_path = build / 'partitions.bin'
    partitions = partitions_path.read_bytes()
    source = Path(__file__).resolve().parents[1] / 'partitions.json'
    with tempfile.TemporaryDirectory(prefix='rp2354-', dir=build) as temporary:
        regenerated = Path(temporary) / 'partitions.bin'
        subprocess.run([str(sdk / 'picotool/2.3.1/picotool/picotool.exe'), 'partition', 'create', str(source), str(regenerated)], check=True)
        if not partitions or len(partitions) > FACTORY_OFFSET or partitions != regenerated.read_bytes():
            raise ValueError('Partition binary differs from the source; rebuild before provisioning')
    print(f'Validated factory {meta["build_id"]}, {len(factory)} bytes; partitions {len(partitions)} bytes')
    if args.dry_run:
        print('Dry run: no probe/device access')
        return
    log = build / 'provision-openocd.log'
    readback_path = build / 'provision-readback.bin'
    with OpenOcd(sdk / 'openocd/0.12.0+dev/openocd.exe', log) as ocd:
        chip = int(ocd.command('lindex [read_memory 0x40000000 32 1] 0'), 0)
        if chip & 0x0fffffff != 0x00004927:
            raise ValueError(f'Unexpected chip ID 0x{chip:08x}; no erase')
        ocd.command('flash probe 0')
        bank = ocd.command('array set bank [lindex [flash list] 0]; list $bank(driver) $bank(base) $bank(size)').split()
        if len(bank) != 3 or bank[0] != 'rp2xxx' or int(bank[1], 0) != 0x10000000 or int(bank[2], 0) != FLASH_SIZE:
            raise ValueError(f'Unexpected flash geometry {bank}; no erase')
        print(f'RP2350-family chip 0x{chip:08x}; detected 2 MiB flash (RP2354 board)')
        print('Erasing ALL 2 MiB, including B and journal')
        ocd.command('flash erase_sector 0 0 last')
        ocd.command(f'flash write_bank 0 {tcl_path(partitions_path)} 0')
        ocd.command(f'flash write_bank 0 {tcl_path(factory_path)} 0x4000')
        # Physical, uncached alias: independent of any boot-selected A/B ATRANS.
        ocd.command(f'dump_image {tcl_path(readback_path)} 0x1c000000 0x200000')
        verify_readback(readback_path.read_bytes(), partitions, factory)
        print('Byte-exact full-flash verification passed; both images match and all other bytes are erased')
        try:
            ocd.command('reset run')
        except (OSError, RuntimeError) as exc:
            raise RuntimeError('Full flash contents VERIFIED, but automatic normal reset was not acknowledged. '
                               'Release BOOTSEL, perform a manual normal reset and check I2C STATUS. '
                               f'OpenOCD: {exc}; log: {log}') from exc
    print(f'Factory provisioning SUCCESS. Readback SHA256: {hashlib.sha256(readback_path.read_bytes()).hexdigest()}')
    print('Normal boot requested. If BOOTSEL is held, release it and reset; verify STATUS before OTA.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Provisioning failed: {exc}', file=sys.stderr)
        sys.exit(1)
