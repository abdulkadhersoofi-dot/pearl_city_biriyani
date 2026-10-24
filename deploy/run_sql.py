"""The "admin-sql" Cloud Run Job: runs SQL as the database's admin user,
for what the Data API (scripts/gcp/sql.sh) can't do - creating roles,
grants, large data fixes. The statement arrives base64-encoded in SQL_B64
(gcloud's argument lists would split it at commas); SQL_DB names the
database. Results are printed to the job's log, which
scripts/gcp/logs.sh reads.

    gcloud run jobs execute arfa-admin-sql --wait \\
        --update-env-vars SQL_DB=arfa,SQL_B64=$(printf %s "$SQL" | base64 -w0)
"""

import base64
import os
import sys
from urllib.parse import urlsplit, urlunsplit

import psycopg2


def main() -> int:
    sql = base64.b64decode(os.environ["SQL_B64"]).decode()
    url = urlsplit(os.environ["DATABASE_URL"].replace("postgresql+psycopg2://", "postgresql://"))
    if os.environ.get("SQL_DB"):
        url = url._replace(path="/" + os.environ["SQL_DB"])
    conn = psycopg2.connect(urlunsplit(url))
    conn.autocommit = True
    with conn.cursor() as cur:
        print(f"-- {url.path.lstrip('/')}: {sql[:2000]}")
        cur.execute(sql)
        if cur.description:
            print("\t".join(col.name for col in cur.description))
            for row in cur.fetchmany(1000):
                print("\t".join("" if v is None else str(v) for v in row))
        print(f"-- done ({cur.rowcount} row(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
