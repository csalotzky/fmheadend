"""SQLite configuration and durable podcast play history; no runtime queues."""
import json
import sqlite3
from pathlib import Path

from .errors import DomainError
from .models.configuration import InputSource, Modulator, ModulatorConfig, PsEntry, SystemConfig, utcnow, RdsProfileConfig, RdsProfile, RDS_FIELDS

SCHEMA_VERSION = 1
SCHEMA_STATEMENTS = (
    "CREATE TABLE rds_profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)",
    "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, rds_profile_id INTEGER UNIQUE REFERENCES rds_profiles(id) ON DELETE RESTRICT, data TEXT NOT NULL)",
    "CREATE TABLE modulators (id INTEGER PRIMARY KEY CHECK(id BETWEEN 0 AND 7), input_source_id INTEGER REFERENCES sources(id) ON DELETE RESTRICT, rds_override_profile_id INTEGER UNIQUE REFERENCES rds_profiles(id) ON DELETE RESTRICT, data TEXT NOT NULL)",
    "CREATE TABLE system_config (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL)",
    "CREATE TABLE podcast_history (source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE, feed_id TEXT NOT NULL, episode_id TEXT NOT NULL, play_count INTEGER NOT NULL DEFAULT 0 CHECK(play_count >= 0), last_played_at TEXT, last_seen_at TEXT NOT NULL, PRIMARY KEY(source_id, feed_id, episode_id))",
    "CREATE TRIGGER fixed_modulator_delete BEFORE DELETE ON modulators BEGIN SELECT RAISE(ABORT, 'Modulators are fixed'); END",
    "CREATE TRIGGER fixed_modulator_id BEFORE UPDATE OF id ON modulators BEGIN SELECT RAISE(ABORT, 'Modulator IDs are fixed'); END",
)
RECORDS = {"sources": InputSource, "modulators": Modulator}


