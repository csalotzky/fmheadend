"""Bounded mock-only runtime capacity and CLI checks."""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

binary = sys.argv[1]
help_text = subprocess.check_output([binary, '--help'], text=True)
assert '--ring-buffer-ms 300' in help_text
for value in ('0', '-1', '1.5', 'bad', '18446744073709551615', '18446744073709551616'):
    result = subprocess.run([binary, '--transport', 'mock', '--ring-buffer-ms', value],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 1, (value, result)
for options, milliseconds in (([], 300), (['--ring-buffer-ms', '1'], 1), (['--ring-buffer-ms', '200'], 200)):
    with tempfile.TemporaryDirectory(prefix='mux-cli-') as directory:
        process = subprocess.Popen([binary, '--transport', 'mock', '--runtime-dir', directory, *options],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not Path(directory, 'mux-control.sock').exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(.01)
        finally:
            process.terminate()
            _, log = process.communicate(timeout=2)
        assert process.returncode == 0, log
        assert f'ring_buffer_ms={milliseconds} ring_buffer_bytes={milliseconds * 192}' in log, log
print('buffer CLI defaults, overrides and invalid values passed')
