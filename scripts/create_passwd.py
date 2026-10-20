#!/usr/bin/env python3
"""Create an FMheadend bcrypt htpasswd file without Apache."""

import argparse
import os
import sys
from pathlib import Path

import bcrypt

DEFAULT_FILE = Path("/var/lib/fmheadend/htpasswd")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an FMheadend bcrypt htpasswd file.")
    parser.add_argument("-f", "--file", type=Path, default=DEFAULT_FILE,
                        help=f"Output path (default: {DEFAULT_FILE})")
    parser.add_argument("-u", "--user", default="admin",
                        help="Username (default: admin)")
    parser.add_argument("-p", "--password", required=True,
                        help="Password to hash (required; no interactive prompt)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace existing file (deletes its existing users)")
    args = parser.parse_args()

    if not args.user or any(ch in args.user for ch in (":", "\n", "\r")):
        parser.error("Invalid username")

    output = args.file.expanduser()
    if output.exists() and not args.overwrite:
        parser.error(f"{output} already exists; use --overwrite to replace it")

    if not args.password:
        parser.error("Password must not be empty")

    raw = args.password.encode("utf-8")
    if len(raw) > 72:
        print("Password exceeds bcrypt's 72-byte limit.", file=sys.stderr)
        return 1

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

    print(f"Created {output} for user {args.user} (mode 0600).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
