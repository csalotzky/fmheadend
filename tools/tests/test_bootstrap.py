"""Offline bootstrap tests. Network and sudo are always replaced before execution.

Only a generated, byte-for-byte checked test installer may be invoked. No test
downloads a real release, executes install.sh, or invokes the system sudo.
"""
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which('bash') or 'C:/Program Files/Git/bin/bash.exe'
API = 'https://api.github.com/repos/csalotzky/fmheadend'
GITHUB = 'https://github.com/csalotzky/fmheadend'
WEBUI = 'fmheadend-webui.tar.gz'
INSTALLER = b'''#!/usr/bin/env bash
set -eu
[[ ${BOOTSTRAP_TEST_ACTIVE:-} == yes ]] || exit 99
[[ $# == 2 && $1 == --webui-archive && -s $2 && -f daemon/pyproject.toml ]] || exit 98
printf '%s\\n' "$PWD" "$1" "$2" "$(cat daemon/pyproject.toml)" > "$TEST_INSTALL_LOG"
printf 'Test installer output\\n'
printf 'Test installer diagnostic\\n' >&2
exit "${TEST_INSTALL_STATUS:-0}"
'''
REQUIRED = ['install.sh', 'daemon/pyproject.toml', 'daemon/app.py', 'muxd/CMakeLists.txt',
            'tools/install_support.py', 'tools/setup_pi.py', 'tools/create_passwd.py',
            'systemd/fmheadendd.service', 'systemd/fmheadend-muxd.service',
            'audio-profiles/default/basic.json']


def archive(path, entries):
    with tarfile.open(path, 'w:gz') as output:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            if isinstance(content, tarfile.TarInfo):
                content.name = name
                output.addfile(content)
            else:
                member.mode = 0o755 if name.endswith('/install.sh') else 0o644
                member.size = len(content)
                output.addfile(member, BytesIO(content))


def native_path(value):
    if os.name == 'nt':
        value = subprocess.check_output([BASH, '-c', 'cygpath -w -- "$1"',
                                         'convert-test-path', value], text=True).strip()
    return Path(value)


