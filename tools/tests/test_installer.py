"""Installer tests: source Bash functions with fake commands and temporary paths.

Never execute production preflight, APT, boot writes or real service commands.
"""
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from urllib.error import HTTPError

import pytest

from fmheadendd.database import Database
from fmheadendd.models.configuration import SystemConfig
from tools import install_support as support

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which('bash') or 'C:/Program Files/Git/bin/bash.exe'


def archive(path, entries=None):
    entries = entries or {'index.html': b'<html></html>', 'main-test.js': b'js',
                           'styles-test.css': b'css', 'assets/logo.svg': b'logo'}
    with tarfile.open(path, 'w:gz') as output:
        for name, content in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            output.addfile(entry, BytesIO(content))
    return path


def shell(tmp_path, script):
    for name in ('config', 'data', 'share', 'units', 'backups', 'work'):
        (tmp_path / name).mkdir(exist_ok=True)
    variables = {
        'CONFIG_DIR': tmp_path / 'config', 'DATA_DIR': tmp_path / 'data',
        'SHARE_DIR': tmp_path / 'share', 'UNIT_DIR': tmp_path / 'units',
        'BACKUP_DIR': tmp_path / 'backups', 'WORK': tmp_path / 'work',
        'DATABASE': tmp_path / 'data/headend.db', 'PASSWD_FILE': tmp_path / 'data/htpasswd',
        'VENV': tmp_path / 'venv', 'MUX_BINARY': tmp_path / 'muxd',
    }
    prefix = 'source ' + shlex.quote((ROOT / 'install.sh').as_posix()) + '\n'
    prefix += '\n'.join(key + '=' + shlex.quote(path.as_posix()) for key, path in variables.items())
    prefix += '\npython3() { ' + shlex.quote(Path(sys.executable).as_posix()) + ' "$@" | tr -d "\\r"; }\n'
    prefix += "TTY=1 MODEL='Test Raspberry Pi' I2C_DEVICE=/dev/i2c-8 RECOMMENDED_PROFILE=default/basic_limiter.json\n"
    # Any unmocked privileged command is a test failure, even on a root CI runner.
    for command in ('apt-get', 'systemctl', 'systemd-analyze', 'runuser', 'groupadd', 'useradd', 'usermod', 'chown'):
        prefix += command + '() { echo "UNEXPECTED PRIVILEGED COMMAND" >&2; return 99; }\n'
    env = dict(os.environ)
    env.pop('BASH_ENV', None)
    env.update(LANG='C', LC_ALL='C')
    return subprocess.run([BASH, '-c', prefix + script], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)


@pytest.mark.parametrize('model,device,recommended,clock', [
    ('Raspberry Pi Zero 2 W Rev 1.0', '/dev/i2c-8', 'basic_limiter', 'Fixed'),
    ('Raspberry Pi 2 Model B Rev 1.2', '/dev/i2c-8', 'basic_wideband', 'Fixed'),
    ('Raspberry Pi 3 Model B Rev 1.2', '/dev/i2c-8', 'basic_wideband', 'Fixed'),
    ('Raspberry Pi 4 Model B Rev 1.5', '/dev/i2c-8', 'moderate_loud', 'Fixed'),
    ('Raspberry Pi 5 Model B Rev 1.0', '/dev/i2c-1', 'moderate_loud', 'unchanged'),
])
def test_model_plan_reuses_setup_pi(tmp_path, model, device, recommended, clock):
    path = tmp_path / 'model'
    path.write_bytes(model.encode() + b'\0')
    plan = support.hardware_plan(path)
    assert plan[1] == device and plan[2] == f'default/{recommended}.json'
    assert clock in plan[4]


def test_real_bundled_catalog_filters_only_installer_menu_and_has_stable_numbers():
    catalog = support.profile_catalog(ROOT / 'audio-profiles/default')
    assert len(catalog) == 10
    assert len(list((ROOT / 'audio-profiles/default').glob('*.json'))) > 10
    assert all('[TX Soft Clip ON]' not in name for _, name in catalog)
    assert catalog[3] == ('default/basic_limiter.json', 'Basic Limiter')
    assert catalog[4] == ('default/basic_wideband.json', 'Basic Wideband')
    assert catalog[8] == ('default/moderate_loud.json', 'Moderate Loud')
    assert ('default/basic_wideband.json', 'Basic Wideband') in catalog


def test_unknown_model_is_rejected_before_installation(tmp_path):
    path = tmp_path / 'model'
    path.write_text('Unknown computer')
    with pytest.raises(support.setup_pi.SetupError):
        support.hardware_plan(path)


