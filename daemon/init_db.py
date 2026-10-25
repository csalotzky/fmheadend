"""Initialize a new database before starting FMheadend services."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from .audio_profile_files import AudioProfileFiles
from .config import Settings
from .database import Database
from .errors import DomainError
from .models.configuration import SystemConfig
from .regions import PLANS


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path(Settings.database_path),
                        help=f'New SQLite file (default: {Settings.database_path})')
    parser.add_argument('--region', type=int, choices=sorted(PLANS), default=1,
                        help='Initial preemphasis: 0 Expert, 1 Europe (default), 2 US, 3 Japan')
    parser.add_argument('--default-audio-profile', default=SystemConfig().default_audio_profile_id,
                        help='Initial system default (default: default/basic_filtering.json)')
    parser.add_argument('--audio-profiles-default-dir', type=Path,
                        default=Path(Settings.audio_profiles_default_dir),
                        help=f'Bundled profiles (default: {Settings.audio_profiles_default_dir})')
    parser.add_argument('--audio-profiles-custom-dir', type=Path,
                        default=Path(Settings.audio_profiles_custom_dir),
                        help=f'Custom profiles (default: {Settings.audio_profiles_custom_dir})')
    return parser


def main(argv=None):
    args = argument_parser().parse_args(argv)
    path = args.database.expanduser().absolute()
    try:
        if os.path.lexists(path):
            raise FileExistsError(f'{path} already exists; initialization refused')
        system = SystemConfig(default_audio_profile_id=args.default_audio_profile)
        profiles = AudioProfileFiles(args.audio_profiles_default_dir.expanduser(),
                                     args.audio_profiles_custom_dir.expanduser())
        profiles.get(system.default_audio_profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Build privately on the same filesystem. No final database is visible
        # until both normal initialization and the selected system setting succeed.
        with tempfile.TemporaryDirectory(prefix='.fmheadend-init-', dir=path.parent) as directory:
            staged = Path(directory) / 'database.sqlite'
            with closing(Database(staged, initial_preemphasis_us=75 if args.region == 2 else 50)) as database:
                database.save_system(system)
            with staged.open('rb+') as stream:
                os.fsync(stream.fileno())
            # Unlike rename/replace, link cannot overwrite a concurrent creator.
            os.link(staged, path)
    except (OSError, sqlite3.Error, ValueError, RuntimeError, DomainError) as exc:
        message = exc.message if isinstance(exc, DomainError) else str(exc)
        print(f'Cannot initialize database: {message}', file=sys.stderr)
        return 1
    print(f'Initialized {path}; default Audio Profile: {system.default_audio_profile_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
