"""Application metadata is shared by editable installs, API and package imports."""
from importlib.metadata import version
from pathlib import Path
import tomllib

from fmheadendd import __version__
from fmheadendd.app import create_app


def test_application_version_matches_package_and_authoritative_project():
    project = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    expected = tomllib.loads(project.read_text(encoding='utf-8'))['project']['version']
    assert __version__ == version('fmheadendd') == expected
    assert create_app().version == expected