def test_archive_extracts_directly_with_safe_permissions(tmp_path):
    output = tmp_path / 'webui'
    support.extract_webui(archive(tmp_path / 'webui.tar.gz'), output)
    assert (output / 'index.html').is_file()
    assert not (output / 'browser').exists()
    assert (output / 'assets/logo.svg').read_bytes() == b'logo'
    if os.name == 'posix':
        assert (output / 'index.html').stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize('name,kind', [
    ('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
    ('C:/escape', tarfile.REGTYPE), ('assets\\escape', tarfile.REGTYPE),
    ('assets/link', tarfile.SYMTYPE), ('assets/link', tarfile.LNKTYPE),
    ('assets/device', tarfile.CHRTYPE), ('assets/fifo', tarfile.FIFOTYPE),
])
def test_unsafe_archives_rejected_before_extraction(tmp_path, name, kind):
    path = tmp_path / 'bad.tar.gz'
    with tarfile.open(path, 'w:gz') as output:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.linkname = '/etc/shadow'
        output.addfile(entry)
    with pytest.raises(ValueError, match='Unsafe'):
        support.extract_webui(path, tmp_path / 'stage')
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('content', [b'not gzip', b''])
def test_malformed_archive(tmp_path, content):
    path = tmp_path / 'broken.tar.gz'
    path.write_bytes(content)
    with pytest.raises(tarfile.TarError):
        support.extract_webui(path, tmp_path / 'stage')


def test_missing_assets_and_nested_browser_directory_rejected(tmp_path):
    path = archive(tmp_path / 'nested.tar.gz', {'browser/index.html': b'html'})
    with pytest.raises(ValueError, match='root index'):
        support.extract_webui(path, tmp_path / 'stage')


def test_archive_duplicates_and_parent_file_conflicts_rejected(tmp_path):
    for entries in (['index.html', './index.html'], ['assets', 'assets/logo.svg']):
        path = tmp_path / 'conflict.tar.gz'
        with tarfile.open(path, 'w:gz') as output:
            for name in entries:
                entry = tarfile.TarInfo(name)
                output.addfile(entry, BytesIO())
        with pytest.raises(ValueError):
            support.extract_webui(path, tmp_path / 'stage')


def test_runtime_environment_is_data_not_executable_code(tmp_path):
    env = tmp_path / 'backend.env'
    db = tmp_path / 'custom.db'
    env.write_text(f'OTHER="preserved"\nFMHEADEND_ARGS="--database {db.as_posix()} --region 2 --port=8181 --passwd-file"\n')
    values = support.runtime_settings(env, tmp_path / 'default.db')
    assert values[:2] == [db.as_posix(), '2']
    assert values[3] == '/var/lib/fmheadend/htpasswd' and values[5] == '8181'
    env.write_text('FMHEADEND_ARGS="--database $(touch /tmp/never-run)"\n')
    with pytest.raises(ValueError):
        support.runtime_settings(env, db)


@pytest.mark.parametrize('args', ['--database relative.db', '--dat /tmp/file', '--region 9', '--database', '--region 1 --region 2'])
def test_ambiguous_runtime_settings_fail(tmp_path, args):
    path = tmp_path / 'backend.env'
    path.write_text(f'FMHEADEND_ARGS="{args}"\n')
    with pytest.raises(ValueError):
        support.runtime_settings(path, tmp_path / 'default.db')


def test_custom_service_startup_requires_manual_review(tmp_path):
    expected = ROOT / 'systemd/fmheadendd.service'
    actual = tmp_path / 'fmheadendd.service'
    actual.write_bytes(expected.read_bytes())
    support.service_contract(actual, expected)
    dropins = tmp_path / 'fmheadendd.service.d'
    dropins.mkdir()
    override = dropins / 'override.conf'
    override.write_text('[Service]\nEnvironment="FMHEADEND_ARGS=--database /other.db"\n')
    with pytest.raises(ValueError, match='manual review'):
        support.service_contract(actual, expected)
    override.write_text('[Service]\nExecStart=\nExecStart=/another/backend\n')
    with pytest.raises(ValueError, match='manual review'):
        support.service_contract(actual, expected)


def test_sqlite_backup_includes_wal_and_preserves_configuration(tmp_path):
    source = tmp_path / 'headend.db'
    db = Database(source)
    db.connection.execute('PRAGMA journal_mode=WAL')
    db.save_system(SystemConfig(default_audio_profile_id='default/moderate_loud.json', global_stopped=True))
    before = db.get_system()
    support.backup_database(source, tmp_path / 'backup.db')
    assert db.get_system() == before
    db.close()
    backup = Database(tmp_path / 'backup.db')
    assert backup.get_system() == before
    backup.close()
    with pytest.raises(FileExistsError):
        support.backup_database(source, tmp_path / 'backup.db')


MENU = '''
HAVE_ENV=no MODE=fresh MODEL='Raspberry Pi 4' I2C_DEVICE=/dev/i2c-8
RECOMMENDED_PROFILE=default/moderate_loud.json
HARDWARE_PLAN=('Raspberry Pi 4' /dev/i2c-8 default/moderate_loud.json 'Software I2C' 'Fixed core clock')
support() {
    if [[ $1 == regions ]]; then printf '  1) Europe\n';
    elif [[ $1 == profiles ]]; then printf 'default/basic_limiter.json\tBasic Limiter\ndefault/moderate_loud.json\tModerate Loud\n';
    else (cd "$SOURCE_DIR"; python3 -m tools.install_support "$@"); fi
}
INDEX=0
read() { if [[ ${2:-} == -u ]]; then printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; else builtin read "$@"; fi; }
'''


@pytest.mark.parametrize('answers,expected', [
    ("('' '' n y '' '' y)", 'RESULT:1:default/moderate_loud.json:no:yes'),
    ("(2 1 y n '' '' y)", 'RESULT:2:default/basic_limiter.json:yes:no'),
])
def test_fresh_wizard_defaults_and_selections(tmp_path, answers, expected):
    result = shell(tmp_path, MENU + f'ANSWERS={answers}\n' + '''collect_configuration
printf 'RESULT:%s:%s:%s:%s\n' "$REGION" "$DEFAULT_PROFILE" "$AUTH" "$HARDWARE"
''')
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert 'Default Audio Profile (default: 2 - Moderate Loud):' in result.stdout
    assert '(recommended)' not in result.stdout
    assert 'Credential entry follows' not in result.stdout
    assert 'This provides the required bcrypt dependency' not in result.stdout


def test_detect_upgrade_and_preserve_database_and_env(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'data').mkdir()
    db_path = tmp_path / 'data/headend.db'
    db = Database(db_path)
    db.save_system(SystemConfig(global_stopped=True))
    db.close()
    backend = tmp_path / 'config/fmheadendd.env'
    backend.write_text(f'FMHEADEND_ARGS="--database {db_path.as_posix()} --region 3 --port 8181"\n')
    mux = tmp_path / 'config/muxd.env'
    mux.write_text('MUXD_ARGS="custom existing arguments"\n')
    before = [p.read_bytes() for p in (backend, mux, db_path)]
    result = shell(tmp_path, 'detect_installation; write_environment; initialize_database; printf "MODE:%s" "$MODE"')
    assert result.returncode == 0, result.stderr
    assert 'MODE:upgrade' in result.stdout
    assert [p.read_bytes() for p in (backend, mux, db_path)] == before


def test_partial_install_requires_explicit_new_database_approval(tmp_path):
    result = shell(tmp_path, 'detect_installation; confirm() { return 1; }; collect_configuration')
    assert result.returncode != 0
    assert 'Partial installation' in result.stdout
    assert 'restore the database' in result.stderr
    assert not (tmp_path / 'data/headend.db').exists()


def test_partial_rerun_preserves_existing_environment_and_initializes_only_on_consent(tmp_path):
    (tmp_path / 'config').mkdir()
    env = tmp_path / 'config/fmheadendd.env'
    env.write_text('FMHEADEND_ARGS="--region 2 --port 8181"\n')
    before = env.read_bytes()
    result = shell(tmp_path, '''
detect_installation
confirm() { return 0; }
choose_number() { REPLY=1; }
collect_configuration
runuser() { printf 'INIT:%s\n' "$*"; }
write_environment
initialize_database
''')
    assert result.returncode == 0, result.stderr
    assert 'Partial installation' in result.stdout and '--region 2' in result.stdout
    assert 'fmheadend-init-db' in result.stdout
    assert env.read_bytes() == before


def test_upgrade_backup_preserves_database_environment_and_credentials(tmp_path):
    shell(tmp_path, ':')  # Create isolated installation directories.
    source = tmp_path / 'data/headend.db'
    db = Database(source)
    db.save_system(SystemConfig(global_stopped=True))
    db.close()
    env = tmp_path / 'config/fmheadendd.env'
    env.write_text('FMHEADEND_ARGS="--region 3"\n')
    password = tmp_path / 'data/htpasswd'
    password.write_text('unchanged credential file')
    before = [p.read_bytes() for p in (source, env, password)]
    result = shell(tmp_path, '''
# MSYS cannot apply POSIX mode 0700 to an existing OneDrive directory.
if [[ $OSTYPE == msys* ]]; then install() { [[ $* == '-d -m 0700 '* ]] || exit 99; mkdir -p -- "${@:4}"; }; fi
MODE=upgrade
backup_existing
''')
    assert result.returncode == 0, result.stderr
    backup, = (tmp_path / 'backups').iterdir()
    restored = Database(backup / 'fmheadend.db')
    assert restored.get_system().global_stopped is True
    restored.close()
    assert (backup / 'environment/fmheadendd.env').read_bytes() == before[1]
    assert (backup / 'htpasswd').read_bytes() == before[2]
    assert [p.read_bytes() for p in (source, env, password)] == before


def test_missing_archive_fails_before_any_installation(tmp_path):
    result = shell(tmp_path, 'parse_arguments --webui-archive /nonexistent/fmheadend.tar.gz')
    assert result.returncode != 0
    assert 'Supply an existing' in result.stderr


def test_relative_archive_is_resolved_before_source_directory_change(tmp_path):
    path = archive(tmp_path / 'site.tar.gz')
    result = shell(tmp_path, 'cd ' + shlex.quote(tmp_path.as_posix()) + '''
parse_arguments --webui-archive site.tar.gz
support webui "$ARCHIVE" "$WORK/site"
''')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'work/site/index.html').is_file()


