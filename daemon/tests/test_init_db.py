"""First-install database CLI: real profile validation, safe file publication."""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile

import pytest

from fmheadendd import init_db
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import SystemConfig

DEFAULTS = Path(__file__).resolve().parents[2] / 'audio-profiles/default'


def arguments(path, *extra):
    return ['--database', str(path), '--audio-profiles-default-dir', str(DEFAULTS), *extra]


@pytest.mark.parametrize('region,preemphasis', [(0, 50), (1, 50), (2, 75), (3, 50)])
def test_initial_region_and_default_profile(tmp_path, region, preemphasis):
    path = tmp_path / 'headend.db'
    assert init_db.main(arguments(path, '--region', str(region), '--default-audio-profile',
                                  'default/moderate_loud.json')) == 0
    db = Database(path)
    try:
        assert db.get_system() == SystemConfig(default_audio_profile_id='default/moderate_loud.json')
        modulators = db.list('modulators')
        assert [m.id for m in modulators] == list(range(8))
        assert all(m.preemphasis_us == preemphasis and not m.enabled for m in modulators)
        assert all(m.gain_mode == 'MANUAL' and m.rds_enabled for m in modulators)
    finally:
        db.close()


def test_cli_defaults_match_application_and_models(tmp_path):
    defaults = init_db.argument_parser().parse_args([])
    assert defaults.database == Path(Settings.database_path)
    assert defaults.region == 1
    assert defaults.default_audio_profile == SystemConfig().default_audio_profile_id
    assert defaults.audio_profiles_default_dir == Path(Settings.audio_profiles_default_dir)
    path = tmp_path / 'headend.db'
    assert init_db.main(arguments(path)) == 0
    db = Database(path)
    try:
        assert db.get_system() == SystemConfig()
        assert all(m.preemphasis_us == 50 for m in db.list('modulators'))
    finally:
        db.close()


def test_invalid_region_does_not_create_database(tmp_path):
    path = tmp_path / 'headend.db'
    with pytest.raises(SystemExit) as exc:
        init_db.main(arguments(path, '--region', '4'))
    assert exc.value.code == 2
    assert not path.exists()


@pytest.mark.parametrize('profile', ['../bad.json', 'default/../bad.json', '', 'default/missing.json'])
def test_invalid_or_missing_profile_does_not_create_database(tmp_path, capsys, profile):
    path = tmp_path / 'new-parent/headend.db'
    assert init_db.main(arguments(path, '--default-audio-profile', profile)) == 1
    assert 'Cannot initialize database' in capsys.readouterr().err
    assert not path.parent.exists()


@pytest.mark.parametrize('content', ['{bad', '{}'])
def test_invalid_profile_contents_rejected_before_database_creation(tmp_path, content):
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    (profiles / 'broken.json').write_text(content)
    path = tmp_path / 'headend.db'
    assert init_db.main(['--database', str(path), '--audio-profiles-default-dir', str(profiles),
                         '--default-audio-profile', 'default/broken.json']) == 1
    assert not path.exists()


def test_missing_profile_storage_does_not_create_database(tmp_path):
    path = tmp_path / 'headend.db'
    assert init_db.main(['--database', str(path), '--audio-profiles-default-dir', str(tmp_path / 'missing')]) == 1
    assert not path.exists()


@pytest.mark.parametrize('initialized', [False, True])
def test_existing_files_are_never_opened_or_modified(tmp_path, monkeypatch, capsys, initialized):
    path = tmp_path / 'existing.db'
    if initialized:
        db = Database(path)
        db.save_system(SystemConfig(global_stopped=True))
        db.close()
    else:
        path.write_bytes(b'')
    before = path.read_bytes()
    monkeypatch.setattr(init_db, 'Database', lambda *a, **k: pytest.fail('Existing database opened'))
    assert init_db.main(arguments(path, '--region', '2')) == 1
    assert 'already exists' in capsys.readouterr().err
    assert path.read_bytes() == before


