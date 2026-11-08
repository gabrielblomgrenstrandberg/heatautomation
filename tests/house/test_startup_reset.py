import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, insert

from heatautomation.house import models
from heatautomation.house.config import config_hash, load_house_config
from heatautomation.house.migrate import migrate_database
from heatautomation.house.reconcile import reconcile_house
from heatautomation.house.startup_reset import reset_mismatched_house


EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "house.example.json"


class StartupResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.database = root / "history.sqlite3"
        self.model = root / "model.joblib"
        self.house_path = root / "house.json"
        self.house_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.house = load_house_config(self.house_path)
        migrate_database(self.database, backup=False)
        engine = create_engine("sqlite:///" + str(self.database))
        try:
            reconcile_house(engine, self.house)
            with engine.begin() as conn:
                conn.execute(insert(models.optimizer_plans).values(
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), mode="advisory", details_json="{}"))
        finally:
            engine.dispose()
        self.model.write_bytes(b"old learning")

    def change_house(self):
        document = json.loads(self.house_path.read_text(encoding="utf-8"))
        document["rooms"][0]["name"] = "Changed room"
        self.house_path.write_text(json.dumps(document), encoding="utf-8")

    def test_equal_or_missing_config_preserves_all_data(self):
        self.assertFalse(reset_mismatched_house(self.database, self.model, self.house_path))
        self.assertFalse(reset_mismatched_house(self.database, self.model, self.house_path.with_name("missing.json")))
        self.assertEqual(self.model.read_bytes(), b"old learning")
        with closing(sqlite3.connect(str(self.database))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM optimizer_plans").fetchone()[0], 1)

    def test_mismatch_replaces_database_and_model_with_reconciled_empty_state(self):
        self.change_house()
        self.assertTrue(reset_mismatched_house(self.database, self.model, self.house_path))
        self.assertFalse(self.model.exists())
        with closing(sqlite3.connect(str(self.database))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM optimizer_plans").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT config_hash FROM houses").fetchone()[0],
                             config_hash(load_house_config(self.house_path)))
        self.assertFalse(reset_mismatched_house(self.database, self.model, self.house_path))

    def test_missing_hash_is_not_proof_of_mismatch(self):
        with closing(sqlite3.connect(str(self.database))) as conn:
            conn.execute("UPDATE houses SET config_hash = NULL")
            conn.commit()
        self.change_house()
        self.assertFalse(reset_mismatched_house(self.database, self.model, self.house_path))
        self.assertTrue(self.model.exists())

    def test_invalid_config_and_migration_error_leave_original_files_unchanged(self):
        self.house_path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            reset_mismatched_house(self.database, self.model, self.house_path)
        self.change_house_from_example()
        with patch("heatautomation.house.startup_reset.migrate_database", side_effect=RuntimeError("migration failed")):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                reset_mismatched_house(self.database, self.model, self.house_path)
        self.assertEqual(self.model.read_bytes(), b"old learning")
        with closing(sqlite3.connect(str(self.database))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM optimizer_plans").fetchone()[0], 1)

    def change_house_from_example(self):
        self.house_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.change_house()

    def test_replace_failure_restores_model_and_keeps_database(self):
        self.change_house()
        real_replace = __import__("os").replace
        def replace(source, target):
            if Path(target) == self.database:
                raise PermissionError("database in use")
            return real_replace(source, target)
        with patch("heatautomation.house.startup_reset.os.replace", side_effect=replace):
            with self.assertRaises(PermissionError):
                reset_mismatched_house(self.database, self.model, self.house_path)
        self.assertEqual(self.model.read_bytes(), b"old learning")
        with closing(sqlite3.connect(str(self.database))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM optimizer_plans").fetchone()[0], 1)

    def test_database_lock_prevents_reset(self):
        self.change_house()
        with closing(sqlite3.connect(str(self.database))) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                reset_mismatched_house(self.database, self.model, self.house_path)
            conn.rollback()
        self.assertEqual(self.model.read_bytes(), b"old learning")


if __name__ == "__main__":
    unittest.main()
