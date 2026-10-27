"""Endpoint placement, ordinary-Python cache and native gain isolation."""
import json
import math
import socket
import statistics
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fmheadendd.errors import DependencyUnavailable
from fmheadendd.models.audio_profile_file import AudioProfileFile
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.models.dsp import neutral_stages, STAGES
from fmheadendd.sources.gst_pipeline import GstPipeline, load_gst


def profile(trim=0):
    stages = neutral_stages()
    for stage, (_, choices) in STAGES.items():
        if 'OFF' in choices:
            stages[stage]['selected'] = 'OFF'
    stages['output_trim']['implementations']['TRIM']['gain_db'] = trim
    return AudioProfileFile.model_validate(dict(name='Peak test', schema_version=1, description="Test profile", stages=stages))


def pipeline(builtin=False, gain=0, trim=0, **kwargs):
    source = InputSourceConfig(name='Peak test', type='UDP', input_gain_db=gain,
        use_default_audio_profile=False,
        audio_profile_id='custom/peak-test.json' if builtin else 'default/bypass.json',
        config=dict(host='127.0.0.1', port=kwargs.pop('port', 12345), format='S16LE', sample_rate=48000, channels=2))
    kwargs.setdefault('telemetry_configured', True)
    return GstPipeline(source, profile=profile(trim) if builtin else None, **kwargs)


@pytest.mark.parametrize('builtin', [False, True])
@pytest.mark.parametrize('configured', [False, True])
def test_endpoint_placement_before_source_gain_after_trim(builtin, configured):
    p = pipeline(builtin, gain=-6, trim=-3, telemetry_configured=configured)
    p.Gst = SimpleNamespace(Pipeline=SimpleNamespace(new=lambda unused: Mock()),
                            Caps=SimpleNamespace(from_string=lambda value: value))
    made = []
    def make(factory, **props):
        element = Mock()
        element.factory, element.props = factory, {k.replace('_', '-'): v for k, v in props.items()}
        element.get_property.side_effect = element.props.__getitem__
        made.append(element)
        return element
    p._make, p._link = make, Mock()
    p._build()
    chain = p._link.call_args_list[0].args
    source_gain = next(e for e in made if e.factory == 'volume')
    assert source_gain.props['volume'] == pytest.approx(10**(-6/20))
    assert sum(e.factory == 'level' for e in made) == (2 if configured else 0)
    if not configured:
        assert p._peak_elements == {}
        assert chain[chain.index(source_gain)-1].factory == 'capsfilter'
        assert [e.factory for e in chain[-3:]] == ['capsfilter', 'queue', 'appsink']
        if builtin:
            assert chain[chain.index(p._dsp_elements['output_trim'])+1].factory == 'audioconvert'
        p.request_telemetry(True)
        p._telemetry_tick()
        p._peak_message(Mock())
        assert not p.telemetry_snapshot()['telemetry_available']
        p._invalidate_telemetry(discard=True)
        made.clear()
        p._build()  # Recovery/rebuild must not add meters.
        assert all(e.factory != 'level' for e in made)
        return
    raw, final = p._peak_elements['input'], p._peak_elements['output']
    assert chain.index(raw) + 1 == chain.index(source_gain)
    assert chain[chain.index(raw)-1].factory == 'capsfilter'
    assert chain[-2] is final and chain[-1].factory == 'appsink'
    assert chain[-3].factory == 'queue'
    if builtin:
        assert chain.index(raw) < chain.index(p._dsp_elements['input_gain'])
        assert chain.index(p._dsp_elements['output_trim']) < chain.index(final)
        assert chain[chain.index(p._dsp_elements['output_trim'])+1].factory == 'audioconvert'
    assert source_gain.props['volume'] == pytest.approx(10**(-6/20))
    assert raw.props['post-messages'] is final.props['post-messages'] is False
    assert raw.props['interval'] == final.props['interval'] == 20_000_000


@pytest.mark.gstreamer
@pytest.mark.parametrize('builtin', [False, True])
@pytest.mark.parametrize('configured', [False, True])
def test_native_level_topology(builtin, configured):
    try:
        Gst = load_gst()
    except DependencyUnavailable as exc:
        pytest.skip(str(exc))
    p = pipeline(builtin, telemetry_configured=configured)
    p.Gst = Gst
    try:
        p._build()
        iterator = p._pipeline.iterate_recurse()
        levels = []
        while True:
            result, element = iterator.next()
            if result == Gst.IteratorResult.DONE:
                break
            assert result == Gst.IteratorResult.OK
            factory = element.get_factory()
            if factory and factory.get_name() == 'level':
                levels.append(element)
        assert len(levels) == (2 if configured else 0)
        assert set(levels) == set(p._peak_elements.values())
        assert p.sink.get_property('caps').to_string().startswith('audio/x-raw')
        p.request_telemetry(True)
        p._telemetry_tick()
        assert all(e.get_property('post-messages') for e in levels)
        p.request_telemetry(False)
        p._telemetry_tick()
        assert all(not e.get_property('post-messages') for e in levels)
    finally:
        if p._pipeline is not None:
            p._pipeline.set_state(Gst.State.NULL)
        p._invalidate_telemetry(discard=True)


