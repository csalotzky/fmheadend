"""Console-only limiter peak hold; no daemon or native plugin required."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def monitor():
    spec = importlib.util.spec_from_file_location(
        'audio_monitor', Path(__file__).resolve().parents[2] / 'scripts/audio_monitor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source(reduction, source_id=3, generation=1):
    return dict(source_id=source_id, generation=generation, telemetry_available=True,
                stages={'limiter': dict(implementation='FM_HEADEND_LIMITER', values={
                    'detector-peak-dbfs': -5., 'gain-reduction-db': reduction})})


def test_rolling_maximum_exact_expiry_and_unavailable(monitor):
    hold = monitor.LimiterPeakHold()
    assert hold.update(source(7.), 10.) == 7.
    assert hold.update(source(2.), 10.5) == 7.
    assert hold.update(source(1.), 11.) == 7.
    assert hold.update(source(1.), 11.000001) == 2.
    assert hold.update(source(0.), 11.500001) == 1.
    assert hold.update(source(None), 11.6) is None
    assert hold.update(source(float('nan')), 11.7) is None
    assert hold.update(source(float('inf')), 11.8) is None
    assert hold.update(source(0.), 13.) == 0.


def test_identity_unavailable_reset_and_instantaneous_display(monitor):
    hold = monitor.LimiterPeakHold()
    hold.update(source(9.), 10.)
    assert hold.update(source(2., source_id=4), 10.1) == 2.
    assert hold.update(source(1., source_id=4, generation=2), 10.2) == 1.
    assert hold.update(None, 10.3) is None
    assert hold.update(source(.5, source_id=4, generation=2), 10.4) == .5
    assert hold.update(dict(source(20.), telemetry_available=False), 10.5) is None
    assert hold.update(source(.1), 10.6) == .1
    lines = monitor.render_limiter(source(2.), 9.)
    assert lines[2] == f"reduction {monitor.fmt_db(2., 'dB  ')} {monitor.level_bar(2., 0., 24., monitor.AGC_BAR_WIDTH)}"
    assert lines[3] == f"max 1s    {monitor.fmt_db(9.)}"
    assert 'n/a' in monitor.render_limiter(source(None))[-1]


def test_collects_between_redraws_and_resets_on_reconnect(monitor, monkeypatch):
    connections = iter([[source(1.), source(20.), source(2.)], [source(1.)]])
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def connect(self, path):
            try:
                self.records = next(connections)
            except StopIteration:
                raise KeyboardInterrupt
        def makefile(self, *args, **kwargs):
            return io.StringIO(''.join(json.dumps({'sources': [s]}) + '\n' for s in self.records))
    clock = iter([10., 10.02, 10.2, 10.4])
    frames = []
    monkeypatch.setattr(monitor, 'socket', SimpleNamespace(
        AF_UNIX=1, SOCK_STREAM=1, socket=lambda *args: Socket()))
    monkeypatch.setattr(monitor.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(monitor.time, 'sleep', lambda unused: None)
    monkeypatch.setattr(monitor, 'draw', frames.append)
    with pytest.raises(KeyboardInterrupt):
        monitor.run(SimpleNamespace(refresh_hz=10, socket='unused', tx=None, source=3))
    rendered = [frame for frame in frames if 'max 1s' in frame]
    assert len(rendered) == 3
    assert f"max 1s    {monitor.fmt_db(20.)}" in rendered[1]
    assert f"max 1s    {monitor.fmt_db(1.)}" in rendered[2]
