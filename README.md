# GST Billing & POS Platform

Multi-tenant, GST-compliant billing and POS web application for a Chartered
Accountant / GST practice firm to provide to its small-business clients.

**Phase 1 (this release):** tenant/user setup, role-based access, and the
Invoicing module with Tax Invoice / Bill of Supply PDFs. POS, Credit/Debit
notes, the firm's admin console, and GST reports are stubbed in the nav and
land in later phases (see [Roadmap](#roadmap)).

## Stack

- Python 3.11 / Flask (REST-ish, server-rendered with HTMX + Alpine.js for
  in-page interactivity - both vendored under `app/static/js/vendor/`, not
  loaded from a CDN, so the app keeps working on a client's offline/LAN
  network)
- PostgreSQL (one shared database; every business table carries `tenant_id`)
- Server-side sessions (Flask-Login + Redis-backed session store)
- WeasyPrint for PDF generation (invoices, thermal/A4 receipts later)
- Gunicorn + Nginx for self-hosted deployment

## Roles

| Role | Access |
|---|---|
| Super Admin (the firm) | Onboard/deactivate clients, read-only cross-tenant view + export, full audit log, reset client passwords |
| Client Admin (business owner) | Own GSTIN(s), invoice numbering, catalog, staff logins, full invoicing/POS/reports |
| Staff/Cashier | POS billing screen only (Phase 2) |

## Local development

### 1. Prerequisites

- Python 3.11+
- PostgreSQL 14+
- Redis 6+

### 2. Set up the database

```bash
sudo -u postgres psql -c "CREATE USER gst_app WITH PASSWORD 'gst_app';"
sudo -u postgres psql -c "CREATE DATABASE gst_billing OWNER gst_app;"
```

### 3. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
# edit .env: DATABASE_URL, REDIS_URL, SECRET_KEY at minimum
```

`MAIL_BACKEND=console` (the default) logs OTP codes to the console instead
of sending real email - convenient for local dev. Set `MAIL_BACKEND=smtp`
and the `SMTP_*` variables to send real email.

### 5. Run migrations

```bash
export FLASK_APP=wsgi.py
flask db upgrade
```

### 6. Create the firm's Super Admin login

```bash
flask create-super-admin
```

### 7. Run the dev server

```bash
flask run --debug
```

Sign in as the Super Admin, onboard a client from **Clients → Onboard
client**, then use the setup code that gets logged to the console (or
emailed, once SMTP is configured) at `/auth/reset-password` to set that
client's first password.

## Tests

Tests run against a real Postgres database (not sqlite) so that Postgres-
specific behavior - row locking in the invoice-numbering allocator, native
JSON/Enum columns - is exercised faithfully.

```bash
sudo -u postgres psql -c "CREATE DATABASE gst_billing_test OWNER gst_app;"
export FLASK_ENV=testing
pytest
```

## Folder structure

```
app/
├── models/          # Tenant, Gstin, User, InvoiceSeries, Customer, Product,
│                     # Invoice, InvoiceLine, AuditLog
├── auth/            # login, OTP password reset, role decorators
├── tenants/         # Super Admin console: onboarding, directory, audit log
├── invoicing/        # Phase 1 core: form, PDF, GST calc, numbering
├── customers/, products/   # tenant-scoped catalog CRUD
├── pos/, notes/, reports/  # stubbed, ship in Phases 2/3/5
├── api/              # small JSON endpoints (product lookup today; POS
│                     # cart/checkout from Phase 2) - kept separate so a
│                     # future SPA frontend can reuse them
├── utils/            # gst.py (tax split + document-type gating),
│                     # numbering.py (atomic per-FY/GSTIN/doc-type counters),
│                     # tenant_scope.py (tenant isolation helper)
├── templates/, static/
migrations/           # Alembic, via Flask-Migrate
tests/
deploy/               # nginx.conf, gunicorn.conf.py
```

## Deployment

### Render (quick start / testing)

`render.yaml` is a Render Blueprint that provisions the web service, a
managed Postgres database, and a managed Redis (Key Value) instance
together, wired to each other automatically.

1. Push this repo to GitHub.
2. In the Render dashboard: **New → Blueprint**, point it at the repo.
   Render reads `render.yaml` and shows you the three resources it's about
   to create - review the plans (see the cost note below) and click
   **Apply**.
3. First deploy will build the Docker image, run `flask db upgrade`
   (wired as `preDeployCommand`), then start Gunicorn.
4. Open the web service's **Shell** tab and run:
   ```bash
   flask create-super-admin
   ```
   to create the firm's first login.
5. `MAIL_BACKEND=console` by default, so OTP codes (onboarding, password
   reset) show up in the service's **Logs** tab rather than a real inbox
   until you set `SMTP_*` env vars and change `MAIL_BACKEND` to `smtp`.

**Cost/durability note:** `render.yaml` requests Render's `free` plan for
all three resources so you can try it at no cost. Render's free Postgres
**auto-deletes after 30 days and has no backups**, and free web services
spin down when idle (the first request after a while takes longer to
wake it up). That's fine for testing; before putting real client data in
it, upgrade at least the database plan from the Render dashboard (Settings
→ Change Plan) - the app itself needs no code change either way.

**Custom domain:** once you're ready to move off the `onrender.com`
subdomain onto your own domain, it's a dashboard step, no code change:
web service → **Settings → Custom Domains** → add your domain, then add
the CNAME/A record it gives you at your DNS provider. Render issues the
TLS certificate automatically.

### Self-hosted (VPS / your own server)

```bash
docker build -t gst-billing .
docker run -p 8000:8000 --env-file .env gst-billing
```

or without Docker:

```bash
gunicorn -c deploy/gunicorn.conf.py wsgi:app
```

Put Nginx in front (see `deploy/nginx.conf` for a starting point) terminating
TLS and proxying to Gunicorn. Sessions live in Redis and PDFs are generated
per-request, so the app is stateless across workers/machines - scale
Gunicorn workers or add app servers behind Nginx as load grows.

## Design notes

- **Multi-tenancy**: one shared database; every business-data table carries
  `tenant_id`. All tenant-scoped routes fetch through
  `app.utils.tenant_scope.tenant_query()`, which filters to the signed-in
  user's own tenant - a client can never see another client's rows even by
  guessing an id in the URL. Super Admin routes take an explicit `tenant_id`
  instead and every access is written to `AuditLog`.
- **Invoices are never hard-deleted.** Voiding sets `status=void` with a
  required reason, keeping the full record for audit. There is deliberately
  no in-place "edit issued invoice" - void + duplicate keeps the numbering
  and audit trail sound.
- **Document types are gated by registration type**, not left to guesswork:
  `app.utils.gst.ALLOWED_DOCUMENT_TYPES` maps Regular/Composition/
  Unregistered to the document types that registration is allowed to issue
  (e.g. Composition clients only ever see Bill of Supply, never Tax
  Invoice).
- **Invoice numbering** is per tenant, per GSTIN, per financial year, per
  document type, with its own editable prefix - allocated under a row lock
  (`SELECT ... FOR UPDATE`) so concurrent checkouts never collide.

## Roadmap

- **Phase 2** - POS module: touchscreen checkout, thermal/A4 receipts, hold/
  resume, refunds, Z-report.
- **Phase 3** - Credit/Debit notes (created from an existing invoice) and
  the remaining GST document types (Export, RCM, Delivery Challan, Receipt/
  Refund Voucher).
- **Phase 4** - Admin console: cross-client drill-down, bulk GSTR-1/3B
  export for multiple clients in one action.
- **Phase 5** - GSTR-1/3B report exports (Excel/PDF/JSON), consolidated
  admin reports, onboarding walkthrough polish, e-invoice IRN/QR hook.
