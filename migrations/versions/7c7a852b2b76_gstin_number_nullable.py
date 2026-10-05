"""Make gstins.gstin nullable

An unregistered tenant has a real business location (a state, maybe an
address) but no GSTIN number. Onboarding now always creates a Gstin row
for every tenant - this lets that row exist with gstin=NULL instead of
onboarding silently skipping it, which left unregistered tenants with no
GSTIN row at all and broke POS checkout and invoicing for them.

Revision ID: 7c7a852b2b76
Revises: 06c23f0fe6c4
Create Date: 2026-10-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7c7a852b2b76'
down_revision = '06c23f0fe6c4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('gstins', schema=None) as batch_op:
        batch_op.alter_column('gstin', existing_type=sa.String(length=15), nullable=True)


def downgrade():
    with op.batch_alter_table('gstins', schema=None) as batch_op:
        batch_op.alter_column('gstin', existing_type=sa.String(length=15), nullable=False)