class Database:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _initialize(self):
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(f"Unsupported database schema {version}; database left untouched.")
        if version == 0:
            if self.connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise RuntimeError("Unversioned nonempty database; database left untouched.")
            # Explicit transaction includes all DDL, seeds and the version bump.
            self.connection.execute("BEGIN IMMEDIATE")
            with self.connection:
                for statement in SCHEMA_STATEMENTS:
                    self.connection.execute(statement)
                for modulator_id in range(8):
                    profile = RdsProfileConfig(rds_pi=0xFFF0 + modulator_id, rds_ps=[PsEntry(text=f"TX {modulator_id}")], rds_rt="", rds_rt_enabled=False)
                    profile_id = self._write_profile(None, profile)
                    config = ModulatorConfig()
                    data = config.model_dump(mode="json", exclude=RDS_FIELDS | {"rds_override_profile"}) | {"timestamp": utcnow().isoformat()}
                    self.connection.execute("INSERT INTO modulators VALUES (?, NULL, ?, ?)", (modulator_id, profile_id, json.dumps(data)))
                self.connection.execute("INSERT INTO system_config VALUES (1, ?)", (SystemConfig().model_dump_json(),))
                self.connection.execute("PRAGMA user_version = 1")
        expected_columns = {'sources': ['id', 'rds_profile_id', 'data'], 'rds_profiles': ['id', 'data'],
                            'podcast_history': ['source_id', 'feed_id', 'episode_id', 'play_count', 'last_played_at', 'last_seen_at'],
                            'modulators': ['id', 'input_source_id', 'rds_override_profile_id', 'data'],
                            'system_config': ['id', 'data'], 'sqlite_sequence': ['name', 'seq']}
        tables = {r[0] for r in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != set(expected_columns) or any(
                [r[1] for r in self.connection.execute(f'PRAGMA table_info({table})')] != columns
                for table, columns in expected_columns.items()):
            raise RuntimeError("Incorrect database schema 1 structure; database left untouched.")
        ids = [row[0] for row in self.connection.execute("SELECT id FROM modulators ORDER BY id")]
        if ids != list(range(8)):
            raise RuntimeError("Database must contain exactly modulators 0..7")
        # Identifier quoting/whitespace is not a structural change.
        def normalize(sql):
            return ' '.join(sql.replace('"', '').split())
        actual = {normalize(r[0]) for r in self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if actual != {normalize(sql) for sql in SCHEMA_STATEMENTS}:
            raise RuntimeError("Incorrect database schema 1 structure; database left untouched.")
        owners = self.connection.execute('''
            SELECT p.id FROM rds_profiles p LEFT JOIN (
                SELECT rds_profile_id AS id FROM sources WHERE rds_profile_id IS NOT NULL
                UNION ALL
                SELECT rds_override_profile_id FROM modulators WHERE rds_override_profile_id IS NOT NULL
            ) owner ON owner.id=p.id GROUP BY p.id HAVING count(owner.id) != 1
        ''').fetchone()
        if owners or self.connection.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Invalid RDS profile ownership/references; database left untouched.')
        # Detect invalid persisted Follow/Custom combinations before TX startup.
        self.list('modulators')

    def _write_profile(self, profile_id, profile):
        data = profile.model_dump_json(exclude={'id'})
        if profile_id is None:
            return self.connection.execute('INSERT INTO rds_profiles(data) VALUES (?)', (data,)).lastrowid
        self.connection.execute('UPDATE rds_profiles SET data=? WHERE id=?', (data, profile_id))
        return profile_id

    def _profile(self, profile_id):
        if profile_id is None:
            return None
        row = self.connection.execute('SELECT data FROM rds_profiles WHERE id=?', (profile_id,)).fetchone()
        if row is None:
            raise DomainError(409, 'INVALID_RDS_PROFILE', 'Owned RDS profile is missing')
        return RdsProfile.model_validate(json.loads(row['data']) | {'id': profile_id})

    def effective_rds_profile(self, config, override):
        """The only effective-content resolver; all Modulator reads use it."""
        if config.get('rds_follow_source', False):
            if config['audio_mode'] != 'DIGITAL' or config['input_source_id'] is None:
                raise DomainError(409, 'INVALID_RDS_FOLLOW', 'Follow source requires DIGITAL and an assigned source')
            source = self.get('sources', config['input_source_id'])
            if source.rds_profile is None:
                raise DomainError(409, 'INVALID_RDS_FOLLOW', 'Selected source has no RDS profile')
            return source.rds_profile
        if override is None:
            raise DomainError(409, 'INVALID_RDS_PROFILE', 'Modulator override RDS profile is missing')
        return override

    def list(self, table):
        RECORDS[table]
        return [self.get(table, row[0]) for row in self.connection.execute(f'SELECT id FROM {table} ORDER BY id')]

    def get(self, table, record_id):
        model = RECORDS[table]
        row = self.connection.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone()
        if row is None:
            raise DomainError(404, 'NOT_FOUND', f'{table} resource {record_id} does not exist')
        data = json.loads(row['data']) | {'id': row['id']}
        if table == 'sources':
            profile = self._profile(row['rds_profile_id'])
            data.update(rds_profile_id=row['rds_profile_id'],
                        rds_profile=profile.model_dump(exclude={'id'}) if profile else None)
        else:
            override = self._profile(row['rds_override_profile_id'])
            effective = self.effective_rds_profile(data, override)
            data.update(effective.model_dump(exclude={'id'}))
            data.update(rds_override_profile_id=row['rds_override_profile_id'],
                        rds_override_profile=override.model_dump(exclude={'id'}) if override else None)
        return model.model_validate(data)

    def rds_followers(self, source_id):
        return [m.id for m in self.list('modulators') if m.input_source_id == source_id and m.rds_follow_source]

    def save(self, table, config, record_id=None):
        if table not in RECORDS or (table == 'modulators' and record_id is None):
            raise ValueError('Invalid repository operation')
        old = self.get(table, record_id) if record_id is not None else None
        data = config.model_dump(mode='json', exclude={'id', 'timestamp', 'rds_profile_id', 'rds_override_profile_id'})
        data['timestamp'] = utcnow().isoformat()
        try:
            with self.connection:
                if table == 'sources':
                    profile = config.rds_profile
                    profile_id = old.rds_profile_id if old else None
                    if profile is None and profile_id is not None:
                        followers = self.rds_followers(record_id)
                        if followers:
                            ids = ', '.join(f'TX{i}' for i in followers)
                            raise DomainError(409, 'RDS_PROFILE_FOLLOWED',
                                f'This RDS profile is currently followed by {ids}. Switch those transmitters to Custom RDS first.')
                    next_id = self._write_profile(profile_id, profile) if profile else None
                    data.pop('rds_profile', None)
                    columns, values = ['data', 'rds_profile_id'], [json.dumps(data), next_id]
                else:
                    profile = config.rds_override_profile
                    if profile is None:
                        profile = RdsProfileConfig.model_validate(config.model_dump(include=RDS_FIELDS))
                    elif old and profile == old.rds_override_profile:
                        # Flat legacy writes are override edits; effective read fields
                        # copied unchanged during hardware/lifecycle saves are not edits.
                        changes = {k: getattr(config, k) for k in RDS_FIELDS if getattr(config, k) != getattr(old, k)}
                        profile = RdsProfileConfig.model_validate(profile.model_dump() | changes)
                    self.effective_rds_profile(data, profile)  # Validate before any persistent effect.
                    profile_id = self._write_profile(old.rds_override_profile_id, profile)
                    for key in RDS_FIELDS | {'rds_override_profile'}:
                        data.pop(key, None)
                    columns, values = ['data', 'input_source_id', 'rds_override_profile_id'], [json.dumps(data), config.input_source_id, profile_id]
                if record_id is None:
                    cursor = self.connection.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", values)
                    record_id = cursor.lastrowid
                else:
                    self.connection.execute(
                        f"UPDATE {table} SET {', '.join(c + ' = ?' for c in columns)} WHERE id = ?", values + [record_id])
                if table == 'sources' and profile_id is not None and next_id is None:
                    self.connection.execute('DELETE FROM rds_profiles WHERE id=?', (profile_id,))
        except sqlite3.IntegrityError as exc:
            raise DomainError(409, 'REFERENCE_CONFLICT', 'Referenced resource does not exist') from exc
        return self.get(table, record_id)

    def delete(self, table, record_id):
        if table != 'sources':
            raise ValueError('Resource cannot be deleted')
        source = self.get(table, record_id)
        try:
            with self.connection:
                self.connection.execute('DELETE FROM sources WHERE id=?', (record_id,))
                if source.rds_profile_id is not None:
                    self.connection.execute('DELETE FROM rds_profiles WHERE id=?', (source.rds_profile_id,))
        except sqlite3.IntegrityError as exc:
            raise DomainError(409, 'RESOURCE_IN_USE', 'Resource is referenced by another configuration') from exc

    def get_system(self):
        return SystemConfig.model_validate_json(self.connection.execute("SELECT data FROM system_config WHERE id = 1").fetchone()[0])

    def save_system(self, config):
        with self.connection:
            self.connection.execute("UPDATE system_config SET data = ? WHERE id = 1", (config.model_dump_json(),))
        return config

    def select_single_modulator(self, modulator_id):
        """Atomically replace a globally stopped selection with one desired TX."""
        self.get("modulators", modulator_id)  # Reject invalid IDs before writing.
        # Do not call save/save_system here: their contexts commit independently.
        with self.connection:
            for row in self.connection.execute("SELECT id, data FROM modulators ORDER BY id").fetchall():
                data = json.loads(row["data"])
                enabled = row["id"] == modulator_id
                if data["enabled"] != enabled:
                    data.update(enabled=enabled, timestamp=utcnow().isoformat())
                    self.connection.execute("UPDATE modulators SET data = ? WHERE id = ?",
                                            (json.dumps(data), row["id"]))
            system = self.get_system().model_copy(update={"global_stopped": False})
            self.connection.execute("UPDATE system_config SET data = ? WHERE id = 1",
                                    (system.model_dump_json(),))

    def close(self):
        self.connection.close()
