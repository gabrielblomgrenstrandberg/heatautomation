"""Back up and apply Alembic migrations to the thermal SQLite database."""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL


ROOT = Path(__file__).resolve().parents[2]


def _backup_database(database_path):
    import sqlite3

    if not database_path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = database_path.with_name(
        "%s.%s.bak" % (database_path.name, stamp)
    )
    if backup_path.exists():
        raise FileExistsError("Refusing to overwrite database backup: %s" % backup_path)

    source = None
    destination = None
    try:
        source = sqlite3.connect(str(database_path))
        destination = sqlite3.connect(str(backup_path))
        source.backup(destination)
        destination.commit()
    except Exception:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if backup_path.exists():
            backup_path.unlink()
        raise
    destination.close()
    source.close()
    return backup_path


def migrate_database(database_path, backup=True):
    database_path = Path(database_path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_database(database_path) if backup else None

    alembic_config = Config(str(ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(ROOT / "migrations"))
    url = URL.create("sqlite", database=str(database_path)).render_as_string(
        hide_password=False
    )
    alembic_config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(alembic_config, "head")
    return backup_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        default="thermal_history.sqlite3",
        help="SQLite database path (default: thermal_history.sqlite3)",
    )
    args = parser.parse_args(argv)
    backup_path = migrate_database(args.database)
    if backup_path:
        print("Database backup created: %s" % backup_path)
    print("Database migrated to Alembic head.")


if __name__ == "__main__":
    main()