def test_upgrade_missing_database_never_invokes_initializer(tmp_path):
    result = shell(tmp_path, 'MODE=upgrade; initialize_database')
    assert result.returncode != 0 and 'refusing to create' in result.stderr
    assert 'UNEXPECTED' not in result.stderr


def test_password_invocation_has_no_plaintext_arguments(tmp_path):
    result = shell(tmp_path, '''
AUTH=yes MODE=fresh
python3() { printf 'PASSWORD_TOOL:%s\n' "$*"; }
chown() { :; }; chmod() { :; }
configure_password
''')
    assert result.returncode == 0, result.stderr
    assert 'create_passwd.py --file' in result.stdout
    assert '--password' not in result.stdout and '--user' not in result.stdout


def test_existing_password_file_is_not_recreated_or_repermissioned(tmp_path):
    shell(tmp_path, ':')
    path = tmp_path / 'data/htpasswd'
    path.write_bytes(b'existing protected credentials')
    result = shell(tmp_path, 'AUTH=yes MODE=fresh; configure_password')
    assert result.returncode == 0, result.stderr
    assert 'Preserving existing password file' in result.stdout
    assert path.read_bytes() == b'existing protected credentials'


def test_failed_webui_staging_keeps_live_tree_intact(tmp_path):
    shell(tmp_path, ':')
    live = tmp_path / 'share/webui'
    live.mkdir()
    (live / 'index.html').write_text('old complete site')
    result = shell(tmp_path, 'cp() { return 23; }; install_webui')
    assert result.returncode == 23
    assert (live / 'index.html').read_text() == 'old complete site'


def test_webui_success_replaces_complete_tree(tmp_path):
    shell(tmp_path, ':')
    live = tmp_path / 'share/webui'
    live.mkdir()
    (live / 'old.js').write_text('obsolete')
    stage = tmp_path / 'work/webui'
    support.extract_webui(archive(tmp_path / 'site.tar.gz'), stage)
    result = shell(tmp_path, 'install_webui')
    assert result.returncode == 0, result.stderr
    assert (live / 'index.html').is_file() and not (live / 'old.js').exists()
    assert not list((tmp_path / 'share').glob('.webui-old.*'))


def test_hardware_overwrite_requires_approval(tmp_path):
    result = shell(tmp_path, '''
MODE=fresh HARDWARE=yes
python3() { if [[ $* == *--overwrite-existing* ]]; then printf 'Reboot required: yes\n'; else printf 'Conflict; use --overwrite-existing\n'; return 2; fi; }
confirm() { printf 'EXPLICIT_APPROVAL\n'; return 0; }
configure_hardware
printf 'REBOOT:%s' "$REBOOT_REQUIRED"
''')
    assert result.returncode == 0, result.stderr
    assert 'EXPLICIT_APPROVAL' in result.stdout and 'REBOOT:yes' in result.stdout


def test_ordinary_upgrade_does_not_touch_boot_settings(tmp_path):
    result = shell(tmp_path, 'MODE=upgrade HARDWARE=yes; python3() { exit 99; }; configure_hardware')
    assert result.returncode == 0


def test_hardware_conflict_decline_never_overwrites(tmp_path):
    result = shell(tmp_path, '''
MODE=fresh HARDWARE=yes
python3() { [[ $* != *--overwrite-existing* ]] || exit 99; printf 'Conflict: --overwrite-existing\n'; return 2; }
confirm() { return 1; }
configure_hardware
printf 'MANUAL:%s' "$MANUAL_PENDING"
''')
    assert result.returncode == 0, result.stderr
    assert 'MANUAL:yes' in result.stdout


