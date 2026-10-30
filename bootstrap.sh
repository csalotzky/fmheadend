#!/usr/bin/env bash
# Download one published release, then delegate installation to its installer.
set -euo pipefail
umask 077

WORK=
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n $WORK && ${WORK##*/} == fmheadend-bootstrap.* && -d $WORK && ! -L $WORK ]]; then
        rm -rf -- "$WORK" || printf 'Warning: could not remove temporary directory: %s\n' "$WORK" >&2
    fi
    return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

version=
while (($#)); do
    case $1 in
        --version)
            (($# >= 2)) && [[ -n $2 && -z $version ]] || die '--version requires one release tag.'
            version=$2
            [[ $version =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc[0-9]+)?$ ]] \
                || die 'Use a release tag such as v1.0.0 or v1.0.0-rc1.'
            shift 2
            ;;
        -h|--help)
            cat <<'HELP'
Usage: bash bootstrap.sh [--version TAG]

Download the latest published stable FMheadend release, or select an existing
stable/RC release with --version v1.0.0 or --version v1.0.0-rc1.
Requires Python 3 and sudo. Run as an ordinary user from an interactive terminal;
sudo is used only to launch the downloaded release's existing installer.

From GitHub:
  curl -fsSL https://raw.githubusercontent.com/csalotzky/fmheadend/master/bootstrap.sh | bash
  curl -fsSL https://raw.githubusercontent.com/csalotzky/fmheadend/master/bootstrap.sh | bash -s -- --version v1.0.0-rc1
HELP
            exit 0
            ;;
        *) die "Unknown argument: $1 (use --help)." ;;
    esac
done

printf 'FMheadend Bootstrap\n\nChecking prerequisites... '
for command in python3 sudo mktemp rm; do
    command -v "$command" >/dev/null || die "Missing prerequisite: $command"
done
printf 'OK\n'
WORK=$(mktemp -d "${TMPDIR:-/tmp}/fmheadend-bootstrap.XXXXXXXX")
WORK=$(cd -- "$WORK" && pwd -P)

python3 - "$WORK" "$version" <<'PY'
import gzip
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request

REPO = 'csalotzky/fmheadend'
API = f'https://api.github.com/repos/{REPO}'
WEBUI = 'fmheadend-webui.tar.gz'
VERSION = r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc[0-9]+)?'
work, requested = Path(sys.argv[1]), sys.argv[2]


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise ValueError('Refusing a non-HTTPS download redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


opener = urllib.request.build_opener(HTTPSRedirect())


def download(url, destination, accept='application/octet-stream'):
    request = urllib.request.Request(url, headers={
        'Accept': accept, 'User-Agent': 'fmheadend-bootstrap',
        'X-GitHub-Api-Version': '2026-03-10'})
    partial = destination.with_suffix(destination.suffix + '.part')
    with opener.open(request, timeout=60) as response, partial.open('wb') as target:
        shutil.copyfileobj(response, target)
        length = response.headers.get('Content-Length')
    if length is not None and partial.stat().st_size != int(length):
        raise ValueError('Incomplete download (Content-Length mismatch)')
    partial.replace(destination)


def archive_entries(path):
    # Read through the gzip trailer too: tar readers can stop before its CRC.
    with gzip.open(path, 'rb') as compressed:
        while compressed.read(1024 * 1024):
            pass
    with tarfile.open(path, 'r:gz') as archive:
        entries = {}
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or '\\' in member.name
                    or ':' in member.name or any(ord(c) < 32 for c in member.name)
                    or not (member.isdir() or member.isfile())):
                raise ValueError(f'Unsafe archive entry: {member.name!r}')
            key = str(name)
            if key == '.' and member.isdir():
                continue
            if key == '.' or key in entries:
                raise ValueError(f'Duplicate/invalid archive entry: {member.name!r}')
            entries[key] = member
        for name in entries:
            if any(str(parent) in entries and not entries[str(parent)].isdir()
                   for parent in PurePosixPath(name).parents):
                raise ValueError('Archive file used as a directory')
        return entries


