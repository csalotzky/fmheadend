"""Physical-channel PS defaults for fresh databases."""
import json


from fmheadendd.database import Database


def raw_rows(db):
    return {row["id"]: json.loads(row["data"]) for row in
            db.connection.execute("SELECT m.id, p.data FROM modulators m JOIN rds_profiles p ON p.id=m.rds_override_profile_id")}


def test_new_database_has_physical_static_ps_defaults(tmp_path):
    db = Database(tmp_path / "db")
    for tx, data in raw_rows(db).items():
        assert data["rds_ps"] == [{"text": f"TX {tx}", "duration_s": 5}]
        assert db.get("modulators", tx).rds_ps[0].text == f"TX {tx}"
        assert data["rds_pi"] == 0xFFF0 + tx and data["rds_ecc"] is None
        assert data["rds_pty"] == 0 and data["rds_rt"] == "" and data["rds_af_khz"] == []
    db.close()