def test_genuine_hardware_configuration_failure_is_not_treated_as_reboot_pending(tmp_path):
    result = shell(tmp_path, '''
MODE=fresh HARDWARE=yes
python3() { printf 'Unsupported boot layout\n'; return 2; }
confirm() { exit 99; }
configure_hardware
echo SHOULD_NOT_RUN
''')
    assert result.returncode != 0
    assert 'Hardware configuration failed' in result.stderr and 'SHOULD_NOT_RUN' not in result.stdout


def test_pending_reboot_enables_services_without_starting_them(tmp_path):
    result = shell(tmp_path, '''
MODE=upgrade REBOOT_REQUIRED=yes
systemctl() { printf 'SYSTEMCTL:%s\n' "$*"; }
systemd-analyze() { return 0; }
install_services
''')
    assert result.returncode == 0, result.stderr
    assert 'SYSTEMCTL:enable ' in result.stdout
    assert 'SYSTEMCTL:restart' not in result.stdout


def test_software_verification_without_reboot_uses_http_not_hardware_commands(tmp_path):
    result = shell(tmp_path, '''
mkdir -p "$SHARE_DIR/webui" "$VENV/bin"
printf html > "$SHARE_DIR/webui/index.html"
printf '#!/bin/sh\n' > "$VENV/bin/fmheadendd"
cp "$VENV/bin/fmheadendd" "$MUX_BINARY"
chmod +x "$VENV/bin/fmheadendd" "$MUX_BINARY"
systemctl() { printf 'SYSTEMCTL:%s\n' "$*"; }
support() {
    if [[ $1 == runtime ]]; then printf '%s\n' "$DATABASE" 1 /dev/i2c-8 '' 0.0.0.0 8080;
    elif [[ $1 == health ]]; then printf 'HTTP_CONTROLLER_UNAVAILABLE\n';
    elif [[ $1 == realtime ]]; then printf 'SCHED_FIFO / 50\n'; else return 99; fi
}
verify_installation
''')
    assert result.returncode == 0, result.stderr
    assert 'is-active' in result.stdout and 'HTTP_CONTROLLER_UNAVAILABLE' in result.stdout
    assert 'SCHED_FIFO / 50' in result.stdout


def test_failure_propagates_and_stops_later_installation_steps(tmp_path):
    result = shell(tmp_path, '''
parse_arguments() { :; }; preflight() { :; }; detect_installation() { :; }
collect_configuration() { :; }; backup_existing() { :; }; stop_services() { :; }
LOG_FILE="$WORK/install.log"
install_dependencies() { return 17; }
install_account() { echo SHOULD_NOT_RUN; }
main
''')
    assert result.returncode == 17
    assert 'SHOULD_NOT_RUN' not in result.stdout and 'Installation failed' in result.stderr


def test_controller_unavailable_is_valid_http_software_health(monkeypatch, capsys):
    @contextmanager
    def response(*args, **kwargs):
        yield BytesIO(json.dumps({'system': {'controller_state': 'ERROR'}}).encode())
    monkeypatch.setattr(support, 'urlopen', response)
    support.http_health('0.0.0.0', '8080')
    assert 'Controller: ERROR' in capsys.readouterr().out
    def protected(*args, **kwargs):
        raise HTTPError('http://localhost', 401, 'Unauthorized', {}, None)
    monkeypatch.setattr(support, 'urlopen', protected)
    support.http_health('127.0.0.1', '8080')
    assert 'authentication enabled' in capsys.readouterr().out


@pytest.mark.parametrize('model', ['Raspberry Pi Zero W Rev 1.1', 'Raspberry Pi 1 Model B', 'Raspberry Pi Model B Rev 2'])
def test_32_bit_only_models_are_not_installer_platforms(tmp_path, model):
    path = tmp_path / 'model'
    path.write_text(model)
    with pytest.raises(ValueError, match='64-bit'):
        support.hardware_plan(path)


def test_region_menu_matches_controller_independent_definitions():
    menu = support.region_menu()
    for index, plan in support.PLANS.items():
        assert f'{index}) {plan.region_name.split(" /")[0]}' in menu
        if index:
            assert f'{plan.fm_min_khz / 1000:.1f}-{plan.fm_max_khz / 1000:.1f} MHz | {plan.fm_step_khz} kHz raster' in menu
    expert = menu.split('  1)')[0]
    assert 'Controller-supported' in expert and 'MHz' not in expert


def test_full_wizard_snapshot_and_platform_defaults(tmp_path):
    result = shell(tmp_path, '''
HAVE_ENV=no MODE=fresh
HARDWARE_PLAN=('Raspberry Pi 4' /dev/i2c-8 default/moderate_loud.json unused unused '  I2C mode: Software I2C')
RECOMMENDED_PROFILE=default/moderate_loud.json
ANSWERS=('' '' n y '' '' y) INDEX=0
read() { if [[ ${2:-} == -u ]]; then printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; else builtin read "$@"; fi; }
collect_configuration
''')
    assert result.returncode == 0, result.stderr
    for heading in ('System Detection', 'FM Region', 'Default Audio Profile', 'WebUI Authentication',
                    'Raspberry Pi Hardware', 'WebUI Network', 'Installation Summary'):
        assert heading in result.stdout
    assert 'Region (default: 1 - Europe):' in result.stdout
    assert 'Default Audio Profile (default: 9 - Moderate Loud):' in result.stdout
    for annotation in ('Basic Limiter (Default for Pi Zero 2 W)', 'Basic Wideband (Default for Pi 2-3)',
                       'Moderate Loud (Default for Pi 4-5)'):
        assert annotation in result.stdout
    assert 'provisional' not in result.stdout and 'lower-powered' not in result.stdout
    assert 'lower-performance Raspberry Pi models' in result.stdout
    assert '[TX Soft Clip ON]' not in result.stdout
    assert '30bd239' not in result.stdout and '/usr/share' not in result.stdout
    assert '\x1b' not in result.stdout
    assert all(len(line) <= 80 for line in result.stdout.splitlines() if ': ' not in line or not line.endswith(': '))


