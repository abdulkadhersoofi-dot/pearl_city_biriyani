"""Counter billing (POS) becomes a module ("pos"). Every client that
exists keeps it: switched on for them, and Included in every package that
doesn't already leave unlisted modules to the client's choice - so nobody
sees any change until the Ultra Admin or the client switches it off.

Revision ID: d5e1a7c3f924
Revises: c2f4a8e6b913
Create Date: 2026-10-23 12:00:00.000000

"""
import json

from alembic import op
import sqlalchemy as sa


revision = 'd5e1a7c3f924'
down_revision = 'c2f4a8e6b913'
branch_labels = None
depends_on = None


def _load(value):
    if value is None:
        return {}
    return value if isinstance(value, dict) else json.loads(value)


def upgrade():
    conn = op.get_bind()
    for row in conn.execute(sa.text("SELECT id, features FROM tenants")).mappings():
        features = _load(row["features"])
        if "pos" not in features:
            features["pos"] = True
            conn.execute(sa.text("UPDATE tenants SET features = CAST(:f AS JSON) WHERE id = :id"),
                         {"f": json.dumps(features), "id": row["id"]})
    for row in conn.execute(sa.text("SELECT id, modules, unlisted_modules FROM packages")).mappings():
        modules = _load(row["modules"])
        if "pos" in modules or row["unlisted_modules"] == "choice":
            continue
        modules["pos"] = "inc"
        conn.execute(sa.text("UPDATE packages SET modules = CAST(:m AS JSON) WHERE id = :id"),
                     {"m": json.dumps(modules), "id": row["id"]})


def downgrade():
    conn = op.get_bind()
    for table, column in (("tenants", "features"), ("packages", "modules")):
        for row in conn.execute(sa.text(f"SELECT id, {column} FROM {table}")).mappings():
            value = _load(row[column])
            if "pos" in value:
                value.pop("pos")
                conn.execute(sa.text(f"UPDATE {table} SET {column} = CAST(:v AS JSON) WHERE id = :id"),
                             {"v": json.dumps(value), "id": row["id"]})
