"""Read-only installer inspection, safe archive extraction and SQLite backup.

No package installation, boot writes, service control or credential handling.
"""
import argparse
from contextlib import closing
import json
import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import socket
import subprocess
import tarfile
from urllib.error import HTTPError
from urllib.request import urlopen

from . import setup_pi
from daemon.regions import PLANS


def hardware_plan(model_path, spi_device='/dev/spidev0.0', spi_clock_hz=25000000):
    model = setup_pi._read_pi_model(model_path)
    if re.match(r'^Raspberry Pi (?:Zero(?! 2)|1(?:\s|$)|Model [AB]|Compute Module (?:1(?:\s|$)|Rev))', model):
        raise ValueError('This model cannot run the required 64-bit Raspberry Pi OS.')
    options = setup_pi._options_from_namespace(setup_pi.argument_parser().parse_args([]), model_path)
    recommendation = ('default/moderate_loud.json' if re.match(r'^Raspberry Pi (?:4|5|400|Compute Module 4)(?:\s|$)', model)
                      else 'default/basic_limiter.json' if 'Zero 2' in model
                      else 'default/basic_wideband.json')
    device = '/dev/i2c-1' if options.i2c_mode == 'hw' else f'/dev/i2c-{options.i2c_bus}'
    i2c = (f'Hardware I2C, {options.i2c_speed} Hz' if options.i2c_mode == 'hw'
           else f'Software I2C, GPIO2/3, bus {options.i2c_bus}, delay {options.i2c_delay_us} us')
    rows = [('I2C mode', 'Software I2C' if options.i2c_mode == 'sw' else 'Hardware I2C'),
            ('I2C pins', 'GPIO2 / GPIO3'), ('I2C bus', device)]
    rows.append(('I2C delay', f'{options.i2c_delay_us} us') if options.i2c_mode == 'sw'
                else ('I2C speed', f'{options.i2c_speed // 1000} kHz'))
    rows += [('', ''), ('SPI interface', 'SPI0'), ('SPI device', str(spi_device)),
             ('SPI clock', f'{int(str(spi_clock_hz)) / 1000000:g} MHz'),
             ('SPI buffer', f'{setup_pi.SPI_BUFSIZ} bytes'), ('', ''),
             ('Core clock', 'Fixed' if options.fix_core_clock else 'Unchanged'),
             ('I2C module', 'Enabled at boot')]
    return [model, device, recommendation, i2c,
            'Fixed core clock' if options.fix_core_clock else 'Core clock unchanged',
            *(f'  {label + ":":<16} {value}' if label else '' for label, value in rows)]


def region_menu():
    lines = []
    for region, plan in PLANS.items():
        name = plan.region_name.split(' /')[0]
        detail = ('Full Controller-supported frequency range and raster' if region == 0 else
                  f'{plan.fm_min_khz / 1000:.1f}-{plan.fm_max_khz / 1000:.1f} MHz | {plan.fm_step_khz} kHz raster')
        lines.extend([f'  {region}) {name}', f'     {detail}', ''])
    return '\n'.join(lines)


def profile_catalog(root):
    result = []
    for path in sorted(Path(root).glob('*.json')):
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.json', path.name):
            raise ValueError(f'Invalid bundled profile filename: {path.name}')
        data = json.loads(path.read_text(encoding='utf-8'))
        name = data.get('name') if isinstance(data, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f'Missing profile display name: {path.name}')
        if '[TX Soft Clip ON]' not in name:
            result.append(('default/' + path.name, ' '.join(name.split())))
    if not result:
        raise ValueError('No bundled Audio Profiles found')
    return sorted(result, key=lambda item: item[1].casefold())


