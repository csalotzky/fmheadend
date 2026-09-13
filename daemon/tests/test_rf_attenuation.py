"""Relative RF output contract and schema-8 migration, no hardware required."""
import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.rp2354 import protocol as p


def test_schema8_upgrade_does_not_guess_absolute_conversion(tmp_path):
    path=tmp_path/'legacy.db';db=Database(path)
    expected={}
    with db.connection:
        for row in db.connection.execute('SELECT id,data FROM modulators').fetchall():
            data=json.loads(row['data']);data.pop('rf_attenuation_db')
            data.update(frequency_khz=98100,enabled=row['id']==0,rds_ecc=224,rds_rt='Keep')
            expected[row['id']]=dict(data,rf_attenuation_db=None)
            data['rf_output_dbuv']=96.0 if row['id']%2 else None
            db.connection.execute('UPDATE modulators SET data=? WHERE id=?',(json.dumps(data),row['id']))
        others={t:db.connection.execute('SELECT * FROM '+t).fetchall() for t in ('sources','audio_profiles','system_config')}
        others={t:[tuple(r) for r in rows] for t,rows in others.items()}
        db.connection.execute('PRAGMA user_version=8')
    db.close()
    for _ in range(2):
        db=Database(path)
        assert db.connection.execute('PRAGMA user_version').fetchone()[0]==9
        for row in db.connection.execute('SELECT id,data FROM modulators'):
            assert json.loads(row['data'])==expected[row['id']]
        for t,rows in others.items():assert [tuple(r) for r in db.connection.execute('SELECT * FROM '+t)]==rows
        db.close()


def test_rf_migration_rollback(tmp_path):
    path=tmp_path/'bad.db';db=Database(path)
    with db.connection:
        db.connection.execute("UPDATE modulators SET data='invalid' WHERE id=7")
        db.connection.execute('PRAGMA user_version=8')
        before=[tuple(r) for r in db.connection.execute('SELECT * FROM modulators')]
    db.close()
    with pytest.raises(json.JSONDecodeError):Database(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==8
        assert db.execute('SELECT * FROM modulators').fetchall()==before


def test_relative_rf_api_round_trip(tmp_path):
    with TestClient(create_app(Settings(tmp_path/'api.db'))) as api:
        url='/api/v1/modulators/0'
        assert api.get(url).json()['rf_attenuation_db'] is None
        assert api.patch(url,json={'rf_output_dbuv':96}).status_code==422
        for i in range(16):
            response=api.patch(url,json={'rf_attenuation_db':i*1.5})
            assert response.status_code==200
            assert response.json()['rf_attenuation_db']==i*1.5
        for bad in [-1,1,23,24]:assert api.patch(url,json={'rf_attenuation_db':bad}).status_code==422


@pytest.mark.parametrize('value',[1,14,226,65535])
def test_invalid_relative_wire_targets(value):
    body=p.TX_CONFIG.pack(98100,value,1,50,0,0,0,0,0,0)
    with pytest.raises(ValueError):p.request(p.Command.SET_TX_CONFIG,0,body)


@pytest.mark.parametrize('value',[-1,1,226])
def test_invalid_relative_status(value):
    row=p.CHANNEL_STATUS.pack(0,value,0,0,0,0,255)
    with pytest.raises(p.ProtocolError):p.parse_status(p.GLOBAL_STATUS.pack(0,0,0,0)+row*8)
