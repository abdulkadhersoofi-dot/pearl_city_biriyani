"""Sign-in sessions in the database (app/utils/web_session.py), for hosts
without Redis (Google Cloud Run). One row per signed-in browser, keyed by
the SHA-256 of its session id; expiry is indexed for the nightly cleanup.

Revision ID: e8b2c4d6f013
Revises: d5e1a7c3f924
Create Date: 2026-10-24 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e8b2c4d6f013'
down_revision = 'd5e1a7c3f924'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'web_sessions',
        sa.Column('id_hash', sa.String(length=64), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('expiry', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id_hash'),
    )
    op.create_index('ix_web_sessions_expiry', 'web_sessions', ['expiry'])


def downgrade():
    op.drop_index('ix_web_sessions_expiry', table_name='web_sessions')
    op.drop_table('web_sessions')
