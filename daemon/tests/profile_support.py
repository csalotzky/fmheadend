"""Real bundled Audio Profiles with test-local writable storage."""
from pathlib import Path

from fmheadendd.audio_profile_files import AudioProfileFiles
from fmheadendd.config import Settings

BUNDLED_PROFILES = Path(__file__).resolve().parents[2] / 'audio-profiles/default'


def profile_settings(database_path, **kwargs):
    # A new test database uses the application's real basic_filtering default.
    return Settings(database_path, audio_profiles_default_dir=BUNDLED_PROFILES,
                    audio_profiles_custom_dir=Path(database_path).parent / 'custom-profiles',
                    **kwargs)


def profile_store(tmp_path):
    return AudioProfileFiles(BUNDLED_PROFILES, tmp_path / 'custom-profiles')