try:
    label = f'Fetching release {requested}' if requested else 'Fetching latest stable release'
    print(f'{label}... ', end='', flush=True)
    endpoint = f'{API}/releases/tags/{requested}' if requested else f'{API}/releases/latest'
    metadata = work / 'release.json'
    download(endpoint, metadata, 'application/vnd.github+json')
    release = json.loads(metadata.read_text(encoding='utf-8'))
    tag = release.get('tag_name')
    if not isinstance(tag, str) or not re.fullmatch(VERSION, tag):
        raise ValueError('GitHub returned an unsupported release tag')
    if (release.get('draft') is not False or type(release.get('prerelease')) is not bool
            or release['prerelease'] != ('-rc' in tag)
            or (not requested and release['prerelease']) or (requested and tag != requested)
            or type(release.get('id')) is not int or release['id'] <= 0
            or release.get('html_url') != f'https://github.com/{REPO}/releases/tag/{tag}'):
        raise ValueError('GitHub returned unexpected or unpublished release metadata')
    source_url = release.get('tarball_url')
    if source_url != f'{API}/tarball/{tag}':
        raise ValueError('Source archive does not belong to the selected release tag')
    assets = release.get('assets')
    if not isinstance(assets, list) or not all(isinstance(a, dict) for a in assets):
        raise ValueError('Invalid release asset metadata')
    matches = [a for a in assets if a.get('name') == WEBUI]
    if len(matches) != 1:
        raise ValueError(f'Release must contain exactly one {WEBUI} asset')
    asset = matches[0]
    webui_url = f'https://github.com/{REPO}/releases/download/{tag}/{WEBUI}'
    if (asset.get('browser_download_url') != webui_url or asset.get('state') != 'uploaded'
            or type(asset.get('size')) is not int or asset['size'] <= 0):
        raise ValueError('WebUI asset is incomplete or belongs to another release')
    digest = asset.get('digest')
    if digest is not None and (not isinstance(digest, str)
                              or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest)):
        raise ValueError('Unsupported or malformed WebUI asset digest')
    print(tag, flush=True)

    for label, url, path in [('source', source_url, work / 'source.tar.gz'),
                             ('WebUI', webui_url, work / WEBUI)]:
        print(f'Downloading {label}... ', end='', flush=True)
        download(url, path)
        print('OK', flush=True)

    print('Verifying downloads... ', end='', flush=True)
    webui = work / WEBUI
    if webui.stat().st_size != asset['size']:
        raise ValueError('WebUI download size does not match release metadata')
    if digest is not None:
        checksum = hashlib.sha256()
        with webui.open('rb') as content:
            for chunk in iter(lambda: content.read(1024 * 1024), b''):
                checksum.update(chunk)
        if checksum.hexdigest() != digest.split(':')[1].lower():
            raise ValueError('WebUI SHA-256 digest mismatch')
    else:
        print('\nWarning: GitHub supplied no WebUI SHA-256 digest; only HTTPS, size '
              'and archive checks are available.', file=sys.stderr)
    web_entries = archive_entries(webui)
    files = {name for name, member in web_entries.items() if member.isfile() and member.size > 0}
    if ('index.html' not in files or not any(re.fullmatch(r'main[^/]*\.js', n) for n in files)
            or not any(re.fullmatch(r'styles[^/]*\.css', n) for n in files)
            or not any(n.startswith('assets/') for n in files)):
        raise ValueError('WebUI archive is missing root index.html, JavaScript, CSS or assets')
    entries = archive_entries(work / 'source.tar.gz')
    roots = {PurePosixPath(name).parts[0] for name in entries}
    if len(roots) != 1:
        raise ValueError('Source archive must contain one project directory')
    prefix = roots.pop() + '/'
    required = ['install.sh', 'daemon/pyproject.toml', 'daemon/app.py', 'muxd/CMakeLists.txt',
                'tools/install_support.py', 'tools/setup_pi.py', 'tools/create_passwd.py',
                'systemd/fmheadendd.service', 'systemd/fmheadend-muxd.service']
    if (any(prefix + name not in entries or not entries[prefix + name].isfile()
            or entries[prefix + name].size == 0 for name in required)
            or not any(name.startswith(prefix + 'audio-profiles/default/') and name.endswith('.json')
                       and member.isfile() for name, member in entries.items())):
        raise ValueError('Source archive is missing installer/project files')
    print('OK', flush=True)

    print('Extracting source... ', end='', flush=True)
    source = work / 'source'
    source.mkdir(mode=0o700)
    with tarfile.open(work / 'source.tar.gz', 'r:gz') as archive:
        for name, member in entries.items():
            if name == prefix[:-1]:
                continue
            relative = name[len(prefix):]
            target = source.joinpath(*PurePosixPath(relative).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as content, target.open('xb') as output:
                    shutil.copyfileobj(content, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    (source / 'install.sh').chmod(0o755)
    print('OK', flush=True)
except (OSError, EOFError, ValueError, TypeError, AttributeError, tarfile.TarError, http.client.HTTPException,
        urllib.error.URLError) as error:
    print(f'\nError: {error}. Release downloads/validation failed; installer was not started.',
          file=sys.stderr)
    sys.exit(1)
PY

printf '\nStarting FMheadend installer...\n'
# Foreground invocation preserves output and the controlling terminal. The
# installer opens /dev/tty itself, including when this script is piped to Bash.
(
    cd -- "$WORK/source"
    sudo ./install.sh --webui-archive "$WORK/fmheadend-webui.tar.gz"
)
