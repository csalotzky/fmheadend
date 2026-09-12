from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline

class Structure:
    def __init__(self, values, name='http-headers'):self.values,self.name=values,name
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def get_name(self):return self.name
    def get_value(self,key):return self.values.get(key)
    def n_fields(self):return len(self.values)
    def nth_field_name(self,index):return list(self.values)[index]

@pytest.mark.parametrize('headers,expected', [({'icy-name':' Station FM '},'Station FM'),({},None),({'title':'Song','organization':'Record label'},None),({'ICY-NAME':'\nStation\t'},'Station')])
def test_transport_identity_only(headers,expected):
    pipeline=GstPipeline(InputSourceConfig(name='test',type='HTTP',config={'uri':'http://station-name.example/live'}))
    pipeline._station_headers(SimpleNamespace(get_structure=lambda:Structure({'response-headers':Structure(headers)})))
    tags=Mock();tags.get_string.side_effect=lambda key:(key in {'title','artist'}, {'title':'Song','artist':'Artist'}.get(key));tags.get_uint.return_value=(False,0)
    pipeline._tags(tags)
    pipeline._update(state='RUNNING',output_caps='audio/x-raw',codec='MP3',input_sample_rate=48000,input_channels=2)
    result=pipeline.probe_result()
    assert result.station_name==expected
    assert result.stream_title=='Artist - Song'
    assert result.codec=='MP3' and result.sample_rate==48000 and result.channels==2
    pipeline._update(state='ERROR')
    assert pipeline.probe_result().station_name is None

def test_non_http_ignores_station_headers():
    pipeline=GstPipeline(InputSourceConfig(name='test',type='ALSA',config={'device':'hw:1,0'}))
    pipeline._station_headers(SimpleNamespace(get_structure=lambda:Structure({'response-headers':Structure({'icy-name':'Station'})})))
    pipeline._update(state='RUNNING',output_caps='audio/x-raw')
    assert pipeline.probe_result().station_name is None
