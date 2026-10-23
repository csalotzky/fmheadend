"""Bounded mock-only runtime capacity and CLI checks."""
import subprocess
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

binary = sys.argv[1]
help_text = subprocess.check_output([binary, '--help'], text=True)
assert '--ring-buffer-ms 300' in help_text
assert '--timing-diagnostics' in help_text
for value in ('0', '-1', '1.5', 'bad', '18446744073709551615', '18446744073709551616'):
    result = subprocess.run([binary, '--transport', 'mock', '--ring-buffer-ms', value],
                            capture_output=True, text=True, timeout=2)
    assert result.returncode == 1, (value, result)
for options, milliseconds in (([], 300), (['--ring-buffer-ms', '1'], 1), (['--ring-buffer-ms', '200'], 200), (['--timing-diagnostics'], 300)):
    with tempfile.TemporaryDirectory(prefix='mux-cli-') as directory:
        process = subprocess.Popen([binary, '--transport', 'mock', '--runtime-dir', directory, *options],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not Path(directory, 'mux-control.sock').exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(.01)
            def command(payload):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(2)
                    peer.connect(str(Path(directory, 'mux-control.sock')))
                    peer.sendall(json.dumps(payload).encode() + b'\n')
                    with peer.makefile('rb') as stream:
                        return json.loads(stream.readline())
            state = command({'command': 'get_status'})
            enabled = '--timing-diagnostics' in options
            assert state['timing_diagnostics'] is enabled
            assert state['send_gap_max_us'] == 0
            assert command({'command': 'set_tx_count', 'count': 1}) == {'ok': True}
            deadline = time.monotonic() + 2
            while True:
                state = command({'command': 'get_status'})
                if state['frames_sent'] >= 3:
                    break
                assert time.monotonic() < deadline
                time.sleep(.01)
            fields = ('late_max_us', 'late_over_100us', 'late_over_500us',
                      'late_over_1000us', 'late_over_3000us', 'send_gap_max_us')
            if enabled:
                assert state['send_gap_max_us'] > 0
                assert state['late_over_100us'] >= state['late_over_500us'] >= state['late_over_1000us'] >= state['late_over_3000us']
            else:
                assert all(state[field] == 0 for field in fields)
        finally:
            process.terminate()
            _, log = process.communicate(timeout=2)
        assert process.returncode == 0, log
        assert f'ring_buffer_ms={milliseconds} ring_buffer_bytes={milliseconds * 192}' in log, log
print('buffer CLI defaults, overrides and invalid values passed')
