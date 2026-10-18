"""One authoritative HTTP User-Agent property."""
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd import __version__
from fmheadendd.config import Settings
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline

@pytest.mark.parametrize('override,expected', [(None,f'fmheadend/{__version__}'),('Station/2','Station/2')])
def test_effective_property(override,expected):
    pipeline=object.__new__(GstPipeline)
    pipeline.source=InputSourceConfig(name='HTTP',type='HTTP',config={'uri':'http://example.org'},
        gst_options={} if override is None else {'source':{'user-agent':override}})
    pipeline.timeout_s=10;pipeline._callback_error=None
    element=Mock();element.find_property.return_value=True
    pipeline._source_setup(None,element)
    writes=[call.args[1] for call in element.set_property.call_args_list if call.args[0]=='user-agent']
    assert writes[-1]==expected
    assert pipeline._callback_error is None

def test_api_rejects_removed_field_and_accepts_authoritative_property(tmp_path):
    with TestClient(create_app(Settings(tmp_path/'api.db'))) as api:
        body={'name':'HTTP','type':'HTTP','config':{'uri':'http://example.org','user_agent':'Old'}}
        assert api.post('/api/v1/sources',json=body).status_code==422
        body['config'].pop('user_agent');body['gst_options']={'source':{'user-agent':'Station/2'}}
        response=api.post('/api/v1/sources',json=body)
        assert response.status_code==201
        assert 'user_agent' not in response.json()['config']
        assert response.json()['gst_options']==body['gst_options']
