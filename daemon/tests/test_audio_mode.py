"""Explicit physical analog selection; no hardware is required."""
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings


def test_api_mode_transition_and_digital_validation(tmp_path):
    with TestClient(create_app(Settings(tmp_path/'config.db'))) as api:
        p='/api/v1'
        assert api.post(p+'/sources',json={'name':'Obsolete','type':'ANALOG','config':{}}).status_code==422
        assert api.post(p+'/sources/test',json={'name':'Obsolete','type':'ANALOG','config':{}}).status_code==422
        source=api.post(p+'/sources',json={'name':'Digital','type':'HTTP','config':{'uri':'http://example.org/live'}}).json()
        assert api.patch(p+'/modulators/0',json={'frequency_khz':98100}).json()['audio_mode']=='DIGITAL'
        assert api.post(p+'/modulators/0/start').status_code==409
        assert api.patch(p+'/modulators/0',json={'input_source_id':source['id']}).status_code==200
        assert api.post(p+'/modulators/0/start').status_code==200
        assert api.post(p+'/modulators/0/stop').status_code==200
        row=api.patch(p+'/modulators/0',json={'audio_mode':'ANALOG'}).json()
        assert row['input_source_id'] is None
        assert api.post(p+'/modulators/0/start').status_code==200
        assert api.get(p+'/status').json()['modulators'][0]['source_state'] is None
        assert api.patch(p+'/modulators/0',json={'input_source_id':source['id']}).status_code == 409
        assert api.post(p+'/modulators/0/stop').status_code==200
        assert api.patch(p+'/modulators/0',json={'input_source_id':source['id']}).json()['input_source_id'] is None
        row=api.patch(p+'/modulators/0',json={'audio_mode':'DIGITAL'}).json()
        assert row['input_source_id'] is None
        assert api.post(p+'/modulators/0/start').status_code==409
