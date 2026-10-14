"""flask backup (app.utils.backup)."""

import gzip
import shutil
from datetime import datetime

import pytest

from app.utils.backup import BackupError, _pg_dump_command, prune_backups, run_backup


def test_password_goes_in_the_environment_never_on_the_command_line():
    cmd, env = _pg_dump_command("postgresql+psycopg2://alice:s3cret@db.example.com:5433/shop")
    assert "s3cret" not in " ".join(cmd)
    assert env["PGPASSWORD"] == "s3cret"
    assert cmd[-1] == "shop"
    assert ["--host", "db.example.com"] == cmd[cmd.index("--host"):cmd.index("--host") + 2]
    assert ["--port", "5433"] == cmd[cmd.index("--port"):cmd.index("--port") + 2]


def test_non_postgres_url_is_refused():
    with pytest.raises(BackupError):
        _pg_dump_command("sqlite:///x.db")


def test_prune_keeps_only_the_newest(tmp_path):
    for stamp in ("20261001-010000", "20261002-010000", "20261003-010000"):
        (tmp_path / f"arfa-{stamp}.sql.gz").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_text("keep me")
    (tmp_path / "arfa-20261004-010000.sql.gz.partial").write_bytes(b"half")
    removed = prune_backups(tmp_path, keep=2)
    assert [p.name for p in removed] == ["arfa-20261001-010000.sql.gz"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "arfa-20261002-010000.sql.gz",
        "arfa-20261003-010000.sql.gz",
        "arfa-20261004-010000.sql.gz.partial",
        "unrelated.txt",
    ]


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_real_backup_of_the_test_database(app, db, tenant, tmp_path):
    path = run_backup(app.config["SQLALCHEMY_DATABASE_URI"], str(tmp_path), keep=3, now=datetime(2026, 10, 14, 2, 0, 0))
    assert path.name == "arfa-20261014-020000.sql.gz"
    with gzip.open(path, "rt") as fh:
        dump = fh.read()
    assert "CREATE TABLE public.tenants" in dump
    assert "Acme Traders" in dump


def test_failed_dump_leaves_no_file(app, tmp_path):
    bad = "postgresql+psycopg2://nobody:wrong@localhost:5432/does_not_exist"
    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump not installed")
    with pytest.raises(BackupError):
        run_backup(bad, str(tmp_path), keep=3)
    assert list(tmp_path.iterdir()) == []