def test_menu_invalid_input_retries_and_enter_accepts_default(tmp_path):
    result = shell(tmp_path, '''
ANSWERS=(oops 22 '') INDEX=0
read() { printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; }
choose_number Region 1 0 3 Europe
printf 'SELECTED:%s' "$REPLY"
''')
    assert result.returncode == 0
    assert result.stdout.count('Choose 0 to 3.') == 2
    assert 'SELECTED:1' in result.stdout


@pytest.mark.parametrize('settings', ['NO_COLOR=1 TERM=xterm', 'TERM=dumb', 'TERM=xterm'])
def test_redirected_monochrome_output(tmp_path, settings):
    result = shell(tmp_path, settings + '\ninit_ui\nsection "FM Region"\n')
    assert result.returncode == 0 and '\x1b' not in result.stdout
    assert '----------------------------------------\nFM Region\n' in result.stdout


def test_progress_logs_command_output_and_keeps_console_clean(tmp_path):
    result = shell(tmp_path, '''
LOG_FILE="$WORK/install.log"
build() { echo 'compiler detail'; echo 'compiler stderr' >&2; }
run_step 'Building SPI multiplexer' build
printf 'LOG:\n'; cat "$LOG_FILE"
''')
    assert result.returncode == 0, result.stderr
    console, log = result.stdout.split('LOG:\n')
    assert '[1/11] Building SPI multiplexer...' in console and 'OK' in console
    assert 'compiler' not in console
    assert 'compiler detail' in log and 'compiler stderr' in log


def test_error_report_has_recent_diagnostic_and_log_path_and_no_false_ok(tmp_path):
    result = shell(tmp_path, '''
LOG_FILE="$WORK/install.log"
trap 'failure "$?" "$LINENO"' ERR
broken_build() { echo 'CMake: missing header' >&2; return 27; }
run_step 'Building SPI multiplexer' broken_build
offer_reboot
''')
    assert result.returncode == 27
    assert 'CMake: missing header' in result.stderr and 'Full installation log:' in result.stderr
    assert 'Building SPI multiplexer' in result.stderr
    assert 'OK' not in result.stdout and 'Reboot now?' not in result.stdout


def interface(name, addresses, state='UP', flags=None):
    return {'ifname': name, 'operstate': state, 'flags': flags if flags is not None else ['UP'],
            'addr_info': [{'local': address, 'scope': 'global'} for address in addresses]}


@pytest.fixture
def interfaces():
    return [interface('enx123', ['192.168.1.42', '2001:4860:1234::42']),
            interface('wifi-test', ['192.168.1.87']),
            interface('tailscale0', ['100.83.12.5', 'fd7a:115c:a1e0::5'], state='UNKNOWN'),
            interface('down0', ['10.0.0.1'], state='DOWN'),
            interface('no-up-flag', ['10.0.0.2'], flags=[]),
            interface('lo', ['127.0.0.1', '::1']),
            interface('bad', ['169.254.1.2', 'fe80::2', '0.0.0.0', '224.0.0.1'])]


def test_all_active_ipv4_interfaces_and_correct_port(interfaces):
    assert support.webui_urls(interfaces, '0.0.0.0', '8181') == [
        ('enx123', 'http://192.168.1.42:8181'), ('wifi-test', 'http://192.168.1.87:8181'),
        ('tailscale0', 'http://100.83.12.5:8181')]


def test_ipv6_brackets_and_ula_vpn(interfaces):
    assert support.webui_urls(interfaces, '::', '8080') == [
        ('enx123', 'http://[2001:4860:1234::42]:8080'),
        ('tailscale0', 'http://[fd7a:115c:a1e0::5]:8080')]


@pytest.mark.parametrize('host,expected', [
    ('192.168.1.87', [('wifi-test', 'http://192.168.1.87:8181')]),
    ('fd7a:115c:a1e0::5', [('tailscale0', 'http://[fd7a:115c:a1e0::5]:8181')]),
    ('127.0.0.1', []), ('::1', []), ('10.99.99.99', [])])
def test_explicit_http_bind_never_advertises_other_addresses(interfaces, host, expected):
    assert support.webui_urls(interfaces, host, 8181) == expected


@pytest.mark.parametrize('flag', ['tentative', 'dadfailed', 'deprecated'])
def test_unusable_ipv6_addresses_are_filtered(flag):
    item = interface('test', ['2001:4860:1234::1'])
    item['addr_info'][0][flag] = True
    assert support.webui_urls([item], '::', 8080) == []


def test_network_inventory_command_and_empty_fallback(monkeypatch, capsys):
    def inventory(command, **kwargs):
        assert command == ['ip', '-j', 'address', 'show']
        return '[]'
    monkeypatch.setattr(support.subprocess, 'check_output', inventory)
    support.network_urls('0.0.0.0', 8080)
    assert 'No active network address' in capsys.readouterr().out


@pytest.mark.parametrize('answer,reboots', [('', False), ('n', False), ('y', True)])
def test_reboot_requires_explicit_yes_and_defaults_no(tmp_path, answer, reboots):
    result = shell(tmp_path, f'ANSWER={shlex.quote(answer)}\n' + '''
REBOOT_REQUIRED=yes
read() { printf -v "${@: -1}" '%s' "$ANSWER"; }
systemctl() { printf 'SYSTEMCTL:%s' "$*"; }
offer_reboot
''')
    assert result.returncode == 0
    assert 'Reboot now? [y/N]:' in result.stdout
    assert ('SYSTEMCTL:reboot' in result.stdout) == reboots


def test_no_reboot_prompt_when_not_required(tmp_path):
    result = shell(tmp_path, 'offer_reboot')
    assert result.returncode == 0 and 'Reboot now?' not in result.stdout


@pytest.mark.parametrize('policy,priority,valid', [(1, 50, True), (0, 0, False), (1, 49, False)])
def test_realtime_effective_process_scheduling(monkeypatch, capsys, policy, priority, valid):
    from types import SimpleNamespace
    monkeypatch.setattr(support.os, 'SCHED_FIFO', 1, raising=False)
    monkeypatch.setattr(support.os, 'sched_getscheduler', lambda pid: policy, raising=False)
    monkeypatch.setattr(support.os, 'sched_getparam', lambda pid: SimpleNamespace(sched_priority=priority), raising=False)
    if valid:
        support.realtime_status('1234')
        assert 'SCHED_FIFO / 50' in capsys.readouterr().out
    else:
        with pytest.raises(ValueError, match='expected SCHED_FIFO / 50'):
            support.realtime_status('1234')


