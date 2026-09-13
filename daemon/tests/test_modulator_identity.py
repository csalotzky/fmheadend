"""Focused contract and migration coverage for physical-only modulator identity."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database
from fmheadendd.models.configuration import AudioProfileConfig, InputSourceConfig, ModulatorConfig


@pytest.mark.parametrize('version', [1, 2, 3])
def test_legacy_names_removed_without_other_configuration_changes(tmp_path, version):
    path = tmp_path / 'legacy.db'
    db = Database(path)
    profile = db.save('audio_profiles', AudioProfileConfig(name='Audio profile'))
    source = db.save('sources', InputSourceConfig(name='Studio source', type='ALSA', config={'device':'hw:1,0'}, audio_profile_id=profile.id))
    expected = {}
    with db.connection:
        for channel in range(8):
            config = ModulatorConfig(input_source_id=source.id, frequency_khz=98100 + channel * 100,
                enabled=channel % 2 == 0, rf_attenuation_db=6.0, rds_ecc=224, rds_af_khz=[101100], rds_rt='Keep text')
            data = config.model_dump(mode='json') | {'timestamp': '2025-01-01T00:00:00+00:00'}
            expected[channel] = data.copy()
            # Include missing and customized legacy names, not just generated defaults.
            if channel != 7:
                data['name'] = f'Legacy channel {channel}'
            db.connection.execute('UPDATE modulators SET input_source_id=?, data=? WHERE id=?', (source.id, json.dumps(data), channel))
        db.connection.execute(f'PRAGMA user_version={version}')
    db.close()
    for _ in range(2):
        db = Database(path)
        assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 9
        rows = db.connection.execute('SELECT * FROM modulators ORDER BY id').fetchall()
        assert [row['id'] for row in rows] == list(range(8))
        for row in rows:
            assert json.loads(row['data']) == expected[row['id']]
            assert row['input_source_id'] == source.id
        assert db.get('sources', source.id).name == 'Studio source'
        assert db.get('audio_profiles', profile.id).name == 'Audio profile'
        assert len(db.list('modulators')) == 8
        db.close()


def test_name_migration_rolls_back_atomically(tmp_path):
    path = tmp_path / 'broken.db'
    db = Database(path)
    with db.connection:
        for channel in range(8):
            data = json.loads(db.connection.execute('SELECT data FROM modulators WHERE id=?', (channel,)).fetchone()[0])
            data['name'] = 'Legacy'
            db.connection.execute('UPDATE modulators SET data=? WHERE id=?', (json.dumps(data), channel))
        db.connection.execute("UPDATE modulators SET data='invalid json' WHERE id=7")
        db.connection.execute('PRAGMA user_version=3')
    db.close()
    with pytest.raises(json.JSONDecodeError):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert json.loads(connection.execute('SELECT data FROM modulators WHERE id=0').fetchone()[0])['name'] == 'Legacy'


def test_modulator_api_has_only_physical_identity(tmp_path):
    path = tmp_path / 'fresh.db'
    with TestClient(create_app(Settings(path))) as api:
        schemas = api.get('/openapi.json').json()['components']['schemas']
        for model in ('Modulator', 'ModulatorPatch'):
            assert 'name' not in schemas[model]['properties']
        rows = api.get('/api/v1/modulators').json()
        assert [row['id'] for row in rows] == list(range(8))
        assert all('name' not in row and row['frequency_khz'] is None for row in rows)
        assert api.patch('/api/v1/modulators/0', json={'name': 'Rejected'}).status_code == 422
        assert api.patch('/api/v1/modulators/0', json={'frequency_khz': 98100}).status_code == 200
        assert 'name' not in api.get('/api/v1/modulators/0').json()
    db = Database(path)
    assert all('name' not in json.loads(row[0]) for row in db.connection.execute('SELECT data FROM modulators'))
    db.close()