@pytest.mark.parametrize('stage', ['initialize', 'save', 'sync', 'publish'])
def test_failure_does_not_leave_a_database_or_staging_files(tmp_path, monkeypatch, stage):
    path = tmp_path / 'headend.db'
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('injected initialization failure')
    if stage == 'initialize':
        monkeypatch.setattr(Database, '_initialize', fail)
    elif stage == 'save':
        monkeypatch.setattr(Database, 'save_system', fail)
    elif stage == 'sync':
        monkeypatch.setattr(init_db.os, 'fsync', fail)
    else:
        monkeypatch.setattr(init_db.os, 'link', fail)
    assert init_db.main(arguments(path)) == 1
    assert list(tmp_path.iterdir()) == []


def test_concurrent_destination_creation_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / 'headend.db'
    link = init_db.os.link
    def race(source, destination):
        path.write_bytes(b'created by another process')
        link(source, destination)
    monkeypatch.setattr(init_db.os, 'link', race)
    assert init_db.main(arguments(path)) == 1
    assert path.read_bytes() == b'created by another process'
    assert list(tmp_path.iterdir()) == [path]


def test_custom_profile_root_is_resolved_and_saved(tmp_path):
    custom = tmp_path / 'custom'
    custom.mkdir()
    shutil.copy2(DEFAULTS / 'basic_filtering.json', custom / 'station.json')
    path = tmp_path / 'headend.db'
    assert init_db.main(arguments(path, '--audio-profiles-custom-dir', str(custom),
                                  '--default-audio-profile', 'custom/station.json')) == 0
    db = Database(path)
    try:
        assert db.get_system().default_audio_profile_id == 'custom/station.json'
    finally:
        db.close()


@pytest.fixture
def installed_directory():
    with tempfile.TemporaryDirectory(prefix='fmheadend-installed-init-') as directory:
        yield Path(directory)


def test_installed_cli_outside_checkout(installed_directory):
    tmp_path = installed_directory
    root = Path(__file__).resolve().parents[2]
    source = tmp_path / 'source'
    shutil.copytree(root / 'daemon', source,
                    ignore=shutil.ignore_patterns('tests', '__pycache__', '*.egg-info', 'build'))
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-index', '--no-deps', '--no-build-isolation',
                    '--wheel-dir', str(tmp_path), str(source)], check=True, capture_output=True, text=True)
    environment = tmp_path / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], check=True)
    scripts = environment / ('Scripts' if os.name == 'nt' else 'bin')
    python = scripts / ('python.exe' if os.name == 'nt' else 'python')
    wheel = next(tmp_path.glob('fmheadendd-*.whl'))
    subprocess.run([sys.executable, '-m', 'pip', '--python', str(python), 'install', '--no-index',
                    '--no-deps', str(wheel)], check=True, capture_output=True, text=True)
    site = subprocess.check_output([str(python), '-I', '-c',
                                   'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip()
    # Reuse existing test dependencies; do not install/download any dependencies.
    (Path(site) / 'test-dependencies.pth').write_text(sysconfig.get_path('purelib') + '\n')
    executable = scripts / ('fmheadend-init-db.exe' if os.name == 'nt' else 'fmheadend-init-db')
    clean_env = dict(os.environ)
    clean_env.pop('PYTHONPATH', None)
    path = tmp_path / 'installed.db'
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    shutil.copy2(DEFAULTS / 'basic_filtering.json', profiles)
    result = subprocess.run([str(executable), '--database', str(path), '--audio-profiles-default-dir',
                             str(profiles), '--region', '2'], cwd=tmp_path,
                            env=clean_env, check=True, capture_output=True, text=True)
    assert 'Initialized' in result.stdout
    db = Database(path)
    try:
        assert db.get_system() == SystemConfig()
        assert all(m.preemphasis_us == 75 for m in db.list('modulators'))
    finally:
        db.close()
    probe = '''import importlib.abc, pathlib, sys
class NoHardware(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'gi' or fullname.startswith(('fmheadendd.app', 'fmheadendd.rp2354', 'fmheadendd.sources.gst_pipeline')):
            raise AssertionError('Unexpected hardware/pipeline import: ' + fullname)
sys.meta_path.insert(0, NoHardware())
import fmheadendd.init_db
assert pathlib.Path(fmheadendd.init_db.__file__).is_relative_to(pathlib.Path(sys.prefix))
'''
    subprocess.run([str(python), '-I', '-c', probe], cwd=tmp_path, env=clean_env, check=True)
