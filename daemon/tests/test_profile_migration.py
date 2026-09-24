import json
import sqlite3

import pytest

from fmheadendd.database import Database
from fmheadendd.models.configuration import BasicConfig, InputSourceConfig, ModulatorConfig


def legacy(path, profile=None, referenced=True):
    db = Database(path)
    source = db.save('sources', InputSourceConfig(name='Keep me', type='HTTP', config={'uri': 'http://localhost/live'}))
    db.save('modulators', ModulatorConfig(input_source_id=source.id, enabled=True, frequency_khz=98100), 3)
    db.close()
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE audio_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)')
        c.execute('ALTER TABLE sources ADD COLUMN audio_profile_id INTEGER REFERENCES audio_profiles(id)')
        data = json.loads(c.execute('SELECT data FROM sources').fetchone()[0])
        for key in ('processor_mode', 'input_gain_db', 'audio_profile_path'): data.pop(key)
        data['audio_profile_id'] = 1 if profile and referenced else None
        c.execute('UPDATE sources SET data=?,audio_profile_id=?', (json.dumps(data), data['audio_profile_id']))
        if profile: c.execute('INSERT INTO audio_profiles VALUES (1, ?)', (json.dumps(profile),))
        c.execute('PRAGMA user_version=1')
    return source.id


@pytest.mark.parametrize('processor', [None, 'NONE', 'GST_BASIC'])
def test_migrate_preserves_sources_tx_and_gain(tmp_path, processor):
    path = tmp_path / 'legacy.db'
    pid = {'name': 'Known', 'processor': processor, 'gain_db': -3, 'basic_config': BasicConfig().model_dump()} if processor else None
    sid = legacy(path, pid)
    db = Database(path)
    source = db.get('sources', sid)
    assert source.name == 'Keep me' and source.config['uri'] == 'http://localhost/live'
    assert source.processor_mode == ('builtin' if processor == 'GST_BASIC' else 'bypass')
    assert source.input_gain_db == (-3 if processor else 0)
    assert source.audio_profile_path == ('default/basic.json' if processor == 'GST_BASIC' else None)
    assert db.get('modulators', 3).input_source_id == sid and db.get('modulators', 3).enabled
    assert not db.connection.execute("SELECT name FROM sqlite_master WHERE name='audio_profiles'").fetchall()
    assert not db.connection.execute('PRAGMA foreign_key_check').fetchall()
    db.close()
    reopened = Database(path)
    assert reopened.connection.total_changes == 0
    reopened.close()


@pytest.mark.parametrize('profile,referenced', [
    ({'name': 'Custom', 'processor': 'OTHER'}, True),
    ({'name': 'Custom', 'processor': 'GST_BASIC', 'basic_config': {'eq_low_db': 3}}, True),
    ({'name': 'Unused', 'processor': 'NONE'}, False),
    ({'name': 'Advanced', 'processor': 'NONE', 'advanced_options': {'unknown': 1}}, True),
])
def test_migration_conflict_preserves_database(tmp_path, profile, referenced):
    path = tmp_path / 'legacy.db'
    legacy(path, profile, referenced)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='migration blocked; database unchanged'):
        Database(path)
    assert path.read_bytes() == before


def test_unresolved_reference_blocks_migration(tmp_path):
    path = tmp_path / 'legacy.db'
    legacy(path)
    with sqlite3.connect(path) as c:
        data = json.loads(c.execute('SELECT data FROM sources').fetchone()[0])
        data['audio_profile_id'] = 17
        c.execute('UPDATE sources SET data=?,audio_profile_id=17', (json.dumps(data),))
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='missing profile 17'):
        Database(path)
    assert path.read_bytes() == before


def test_deleted_source_id_is_not_reused_after_migration(tmp_path):
    path = tmp_path / 'legacy.db'
    sid = legacy(path)
    with sqlite3.connect(path) as c:
        c.execute('UPDATE modulators SET input_source_id=NULL')
        c.execute('DELETE FROM sources')
    db = Database(path)
    created = db.save('sources', InputSourceConfig(name='New', type='HTTP', config={'uri': 'http://localhost/new'}))
    assert created.id > sid
    db.close()
