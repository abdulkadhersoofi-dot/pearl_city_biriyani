"""Drop password_reset_otps

Password resets no longer go through an emailed OTP: a Super Admin sets a
client admin's password directly, and a Client Admin sets a staff user's
password directly, both in-app. Nothing writes to this table anymore.

Revision ID: 06c23f0fe6c4
Revises: 911d63765c5d
Create Date: 2026-09-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '06c23f0fe6c4'
down_revision = '911d63765c5d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('password_reset_otps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_password_reset_otps_user_id'))

    op.drop_table('password_reset_otps')


def downgrade():
    op.create_table('password_reset_otps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('otp_hash', sa.String(length=255), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('password_reset_otps', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_password_reset_otps_user_id'), ['user_id'], unique=False)
