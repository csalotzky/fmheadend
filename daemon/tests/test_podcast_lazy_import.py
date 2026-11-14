"""Worker imports must not initialize the RSS parser in ordinary processes."""
import subprocess
import sys


def test_audiod_import_does_not_load_feedparser():
    result = subprocess.run([sys.executable, '-c',
        "import sys; from fmheadendd.sources import audiod, gst_pipeline, podcast_worker; "
        "assert not any(n == 'feedparser' or n.startswith('feedparser.') for n in sys.modules)"],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
