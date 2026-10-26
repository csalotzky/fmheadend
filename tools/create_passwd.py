#!/usr/bin/env python3
"""Create an FMheadend bcrypt htpasswd file without Apache."""

import argparse
from contextlib import ExitStack
import getpass
import os
import sys
from pathlib import Path
import warnings

import bcrypt

DEFAULT_FILE = Path("/var/lib/fmheadend/htpasswd")


def _username_error(user):
    if not user or any(ord(ch) < 32 or ord(ch) > 126 or ch == ':' for ch in user):
        return 'Use a nonempty ASCII username without colons or control characters.'


def _password_error(password):
    if not password:
        return 'Password cannot be empty.'
    if len(password.encode('utf-8')) > 72:
        return "Password exceeds bcrypt's 72-byte limit (UTF-8 bytes)."


def _credentials(user, password):
    if user is not None and password is not None:
        return user, password
    with ExitStack() as stack:
        try:
            reader = stack.enter_context(open('CONIN$' if os.name == 'nt' else '/dev/tty', 'r', encoding='utf-8'))
            writer = stack.enter_context(open('CONOUT$' if os.name == 'nt' else '/dev/tty', 'w', encoding='utf-8'))
            if not reader.isatty() or not writer.isatty():
                raise OSError('No controlling terminal')
        except OSError as exc:
            raise ValueError('Interactive credentials require a terminal; supply --user and --password for noninteractive use') from exc
        if user is None:
            while True:
                writer.write('Username (press Enter for default: admin): ')
                writer.flush()
                entered = reader.readline()
                if not entered:
                    raise EOFError
                user = entered.rstrip('\r\n') or 'admin'
                error = _username_error(user)
                if not error:
                    break
                writer.write(error + ' Please try again.\n\n')
        if password is None:
            # getpass normally falls back to echoed input if terminal control
            # fails. Treat that warning as an error before fallback can read.
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                while True:
                    password = getpass.getpass('Password: ', stream=writer)
                    error = _password_error(password)
                    if not error:
                        confirmation = getpass.getpass('Confirm password: ', stream=writer)
                        if password != confirmation:
                            error = 'Passwords do not match.'
                    if not error:
                        break
                    writer.write(error + ' Please try again.\n\n')
                    writer.flush()
    return user, password


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create an FMheadend bcrypt htpasswd file.")
    parser.add_argument("-f", "--file", type=Path, default=DEFAULT_FILE,
                        help=f"Output path (default: {DEFAULT_FILE})")
    parser.add_argument("-u", "--user",
                        help="Username (prompt when omitted; default: admin)")
    parser.add_argument("-p", "--password",
                        help="Password to hash (hidden prompt and confirmation when omitted)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace existing file (deletes its existing users)")
    parser.add_argument("--quiet", action="store_true",
                        help="Omit the success message (prompts and errors remain visible)")
    args = parser.parse_args(argv)

    if args.user is not None and _username_error(args.user):
        parser.error('Invalid username. ' + _username_error(args.user))
    # Explicit invalid arguments fail before opening a terminal, even if the
    # other value was omitted. Only interactive input is retried.
    if args.password is not None and _password_error(args.password):
        parser.error(_password_error(args.password))

    output = args.file.expanduser()
    if os.path.lexists(output) and not args.overwrite:
        parser.error(f"{output} already exists; use --overwrite to replace it")

    try:
        args.user, args.password = _credentials(args.user, args.password)
    except (ValueError, OSError, EOFError, KeyboardInterrupt, getpass.GetPassWarning) as exc:
        print(str(exc) or 'Credential entry cancelled.', file=sys.stderr)
        return 1
    raw = args.password.encode("utf-8")

    record = f"{args.user}:{bcrypt.hashpw(raw, bcrypt.gensalt()).decode('ascii')}\n"

    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if args.overwrite else os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(output, flags, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(record)
        os.chmod(output, 0o600)
    except OSError as exc:
        print(f"Cannot write {output}: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"Created {output} for user {args.user} (mode 0600).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