@pytest.mark.parametrize('pending', ['REBOOT_REQUIRED=yes', 'MANUAL_PENDING=yes'])
def test_pending_services_never_check_http_or_realtime(tmp_path, pending):
    result = shell(tmp_path, pending + '''
mkdir -p "$SHARE_DIR/webui" "$VENV/bin"
echo html > "$SHARE_DIR/webui/index.html"
printf '#!/bin/sh\n' > "$VENV/bin/fmheadendd"
cp "$VENV/bin/fmheadendd" "$MUX_BINARY"
chmod +x "$VENV/bin/fmheadendd" "$MUX_BINARY"
support() { exit 99; }
verify_installation
''')
    assert result.returncode == 0, result.stderr


def test_authentication_is_before_native_builds_and_outside_logging():
    source = (ROOT / 'install.sh').read_text()
    main = source[source.index('main() {'):]
    assert main.index('install_dependencies') < main.index('configure_password') < main.index('install_backend')
    assert "run_step '" not in main[main.index('    configure_password'):main.index('    # Credentials')]
    assert 'CPUSchedulingPolicy=fifo' in (ROOT / 'systemd/fmheadend-muxd.service').read_text()
    assert 'CPUSchedulingPriority=50' in (ROOT / 'systemd/fmheadend-muxd.service').read_text()


@pytest.mark.parametrize('state,expected', [
    ('REBOOT_REQUIRED=yes', 'available after reboot'),
    ('MANUAL_PENDING=yes', 'after hardware preparation and service startup'),
    ('REBOOT_REQUIRED=no', 'services running; subject to your network/firewall rules')])
def test_completion_respects_preserved_bind_port_and_service_state(tmp_path, state, expected):
    shell(tmp_path, ':')
    (tmp_path / 'config/fmheadendd.env').write_text('FMHEADEND_ARGS="--host 192.168.1.87 --port 8181"\n')
    result = shell(tmp_path, state + '''
LOG_FILE="$WORK/install.log"
original_support=$(declare -f support)
eval "${original_support/support ()/real_support ()}"
support() {
    if [[ $1 == urls ]]; then
        [[ $2 == 192.168.1.87 && $3 == 8181 ]] || return 99
        printf '  wifi-test    http://192.168.1.87:8181\n'
    else real_support "$@"; fi
}
complete_installation
''')
    assert result.returncode == 0, result.stderr
    assert 'FMheadend installation completed!' in result.stdout
    assert expected in result.stdout and 'http://192.168.1.87:8181' in result.stdout
    for command in ('systemctl status fmheadendd fmheadend-muxd',
                    'journalctl -fu fmheadendd -u fmheadend-muxd',
                    'sudo systemctl restart fmheadendd fmheadend-muxd'):
        assert command in result.stdout
    assert 'http://192.168.1.87:8080' not in result.stdout


def test_runtime_realtime_mismatch_is_a_visible_warning(tmp_path):
    result = shell(tmp_path, '''
mkdir -p "$SHARE_DIR/webui" "$VENV/bin"
echo html > "$SHARE_DIR/webui/index.html"
printf '#!/bin/sh\n' > "$VENV/bin/fmheadendd"
cp "$VENV/bin/fmheadendd" "$MUX_BINARY"
chmod +x "$VENV/bin/fmheadendd" "$MUX_BINARY"
systemctl() { if [[ $1 == show ]]; then echo 1234; fi; }
support() {
    case $1 in
        runtime) printf '%s\n' "$DATABASE" 1 /dev/i2c-8 '' 0.0.0.0 8080 ;;
        health) return 0 ;;
        realtime) [[ $2 == 1234 ]] || exit 99; echo 'PID 1234: SCHED_OTHER / 0'; return 1 ;;
    esac
}
verify_installation
''')
    assert result.returncode == 0, result.stderr
    assert 'WARNING:' in result.stdout and 'PID 1234: SCHED_OTHER / 0' in result.stdout
    assert 'Expected SCHED_FIFO / 50' in result.stdout


def test_password_tool_output_is_not_written_to_command_log(tmp_path):
    result = shell(tmp_path, '''
AUTH=yes MODE=fresh LOG_FILE="$WORK/install.log"
python3() { printf 'credential-tool-output\n'; touch "$PASSWD_FILE"; }
chown() { :; }; chmod() { :; }
configure_password
printf 'LOG:\n'; cat "$LOG_FILE"
''')
    assert result.returncode == 0, result.stderr
    console, log = result.stdout.split('LOG:\n')
    assert 'credential-tool-output' in console and 'credential-tool-output' not in log
    assert 'WebUI Authentication - Credentials' in console


def test_terminal_eof_cancels_menu_without_installation(tmp_path):
    result = shell(tmp_path, 'read() { return 1; }; choose_number Region 1 0 3 Europe; echo SHOULD_NOT_RUN')
    assert result.returncode != 0
    assert 'Terminal input ended' in result.stderr and 'SHOULD_NOT_RUN' not in result.stdout


def test_help_is_plain_and_needs_no_tty_or_root(tmp_path):
    result = shell(tmp_path, 'main --help')
    assert result.returncode == 0
    assert 'Usage:' in result.stdout and '\x1b' not in result.stdout


def test_log_file_is_exclusive_and_permissions_explicit(tmp_path):
    result = shell(tmp_path, '''
mktemp() { [[ $1 == /var/tmp/fmheadend-install.XXXXXXXX.log ]] || return 99; : > "$WORK/new.log"; printf '%s' "$WORK/new.log"; }
chmod() { printf 'MODE:%s\n' "$1"; }
init_log
''')
    assert result.returncode == 0
    assert 'MODE:0600' in result.stdout


