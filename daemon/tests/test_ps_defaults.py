"""Physical-channel PS defaults for fresh databases."""
import json


from fmheadendd.database import Database


def raw_rows(db):
    return {row["id"]: json.loads(row["data"]) for row in
            db.connection.execute("SELECT m.id, p.data FROM modulators m JOIN rds_profiles p ON p.id=m.rds_override_profile_id")}


def test_new_database_has_physical_static_ps_defaults(tmp_path):
    db = Database(tmp_path / "db")
    assert set(raw_rows(db)) == set(range(8))
    for tx, data in raw_rows(db).items():
        assert data["rds_ps"] == [{"text": f"TX {tx}", "duration_s": 5}]
        assert db.get("modulators", tx).rds_ps[0].text == f"TX {tx}"
        assert data["rds_pi"] == 0xFFF0 + tx and data["rds_ecc"] is None
        assert data["rds_rt_enabled"] is False
        assert data["rds_pty"] == 0 and data["rds_rt"] == "" and data["rds_af_khz"] == []
    db.close()


def test_reopen_preserves_saved_rt_and_source_profile_defaults(tmp_path):
    from fmheadendd.models.configuration import InputSourceConfig, RdsProfileConfig

    path = tmp_path / "db"
    db = Database(path)
    tx = db.get("modulators", 0)
    override = tx.rds_override_profile.model_copy(update={"rds_rt_enabled": True, "rds_rt": "Saved RT"})
    db.save("modulators", tx.model_copy(update={"rds_override_profile": override}), 0)
    source = db.save("sources", InputSourceConfig(name="Station", type="HTTP",
        config={"uri": "https://example.org/live"}, rds_profile=RdsProfileConfig()))
    before = raw_rows(db)
    db.close()
    db = Database(path)
    try:
        assert raw_rows(db) == before
        assert db.get("modulators", 0).rds_override_profile == override
        assert db.get("sources", source.id).rds_profile == RdsProfileConfig()
        assert db.get("sources", source.id).rds_profile.rds_rt_enabled is True
    finally:
        db.close()
