"""Remove tenant hard expiry, add auditor billing cycle fields

Revision ID: 596c823a290c
Revises: d356140aac44
Create Date: 2026-10-08 10:30:35.007663

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '596c823a290c'
down_revision = 'd356140aac44'
branch_labels = None
depends_on = None


def upgrade():
    # The firm no longer uses a hard expiry date - pending verification,
    # pause, and billing-overdue are the only gates now.
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('valid_until')

    # Auditor/Sub-Auditor accounts get the exact same billing system a
    # client (Tenant) has - the `billing_cycle` Postgres enum type
    # already exists from the previous migration, so create_type=False
    # here to avoid trying (and failing) to create it a second time.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'billing_cycle',
            postgresql.ENUM('MONTHLY', 'YEARLY', name='billing_cycle', create_type=False),
            nullable=True,
        ))
        batch_op.add_column(sa.Column('cycle_anchor_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('next_billing_due', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('manual_alarm_active', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    # Any Auditor/Sub-Auditor already created before this migration (the
    # role was introduced one migration ago) is treated as already-
    # verified, same as the pre-existing-tenant backfill above it -
    # nobody already using the app gets retroactively locked out
    # pending a billing cycle they were never asked to pick.
    op.execute(
        "UPDATE users SET billing_cycle = 'MONTHLY' "
        "WHERE role IN ('AUDITOR', 'SUB_AUDITOR') AND billing_cycle IS NULL"
    )


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('manual_alarm_active')
        batch_op.drop_column('next_billing_due')
        batch_op.drop_column('cycle_anchor_date')
        batch_op.drop_column('billing_cycle')

    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('valid_until', sa.DATE(), autoincrement=False, nullable=True))
