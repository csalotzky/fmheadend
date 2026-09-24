"""Current pre-V1 schema initialization, persistence, and integrity."""
import json
import sqlite3

import pytest

from fmheadendd.database import Database, SCHEMA_VERSION
from fmheadendd.errors import DomainError
from fmheadendd.models.configuration import (
    InputSourceConfig, ModulatorConfig, PsEntry, SystemConfig,
)

TABLES = ("sources", "modulators", "system_config")


def snapshot(connection):
    return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in TABLES}


def test_fresh_schema_and_model_defaults(tmp_path):
    db = Database(tmp_path / "current.db")
    try:
        assert SCHEMA_VERSION == 2
        assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {row[0] for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == set(TABLES) | {"sqlite_sequence"}
        assert db.list("sources") == []
        assert db.get_system() == SystemConfig()
        assert [m.id for m in db.list("modulators")] == list(range(8))
        for tx in range(8):
            expected = ModulatorConfig(rds_ps=[PsEntry(text=f"FM TX {tx}")])
            actual = db.get("modulators", tx)
            assert actual.model_dump(exclude={"id", "timestamp"}) == expected.model_dump()
            raw = json.loads(db.connection.execute(
                "SELECT data FROM modulators WHERE id=?", (tx,)).fetchone()[0])
            assert raw.pop("timestamp")
            assert raw == expected.model_dump(mode="json")
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_current_configuration_reopens_without_writes(tmp_path):
    path = tmp_path / "current.db"
    db = Database(path)
    source = db.save("sources", InputSourceConfig(
        name="Station", type="HTTP", config={"uri": "https://example.org/live"},
        processor_mode='builtin', input_gain_db=-3, audio_profile_path='default/basic.json',
        gst_options={"source": {"user-agent": "Station/2"}}))
    db.save("modulators", ModulatorConfig(
        enabled=True, input_source_id=source.id, frequency_khz=98100,
        rf_attenuation_db=7.5, gain_mode="MANUAL", analog_gain_db=-1.5,
        digital_trim_db=1, soft_clipping=True, stereo=False, preemphasis_us=75,
        rds_enabled=True, rds_pi=0x1234, rds_ecc=224, rds_af_khz=[101100],
        rds_ps=[PsEntry(text="STATION", duration_s=9), PsEntry(text="SECOND")],
        rds_pty=10, rds_tp=True, rds_rt="Saved text", rds_metadata_to_rt=True), 3)
    db.save("modulators", ModulatorConfig(
        audio_mode="ANALOG", frequency_khz=99500,
        rds_ps=[PsEntry(text="STUDIO")]), 7)
    db.save_system(SystemConfig(web_port=8090, log_level="DEBUG", rp2354_i2c_address=32))
    before = snapshot(db.connection)
    db.close()
    for _ in range(2):
        db = Database(path)
        try:
            assert snapshot(db.connection) == before
            assert db.connection.total_changes == 0
            assert db.connection.execute("PRAGMA user_version").fetchone()[0] == 2
            assert db.get("sources", source.id) == source
            assert db.get("modulators", 3).rds_ps[0].duration_s == 9
            assert db.get_system().web_port == 8090
        finally:
            db.close()


@pytest.mark.parametrize("version", [3, 9, 99])
def test_unsupported_pre_release_version_is_untouched(tmp_path, version):
    path = tmp_path / "unsupported.db"
    # Only the version matters: startup must reject it before accessing any tables.
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('untouched')")
        connection.execute(f"PRAGMA user_version={version}")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match=(
            rf"Unsupported database schema {version}")):
        Database(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("statement", [
    "DELETE FROM modulators WHERE id=0",
    "UPDATE modulators SET id=8 WHERE id=0",
    "UPDATE modulators SET id=id WHERE id=0",
    "INSERT INTO modulators(id,data) VALUES (8,'{}')",
    "INSERT INTO modulators(id,data) VALUES (-1,'{}')",
])
def test_fixed_modulator_protection(tmp_path, statement):
    db = Database(tmp_path / "current.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with db.connection:
                db.connection.execute(statement)
        assert [m.id for m in db.list("modulators")] == list(range(8))
    finally:
        db.close()


def test_reopen_requires_complete_modulator_set(tmp_path):
    path = tmp_path / "incomplete.db"
    db = Database(path)
    with db.connection:
        db.connection.execute("DROP TRIGGER fixed_modulator_delete")
        db.connection.execute("DELETE FROM modulators WHERE id=7")
    db.close()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match=r"exactly modulators 0\.\.7"):
        Database(path)
    assert path.read_bytes() == before


def test_foreign_keys_and_delete_restrictions(tmp_path):
    db = Database(tmp_path / "current.db")
    try:
        source = db.save("sources", InputSourceConfig(
            name="Capture", type="ALSA", config={"device": "hw:1,0"}))
        db.save("modulators", ModulatorConfig(input_source_id=source.id), 0)
        for table, record_id in (("sources", source.id),):
            with pytest.raises(DomainError) as error:
                db.delete(table, record_id)
            assert error.value.status == 409
        for statement in (
            "UPDATE modulators SET input_source_id=999 WHERE id=0",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                with db.connection:
                    db.connection.execute(statement)
        db.save("modulators", ModulatorConfig(), 0)
        db.delete("sources", source.id)
    finally:
        db.close()


def test_initialization_rolls_back_ddl_seeds_and_version(tmp_path, monkeypatch):
    path = tmp_path / "failed.db"
    calls = 0

    def fail_during_seeding():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("seed failure")
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    monkeypatch.setattr("fmheadendd.database.utcnow", fail_during_seeding)
    with pytest.raises(RuntimeError, match="seed failure"):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    monkeypatch.undo()
    db = Database(path)
    assert len(db.list("modulators")) == 8
    db.close()