def runtime_settings(path, default_database):
    assignments = []
    for line in Path(path).read_text().splitlines():
        if re.match(r'^\s*FMHEADEND_ARGS\s*=', line):
            assignments.append(line.split('=', 1)[1].strip())
    if len(assignments) != 1:
        raise ValueError(f'{path}: require exactly one single-line FMHEADEND_ARGS assignment')
    raw = assignments[0]
    # Parse data, never source/eval an administrator's environment file.
    if raw.startswith(('"', "'")):
        values = shlex.split(raw)
        if len(values) != 1:
            raise ValueError(f'{path}: ambiguous FMHEADEND_ARGS quoting')
        raw = values[0]
    args = shlex.split(raw)
    values = {'--database': str(default_database), '--region': '1', '--i2c-device': '/dev/i2c-1',
              '--host': '0.0.0.0', '--port': '8080', '--passwd-file': ''}
    seen = set()
    for index, argument in enumerate(args):
        key, equals, value = argument.partition('=')
        if key not in values:
            if any(option.startswith(key) for option in values) and key.startswith('--'):
                raise ValueError(f'{path}: abbreviated startup option {key} requires manual review')
            continue
        if key in seen:
            raise ValueError(f'{path}: repeated startup option {key}')
        seen.add(key)
        if not equals:
            if index + 1 < len(args) and not args[index + 1].startswith('--'):
                value = args[index + 1]
            elif key == '--passwd-file':
                value = '/var/lib/fmheadend/htpasswd'
            else:
                raise ValueError(f'{path}: missing value for {key}')
        if not value or any(c in value for c in '\n\r\x00$`'):
            raise ValueError(f'{path}: ambiguous value for {key}')
        values[key] = value
    if not Path(values['--database']).is_absolute():
        raise ValueError(f'{path}: relative/in-memory database requires manual review')
    if values['--region'] not in ('0', '1', '2', '3'):
        raise ValueError(f'{path}: invalid region')
    if not values['--port'].isdigit() or not 1 <= int(values['--port']) <= 65535:
        raise ValueError(f'{path}: invalid HTTP port')
    return [values[key] for key in ('--database', '--region', '--i2c-device', '--passwd-file', '--host', '--port')]


def webui_urls(interfaces, host, port):
    """Only advertise active, assigned addresses reachable through this bind.

    IPv6 wildcard is treated as IPv6-only, as with asyncio/uvicorn sockets.
    UNKNOWN operational state permits UP tunnels (e.g. WireGuard).
    """
    host = str(host).strip('[]')
    port = int(str(port))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid HTTP port')
    bound = None
    if host not in ('0.0.0.0', '::'):
        try:
            bound = {ipaddress.ip_address(host)}
        except ValueError:
            bound = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    result = []
    for interface in interfaces:
        if 'UP' not in interface.get('flags', []) or interface.get('operstate') not in ('UP', 'UNKNOWN'):
            continue
        name = interface['ifname']
        for info in interface.get('addr_info', []):
            if (info.get('scope') != 'global' or info.get('tentative') or info.get('dadfailed')
                    or info.get('deprecated') or info.get('preferred_life_time') == 0
                    or set(info.get('flags', [])) & {'tentative', 'dadfailed', 'deprecated'}):
                continue
            address = ipaddress.ip_address(info['local'])
            if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved:
                continue
            if bound is not None and address not in bound:
                continue
            if host == '0.0.0.0' and address.version != 4 or host == '::' and address.version != 6:
                continue
            literal = f'[{address}]' if address.version == 6 else str(address)
            entry = (name, f'http://{literal}:{port}')
            if entry not in result:
                result.append(entry)
    return result


def network_urls(host, port):
    interfaces = json.loads(subprocess.check_output(['ip', '-j', 'address', 'show'], text=True))
    urls = webui_urls(interfaces, str(host), port)
    for interface, url in urls:
        print(f'  {interface:<12} {url}')
    if not urls:
        print('  No active network address matches the configured HTTP bind.')
        print(f'  Configured bind: {host}, port {port}.')
        print('  Check the bind address and network configuration before connecting.')


def realtime_status(pid):
    pid = int(str(pid))
    if pid <= 0:
        raise ValueError('Mux daemon has no running main process')
    policy = os.sched_getscheduler(pid)
    priority = os.sched_getparam(pid).sched_priority
    if policy != os.SCHED_FIFO or priority != 50:
        name = next((name for name in ('SCHED_OTHER', 'SCHED_FIFO', 'SCHED_RR', 'SCHED_BATCH', 'SCHED_IDLE')
                     if getattr(os, name, None) == policy), str(policy))
        raise ValueError(f'PID {pid}: policy {name} ({policy}), priority {priority}; expected SCHED_FIFO / 50')
    print(f'Mux realtime scheduling: SCHED_FIFO / 50 (PID {pid})')


