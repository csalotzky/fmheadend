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
                          capture_output=True, text=True, encoding='utf-8', errors='replace')


@pytest.mark.parametrize('model,device,recommended,clock', [
    ('Raspberry Pi Zero W Rev 1.1', '/dev/i2c-8', 'basic_limiter', 'Fixed'),
    ('Raspberry Pi Zero 2 W Rev 1.0', '/dev/i2c-8', 'basic_limiter', 'Fixed'),
    ('Raspberry Pi 3 Model B Rev 1.2', '/dev/i2c-8', 'basic_limiter', 'Fixed'),
    ('Raspberry Pi 4 Model B Rev 1.5', '/dev/i2c-8', 'moderate_loud', 'Fixed'),
    ('Raspberry Pi 5 Model B Rev 1.0', '/dev/i2c-1', 'moderate_loud', 'unchanged'),
])
def test_model_plan_reuses_setup_pi(tmp_path, model, device, recommended, clock):
    path = tmp_path / 'model'
    path.write_bytes(model.encode() + b'\0')
    plan = support.hardware_plan(path)
    assert plan[1] == device and plan[2] == f'default/{recommended}.json'
    assert clock in plan[4]


def test_real_bundled_catalog_includes_all_profiles_and_display_names():
    catalog = support.profile_catalog(ROOT / 'audio-profiles/default')
    assert len(catalog) == len(list((ROOT / 'audio-profiles/default').glob('*.json')))
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
support() { printf 'default/basic_limiter.json\tBasic Limiter\ndefault/moderate_loud.json\tModerate Loud\n'; }
INDEX=0
read() { if [[ ${2:-} == -u ]]; then printf -v "${@: -1}" '%s' "${ANSWERS[INDEX++]}"; else builtin read "$@"; fi; }
'''


@pytest.mark.parametrize('answers,expected', [
    ("('' '' n y y)", 'RESULT:1:default/moderate_loud.json:no:yes'),
    ("(2 1 y n y)", 'RESULT:2:default/basic_limiter.json:yes:no'),
])
def test_fresh_wizard_defaults_and_selections(tmp_path, answers, expected):
    result = shell(tmp_path, MENU + f'ANSWERS={answers}\n' + '''collect_configuration
printf 'RESULT:%s:%s:%s:%s\n' "$REGION" "$DEFAULT_PROFILE" "$AUTH" "$HARDWARE"
''')
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout and 'Moderate Loud (recommended)' in result.stdout


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
    assert 'installation mode: partial' in result.stdout
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
    assert 'mode: partial' in result.stdout and '--region 2' in result.stdout
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
mkdir -p "$VENV/bin"
printf '#!/bin/sh\nprintf "PASSWORD_TOOL:%s\\n" "$*"\n' '%s' > "$VENV/bin/python"
chmod +x "$VENV/bin/python"
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
    elif [[ $1 == health ]]; then printf 'HTTP_CONTROLLER_UNAVAILABLE\n'; else return 99; fi
}
verify_installation
''')
    assert result.returncode == 0, result.stderr
    assert 'is-active' in result.stdout and 'HTTP_CONTROLLER_UNAVAILABLE' in result.stdout
    assert 'Installation complete' in result.stdout


def test_failure_propagates_and_stops_later_installation_steps(tmp_path):
    result = shell(tmp_path, '''
parse_arguments() { :; }; preflight() { :; }; detect_installation() { :; }
collect_configuration() { :; }; backup_existing() { :; }; stop_services() { :; }
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
