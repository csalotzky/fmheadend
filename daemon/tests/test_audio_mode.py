"""Explicit physical analog selection; no hardware is required."""
import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import InputSourceConfig


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


def legacy(path, conflict=False):
    db=Database(path)
    digital=db.save('sources',InputSourceConfig(name='Keep',type='ALSA',config={'device':'hw:1,0'}))
    with db.connection:
        analog={'name':'Legacy shared analog','type':'ANALOG','enabled':False,'config':{},'audio_profile_id':None,'gst_options':{},'timestamp':'2025-01-01T00:00:00Z'}
        sid=db.connection.execute('INSERT INTO sources(data) VALUES (?)',(json.dumps(analog),)).lastrowid
        expected={}
        for channel in range(8):
            data=json.loads(db.connection.execute('SELECT data FROM modulators WHERE id=?',(channel,)).fetchone()[0])
            data.pop('audio_mode');data['input_source_id']=sid if channel<2 else digital.id
            data['frequency_khz']=98100+100*channel;data['enabled']=channel==0;data['rds_enabled']=True
            expected[channel]=data.copy()
            db.connection.execute('UPDATE modulators SET input_source_id=?,data=? WHERE id=?',(digital.id if conflict and channel==1 else data['input_source_id'],json.dumps(data),channel))
        db.connection.execute('PRAGMA user_version=4')
    db.close()
    return digital,expected


def test_legacy_analog_migration_preserves_configuration_and_is_idempotent(tmp_path):
    path=tmp_path/'legacy.db';digital,expected=legacy(path)
    for _ in range(2):
        db=Database(path)
        assert db.connection.execute('PRAGMA user_version').fetchone()[0]==9
        assert db.list('sources')==[digital]
        for channel,data in expected.items():
            want=data | {'audio_mode':'ANALOG' if channel<2 else 'DIGITAL'}
            if channel<2:want['input_source_id']=None
            stored=db.connection.execute('SELECT input_source_id,data FROM modulators WHERE id=?',(channel,)).fetchone()
            assert json.loads(stored['data'])==want
            assert stored['input_source_id']==want['input_source_id']
        db.close()


def test_conflicting_legacy_references_abort_without_partial_migration(tmp_path):
    path=tmp_path/'conflict.db';legacy(path,True)
    with pytest.raises(RuntimeError,match='conflicting legacy source references'):Database(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==4
        assert len(db.execute('SELECT * FROM sources').fetchall())==2
        assert 'audio_mode' not in json.loads(db.execute('SELECT data FROM modulators WHERE id=0').fetchone()[0])


def test_complete_m7b1_chain_preserves_unrelated_records(tmp_path):
    path=tmp_path/'complete-chain.db'
    digital,expected=legacy(path)
    # Add a pre-M7B1 HTTP record and an unreferenced analog record without invoking migration.
    with sqlite3.connect(path) as db:
        profile={'name':'Keep profile','processing_enabled':True,'gain_db':-3,'eq_config':None,
            'compressor_config':None,'limiter_config':None,'processor':'GSTREAMER','advanced_options':None,
            'timestamp':'2025-01-02T00:00:00Z'}
        pid=db.execute('INSERT INTO audio_profiles(data) VALUES (?)',(json.dumps(profile),)).lastrowid
        http={'name':'Keep HTTP','type':'HTTP','enabled':True,'config':{'uri':'http://example.org/live',
            'buffer_ms':4321,'connect_timeout_ms':9000,'user_agent':'Old'},'audio_profile_id':pid,
            'gst_options':{'source':{'user-agent':'Effective','timeout':2},'resample':{'quality':5}},
            'timestamp':'2025-01-03T00:00:00Z'}
        hid=db.execute('INSERT INTO sources(audio_profile_id,data) VALUES (?,?)',(pid,json.dumps(http))).lastrowid
        unused={'name':'Unused analog','type':'ANALOG','config':{},'audio_profile_id':pid,'timestamp':'2025-01-03T00:00:00Z'}
        db.execute('INSERT INTO sources(audio_profile_id,data) VALUES (?,?)',(pid,json.dumps(unused)))
        system=db.execute('SELECT data FROM system_config').fetchone()[0]
    expected_http=json.loads(json.dumps(http));expected_http['config'].pop('user_agent');expected_http.pop('enabled')
    for _ in range(2):
        db=Database(path)
        assert db.connection.execute('PRAGMA user_version').fetchone()[0]==9
        assert [row.id for row in db.list('sources')]==[digital.id,hid]
        assert db.get('sources',digital.id)==digital
        assert json.loads(db.connection.execute('SELECT data FROM sources WHERE id=?',(hid,)).fetchone()[0])==expected_http
        assert json.loads(db.connection.execute('SELECT data FROM audio_profiles WHERE id=?',(pid,)).fetchone()[0])==profile
        assert db.connection.execute('SELECT data FROM system_config').fetchone()[0]==system
        for channel,data in expected.items():
            want=data | {'audio_mode':'ANALOG' if channel<2 else 'DIGITAL'}
            if channel<2:want['input_source_id']=None
            assert json.loads(db.connection.execute('SELECT data FROM modulators WHERE id=?',(channel,)).fetchone()[0])==want
        db.close()