def check_database(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError(f'Expected an existing regular database: {path}')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'system_config', 'modulators'}.issubset(tables):
            raise ValueError(f'{path}: not an initialized FMheadend database; refusing automatic initialization')
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError(f'{path}: SQLite integrity check failed')


def service_contract(actual, expected):
    def startup(paths):
        values = []
        for path in paths:
            section = ''
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith('['):
                    section = line
                if section == '[Service]' and line.startswith(('ExecStart=', 'EnvironmentFile=')):
                    values.append(line)
                if section == '[Service]' and line.startswith('Environment=') and any(
                        name in line for name in ('FMHEADEND_ARGS=', 'MUXD_ARGS=')):
                    values.append(line)
        return values
    overrides = sorted(Path(str(actual) + '.d').glob('*.conf'))
    if startup([actual, *overrides]) != startup([expected]):
        raise ValueError(f'{actual}: custom ExecStart/EnvironmentFile needs manual review; configuration left untouched')


def backup_database(source_path, destination):
    check_database(source_path)
    # O_EXCL protects the backup as well as the source. Include committed WAL data.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(Path(source_path).resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)


def extract_webui(archive, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f'Staging destination already exists: {destination}')
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        entries = {}
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name or ':' in member.name
                    or any(ord(c) < 32 for c in member.name)
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f'Unsafe archive entry: {member.name!r}')
            name = str(path)
            if name == '.' and member.isdir():
                continue
            if name in entries or name == '.':
                raise ValueError(f'Duplicate/invalid archive entry: {member.name!r}')
            entries[name] = member
        for name in entries:
            for parent in PurePosixPath(name).parents:
                if str(parent) in entries and not entries[str(parent)].isdir():
                    raise ValueError(f'Archive file used as a directory: {parent}')
        files = {name for name, member in entries.items() if member.isfile() and member.size > 0}
        if ('index.html' not in files or not any(re.fullmatch(r'main[^/]*\.js', name) for name in files)
                or not any(re.fullmatch(r'styles[^/]*\.css', name) for name in files)
                or not any(name.startswith('assets/') for name in files)):
            raise ValueError('WebUI archive must contain root index.html, JavaScript, CSS and assets')
        if sum(member.size for member in entries.values()) > shutil.disk_usage(destination.parent).free:
            raise ValueError('Insufficient space to unpack WebUI')
        destination.mkdir(mode=0o755)
        for name, member in entries.items():
            target = destination.joinpath(*PurePosixPath(name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with source.extractfile(member) as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                target.chmod(0o644)


def http_health(host, port):
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(str(host), str(host))
    if ':' in host:
        host = '[' + host.strip('[]') + ']'
    try:
        with urlopen(f'http://{host}:{int(str(port))}/api/v1/status', timeout=1) as response:
            status = json.load(response)
        print('Backend HTTP ready; Controller: ' + str(status['system']['controller_state']))
    except HTTPError as exc:
        if exc.code != 401:
            raise
        print('Backend HTTP ready; authentication enabled. Inspect Controller status after login.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['hardware', 'regions', 'profiles', 'runtime', 'check-db', 'backup-db', 'webui', 'health', 'service-contract', 'urls', 'realtime'])
    parser.add_argument('paths', nargs='*', type=Path)
    args = parser.parse_args()
    try:
        if args.operation == 'hardware':
            print('\n'.join(hardware_plan(*args.paths)))
        elif args.operation == 'regions':
            print(region_menu())
        elif args.operation == 'urls':
            network_urls(*args.paths)
        elif args.operation == 'realtime':
            realtime_status(*args.paths)
        elif args.operation == 'profiles':
            for identifier, name in profile_catalog(*args.paths):
                print(identifier + '\t' + name)
        elif args.operation == 'runtime':
            print('\n'.join(runtime_settings(*args.paths)))
        elif args.operation == 'check-db':
            check_database(*args.paths)
        elif args.operation == 'backup-db':
            backup_database(*args.paths)
        elif args.operation == 'health':
            http_health(*args.paths)
        elif args.operation == 'service-contract':
            service_contract(*args.paths)
        else:
            extract_webui(*args.paths)
    except (OSError, ValueError, subprocess.CalledProcessError, sqlite3.Error, tarfile.TarError, setup_pi.SetupError) as exc:
        parser.exit(1, f'Installer input error: {exc}\n')


if __name__ == '__main__':
    main()
