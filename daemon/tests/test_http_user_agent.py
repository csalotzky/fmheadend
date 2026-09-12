"""One authoritative HTTP User-Agent property, with legacy persistence coverage."""
import json
import sqlite3
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig
from fmheadendd.sources.gst_pipeline import GstPipeline

@pytest.mark.parametrize('override,expected', [(None,'fmheadend/0.1.0'),('Station/2','Station/2')])
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

@pytest.mark.parametrize('advanced', [None,'Advanced/3'])
def test_migration_preserves_effective_value_and_timestamp(tmp_path,advanced):
    path=tmp_path/'legacy.db';db=Database(path)
    source=db.save('sources',InputSourceConfig(name='HTTP',type='HTTP',config={'uri':'http://example.org'}))
    data=source.model_dump(mode='json',exclude={'id'});data['config']['user_agent']='Legacy/2'
    if advanced:data['gst_options']={'source':{'user-agent':advanced,'timeout':2}}
    with db.connection:
        db.connection.execute('UPDATE sources SET data=? WHERE id=?',(json.dumps(data),source.id))
        db.connection.execute('PRAGMA user_version=5')
    db.close()
    for _ in range(2):
        db=Database(path);saved=db.get('sources',source.id)
        assert 'user_agent' not in saved.config
        assert saved.gst_options['source']['user-agent']==(advanced or 'Legacy/2')
        assert saved.timestamp==source.timestamp
        if advanced:assert saved.gst_options['source']['timeout']==2
        assert db.connection.execute('PRAGMA user_version').fetchone()[0]==7
        db.close()

def test_migration_rolls_back_on_invalid_record(tmp_path):
    path=tmp_path/'bad.db';db=Database(path)
    with db.connection:
        db.connection.execute("INSERT INTO sources(data) VALUES (?)",(json.dumps({'type':'HTTP','config':{'user_agent':'Keep'},'gst_options':{}}),))
        db.connection.execute("INSERT INTO sources(data) VALUES ('invalid json')")
        db.connection.execute('PRAGMA user_version=5')
    db.close()
    with pytest.raises(json.JSONDecodeError):Database(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==5
        assert json.loads(db.execute('SELECT data FROM sources WHERE id=1').fetchone()[0])['config']['user_agent']=='Keep'

def test_api_rejects_removed_field_and_accepts_authoritative_property(tmp_path):
    with TestClient(create_app(Settings(tmp_path/'api.db'))) as api:
        body={'name':'HTTP','type':'HTTP','config':{'uri':'http://example.org','user_agent':'Old'}}
        assert api.post('/api/v1/sources',json=body).status_code==422
        body['config'].pop('user_agent');body['gst_options']={'source':{'user-agent':'Station/2'}}
        response=api.post('/api/v1/sources',json=body)
        assert response.status_code==201
        assert 'user_agent' not in response.json()['config']
        assert response.json()['gst_options']==body['gst_options']