def test_logged_hardware_step_keeps_confirmation_on_terminal(tmp_path):
    result = shell(tmp_path, '''
init_ui
exec {TTY}>&1
MODE=fresh HARDWARE=yes LOG_FILE="$WORK/install.log"
python3() {
    if [[ $* == *--overwrite-existing* ]]; then echo 'Reboot required: yes';
    else echo 'Existing boot setting: --overwrite-existing'; return 2; fi
}
read() { printf -v "${@: -1}" '%s' y; }
run_step 'Applying hardware settings' configure_hardware
printf 'LOG:\n'; cat "$LOG_FILE"
''')
    assert result.returncode == 0, result.stderr
    console, log = result.stdout.split('LOG:\n')
    assert 'Raspberry Pi Hardware - Existing Settings' in console
    assert 'Permit setup_pi.py to replace those settings (with backups)? [y/N]:' in console
    assert 'Permit setup_pi.py' not in log
    assert 'Existing boot setting' in log and 'Reboot required: yes' in log


def test_nested_command_failure_cannot_continue_to_later_mutations_or_reboot(tmp_path):
    result = shell(tmp_path, '''
init_ui
LOG_FILE="$WORK/install.log" REBOOT_REQUIRED=yes
trap 'failure "$?" "$LINENO"' ERR
broken_build() { echo 'Compiler diagnostic'; false; echo SHOULD_NOT_MUTATE; }
run_step 'Building' broken_build
offer_reboot
''')
    assert result.returncode != 0
    assert 'Compiler diagnostic' in result.stderr and 'Full installation log' in result.stderr
    assert 'SHOULD_NOT_MUTATE' not in result.stdout + result.stderr
    assert 'Reboot now?' not in result.stdout


@pytest.fixture
def bare_checkout(tmp_path):
    """Only source files: no site-packages, editable install or package metadata."""
    checkout = tmp_path / 'bare-checkout'
    for relative in ('tools/install_support.py', 'tools/setup_pi.py',
                     'daemon/__init__.py', 'daemon/regions.py'):
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    model = checkout / 'model'
    model.write_text('Raspberry Pi 4 Model B Rev 1.5')
    shutil.copytree(ROOT / 'audio-profiles/default', checkout / 'profiles')
    archive(checkout / 'webui.tar.gz')
    (checkout / 'backend.env').write_text('FMHEADEND_ARGS="--region 1 --port 8080"\n')
    return checkout


