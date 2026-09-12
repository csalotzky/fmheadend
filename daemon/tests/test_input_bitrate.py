"""Reported input bitrate must never be calculated from normalized output PCM."""
from unittest.mock import Mock
import pytest
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline

@pytest.mark.parametrize('reported,expected', [({'bitrate':192000},192000),
    ({'nominal-bitrate':128000},128000),({'bitrate':191873,'nominal-bitrate':192000},191873),
    ({},None),({'bitrate':0},None)])
def test_http_reported_bitrate_preserves_existing_metadata(reported,expected):
    pipeline=GstPipeline(InputSourceConfig(name='test',type='HTTP',config={'uri':'http://example.org/live'}))
    pipeline._station_name='Station FM'
    tags=Mock()
    text={'title':'Track','artist':'Artist','audio-codec':'MP3'}
    tags.get_string.side_effect=lambda key:(key in text,text.get(key))
    tags.get_uint.side_effect=lambda key:(key in reported,reported.get(key,0))
    pipeline._tags(tags)
    pipeline._update(state='RUNNING',output_caps='audio/x-raw,format=S16LE,rate=48000,channels=2',input_sample_rate=44100,input_channels=1)
    result=pipeline.probe_result()
    assert result.input_bitrate_bps==expected
    assert result.station_name=='Station FM' and result.stream_title=='Artist - Track'
    assert result.codec=='MP3' and result.sample_rate==44100 and result.channels==1
    assert result.output_caps.endswith('channels=2')
    pipeline._update(state='ERROR')
    assert pipeline.probe_result().input_bitrate_bps is None


def test_non_http_does_not_gain_bitrate_result():
    pipeline=GstPipeline(InputSourceConfig(name='ALSA',type='ALSA',config={'device':'hw:1,0'}))
    pipeline._update(state='RUNNING',output_caps='audio/x-raw',bitrate_bps=1536000)
    assert pipeline.probe_result().input_bitrate_bps is None
