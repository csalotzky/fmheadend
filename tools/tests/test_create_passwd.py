"""Credential prompts use the terminal; password files retain bcrypt and safety."""
import io
import os
import stat
import warnings

import bcrypt
import pytest

from tools import create_passwd


class Terminal(io.StringIO):
    def isatty(self):
        return True
    def close(self):
        pass


@pytest.fixture
def terminal(monkeypatch):
    reader, writer = Terminal('\n'), Terminal()
    opened, prompts = [], []
    answers = iter(['test-password', 'test-password'])
    def open_terminal(path, mode, **kwargs):
        opened.append((path, mode))
        return reader if mode == 'r' else writer
    def hidden(prompt, stream):
        assert stream is writer
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr(create_passwd, 'open', open_terminal, raising=False)
    monkeypatch.setattr(create_passwd.getpass, 'getpass', hidden)
    return reader, writer, opened, prompts


def check_record(path, user='admin', password='test-password'):
    username, hashed = path.read_text().strip().split(':')
    assert username == user
    assert hashed.startswith('$2b$')
    assert bcrypt.checkpw(password.encode(), hashed.encode())


def test_fully_interactive_default_username_and_redirected_stdin(tmp_path, monkeypatch, terminal, capsys):
    _, writer, opened, prompts = terminal
    redirected = io.StringIO('installer input must not be consumed\n')
    monkeypatch.setattr(create_passwd.sys, 'stdin', redirected)
    path = tmp_path / 'htpasswd'
    assert create_passwd.main(['--file', str(path)]) == 0
    check_record(path)
    assert writer.getvalue() == 'Username [admin]: '
    assert prompts == ['Password: ', 'Confirm password: ']
    assert redirected.tell() == 0
    expected = ['CONIN$', 'CONOUT$'] if os.name == 'nt' else ['/dev/tty', '/dev/tty']
    assert [name for name, _ in opened] == expected
    captured = capsys.readouterr()
    assert 'test-password' not in captured.out + captured.err + writer.getvalue()


def test_username_only_prompts_only_for_password(tmp_path, terminal):
    _, writer, _, prompts = terminal
    path = tmp_path / 'htpasswd'
    assert create_passwd.main(['--file', str(path), '--user', 'operator']) == 0
    check_record(path, 'operator')
    assert not writer.getvalue()
    assert prompts == ['Password: ', 'Confirm password: ']


def test_password_only_prompts_only_for_username(tmp_path, terminal):
    reader, _, _, prompts = terminal
    reader.seek(0)
    reader.truncate()
    reader.write('operator\n')
    reader.seek(0)
    path = tmp_path / 'htpasswd'
    assert create_passwd.main(['--file', str(path), '--password', 'explicit']) == 0
    check_record(path, 'operator', 'explicit')
    assert prompts == []


def test_both_cli_values_need_no_terminal_and_keep_secure_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(create_passwd, 'open', lambda *a, **k: pytest.fail('Unexpected prompt'), raising=False)
    chmod = create_passwd.os.chmod
    modes = []
    def record_mode(path, mode):
        modes.append(mode)
        chmod(path, mode)
    monkeypatch.setattr(create_passwd.os, 'chmod', record_mode)
    path = tmp_path / 'htpasswd'
    assert create_passwd.main(['--file', str(path), '--user', 'operator', '--password', 'explicit']) == 0
    check_record(path, 'operator', 'explicit')
    assert modes == [0o600]
    if os.name == 'posix':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_confirmation_mismatch_creates_no_file(tmp_path, monkeypatch, terminal, capsys):
    answers = iter(['private-first', 'private-second'])
    monkeypatch.setattr(create_passwd.getpass, 'getpass', lambda *a, **k: next(answers))
    path = tmp_path / 'htpasswd'
    assert create_passwd.main(['--file', str(path)]) == 1
    output = capsys.readouterr()
    assert 'Passwords do not match' in output.err
    assert 'private-' not in output.out + output.err
    assert not path.exists()


def test_no_controlling_terminal_fails_clearly(tmp_path, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise OSError('no tty')
    monkeypatch.setattr(create_passwd, 'open', unavailable, raising=False)
    path = tmp_path / 'htpasswd'
    assert create_passwd.main(['--file', str(path)]) == 1
    assert 'require a terminal' in capsys.readouterr().err
    assert not path.exists()


def test_echo_fallback_is_rejected_before_input(tmp_path, monkeypatch, terminal, capsys):
    def cannot_hide(*args, **kwargs):
        warnings.warn('Cannot control echo on the terminal.', create_passwd.getpass.GetPassWarning)
        pytest.fail('Echoed fallback must not run')
    monkeypatch.setattr(create_passwd.getpass, 'getpass', cannot_hide)
    path = tmp_path / 'htpasswd'
    assert create_passwd.main(['--file', str(path), '--user', 'admin']) == 1
    assert 'Cannot control echo' in capsys.readouterr().err
    assert not path.exists()


def test_existing_file_protection_precedes_prompting(tmp_path, monkeypatch):
    path = tmp_path / 'htpasswd'
    path.write_text('existing users\n')
    monkeypatch.setattr(create_passwd, 'open', lambda *a, **k: pytest.fail('Unexpected prompt'), raising=False)
    with pytest.raises(SystemExit) as exc:
        create_passwd.main(['--file', str(path)])
    assert exc.value.code == 2
    assert path.read_text() == 'existing users\n'


def test_explicit_overwrite_replaces_existing_users(tmp_path):
    path = tmp_path / 'htpasswd'
    path.write_text('old user\n')
    assert create_passwd.main(['--file', str(path), '--user', 'new', '--password', 'replacement', '--overwrite']) == 0
    check_record(path, 'new', 'replacement')


@pytest.mark.parametrize('user', ['', 'bad:user', 'bad\nuser', 'bad\ruser'])
def test_username_validation_is_preserved(tmp_path, user):
    path = tmp_path / 'htpasswd'
    with pytest.raises(SystemExit):
        create_passwd.main(['--file', str(path), '--user', user, '--password', 'secret'])
    assert not path.exists()


@pytest.mark.parametrize('password', ['', 'é' * 37])
def test_password_length_validation_is_preserved(tmp_path, password):
    path = tmp_path / 'htpasswd'
    try:
        result = create_passwd.main(['--file', str(path), '--user', 'admin', '--password', password])
    except SystemExit as exc:
        result = exc.code
    assert result != 0
    assert not path.exists()
