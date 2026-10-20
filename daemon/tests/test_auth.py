import asyncio
import base64
import threading

import bcrypt
import pytest
from fastapi.testclient import TestClient

from fmheadendd.app import argument_parser, create_app
from fmheadendd.auth import BasicAuth, CACHE_SIZE, CACHE_TTL, MAX_PENDING, PasswordFileError
from fmheadendd.config import DEFAULT_PASSWD_FILE
from fmheadendd.tests.profile_support import profile_settings
from fmheadendd.sources.output import PcmOutput


def write_password(path, password=b'secret', prefix=b'2b'):
    hashed = bcrypt.hashpw(password, bcrypt.gensalt(rounds=4))
    path.write_bytes(b'alice:' + b'$' + prefix + hashed[3:] + b'\n')


def headers(password=b'secret', user=b'alice'):
    return [(b'authorization', b'Basic ' + base64.b64encode(user + b':' + password))]


def test_cli_paths_are_independent():
    parser = argument_parser()
    assert parser.parse_args([]).passwd_file is None
    assert parser.parse_args([]).audio_profiles_custom_dir == '/var/lib/fmheadend/audio-profiles/custom'
    assert parser.parse_args(['--passwd-file']).passwd_file == '/var/lib/fmheadend/htpasswd'
    assert parser.parse_args(['--passwd-file', '--audio-profiles-custom-dir', '/elsewhere']).passwd_file == DEFAULT_PASSWD_FILE
    assert parser.parse_args(['--passwd-file', '/private/passwords']).passwd_file == '/private/passwords'


@pytest.mark.parametrize('raw', [None, b'', b'\n# empty\n', b'alice:plain', b'alice:$apr1$old',
                                  b'alice:$2b$31$' + b'.' * 53, b'not-an-entry', b'x' * 65537],
                         ids=['missing', 'empty', 'comments', 'plaintext', 'md5', 'cost', 'syntax', 'oversized'])
def test_bad_file_fails_before_service_start(tmp_path, raw):
    path = tmp_path / 'htpasswd'
    if raw is not None:
        path.write_bytes(raw)
    database = tmp_path / 'never-created.db'
    app = create_app(profile_settings(database, passwd_file=path))
    with pytest.raises(PasswordFileError), TestClient(app):
        pass
    assert not database.exists()


def test_duplicate_users_and_unreadable_file(tmp_path, monkeypatch):
    path = tmp_path / 'htpasswd'
    write_password(path)
    path.write_bytes(path.read_bytes() * 2)
    auth = BasicAuth(path)
    with pytest.raises(PasswordFileError, match='line 2'):
        auth._read()
    write_password(path)
    def denied(*args, **kwargs):
        raise PermissionError('sensitive OS message')
    monkeypatch.setattr('fmheadendd.auth.os.open', denied)
    with pytest.raises(PasswordFileError, match='PermissionError') as error:
        asyncio.run(auth.start())
    assert 'sensitive' not in str(error.value)
    assert auth.executor is None


@pytest.mark.parametrize('prefix', [b'2a', b'2b', b'2y'])
def test_entire_http_app_is_protected(tmp_path, prefix):
    path = tmp_path / 'htpasswd'
    write_password(path, prefix=prefix)
    www = tmp_path / 'www'
    www.mkdir()
    (www / 'index.html').write_text('<html>SPA</html>')
    (www / 'main.js').write_text('asset')
    app = create_app(profile_settings(tmp_path / 'db', passwd_file=path, webui_dir=www))
    with TestClient(app) as client:
        for route in ['/api/v1/status', '/api/v1/system/config', '/docs', '/openapi.json',
                      '/', '/index.html', '/main.js', '/sources/1', '/missing']:
            response = client.get(route)
            assert response.status_code == 401
            assert response.headers['www-authenticate'] == 'Basic realm="FMheadend", charset="UTF-8"'
            assert response.headers['cache-control'] == 'no-store'
        assert client.patch('/api/v1/system/config', json={'web_port': 9999}).status_code == 401
        assert client.options('/api/v1/status').status_code == 401
        assert client.get('/main.js', auth=('alice', 'wrong')).status_code == 401
        assert client.get('/main.js', auth=('alice', 'secret')).text == 'asset'
        assert client.get('/sources/1', auth=('alice', 'secret')).text == '<html>SPA</html>'
        assert client.get('/api/v1/status', auth=('alice', 'secret')).status_code == 200
        assert client.get('/api/v1/system/config', auth=('alice', 'secret')).json()['web_port'] == 8080
        assert client.patch('/api/v1/system/config', json={'web_port': 8888}, auth=('alice', 'secret')).status_code == 200


