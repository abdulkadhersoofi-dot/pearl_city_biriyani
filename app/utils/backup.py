"""Database backups: pg_dump -> gzip -> a timestamped file in a folder.

Local file only for now (the firm's choice); uploading the file somewhere
off-server is a later, separate step. Each run writes
arfa-YYYYmmdd-HHMMSS.sql.gz and keeps the newest `keep` of those,
deleting older ones. Restore with:

    gunzip -c arfa-20261014-020000.sql.gz | psql "$DATABASE_URL"

into an empty database.
"""

import gzip
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

BACKUP_PREFIX = "arfa-"
BACKUP_SUFFIX = ".sql.gz"


class BackupError(RuntimeError):
    pass


def _pg_dump_command(database_url: str) -> tuple[list[str], dict]:
    """pg_dump argv + env for this SQLAlchemy URL. The password goes in
    PGPASSWORD, never on the command line (where any local user could
    read it from the process list)."""
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise BackupError("Backups need a PostgreSQL DATABASE_URL.")
    cmd = ["pg_dump", "--no-owner", "--no-privileges", "--format=plain"]
    if url.host:
        cmd += ["--host", url.host]
    if url.port:
        cmd += ["--port", str(url.port)]
    if url.username:
        cmd += ["--username", url.username]
    cmd.append(url.database)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    return cmd, env


def run_backup(database_url: str, out_dir: str, keep: int = 14, now: datetime | None = None) -> Path:
    if shutil.which("pg_dump") is None:
        raise BackupError("pg_dump isn't installed on this machine (it comes with the PostgreSQL client tools).")
    if keep < 1:
        raise BackupError("Keep at least 1 backup.")
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    final = folder / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    partial = final.with_name(final.name + ".partial")

    cmd, env = _pg_dump_command(database_url)
    # stderr goes to a temp file, not a pipe: only stdout is drained while
    # the dump streams, so a chatty stderr filling a pipe buffer would
    # otherwise block pg_dump forever.
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, env=env)
        try:
            with gzip.open(partial, "wb") as gz:
                shutil.copyfileobj(proc.stdout, gz)
            if proc.wait() != 0:
                err.seek(0)
                message = err.read().decode("utf-8", "replace").strip()
                raise BackupError(f"pg_dump failed: {message or 'unknown error'}")
        except BaseException:
            proc.kill()
            proc.wait()
            partial.unlink(missing_ok=True)
            raise
    # A finished dump is only ever visible under its final name - a crash
    # mid-dump leaves a .partial that pruning ignores, never a truncated
    # file that looks like a good backup.
    partial.rename(final)
    prune_backups(folder, keep)
    return final


def prune_backups(folder: Path, keep: int) -> list[Path]:
    backups = sorted(p for p in Path(folder).glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file())
    stale = backups[:-keep] if len(backups) > keep else []
    for path in stale:
        path.unlink()
    return stale
