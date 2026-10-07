"""Sampled clipper peaks: monotonic rolling window and console presentation."""
import importlib.util
import json
from pathlib import Path

import pytest

from fmheadendd.sources.dsp_telemetry import StageMeters


class Clipper:
    def __init__(self):
        self.values = {'detector-peak-dbfs': -5.0, 'clip-count': 12345,
                       'max-clipping-db': 5.0, 'buffer-max-clipping-db': 1.37}

    def get_property(self, name):
        return self.values[name]


def test_rolling_clipper_maximum_and_expiry(monkeypatch):
    now = [0]
    monkeypatch.setattr('fmheadendd.sources.dsp_telemetry.time.monotonic_ns', lambda: now[0])
    element = Clipper()
    meter = StageMeters(element, 'FM_HEADEND_CLIPPER', {})
    assert meter.collect()['values']['buffer-max-clipping-db'] == 1.37
    element.values['buffer-max-clipping-db'] = .42
    now[0] = 5_000_000_000
    assert meter.collect()['values']['max-clipping-db-10s'] == 1.37
    now[0] = 10_000_000_000
    assert meter.collect()['values']['max-clipping-db-10s'] == 1.37
    now[0] += 1  # Only samples OLDER than ten seconds expire.
    values = meter.collect()['values']
    assert values['max-clipping-db-10s'] == .42
    assert values['clip-count'] == 12345 and values['max-clipping-db'] == 5.0
    now[0] = 30_000_000_000  # A telemetry pause cannot preserve expired peaks.
    element.values['buffer-max-clipping-db'] = 0.0
    assert meter.collect()['values']['max-clipping-db-10s'] == 0.0


def test_replacement_has_no_previous_clipper_window():
    element = Clipper()
    old = StageMeters(element, 'FM_HEADEND_CLIPPER', {})
    assert old.collect()['values']['max-clipping-db-10s'] == 1.37
    element.values['buffer-max-clipping-db'] = .1
    replacement = StageMeters(element, 'FM_HEADEND_CLIPPER', {})
    assert replacement.collect()['values']['max-clipping-db-10s'] == .1
    assert old.collect()['values']['max-clipping-db-10s'] == 1.37


@pytest.mark.parametrize('unavailable', ['missing', float('nan')])
def test_older_or_unavailable_clipper_property(unavailable):
    element = Clipper()
    if unavailable == 'missing':
        del element.values['buffer-max-clipping-db']
    else:
        element.values['buffer-max-clipping-db'] = unavailable
    sample = StageMeters(element, 'FM_HEADEND_CLIPPER', {}).collect()
    assert sample['values']['clip-count'] == 12345
    assert sample['values']['max-clipping-db'] == 5.0
    assert 'max-clipping-db-10s' not in sample['values']
    assert 'buffer-max-clipping-db' in sample['errors']
    json.dumps(sample, allow_nan=False)


def test_console_clipper_window_and_legacy_display():
    spec = importlib.util.spec_from_file_location(
        'audio_monitor', Path(__file__).resolve().parents[2] / 'scripts/audio_monitor.py')
    monitor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitor)
    stage = StageMeters(Clipper(), 'FM_HEADEND_CLIPPER', {}).collect()
    source = {'stages': {'clipper': stage}}
    text = '\n'.join(monitor.render_clipper(source))
    for expected in ('FM CLIPPER', '-5.00 dBFS', '12345', 'max 10s   1.37 dB', 'max ever  5.00 dB'):
        assert expected in text
    del stage['values']['max-clipping-db-10s']
    text = '\n'.join(monitor.render_clipper(source))
    assert 'max 10s   n/a' in text
    assert 'max ever  5.00 dB' in text
