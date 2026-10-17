"""Packages and powers (Ultra Admin control, stage 1): packages, each
client's package and exceptions, and each auditor's powers.

Hand-written. Data:
  - ARFA's starting packages are created (a frozen copy of
    app/utils/entitlements.STARTING_PACKAGES as it stood when this
    shipped - later edits to that list never rewrite a database).
  - Every client that already exists is put on "Current setup": every
    module stays their own choice and every setting theirs, so nothing
    changes for anyone until the Ultra Admin assigns another package.
  - No auditor gets a powers row, which means "the role's powers as
    before" - nothing changes for them either.

Revision ID: a7c3f9d21b40
Revises: e899b9d7fa9d
Create Date: 2026-10-17 12:00:00

"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = 'a7c3f9d21b40'
down_revision = 'e899b9d7fa9d'
branch_labels = None
depends_on = None


def _modules(included=(), choice=()):
    out = {key: "inc" for key in included}
    out.update({key: "choice" for key in choice if key not in out})
    return out


_BILLING_INC = ["credit_sales", "favourites"]
_RETAIL_INC = _BILLING_INC + ["inventory", "purchases", "expenses", "business_reports", "daily_summary"]
_COUNTER_CHOICE = ["categories", "day_close", "cashier_pin", "offline_pos"]
_BUSINESS = {"business_type": "arfa", "aato": "arfa"}

PACKAGES = [
    ("current", "Current setup", 0, "choice", {}, {}, {}, True,
     "Everything as it was before packages: every module the client's own choice, every setting theirs, no limits. "
     "Every client that existed when packages arrived starts here."),
    ("billing", "Billing", 10, "off", _modules(_BILLING_INC, ["categories", "day_close", "cashier_pin"]),
     {**_BUSINESS, "branding": "arfa"}, {"branches": 1, "logins": 2, "gstins": 1}, False,
     "The counter and invoices only."),
    ("retail", "Retail", 20, "off", _modules(_RETAIL_INC, _COUNTER_CHOICE + ["loyalty", "multi_price_list", "variants"]),
     dict(_BUSINESS), {"branches": 3, "logins": 6, "gstins": 1}, True,
     "Billing plus stock, buying, expenses and the dashboard."),
    ("restaurant", "Restaurant", 30, "off",
     _modules(_BILLING_INC + ["restaurant_mode", "categories", "business_reports", "daily_summary"],
              ["expenses", "inventory", "purchases", "day_close", "cashier_pin", "offline_pos"]),
     dict(_BUSINESS), {"branches": 3, "logins": 8, "gstins": 1}, True,
     "Billing plus restaurant tables, kitchen tickets and the dashboard."),
    ("pharmacy", "Pharmacy", 40, "off", _modules(_RETAIL_INC + ["batch_expiry", "pharmacy"], _COUNTER_CHOICE + ["receivables"]),
     dict(_BUSINESS), {"branches": 3, "logins": 6, "gstins": 1}, True,
     "Retail plus batch and expiry, and the scheduled-drug register."),
    ("electronics", "Mobiles & Electronics", 50, "off",
     _modules(_RETAIL_INC + ["serial_imei", "margin_scheme", "job_cards", "electronics"],
              _COUNTER_CHOICE + ["receivables", "loyalty"]),
     dict(_BUSINESS), {"branches": 3, "logins": 6, "gstins": 1}, True,
     "Retail plus serial / IMEI numbers, the margin scheme and repairs."),
    ("wholesale", "Wholesale", 60, "off",
     _modules(_RETAIL_INC + ["receivables", "payables", "sales_documents", "multi_price_list", "eway_bill", "wholesale"],
              _COUNTER_CHOICE + ["einvoice", "cash_bank_book", "recurring_invoices"]),
     dict(_BUSINESS), {"branches": 5, "logins": 12, "gstins": 3}, True,
     "Retail plus customer and supplier accounts, quotes and orders, price lists, e-Way bills and salesmen."),
    ("everything", "Everything", 70, "choice", {}, dict(_BUSINESS), {}, True,
     "Every module as the client's own choice, no limits."),
]


def upgrade():
    packages = op.create_table('packages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=40), nullable=True),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('modules', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('unlisted_modules', sa.String(length=10), server_default='off', nullable=False),
    sa.Column('settings', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('limits', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('own_branding', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('price_monthly', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('price_yearly', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key'),
    sa.UniqueConstraint('name')
    )
    op.create_table('tenant_packages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('package_id', sa.Integer(), nullable=False),
    sa.Column('module_overrides', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('setting_overrides', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('limit_overrides', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('own_branding_override', sa.Boolean(), nullable=True),
    sa.Column('read_only_modules', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('assigned_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name='fk_tenant_packages_tenant'),
    sa.ForeignKeyConstraint(['package_id'], ['packages.id'], name='fk_tenant_packages_package'),
    sa.ForeignKeyConstraint(['assigned_by_id'], ['users.id'], name='fk_tenant_packages_assigned_by'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_tenant_packages_tenant_id', 'tenant_packages', ['tenant_id'], unique=True)
    op.create_index('ix_tenant_packages_package_id', 'tenant_packages', ['package_id'], unique=False)
    op.create_table('auditor_powers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('powers', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('allowed_package_ids', sa.JSON(), server_default='[]', nullable=False),
    sa.Column('max_clients', sa.Integer(), nullable=True),
    sa.Column('max_sub_auditors', sa.Integer(), nullable=True),
    sa.Column('set_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_auditor_powers_user'),
    sa.ForeignKeyConstraint(['set_by_id'], ['users.id'], name='fk_auditor_powers_set_by'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_auditor_powers_user_id', 'auditor_powers', ['user_id'], unique=True)

    now = datetime.now(timezone.utc)
    op.bulk_insert(packages, [
        {"key": key, "name": name, "sort_order": order, "unlisted_modules": unlisted, "modules": modules,
         "settings": settings, "limits": limits, "own_branding": branding, "description": description,
         "is_active": True, "created_at": now, "updated_at": now}
        for key, name, order, unlisted, modules, settings, limits, branding, description in PACKAGES
    ])
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO tenant_packages (tenant_id, package_id, module_overrides, setting_overrides, limit_overrides, "
        "read_only_modules, created_at, updated_at) "
        "SELECT t.id, p.id, '{}', '{}', '{}', '[]', :now, :now FROM tenants t, packages p WHERE p.key = 'current'"
    ), {"now": now})


def downgrade():
    op.drop_index('ix_auditor_powers_user_id', table_name='auditor_powers')
    op.drop_table('auditor_powers')
    op.drop_index('ix_tenant_packages_package_id', table_name='tenant_packages')
    op.drop_index('ix_tenant_packages_tenant_id', table_name='tenant_packages')
    op.drop_table('tenant_packages')
    op.drop_table('packages')