@pytest.fixture
def harness(tmp_path):
    private = tmp_path / 'private temp directories'
    private.mkdir()
    outside = tmp_path / 'keep-me'
    outside.write_text('outside the bootstrap workspace')
    expected_installer = tmp_path / 'expected-installer.sh'
    expected_installer.write_bytes(INSTALLER)
    shim = tmp_path / 'python-shim.py'
    shim.write_text('''import http.client, io, json, os, sys, urllib.error, urllib.request
from pathlib import Path
config = json.loads(Path(os.environ['TEST_HTTP_CONFIG']).read_text())
class Response(io.BytesIO):
    def read(self, size=-1):
        if self.break_transfer:
            if self.tell():
                raise http.client.IncompleteRead(b'partial', 10)
            size = 10
        return super().read(size)
class OfflineOpener:
    def open(self, request, timeout):
        assert request.full_url.startswith('https://') and timeout == 60
        assert request.get_header('X-github-api-version') == '2026-03-10'
        work = Path(sys.argv[1])
        if os.name == 'posix':
            assert work.stat().st_mode & 0o777 == 0o700
        with open(os.environ['TEST_HTTP_LOG'], 'a') as log:
            log.write(request.full_url + '\\n')
        response = config.get(request.full_url)
        if response is None or 'http_error' in response:
            raise urllib.error.HTTPError(request.full_url, (response or {}).get('http_error', 404),
                                         'stubbed HTTP failure', {}, None)
        if 'network_error' in response:
            raise urllib.error.URLError(response['network_error'])
        data = Path(response['path']).read_bytes()
        result = Response(data)
        result.break_transfer = response.get('read_error', False)
        result.headers = {'Content-Length': str(response.get('length', len(data)))}
        return result
# No real opener or unlisted URL can be used by these tests.
urllib.request.build_opener = lambda *handlers: OfflineOpener()
sys.argv = ['-', *sys.argv[2:]]
exec(compile(sys.stdin.read(), '<bootstrap-inline>', 'exec'), {'__name__': '__main__'})
''')
    state = SimpleNamespace(private=private, outside=outside, source=tmp_path / 'source.tar.gz',
                            webui=tmp_path / WEBUI, metadata=tmp_path / 'release.json',
                            install_log=tmp_path / 'install.log', http_log=tmp_path / 'http.log',
                            config_file=tmp_path / 'http.json', responses={}, tmp_path=tmp_path)

    def select(tag='v1.0.0'):
        state.tag = tag
        state.source_entries = {f'csalotzky-fmheadend-123abcd/{name}':
                                INSTALLER if name == 'install.sh' else tag.encode()
                                for name in REQUIRED}
        archive(state.source, state.source_entries)
        state.web_entries = {'./index.html': tag.encode(), './main-test.js': b'js',
                             './styles-test.css': b'css', './assets/logo.svg': b'svg'}
        archive(state.webui, state.web_entries)
        state.release = {'id': 123, 'tag_name': tag, 'draft': False, 'prerelease': '-rc' in tag,
                         'html_url': f'{GITHUB}/releases/tag/{tag}',
                         'tarball_url': f'{API}/tarball/{tag}',
                         'assets': [{'name': WEBUI, 'state': 'uploaded',
                                     'browser_download_url': f'{GITHUB}/releases/download/{tag}/{WEBUI}',
                                     'size': state.webui.stat().st_size,
                                     'digest': 'sha256:' + hashlib.sha256(state.webui.read_bytes()).hexdigest()}]}
        state.responses = {f'{API}/releases/latest': {'path': str(state.metadata)},
                           f'{API}/releases/tags/{tag}': {'path': str(state.metadata)},
                           f'{API}/tarball/{tag}': {'path': str(state.source)},
                           f'{GITHUB}/releases/download/{tag}/{WEBUI}': {'path': str(state.webui)}}

    def refresh_webui_metadata():
        state.release['assets'][0].update(size=state.webui.stat().st_size,
            digest='sha256:' + hashlib.sha256(state.webui.read_bytes()).hexdigest())

    def run(*args, status=0, interrupt='', missing='', raw_metadata=None):
        state.metadata.write_text(raw_metadata if raw_metadata is not None else json.dumps(state.release))
        state.config_file.write_text(json.dumps(state.responses))
        env = dict(os.environ)
        env.pop('BASH_ENV', None)
        env.pop('ENV', None)
        env.update(TMPDIR=private.as_posix(), TEST_HTTP_CONFIG=state.config_file.as_posix(),
                   TEST_HTTP_LOG=state.http_log.as_posix(), TEST_INSTALL_LOG=state.install_log.as_posix(),
                   TEST_INSTALL_STATUS=str(status), BOOTSTRAP_TEST_ACTIVE='yes',
                   TEST_INTERRUPT=interrupt, TEST_MISSING=missing, LANG='C', LC_ALL='C')
        prefix = f'''
command() {{
    if [[ $1 == -v && $2 == "$TEST_MISSING" ]]; then return 1; fi
    builtin command "$@"
}}
python3() {{
    if [[ $TEST_INTERRUPT == download ]]; then kill -INT "$$"; return 130; fi
    {shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(shim.as_posix())} "$@"
}}
sudo() {{
    [[ $# == 3 && $1 == ./install.sh && $2 == --webui-archive ]] || return 97
    cmp -s -- ./install.sh {shlex.quote(expected_installer.as_posix())} || return 96
    if [[ $TEST_INTERRUPT == installer ]]; then kill -TERM "$$"; return 143; fi
    "$BASH" "$@"
}}
export -f command python3 sudo
bash -s -- "$@"
'''
        result = subprocess.run([BASH, '-c', prefix, 'bootstrap-test', *args],
                                input=(ROOT / 'bootstrap.sh').read_text(), cwd=tmp_path, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert list(private.iterdir()) == [], 'bootstrap temporary files were not cleaned up'
        assert outside.read_text() == 'outside the bootstrap workspace'
        return result

    state.select = select
    state.run = run
    state.refresh_webui_metadata = refresh_webui_metadata
    select()
    return state


def assert_not_installed(harness, result, message=None):
    assert result.returncode != 0
    assert not harness.install_log.exists()
    assert 'Starting FMheadend installer' not in result.stdout
    if message:
        assert message in result.stderr, result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('tag,explicit', [('v1.0.0', False), ('v1.0.0', True),
                                          ('v1.0.0-rc1', True), ('v12.34.567-rc12', True)])
def test_release_selection_and_installation_paths(harness, tag, explicit):
    harness.select(tag)
    result = harness.run(*(['--version', tag] if explicit else []))
    assert result.returncode == 0, result.stderr
    calls = harness.http_log.read_text().splitlines()
    endpoint = f'{API}/releases/tags/{tag}' if explicit else f'{API}/releases/latest'
    assert calls == [endpoint, f'{API}/tarball/{tag}', f'{GITHUB}/releases/download/{tag}/{WEBUI}']
    cwd, option, webui, source_tag = harness.install_log.read_text().splitlines()
    assert option == '--webui-archive' and source_tag == tag
    cwd, webui = native_path(cwd), native_path(webui)
    assert cwd.parent == webui.parent and cwd.name == 'source' and webui.name == WEBUI
    assert cwd.parent.parent == harness.private
    assert not cwd.exists() and not webui.exists()
    assert 'Test installer output' in result.stdout
    assert 'Test installer diagnostic' in result.stderr


@pytest.mark.parametrize('args', [('--version',), ('--version', ''), ('--version', '--help'),
    ('--version=v1.0.0',), ('--unknown',), ('v1.0.0',), ('--version', 'master'),
    ('--version', '1.0.0'), ('--version', 'v1.0'), ('--version', 'v01.0.0'),
    ('--version', 'v1.01.0'), ('--version', 'v1.0.01'), ('--version', 'v1.0.0-beta1'),
    ('--version', 'v1.0.0-rc'), ('--version', 'v1.0.0-rc1/../master'),
    ('--version', 'v1.0.0; touch keep-me'), ('--version', 'v1.0.0', '--version', 'v1.0.1')])
def test_invalid_arguments_fail_without_downloads(harness, args):
    assert_not_installed(harness, harness.run(*args), 'Error:')
    assert not harness.http_log.exists()


def test_help_needs_no_downloads_or_privileges(harness):
    result = harness.run('--help')
    assert result.returncode == 0 and '--version v1.0.0-rc1' in result.stdout
    assert not harness.http_log.exists() and not harness.install_log.exists()


@pytest.mark.parametrize('command', ['python3', 'sudo', 'mktemp', 'rm'])
def test_missing_prerequisites(harness, command):
    assert_not_installed(harness, harness.run(missing=command), f'Missing prerequisite: {command}')
    assert not harness.http_log.exists()


@pytest.mark.parametrize('raw', ['not JSON', '[]', 'null', '{}', '{"message":"Not Found"}'])
def test_bad_api_response(harness, raw):
    assert_not_installed(harness, harness.run(raw_metadata=raw), 'Error:')


@pytest.mark.parametrize('field,value', [('draft', True), ('draft', 'false'), ('prerelease', True),
    ('prerelease', 'false'), ('id', 0), ('id', True), ('tag_name', 'master'),
    ('html_url', 'https://github.com/other/project/releases/tag/v1.0.0'),
    ('tarball_url', f'{API}/tarball/master'), ('tarball_url', 'http://example.test/source.tar.gz'),
    ('assets', None), ('assets', [None]), ('assets', [])])
def test_invalid_release_metadata(harness, field, value):
    harness.release[field] = value
    assert_not_installed(harness, harness.run(), 'Error:')
    assert len(harness.http_log.read_text().splitlines()) == 1


def test_explicit_version_never_falls_back(harness):
    assert_not_installed(harness, harness.run('--version', 'v2.0.0'), 'HTTP Error 404')
    assert harness.http_log.read_text().splitlines() == [f'{API}/releases/tags/v2.0.0']


def test_latest_does_not_accept_an_rc(harness):
    harness.select('v1.0.0-rc1')
    assert_not_installed(harness, harness.run(), 'release metadata')


def test_rc_metadata_must_be_marked_prerelease(harness):
    harness.select('v1.0.0-rc1')
    harness.release['prerelease'] = False
    assert_not_installed(harness, harness.run('--version', 'v1.0.0-rc1'), 'release metadata')


def test_requested_tag_must_match_response(harness):
    harness.responses[f'{API}/releases/tags/v2.0.0'] = {'path': str(harness.metadata)}
    assert_not_installed(harness, harness.run('--version', 'v2.0.0'), 'release metadata')


@pytest.mark.parametrize('field,value', [('state', 'new'), ('size', 0), ('size', True),
    ('browser_download_url', f'{GITHUB}/releases/download/v2.0.0/{WEBUI}'),
    ('browser_download_url', 'https://example.test/untrusted.tar.gz'),
    ('digest', ''), ('digest', 'sha256:bad'), ('digest', 'sha512:' + 'a' * 128),
    ('digest', True)])
def test_invalid_asset_metadata(harness, field, value):
    harness.release['assets'][0][field] = value
    assert_not_installed(harness, harness.run(), 'Error:')


def test_duplicate_webui_assets_rejected(harness):
    harness.release['assets'] *= 2
    assert_not_installed(harness, harness.run(), 'exactly one')


@pytest.mark.parametrize('which', ['api', 'source', 'webui'])
@pytest.mark.parametrize('failure', [{'http_error': 404}, {'http_error': 403},
                                    {'http_error': 500}, {'network_error': 'connection timed out'},
                                    {'length': 999999}, {'read_error': True}])
def test_http_and_incomplete_download_failures(harness, which, failure):
    url = {'api': f'{API}/releases/latest', 'source': f'{API}/tarball/v1.0.0',
           'webui': f'{GITHUB}/releases/download/v1.0.0/{WEBUI}'}[which]
    harness.responses[url].update(failure)
    assert_not_installed(harness, harness.run(), 'Error:')


@pytest.mark.parametrize('field,value,message', [('size', 1, 'size'),
                                               ('digest', 'sha256:' + 'f' * 64, 'SHA-256')])
def test_integrity_mismatch(harness, field, value, message):
    harness.release['assets'][0][field] = value
    assert_not_installed(harness, harness.run(), message)


@pytest.mark.parametrize('missing', [False, True])
def test_missing_digest_is_explicitly_reported(harness, missing):
    if missing:
        del harness.release['assets'][0]['digest']
    else:
        harness.release['assets'][0]['digest'] = None
    result = harness.run()
    assert result.returncode == 0
    assert 'no WebUI SHA-256 digest' in result.stderr and 'only HTTPS, size' in result.stderr


@pytest.mark.parametrize('which', ['source', 'webui'])
@pytest.mark.parametrize('content', [b'', b'not gzip', b'\x1f\x8b\x08'])
def test_bad_archives(harness, which, content):
    getattr(harness, which).write_bytes(content)
    if which == 'webui':
        harness.refresh_webui_metadata()
    assert_not_installed(harness, harness.run(), 'Error:')


@pytest.mark.parametrize('which', ['source', 'webui'])
def test_truncated_gzip_footer(harness, which):
    path = getattr(harness, which)
    path.write_bytes(path.read_bytes()[:-6])
    if which == 'webui':
        harness.refresh_webui_metadata()
    assert_not_installed(harness, harness.run(), 'Error:')


@pytest.mark.parametrize('which', ['source', 'webui'])
@pytest.mark.parametrize('name,kind', [('../keep-me', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
    ('C:/escape', tarfile.REGTYPE), ('dir\\escape', tarfile.REGTYPE), ('a\nb', tarfile.REGTYPE),
    ('link', tarfile.SYMTYPE), ('link', tarfile.LNKTYPE), ('fifo', tarfile.FIFOTYPE),
    ('device', tarfile.CHRTYPE)])
def test_unsafe_archive_entries(harness, which, name, kind):
    entry = tarfile.TarInfo(name)
    entry.type, entry.linkname = kind, '../../keep-me'
    entries = dict(getattr(harness, 'source_entries' if which == 'source' else 'web_entries'))
    entries[name] = entry
    archive(getattr(harness, which), entries)
    if which == 'webui':
        harness.refresh_webui_metadata()
    assert_not_installed(harness, harness.run(), 'Unsafe archive')


@pytest.mark.parametrize('missing', REQUIRED)
def test_missing_source_files(harness, missing):
    del harness.source_entries['csalotzky-fmheadend-123abcd/' + missing]
    archive(harness.source, harness.source_entries)
    assert_not_installed(harness, harness.run(), 'missing installer/project')


def test_missing_webui_root(harness):
    archive(harness.webui, {'browser/index.html': b'html'})
    harness.refresh_webui_metadata()
    assert_not_installed(harness, harness.run(), 'missing root index.html')


def test_source_requires_one_top_level_directory(harness):
    harness.source_entries['another-project/file'] = b'foreign file'
    archive(harness.source, harness.source_entries)
    assert_not_installed(harness, harness.run(), 'one project directory')


@pytest.mark.parametrize('kind', ['duplicate', 'file-parent'])
def test_conflicting_archive_entries(harness, kind):
    harness.web_entries['index.html' if kind == 'duplicate' else './assets'] = b'conflict'
    archive(harness.webui, harness.web_entries)
    harness.refresh_webui_metadata()
    assert_not_installed(harness, harness.run(), 'archive' if kind == 'duplicate' else 'directory')


@pytest.mark.parametrize('status', [1, 42, 130, 143])
def test_installer_exit_status_is_preserved(harness, status):
    result = harness.run(status=status)
    assert result.returncode == status and harness.install_log.exists()
    assert 'Test installer diagnostic' in result.stderr


@pytest.mark.parametrize('stage,status', [('download', 130), ('installer', 143)])
def test_interruption_cleans_private_directory(harness, stage, status):
    result = harness.run(interrupt=stage)
    assert result.returncode == status


def test_no_privileged_bootstrap_work_or_installation_logic():
    script = (ROOT / 'bootstrap.sh').read_text()
    assert script.count('    sudo ') == 1
    assert 'sudo ./install.sh --webui-archive' in script
    assert not any(command in script for command in ['apt-get', 'systemctl', 'reboot', 'eval ', 'EUID'])
    assert '/dev/tty' in (ROOT / 'install.sh').read_text()


def test_https_redirects_cannot_downgrade():
    script = (ROOT / 'bootstrap.sh').read_text()
    python = script.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    definition = python[python.index('class HTTPSRedirect'):python.index('\n\nopener =')]
    import urllib.request
    import urllib.parse
    namespace = {'urllib': __import__('urllib')}
    exec(definition, namespace)
    handler = namespace['HTTPSRedirect']()
    with pytest.raises(ValueError, match='non-HTTPS'):
        handler.redirect_request(None, None, 302, '', {}, 'http://example.test/archive')