def test_disabled_and_credential_limits(tmp_path):
    with TestClient(create_app(profile_settings(tmp_path / 'db'))) as client:
        assert client.get('/api/v1/status').status_code == 200
    path = tmp_path / 'htpasswd'
    write_password(path)
    async def run():
        auth = BasicAuth(path)
        await auth.start()
        try:
            for value in [[], headers() * 2, [(b'authorization', b'Bearer token')],
                          [(b'authorization', b'Basic !!!!')], [(b'authorization', b'x' * 513)],
                          headers(b'x' * 73), headers(user=b'x' * 129), headers(b'\xff')]:
                assert not await auth.authenticate(value)
        finally:
            await auth.close()
    asyncio.run(run())


def test_cache_polling_expiry_and_file_replacement(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'htpasswd'
    write_password(path)
    now = [0.0]
    original = bcrypt.checkpw
    calls = []
    def check(password, hashed):
        calls.append(True)
        return original(password, hashed)
    monkeypatch.setattr(bcrypt, 'checkpw', check)
    async def run():
        auth = BasicAuth(path, clock=lambda: now[0])
        await auth.start()
        try:
            for _ in range(20):
                assert await auth.authenticate(headers())
            assert len(calls) == 1
            assert all(isinstance(key, bytes) and len(key) == 32 for key in auth.cache)
            now[0] += CACHE_TTL + 1
            assert await auth.authenticate(headers())
            assert len(calls) == 2
            write_password(path, b'replaced')
            assert not await auth.authenticate(headers())
            assert await auth.authenticate(headers(b'replaced'))
            path.write_bytes(b'corrupt')
            assert not await auth.authenticate(headers(b'replaced'))
            assert not auth.cache
            write_password(path, b'replaced')
            assert await auth.authenticate(headers(b'replaced'))
            path.unlink()
            assert not await auth.authenticate(headers(b'replaced'))
            assert not auth.cache
        finally:
            await auth.close()
    asyncio.run(run())
    assert 'Password file' in caplog.text
    assert 'secret' not in caplog.text and 'replaced' not in caplog.text and '$2' not in caplog.text


def test_cache_size_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'htpasswd'
    write_password(path)
    # Exercise eviction without hundreds of expensive real password checks.
    monkeypatch.setattr(bcrypt, 'checkpw', lambda password, hashed: True)
    async def run():
        auth = BasicAuth(path)
        await auth.start()
        try:
            for n in range(CACHE_SIZE + 10):
                assert await auth.authenticate(headers(str(n).encode()))
            assert len(auth.cache) == CACHE_SIZE
        finally:
            await auth.close()
    asyncio.run(run())


def test_file_change_during_bcrypt_is_not_accepted(tmp_path, monkeypatch):
    path = tmp_path / 'htpasswd'
    write_password(path)
    original = bcrypt.checkpw
    def replace(password, hashed):
        valid = original(password, hashed)
        write_password(path, b'new-password')
        return valid
    monkeypatch.setattr(bcrypt, 'checkpw', replace)
    async def run():
        auth = BasicAuth(path)
        await auth.start()
        try:
            assert not await auth.authenticate(headers())
            assert not auth.cache
        finally:
            await auth.close()
    asyncio.run(run())


def test_blocked_verification_leaves_control_responsive_and_shutdown_drains(tmp_path, monkeypatch):
    path = tmp_path / 'htpasswd'
    write_password(path)
    entered, release = threading.Event(), threading.Event()
    original = bcrypt.checkpw
    def blocked(password, hashed):
        entered.set()
        assert release.wait(5)
        return original(password, hashed)
    monkeypatch.setattr(bcrypt, 'checkpw', blocked)
    app = create_app(profile_settings(tmp_path / 'db', passwd_file=path))
    async def run():
        async with app.router.lifespan_context(app):
            auth = BasicAuth(path)
            await auth.start()
            tasks = []
            try:
                tasks = [asyncio.create_task(auth.authenticate(headers())) for _ in range(MAX_PENDING)]
                async with asyncio.timeout(3):
                    while not entered.is_set() or len(auth.pending) != MAX_PENDING:
                        await asyncio.sleep(0)
                assert not await auth.authenticate(headers())
                # Uses the actual serialized control worker, independently of bcrypt.
                await asyncio.wait_for(app.state.control.call(app.state.publish_status), 1)
                output = PcmOutput()
                output.attach(0)
                for _ in range(10):
                    output.publish(b'\x00\x00\x01\x00' * 128)
                    assert output.read(0) == b'\x00\x00\x01\x00' * 128
                tasks[0].cancel()
                await asyncio.sleep(0)
                assert len(auth.pending) == MAX_PENDING
                closing = asyncio.create_task(auth.close())
                await asyncio.sleep(0)
                assert not closing.done()
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                await auth.close()
            await closing
            assert auth.executor is None and not auth.pending
    asyncio.run(run())
