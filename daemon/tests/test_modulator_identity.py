"""Focused contract coverage for physical-only modulator identity."""
import json

from fastapi.testclient import TestClient
from fmheadendd.app import create_app
from fmheadendd.config import Settings
from fmheadendd.database import Database


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
