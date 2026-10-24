"""Sign-in domains as their own records (sign_in_domains), each with the
clients whose logins may use it (tenants.sign_in_domain_id), replacing the
one-domain-per-client tenants.login_domain. Any address already set moves
across, with that client as its only client and its look.

Revision ID: b7e2d9c4a815
Revises: a4c8e2f61d37
Create Date: 2026-10-24 16:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b7e2d9c4a815'
down_revision = 'a4c8e2f61d37'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sign_in_domains',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('domain', sa.String(length=253), nullable=False),
        sa.Column('brand_tenant_id', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['brand_tenant_id'], ['tenants.id'], name='fk_sign_in_domains_brand_tenant'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_sign_in_domains_created_by'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('domain'),
    )
    op.add_column('tenants', sa.Column('sign_in_domain_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_tenants_sign_in_domain', 'tenants', 'sign_in_domains', ['sign_in_domain_id'], ['id'])
    op.create_index('ix_tenants_sign_in_domain_id', 'tenants', ['sign_in_domain_id'])
    op.execute("""
        INSERT INTO sign_in_domains (domain, brand_tenant_id, is_active, created_at, updated_at)
        SELECT login_domain, id, true, now(), now() FROM tenants WHERE login_domain IS NOT NULL
    """)
    op.execute("""
        UPDATE tenants SET sign_in_domain_id = d.id FROM sign_in_domains d WHERE d.domain = tenants.login_domain
    """)
    op.drop_constraint('uq_tenants_login_domain', 'tenants', type_='unique')
    op.drop_column('tenants', 'login_domain')


def downgrade():
    op.add_column('tenants', sa.Column('login_domain', sa.String(length=253), nullable=True))
    op.create_unique_constraint('uq_tenants_login_domain', 'tenants', ['login_domain'])
    # One client per domain before this: the look's client keeps it.
    op.execute("""
        UPDATE tenants SET login_domain = d.domain FROM sign_in_domains d
        WHERE d.brand_tenant_id = tenants.id AND tenants.sign_in_domain_id = d.id
    """)
    op.drop_index('ix_tenants_sign_in_domain_id', table_name='tenants')
    op.drop_constraint('fk_tenants_sign_in_domain', 'tenants', type_='foreignkey')
    op.drop_column('tenants', 'sign_in_domain_id')
    op.drop_table('sign_in_domains')
