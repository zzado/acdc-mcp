import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from team_mcp import cli, db
from team_mcp.settings import Settings


@pytest.mark.parametrize("command", ["status", "db-backup"])
def test_inspect_legacy_database_without_migrating(tmp_path, monkeypatch, capsys, command):
    source = tmp_path / "legacy #1.sqlite"
    backup = tmp_path / "backup.sqlite"
    cfg = Settings(database=source, disk_path=tmp_path)
    monkeypatch.setattr(Settings, "from_env", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["team-mcp", command] + ([str(backup)] if command == "db-backup" else []))
    initial = Path(db.__file__).with_name("migrations") / "001_initial.sql"
    # Keep the writer open so the snapshot includes committed rows still in the WAL.
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.executescript(initial.read_text(encoding="utf-8") + """
            PRAGMA user_version=1;
            INSERT INTO settings VALUES ('event_epoch','before-upgrade');
            INSERT INTO users VALUES ('user','owner','member',NULL);
            INSERT INTO agents(id,user_id,created_at) VALUES ('owner','user',1);
            INSERT INTO channels VALUES ('room','Round','private','owner',NULL,NULL,1);
            INSERT INTO channel_members VALUES ('room','user');
        """)
        cli.main()
        if command == "status":
            assert json.loads(capsys.readouterr().out)["uploads"] == {}
        for path in ([source, backup] if command == "db-backup" else [source]):
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as cx:
                assert cx.execute("PRAGMA user_version").fetchone() == (1,)
                assert cx.execute("SELECT kind FROM channels").fetchone() == ("private",)
                assert cx.execute("SELECT * FROM channel_members").fetchall() == [("room", "user")]
                assert cx.execute("SELECT value FROM settings WHERE key='event_epoch'").fetchone() == ("before-upgrade",)


@pytest.mark.parametrize("command", ["status", "db-backup"])
def test_inspection_requires_existing_source(tmp_path, monkeypatch, command):
    cfg = Settings(database=tmp_path / "missing.sqlite", disk_path=tmp_path)
    backup = tmp_path / "backup.sqlite"
    monkeypatch.setattr(Settings, "from_env", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["team-mcp", command] + ([str(backup)] if command == "db-backup" else []))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code != 0
    assert not cfg.database.exists() and not backup.exists()


def test_backup_refuses_existing_file(tmp_path):
    backup = tmp_path / "existing.sqlite"
    backup.write_bytes(b"existing backup")
    with closing(sqlite3.connect(":memory:")) as source, pytest.raises(ValueError, match="new backup file"):
        cli.backup_database(source, backup)
    assert backup.read_bytes() == b"existing backup"