def bare_python(checkout, *arguments):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    return subprocess.run([sys.executable, '-S', *arguments], cwd=checkout, env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('operation,paths,expected', [
    ('hardware', ['model'], 'default/moderate_loud.json'),
    ('regions', [], '87.5-108.0 MHz | 100 kHz raster'),
    ('profiles', ['profiles'], 'Moderate Loud'),
    ('runtime', ['backend.env', 'new.db'], '8080'),
    ('validate-bind', ['::1'], '::1'),
    ('webui', ['webui.tar.gz', 'webui'], ''),
])
def test_preinstall_helpers_work_without_installed_backend(bare_checkout, operation, paths, expected):
    # Prove the exact condition which the development venv used to mask.
    probe = bare_python(bare_checkout, '-c', 'from importlib.metadata import version; version("fmheadendd")')
    assert probe.returncode != 0 and 'PackageNotFoundError' in probe.stderr
    if operation != 'validate-bind':
        paths = [str(bare_checkout / path) for path in paths]
    result = bare_python(bare_checkout, '-m', 'tools.install_support', operation, *paths)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert not result.stderr
    if operation == 'webui':
        assert (bare_checkout / 'webui/index.html').is_file()


def test_command_substitution_failure_is_reported_once(tmp_path):
    result = shell(tmp_path, '''
init_ui
LOG_FILE="$WORK/install.log" REBOOT_REQUIRED=yes
trap 'failure "$?" "$LINENO"' ERR
python3() { echo 'Bootstrap diagnostic' >&2; return 23; }
plan=$(support hardware "$WORK/model")
echo SHOULD_NOT_RUN
offer_reboot
''')
    assert result.returncode == 23
    assert 'Bootstrap diagnostic' in result.stderr
    assert result.stderr.count('Installation failed (') == 1
    assert result.stderr.count('Full installation log:') == 1
    assert 'SHOULD_NOT_RUN' not in result.stdout
    assert 'Reboot now?' not in result.stdout


def test_source_wizard_runs_with_only_standard_library(tmp_path):
    model = tmp_path / 'model'
    model.write_text('Raspberry Pi 4 Model B Rev 1.5')
    webui = archive(tmp_path / 'webui.tar.gz')
    result = shell(tmp_path, '''
init_ui
exec {TTY}>&1
''' + 'python3() { ' + shlex.quote(Path(sys.executable).as_posix()) + ''' -S "$@" | tr -d '\\r'; }
''' + 'MODEL_FILE=' + shlex.quote(model.as_posix()) + '\n' + '''
DATA_DIR="$WORK/not-installed" DATABASE="$WORK/not-installed/fmheadend.db"
''' + 'parse_arguments --webui-archive ' + shlex.quote(webui.as_posix()) + '\n' + '''
plan=$(support hardware "$MODEL_FILE" "$SPI_DEVICE" "$SPI_CLOCK_HZ")
mapfile -t HARDWARE_PLAN <<< "$plan"
MODEL=${HARDWARE_PLAN[0]} I2C_DEVICE=${HARDWARE_PLAN[1]} RECOMMENDED_PROFILE=${HARDWARE_PLAN[2]}
support webui "$ARCHIVE" "$WORK/site"
detect_installation
ANSWERS=('' '' n y '' '' y) INDEX=0
read() { if [[ ${2:-} == -u ]]; then printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; else builtin read "$@"; fi; }
collect_configuration
printf 'WIZARD_COMPLETE:%s:%s:%s\n' "$MODE" "$REGION" "$DEFAULT_PROFILE"
''')
    assert result.returncode == 0, result.stderr
    assert 'WIZARD_COMPLETE:fresh:1:default/moderate_loud.json' in result.stdout
    assert 'Default Audio Profile (default: 9 - Moderate Loud):' in result.stdout
    assert (tmp_path / 'work/site/index.html').is_file()
    assert not (tmp_path / 'work/not-installed').exists()


@pytest.mark.parametrize('port,host,expected_port,expected_host', [
    ('', '', 8080, '0.0.0.0'),
    ('8181', '127.0.0.1', 8181, '127.0.0.1'),
    ('9090', '2001:4860:0000::1', 9090, '2001:4860::1'),
    ('1', '::', 1, '::'),
    ('65535', '192.168.1.42', 65535, '192.168.1.42'),
    ('08080', '::1', 8080, '::1'),
])
def test_network_choices_persist_as_actual_backend_arguments(tmp_path, port, host, expected_port, expected_host):
    answers = 'ANSWERS=(' + ' '.join(shlex.quote(value) for value in (port, host)) + ') INDEX=0\n'
    result = shell(tmp_path, answers + '''
read() { printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; }
collect_network_configuration
AUTH=yes
write_environment
''')
    assert result.returncode == 0, result.stderr
    assert 'WebUI port (default: 8080):' in result.stdout
    assert 'HTTP bind address (default: 0.0.0.0 - all interfaces):' in result.stdout
    env_path = tmp_path / 'config/fmheadendd.env'
    settings = support.runtime_settings(env_path, tmp_path / 'data/headend.db')
    assert settings[4:] == [expected_host, str(expected_port)]
    # Parse the same arguments systemd passes to the real application.
    from fmheadendd.app import argument_parser
    arguments = shlex.split(shlex.split(env_path.read_text().split('=', 1)[1])[0])
    options = argument_parser().parse_args(arguments)
    assert (options.host, options.port) == (expected_host, expected_port)
    assert options.passwd_file == (tmp_path / 'data/htpasswd').as_posix()


def test_network_questions_precede_summary_and_show_selected_values(tmp_path):
    result = shell(tmp_path, MENU + '''
ANSWERS=('' '' n y 8181 ::1 y)
collect_configuration
write_environment
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.index('HTTP bind address (default:') < result.stdout.index('Installation Summary')
    summary = result.stdout.split('Installation Summary')[1]
    assert 'WebUI port:         8181' in summary
    assert 'HTTP bind:          ::1' in summary
    assert support.runtime_settings(tmp_path / 'config/fmheadendd.env', tmp_path / 'data/headend.db')[4:] == ['::1', '8181']


def test_network_invalid_inputs_retry_then_persist_valid_choices(tmp_path):
    answers = ['0', '-1', '65536', '8080.5', '0x1F90', '99999999999999999999999', '8181',
               'bad host', 'http://127.0.0.1', '127.0.0.999', '::g', '0.0.0.0/', '::1']
    script = 'ANSWERS=(' + ' '.join(shlex.quote(value) for value in answers) + ') INDEX=0\n'
    result = shell(tmp_path, script + '''
read() { printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; }
collect_network_configuration
write_environment
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('Enter a port from 1 to 65535.') == 6
    assert result.stdout.count('Enter a valid IPv4 or IPv6 bind address.') == 5
    assert support.runtime_settings(tmp_path / 'config/fmheadendd.env', tmp_path / 'data/headend.db')[4:] == ['::1', '8181']


@pytest.mark.parametrize('value', ['0.0.0.0', '127.0.0.1', '192.168.1.42', '::', '::1', 'fd00::1'])
def test_valid_bind_addresses(value):
    assert support.validate_bind_address(value) == value


@pytest.mark.parametrize('value', ['localhost', 'example.com', '256.0.0.1', '127.1', '[::1]', '::g',
                                 '0.0.0.0/', '::1/128', 'fe80::1%eth0', '224.0.0.1', 'ff02::1',
                                 '255.255.255.255', '" --port 80', '$(touch /tmp/never-run)', '::1\n'])
def test_invalid_or_unsafe_bind_addresses_are_rejected(value):
    with pytest.raises(ValueError):
        support.validate_bind_address(value)


@pytest.mark.parametrize('value', ['0.0.0.0/', '::1/128', '" --port 80'])
def test_bind_validation_cli_preserves_raw_input(bare_checkout, value):
    result = bare_python(bare_checkout, '-m', 'tools.install_support', 'validate-bind', value)
    assert result.returncode == 2
    assert 'Invalid bind address' in result.stderr and 'Traceback' not in result.stderr


def test_network_validator_failure_aborts_instead_of_retrying_forever(tmp_path):
    result = shell(tmp_path, '''
ANSWERS=(8080 0.0.0.0) INDEX=0
read() { printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; }
support() { return 1; }
collect_network_configuration
echo SHOULD_NOT_RUN
''')
    assert result.returncode != 0
    assert 'Could not validate the HTTP bind address' in result.stderr
    assert 'SHOULD_NOT_RUN' not in result.stdout


def test_network_terminal_eof_cancels_before_configuration_is_written(tmp_path):
    result = shell(tmp_path, 'read() { return 1; }; collect_network_configuration; write_environment')
    assert result.returncode != 0 and 'Terminal input ended' in result.stderr
    assert not (tmp_path / 'config/fmheadendd.env').exists()


@pytest.mark.parametrize('mode', ['upgrade', 'partial'])
def test_existing_network_configuration_is_preserved_without_prompts(tmp_path, mode):
    shell(tmp_path, ':')
    backend = tmp_path / 'config/fmheadendd.env'
    backend.write_text('FMHEADEND_ARGS="--host 127.0.0.1 --port 8181 --region 2"\n')
    (tmp_path / 'config/muxd.env').write_text('MUXD_ARGS="preserved"\n')
    if mode == 'upgrade':
        db = Database(tmp_path / 'data/headend.db')
        db.close()
    before = backend.read_bytes()
    result = shell(tmp_path, '''
detect_installation
confirm() { return 0; }
choose_number() { REPLY=1; }
read() { if [[ ${2:-} == -u ]]; then exit 99; else builtin read "$@"; fi; }
collect_configuration
write_environment
''')
    assert result.returncode == 0, result.stderr
    assert 'WebUI port (default:' not in result.stdout
    assert 'HTTP bind address (default:' not in result.stdout
    assert 'WebUI port:         8181' in result.stdout
    assert 'HTTP bind:          127.0.0.1' in result.stdout
    assert backend.read_bytes() == before