def test_peak_messages_cache_disable_fencing_and_expiry(monkeypatch):
    p = pipeline()
    now = [1_000_000_000]
    monkeypatch.setattr('fmheadendd.sources.gst_pipeline.time.monotonic_ns', lambda: now[0])
    p.Gst = SimpleNamespace(util_seqnum_next=lambda: 100, util_seqnum_compare=lambda a,b: a-b)
    p._pipeline = Mock()
    raw, final = Mock(), Mock()
    p._peak_elements = dict(input=raw, output=final)
    def message(element, peaks, seq=101):
        structure = SimpleNamespace(get_name=lambda: 'level', get_value=lambda name: peaks)
        return SimpleNamespace(src=element, get_seqnum=lambda: seq, get_structure=lambda: structure)
    p._telemetry_tick()
    assert not p.telemetry_snapshot()['telemetry_available']
    assert not any(c.args == ('post-messages', True) for c in raw.set_property.call_args_list)
    p.request_telemetry(True, 100)
    p._telemetry_tick()
    raw.set_property.assert_any_call('interval', 10_000_000)
    assert p.telemetry_snapshot()['input'] is None
    p._peak_message(message(raw, [-8.42, -10.07]))
    p._peak_message(message(final, [-14.42, -16.07]))
    now[0] += 10_000_000
    p._telemetry_tick()
    sample = p.telemetry_snapshot()
    assert sample['input'] == dict(peak_l_dbfs=-8.42, peak_r_dbfs=-10.07)
    assert sample['output'] == dict(peak_l_dbfs=-14.42, peak_r_dbfs=-16.07)
    assert sample['stages'] == {}  # Bypass still exposes endpoint telemetry.
    p._peak_message(message(raw, [float('-inf'), float('nan')]))
    now[0] += 10_000_000
    p._telemetry_tick()
    assert p.telemetry_snapshot()['input'] == dict(peak_l_dbfs=None, peak_r_dbfs=None)
    json.dumps(p.telemetry_snapshot(), allow_nan=False)
    now[0] += 400_000_000
    p._telemetry_tick()
    assert p.telemetry_snapshot()['output'] is None
    p.request_telemetry(False)
    p._telemetry_tick()
    raw.set_property.assert_called_with('post-messages', False)
    p._peak_message(message(raw, [-1, -1]))
    assert p._peak_values == {}
    p.request_telemetry(True)
    p._telemetry_tick()
    p._peak_message(message(raw, [-1, -1], seq=99))
    assert p._peak_values == {}  # Old queued message after reconnecting client.
    p._peak_message(message(raw, [-8, -9]))
    p._invalidate_telemetry()
    assert not p.telemetry_snapshot()['telemetry_available']
    assert p._peak_values == {}
    p._peak_message(message(raw, [-1, -1]))
    assert p._peak_values == {}
    p._invalidate_telemetry(discard=True)
    p._peak_elements = dict(input=Mock(), output=Mock())
    p._telemetry_tick()
    p._peak_message(message(raw, [-1, -1]))
    assert p._peak_values == {}  # Old native generation cannot contaminate new one.


@pytest.mark.gstreamer
def test_native_source_gain_trim_and_bypass_endpoints():
    try:
        Gst = load_gst()
    except DependencyUnavailable as exc:
        pytest.skip(str(exc))
    for factory in ('audiotestsrc', 'udpsink', 'level'):
        if not Gst.ElementFactory.find(factory):
            pytest.skip('Missing GStreamer factory: ' + factory)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    sender = Gst.parse_launch('audiotestsrc is-live=true volume=0.125 ! '
        f'audio/x-raw,format=S16LE,rate=48000,channels=2 ! udpsink host=127.0.0.1 port={port} sync=true')
    measurements = []
    sender.set_state(Gst.State.PLAYING)
    try:
        for builtin, gain, trim in ((True, 0, 0), (True, -6, 0), (True, 0, -3), (False, -6, 0)):
            p = pipeline(builtin, gain, trim, port=port)
            p.request_telemetry(True)
            try:
                p.start()
                samples, seen = [], set()
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline and len(samples) < 12:
                    sample = p.telemetry_snapshot()
                    if sample.get('input') and sample.get('output') and sample['sample_monotonic_ns'] not in seen:
                        if all(math.isfinite(v) for side in ('input', 'output') for v in sample[side].values()):
                            samples.append(sample)
                            seen.add(sample['sample_monotonic_ns'])
                    time.sleep(.01)
                assert len(samples) == 12, p.snapshot()
                measurements.append({side: statistics.median(s[side]['peak_l_dbfs'] for s in samples[2:])
                                     for side in ('input', 'output')})
                assert all(abs(s[side]['peak_l_dbfs']-s[side]['peak_r_dbfs']) < .05
                           for s in samples for side in ('input', 'output'))
                print('NATIVE_PEAK', json.dumps(dict(builtin=builtin, gain_db=gain, trim_db=trim, sample=samples[-1])))
            finally:
                p.stop()
                assert not p.telemetry_snapshot()['telemetry_available']
                assert p._peak_values == {} and p._peak_elements == {}
        baseline, source_gain, output_trim, bypass = measurements
        for reading in measurements:
            assert reading['input'] == pytest.approx(20*math.log10(.125), abs=.05)
        assert source_gain['output'] - baseline['output'] == pytest.approx(-6, abs=.05)
        assert output_trim['output'] - baseline['output'] == pytest.approx(-3, abs=.05)
        assert bypass['output'] - bypass['input'] == pytest.approx(-6, abs=.05)
    finally:
        sender.set_state(Gst.State.NULL)
