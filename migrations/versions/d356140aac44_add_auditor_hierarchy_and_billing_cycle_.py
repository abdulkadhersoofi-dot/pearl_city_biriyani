"""Add auditor hierarchy and billing cycle overhaul

Revision ID: d356140aac44
Revises: c530a2d610fa
Create Date: 2026-10-08 09:57:41.096159

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'd356140aac44'
down_revision = 'c530a2d610fa'
branch_labels = None
depends_on = None


def upgrade():
    # Postgres can't add an enum value inside the migration's normal
    # transaction - needs its own autocommit block. These two new roles
    # (Auditor, Sub-Auditor) sit below the existing Super Admin
    # ("Ultra Admin" - display label only, the stored value is untouched)
    # and above Client Admin/Staff.
    # SQLAlchemy's Enum(UserRole, ...) stores the member *name*
    # (SUPER_ADMIN, CLIENT_ADMIN, STAFF), not `.value` - match that
    # exactly or every `role ==` comparison in the app breaks.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'AUDITOR'")
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'SUB_AUDITOR'")

    # batch_alter_table's ADD COLUMN doesn't emit CREATE TYPE for a new
    # Postgres enum the way a bare op.add_column does - create it
    # explicitly first, then reference it with create_type=False so the
    # column add below doesn't try (and fail) to create it again.
    postgresql.ENUM('MONTHLY', 'YEARLY', name='billing_cycle').create(op.get_bind(), checkfirst=True)

    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auditor_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column(
            'billing_cycle',
            postgresql.ENUM('MONTHLY', 'YEARLY', name='billing_cycle', create_type=False),
            nullable=True,
        ))
        batch_op.add_column(sa.Column('cycle_anchor_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('manual_alarm_active', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.create_index(batch_op.f('ix_tenants_auditor_id'), ['auditor_id'], unique=False)
        batch_op.create_foreign_key('fk_tenants_auditor', 'users', ['auditor_id'], ['id'], use_alter=True)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_auditor_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_parent_auditor_id'), ['parent_auditor_id'], unique=False)
        batch_op.create_foreign_key('fk_users_parent_auditor', 'users', ['parent_auditor_id'], ['id'], use_alter=True)

    # Every tenant that already existed before this feature is treated as
    # already-verified (MONTHLY, the prior system's implicit cycle) - the
    # new "pending verification" gate (billing_cycle IS NULL) must never
    # retroactively lock out a client that was already active. Their
    # existing next_billing_due/valid_until (if any) keep working exactly
    # as before; cycle_anchor_date is deliberately left null since there's
    # no "first login after password change" to anchor to retroactively -
    # the already-working next_billing_due computed under the old system
    # is enough until it next advances.
    op.execute("UPDATE tenants SET billing_cycle = 'MONTHLY' WHERE billing_cycle IS NULL")


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_parent_auditor', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_users_parent_auditor_id'))
        batch_op.drop_column('parent_auditor_id')

    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tenants_auditor', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_tenants_auditor_id'))
        batch_op.drop_column('manual_alarm_active')
        batch_op.drop_column('cycle_anchor_date')
        batch_op.drop_column('billing_cycle')
        batch_op.drop_column('auditor_id')

    postgresql.ENUM('MONTHLY', 'YEARLY', name='billing_cycle').drop(op.get_bind(), checkfirst=True)

    # Postgres can't drop a single enum value - 'auditor'/'sub_auditor'
    # are left in the user_role type on downgrade. Harmless: nothing
    # references them once the columns above are gone, and re-running
    # upgrade() is idempotent (IF NOT EXISTS).
