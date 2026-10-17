# GST Billing & POS Platform

Multi-tenant, GST-compliant billing and POS web application for a Chartered
Accountant / GST practice firm to provide to its small-business clients.

**Shipped so far:** tenant/user setup and role-based access; Invoicing
(Tax Invoice / Bill of Supply PDFs, every GST registration type including
unregistered clients); POS (touchscreen checkout, direct-to-printer thermal
receipts, hold/resume, refunds, Z-report); Credit/Debit Notes issued
against an existing invoice; GSTR-1/3B return working papers (Excel/PDF/
JSON); a Super Admin console with cross-client drill-down and bulk GST
exports. See [Roadmap](#roadmap) for what's still ahead and the explicit
limits of the GST reports.

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
| Super Admin ("Ultra Admin" in the UI - the stored role name is unchanged) | Unrestricted: every Auditor, every client, every branch. Only role that verifies a pending client and sets its billing cycle, reassigns a client between auditors, or triggers/mutes the manual billing alarm. Full audit log, GST exports. |
| Auditor | Same operational power as the Ultra Admin (onboard/pause/"act as" clients, create branches via "act as") but scoped to only the clients allocated to them (`auditor_id`) plus their own Sub-Auditors' clients. Never sets a billing cycle - every client they onboard lands pending until the Ultra Admin verifies it. Can create their own Sub-Auditors and allocate any of their own clients to one. |
| Sub-Auditor | Created by one Auditor ("within their office"), scoped to only the clients that Auditor explicitly allocates to them - never their parent's full pool. Otherwise the same console as an Auditor. |
| Client Admin (business owner) | Own GSTIN(s), invoice numbering, catalog, staff logins, full invoicing/POS/reports, sets/resets Staff passwords directly |
| Staff/Cashier | POS billing screen only |

Every login is created with a password set directly by the admin creating
it (Super Admin sets a Client Admin's password, Client Admin sets a Staff
user's password) - no OTP, no email step. The new user is asked to change
that password the next time they sign in.

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
client** - you set that client's first password right there in the form,
then share it with them yourself. They'll be asked to change it on first
sign-in.

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
│                     # Invoice, InvoiceLine, POSBill(Line), CreditDebitNote,
│                     # NoteLine, AuditLog
├── auth/            # login, change password, role decorators
├── tenants/         # Super Admin console: onboarding, directory, audit log,
│                     # cross-client drill-down, bulk GST exports
├── staff/           # Client Admin: create/deactivate/reset staff logins
├── invoicing/        # form, PDF, GST calc, numbering
├── customers/, products/   # tenant-scoped catalog CRUD
├── bulk/             # Excel import/export (items, customers): template,
│                     # row-by-row preview, confirm - specs.py per kind
├── branches/         # branch logins, stock, per-branch permissions
├── settings/         # Client Admin settings: branding, business profile,
│                     # modules (feature flags)
├── pos/             # POS module: terminal, hold/resume, refunds, Z-report;
│                     # returns.py (sales returns), sessions.py (day
│                     # open/close), share.py (UPI QR, share links),
│                     # offline.py (device series + sync), cashiers.py,
│                     # receipt_doc.py (receipt as data for ESC/POS)
├── notes/           # Credit/Debit notes, created from an existing invoice
├── reports/         # gstr.py (aggregation), export.py (Excel/PDF/JSON) -
│                     # GSTR-1/3B working papers, see Roadmap for scope
├── einvoice/        # e-Invoice IRN + e-way bills (Phase 8): rules.py
│                     # (when they apply), schema.py (INV-01 JSON),
│                     # providers/ (NIC sandbox/direct, GSP), vault.py
│                     # (encrypted logins), services.py, eway.py, errors.py
├── restaurant/      # tables, KOT, order types, sold-out (Phase 9)
├── pharmacy/        # substitutes, Schedule H1 / X register (Phase 9)
├── jewellery/       # metal rates, old gold vouchers (Phase 9)
├── jobs/            # job cards, warranty lookup (Phase 9)
├── wholesale/       # customer groups, quantity slabs, salesmen (Phase 9)
├── loyalty/         # points ledger and history (Phase 9)
├── subscriptions/   # the firm's own subscription invoices (Phase 10)
├── onboarding.py    # first-run checklist (Phase 10)
├── monitoring.py    # Sentry, only with SENTRY_DSN (Phase 10)
├── api/              # small JSON endpoints (product lookup today; POS
│                     # cart/checkout from Phase 2) - kept separate so a
│                     # future SPA frontend can reuse them
├── utils/            # gst.py (tax split, inclusive pricing, HSN rules),
│                     # gst_config.py (rate slabs, HSN/AATO thresholds,
│                     # UQC codes - with source + effective date),
│                     # features.py (business types, feature flags),
│                     # permissions.py (per-staff permissions),
│                     # backup.py (`flask backup`),
│                     # numbering.py (atomic per-FY/GSTIN/doc-type counters),
│                     # tenant_scope.py (tenant isolation helper)
├── templates/, static/
migrations/           # Alembic, via Flask-Migrate
tests/
deploy/               # nginx.conf, gunicorn.conf.py
android/              # ARFA POS - native Android wrapper for branch
                      # (Staff) logins only, see android/README.md
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
3. First deploy will build the Docker image, then the container's own
   startup (`deploy/entrypoint.sh`) runs `flask db upgrade` before
   starting Gunicorn - `preDeployCommand` would be the cleaner way to do
   this, but it's a paid-tier-only Render feature.
4. Render's free plan has no Shell tab, so the firm's first login isn't
   created by hand - it's created automatically at startup from env vars.
   Web service → **Environment** tab → set `SUPER_ADMIN_EMAIL` and
   `SUPER_ADMIN_PASSWORD` (and optionally `SUPER_ADMIN_NAME`) to whatever
   you want to sign in with. Saving triggers a redeploy; once it's live
   again, sign in with what you set. (This only *creates* the account -
   changing the env var later won't update an existing one.)
5. Sign in and onboard clients from **Clients → Onboard client** - you set
   each Client Admin's password right there in the form and share it with
   them yourself. Nothing to configure: there's no email step anywhere in
   the app, so there's no separate mail provider to set up.

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

### Phase 10 settings and scheduled jobs

Optional environment variables (nothing breaks without them):

| Variable | What it does |
|---|---|
| `SENTRY_DSN` (+ `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`) | error reports to Sentry; off when unset |
| `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_USE_TLS` (1) | the owner's day summary by email; WhatsApp sharing works without |
| `PROXY_FIX_X_FOR` | set to `1` behind Render / nginx so lockout and limits see the real client IP |
| `ADMIN_2FA_REQUIRED` (1), `ADMIN_IDLE_MINUTES` (30), `LOGIN_IP_LIMIT` (40) | sign-in security for the firm's own logins |
| `ANDROID_RELEASE_REPO` | where the app's update check looks (the APK's `RELEASE_REPO` must match) |

The first time the Ultra Admin signs in after this phase, the app asks
them to set up two-step sign-in (an authenticator app) - have the phone
ready. Lost phone: a recovery code, or `flask reset-2fa EMAIL`.

Daily cron (alongside the expense / recurring-invoice ones):
`flask platform-invoices-generate` (subscription invoices due within the
billing profile's days) and, in the evening, `flask owner-summary-send`.

### Database backups

`flask backup` runs `pg_dump` against `DATABASE_URL`, gzips it to
`backups/arfa-YYYYmmdd-HHMMSS.sql.gz` (or `--dir` / `BACKUP_DIR`) and
keeps the newest 14 (`--keep` / `BACKUP_KEEP`). The password goes to
`pg_dump` through `PGPASSWORD`, never the command line; a failed dump
leaves no file behind. Restore with
`gunzip -c arfa-....sql.gz | psql <database-url>`.

On Render the container disk is wiped on every deploy, so a backup
written there doesn't survive. Instead `.github/workflows/db-backup.yml`
runs nightly (02:00 IST, or on demand from the Actions tab) and keeps
each dump as a 30-day workflow artifact - set the repository secret
`BACKUP_DATABASE_URL` to the database's **external** URL to switch it on
(it skips quietly until then). No cloud-storage upload is wired in yet.

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
- **An item's GST rate is a dropdown of the GST portal's own published
  slabs** (`app.products.forms.GST_RATE_CHOICES`: 0, 0.1, 0.25, 1, 1.5,
  3, 5, 6, 7.5, 12, 18, 28, 40%), not a free-typed number - a typo like
  "2.8" meant to be "28" can't become a real, wrong tax rate on an item.
  0% is a normal choice, not a validation failure: the field uses
  `InputRequired`, not `DataRequired`, since `DataRequired` treats
  `Decimal('0')` as falsy and would otherwise reject nil-rated items.
- **Invoice numbering** is per tenant, per GSTIN, per financial year, per
  document type, with its own editable prefix - allocated under a row lock
  (`SELECT ... FOR UPDATE`) so concurrent checkouts never collide.
- **An unregistered client still needs a business location.** Onboarding
  always creates a `Gstin` row (state + optional address) even when the
  client has no actual GSTIN number (`gstin` is nullable) - POS and
  invoicing bill from that row regardless of whether it carries a real
  GSTIN. A Super Admin can backfill one from a client's detail page for
  any tenant that somehow has none.
- **Credit/Debit notes** are always created from an existing invoice, never
  standalone - they inherit its GSTIN, place of supply, and GST treatment
  (an unregistered/composition client's notes never carry GST, same as
  their invoices). Lines default to a copy of the original invoice's
  lines and are editable, so a partial return/correction is as easy as a
  full one. Each gets its own numbering series (`CN`/`DN` prefixes) and is
  never hard-deleted - voiding works the same way as an invoice.
- **POS checkout prints directly, no preview page or size prompt -
  anywhere.** Confirming a sale posts via `fetch()` instead of a form
  submit, then prints immediately through a hidden iframe at the
  tenant's own `default_receipt_format` (Settings → Print sizes → "POS
  bill print size"). There is deliberately no paper-size picker left in
  the app at all - not after checkout, not on the Day-book, not on a
  bill's own receipt page - every reprint button just reprints at that
  same configured default. Invoices have no size setting to make since
  they only ever render on A4; Settings shows that as a fixed,
  non-editable value next to the POS one so both print sizes are visible
  in one place.
- **Platform branding vs. tenant branding.** `FIRM_NAME` (env var,
  defaults to "ARFA") and `app/static/img/arfa-logo.jpg` are the
  platform's own identity - shown to the Super Admin and on the generic
  `/auth/login` page. Once a Client Admin or Staff user signs in, every
  `firm_name` reference (nav, page titles) and every printed invoice/POS
  receipt switches to that tenant's own `display_name` and uploaded logo
  instead - a client's customer never sees the platform's name on their
  bill. Each tenant also gets its own branded sign-in page at
  `/auth/login/<login_slug>` (shown on their Settings page), themed with
  their own logo.
- **Per-tenant website theme** (`app/utils/theme.py`): Settings lets a
  Client Admin set their button colour, page background colour, page
  text colour, and body font independently (`Tenant.primary_color` /
  `background_color` / `text_color` / `font_family`), applied site-wide -
  nav, buttons, pages - and on their branded sign-in page, not just the
  login screen. Rendered as a CSS custom-property override injected
  after `app.css`; "surface" elements (cards, inputs, the topbar) pin
  their own text color so they stay readable regardless of the chosen
  page background. `text_color` is fully explicit, never auto-derived -
  a live preview in Settings is what catches a bad combination, not
  silent magic; the migration that added it seeded a contrasting default
  for tenants that already had a background set, so nobody already
  themed went instantly unreadable. Font choice is a fixed set of
  OS-available stacks, not a CDN font, to keep the app's offline story
  intact.
- **Uploaded images (tenant logos, product photos) are stored in the
  database**, not on local disk - an `uploaded_images` table
  (`app/models/media.py`), served through `GET /media/<id>`
  (`app/main/routes.py`), not Flask's static handler. An earlier version
  saved files under `app/static/uploads/` instead; that's lost on every
  container restart or redeploy on most hosts (this one included),
  which is exactly the bug this was changed to fix - a client's logo and
  item photos would vanish and need re-uploading after any sign-out/
  sign-in that happened to land after a restart. A database row is as
  durable as the rest of the tenant's data, with no extra infrastructure
  (S3, etc.) needed. Served images are cached hard (`Cache-Control:
  immutable`, a 1-year max-age) since replacing an image creates a new
  row/id rather than overwriting the old one. Product photos are
  center-cropped and resized to an exact 512x512 square on upload
  (Pillow), so a POS tile always shows a cleanly filled square
  regardless of the original's aspect ratio. A PDF (invoice/note) embeds
  the logo as a base64 `data:` URI directly in the HTML WeasyPrint
  renders, since a PDF has no way to reach a database-backed `/media/<id>`
  URL or a filesystem path.
- **Thermal receipts are sized in real inches, not a broken "auto".**
  `app/pos/receipt.py` renders at an actual 2.28in/3.15in page width -
  CSS Paged Media has no "fixed width, auto height" value, so an earlier
  version that tried `58mm auto` was silently ignored by WeasyPrint and
  fell back to a full A4 page every time. The height is seeded from the
  bill's line count and doubled and re-rendered until the content fits
  one page, so a big cart never spills onto a second page and a small
  one never shrinks to an unreadable sliver in a PDF viewer's fit-to-page
  view. Thermal font sizes are bumped well above the A4 copy's (counter
  paper is read up close, not across a desk) and tuned separately per
  width, since the 2-inch roll wraps long values the 3-inch size doesn't.
- **A refund is two entries, not one flipped status.** The Day-book shows
  a refunded sale as its original `+` row (untouched) plus a separate
  `-` row for the refund - summing the two gets you back to zero, same
  as a credit note against an invoice. Refund is a Day-book row action
  (not on the receipt page), since that's where the two entries live
  together.
- **Branches, and stock that transfers through Invoicing, not a separate
  "send stock" screen** (`app/models/user.py` BranchType, `app/models/stock.py`,
  `app/pos/stock.py`, `app/invoicing/services.py`, "Staff" renamed to
  "Branches" throughout). A kitchen-style tenant (one Client Admin cooking
  in bulk, billed out through several branch logins) bills a branch the
  same way it bills any customer - New invoice → Bill to → Branch - and
  that invoice *is* the stock transfer: saving it both issues the
  document and adds to that branch's running stock total for the day, in
  one step, no separate action. A branch is one of two types
  (`User.branch_type`, set when the branch is created): **PCB-owned** is
  the kitchen's own branch, so its invoice is always forced to a
  **Delivery Challan with no GST**, whatever document type was picked in
  the form; **third-party** is an independent reseller, so its invoice is
  always forced to a **Tax Invoice with GST applied as normal**. This
  override happens in `create_invoice` itself, not the route, so it can
  never be the operator's choice or drift from the branch's own type.
  Stock is an append-only ledger (`BranchStockAllocation`: branch,
  product, qty, date) rather than one mutable row - a second invoice to
  the same branch the same day adds to, not replaces, the running total,
  so "resets daily" falls out of every query being scoped to the
  invoice's own date and nothing needs a nightly reset job. "Sold" is the
  sum of `POSBillLine.qty` across that branch login's own `COMPLETED`
  POS bills that day; a refund doesn't currently restore the counter,
  since this app tracks refunds by amount, not by original line
  quantity, and voiding a branch invoice doesn't reverse the transfer
  either - both are documented simplifications, not silent bugs. **There
  is no checkout limit at all**: a branch can keep billing on its POS
  past its allocation - portion sizes vary too much, branch to branch, to
  enforce a hard stop - so the branch's own terminal and the Client
  Admin's Branches list both show a collapsible "Today's stock" counter
  that's purely a tracking signal, and its `remaining` is allowed to go
  negative (shown in red) rather than floored at zero, so an oversell is
  visible rather than hidden. The Branches tab itself is view-only for
  stock now - it tracks and displays, it never sends; the only two ways a
  number there changes are a branch invoice (adds to it) and a POS sale
  on that branch's own login (subtracts from it). Credit/debit notes
  aren't supported against a branch invoice (`customer_id` is only set
  for an ordinary Customer; `CreditDebitNote.customer_id` is required) -
  `create_note` raises a clear error and the invoice view page hides
  those buttons for a branch invoice; voiding still works normally.
  `branch_stock_status()` lists every active product in the tenant's
  catalog, allocated today or not (at 0 sent/0 remaining when not) -
  it used to only list products with an allocation, so an item that had
  never been sent to a branch was silently missing instead of visibly at
  zero. The POS terminal's own counter also refreshes itself the instant
  checkout completes - `checkout_route`'s JSON response carries the
  branch's freshly recomputed `stock_status` alongside the bill it just
  created, and the terminal's own Alpine state is overwritten with it, so
  a cashier doesn't need to reload the page to see what they just sold
  reflected in the running total.
- **Sales report** (`app/reports/sales.py`, Reports → Sales report) is a
  document-level list - invoices and POS bills combined, any date range,
  every GSTIN, any registration type (unlike GSTR-1/3B, which is one
  calendar month, one GSTIN, Regular scheme only) - for a client's own
  records, downloadable as Excel or PDF. The Day-book still covers one
  day of POS activity only; this is the "what did I sell between these
  two dates" view across both channels.
- **A whitespace-only GSTIN counts as no GSTIN.** WTForms' `Optional()`
  validator skips the format check on blank input but doesn't clear
  `field.data`, so a stray space left in the customer GSTIN field used
  to survive as a truthy-but-blank value - silently misclassifying that
  customer as B2B (has a GSTIN) instead of B2C in the GSTR-1 report,
  with an empty-looking GSTIN cell in that section. Fixed at both ends:
  `CustomerForm.gstin` now normalizes (strip/uppercase/blank-to-`None`)
  on input, and `app.reports.gstr.normalized_customer_gstin()` does the
  same defensively when reading a (possibly historical) customer
  snapshot, so existing bad data also reports correctly without a data
  migration.
- **A Reverse Charge Invoice's GST lives in its own columns, never
  `cgst_amount`/`sgst_amount`/`igst_amount`.** Under RCM the recipient
  pays the tax directly to the government, not this tenant - mixing it
  into the same columns a Tax Invoice uses would overstate this tenant's
  own GSTR-3B output tax payable. `Invoice`/`InvoiceLine` carry a second,
  parallel set of columns (`total_rcgst`/`rsgst`/`rigst` and
  `rcgst_amount`/`rsgst_amount`/`rigst_amount`) exclusively for
  `RCM_INVOICE` documents; `create_invoice` computes the tax the normal
  way and then routes it into one set of columns or the other based on
  the resolved document type, never both. `gstr3b_data` deliberately
  sums only the plain columns, so RCM tax correctly never inflates
  `gross_tax_payable`; `gstr1_data` and the sales report fold the two
  sets back together, since GSTR-1 still has to disclose an RCM supply's
  tax for reconciliation and the sales report is just "what did I sell",
  not a liability figure. The invoice view/PDF show a separate
  "RCGST/RSGST/RIGST (reverse charge)" row instead of CGST/SGST/IGST for
  these documents. A migration moved any pre-existing RCM invoices' tax
  out of the shared columns into the new ones so historical data reports
  correctly too.
- **The topbar is two rows, the second only when there's a nav to show.**
  `app/templates/base.html`'s header is a brand+sign-out row (`.topbar-row`,
  always `justify-content: space-between`, so Sign out sits at top right
  on every screen size - the brand shrinks and truncates with an ellipsis
  before this ever wraps) plus a second `<nav>` row rendered only for a
  Super Admin or Client Admin. A branch (Staff) login only ever has one
  destination, POS, so it gets no nav at all rather than a single link on
  its own row - on a narrow phone that used to read as a floating "POS"
  pill with a half-empty header above it. `.main-nav` scrolls
  horizontally instead of wrapping onto several rows, so an admin's full
  link set never grows the header taller than the brand row, on desktop
  or mobile; `.user-name` hides under 640px purely to keep Sign out from
  crowding, not because it stopped mattering.
- **Tenant access gating** (`app/utils/billing.py`) - see the Auditor/
  Sub-Auditor hierarchy bullet below for the full current gate order
  (pending verification, pause, billing overdue - there is deliberately
  no hard expiry date). Checked both at login and on every request of an
  already open session (`app.__init__._register_request_hooks`), not
  just at login. A Super Admin's manual **pause** (`Tenant.is_active`,
  now actually enforced - previously this flag was set by the
  deactivate/activate routes but nothing ever checked it outside the
  tenant's own login-slug lookup, so a deactivated client's users could
  still sign in and work) and a recurring **billing reminder**
  (`Tenant.next_billing_due`, monthly or yearly per `Tenant.billing_cycle`,
  advanced by exactly one more period, from whatever it already was,
  only by the Ultra Admin's "Mark this period paid" - a calendar-correct
  `add_one_month()`/`add_one_year()` so e.g. the 31st of a 31-day month
  lands on the 28th/29th of February rather than overflowing into
  March) share a `BILLING_GRACE_DAYS` (3) window after the due date: an
  in-app banner (`billing_notice` in `base.html`, shown to that tenant's
  own users on every page) warns during the grace window without
  blocking anything; only once the grace window itself elapses unpaid
  does it also block.
- **"Act as"** (`tenants.act_as` / `auth.stop_impersonating`) - full
  Super Admin operational access to a client's own screens (issuing an
  invoice as that client, billing a POS sale under a specific branch)
  without a second, parallel set of admin-only forms to keep in sync
  with the real ones. Clicking "Act as" on a user row in a client's admin
  page signs the Super Admin in as that Client Admin or branch login
  (`flask_login.login_user`), with the admin's own id stashed in
  `session['impersonator_id']`; from that point on `current_user` really
  is that tenant user, so every existing tenant-scoped route, template
  and business-logic function (`tenant_query`, POS, invoicing, stock)
  just works completely unmodified - no "pretend tenant_id" plumbing
  threaded through the app. A banner on every page while this is active
  ("Acting as X - signed in as <admin>") links to "Return to admin
  console", which logs back in as the stashed admin and clears the
  session key. Impersonation deliberately bypasses the tenant-access
  gate above (`enforce_tenant_access` exempts a session carrying
  `impersonator_id`) - the Super Admin needs to actually reach a paused
  or expired client's screens to resolve the problem (e.g. mark them
  paid), and the one gate they're there to lift must not also lock them
  out. Both the start and the stop are written to `AuditLog`. A Super
  Admin can also activate/deactivate any single user - a specific branch,
  or the Client Admin itself - directly from the client's admin page
  (`tenants.activate_user`/`deactivate_user`), the same `User.is_active`
  flag the branch's own Client Admin already used from the Branches tab,
  just reachable from the firm's side too now.
- **Auditor / Sub-Auditor hierarchy and the billing-cycle overhaul**
  replaced the old "every client is billed monthly from onboarding" model,
  which the firm rejected outright. The role enum gained `AUDITOR` and
  `SUB_AUDITOR` (`ALTER TYPE user_role ADD VALUE` inside Alembic's
  `autocommit_block()`, since Postgres can't add an enum value inside a
  normal transaction) sitting between Super Admin ("Ultra Admin") and
  Client Admin/Staff. `User.parent_auditor_id` (self-referential) names
  the Auditor who created a Sub-Auditor; `Tenant.auditor_id` names which
  Auditor/Sub-Auditor a client is currently allocated to (null =
  unallocated). `app.utils.auditor_scope.auditor_tenant_ids(user)` is the
  admin-console counterpart to `tenant_scope.tenant_query` - `None` for an
  unrestricted Ultra Admin, or the concrete set of tenant ids an
  Auditor/Sub-Auditor may see (an Auditor's own clients plus every client
  allocated to any of their Sub-Auditors; a Sub-Auditor's own direct
  allocation only) - and every admin-console route checks it via
  `assert_auditor_owns_tenant`/`assert_auditor_owns_auditor` before acting.
  The shared console (`admin_or_auditor_required`) renders the exact same
  `tenants/directory.html` for an Auditor as it always did for the Super
  Admin, just pre-filtered by scope; the Ultra Admin's equivalent nav slot
  becomes an "Auditors" tab (`tenants/auditors_directory.html`) listing
  every Auditor plus the pending-verification and unallocated-client
  queues. **Billing selection always stays with the Ultra Admin, whoever
  onboards the client.** `tenants.onboard()` behaves identically no matter
  who calls it - it never sets `Tenant.billing_cycle`, so every new client
  starts in the highest-priority gate in
  `tenant_access_status()` ("pending verification",
  `billing_cycle IS NULL`) that blocks sign-in outright, ahead of the
  pause/billing-overdue gates - only `tenants.set_billing_cycle` (Ultra
  Admin only) sets `billing_cycle` (`MONTHLY`/`YEARLY`,
  `app.models.tenant.BillingCycle`), lifting it; the same action changes
  an already-active client's cycle later too (see the next bullet). The
  migration backfills `billing_cycle='MONTHLY'` for every tenant that
  existed before this feature, so nobody already active gets retroactively
  locked out pending verification. The recurring cycle's anchor
  deliberately isn't stamped at onboarding or verification: `Tenant.
  cycle_anchor_date` is set, and the first `next_billing_due` computed
  (`app.utils.billing.advance_billing_cycle` - one month or one year,
  whichever `billing_cycle` says), only inside `auth.change_password` on
  the Client Admin's first successful login after changing the initial
  password the Ultra Admin set for them - never re-stamped on a later
  password change, and never set at all for a Staff/branch login.
  `Tenant.manual_alarm_active` lets the Ultra Admin force the billing
  banner on (`tenants.trigger_alarm`) or silence it
  (`tenants.mute_alarm`) at any time regardless of where the calendar
  cycle actually is - it only ever affects the notice, pause already
  covers an actual cutoff. An Auditor allocating one of their own clients
  to one of their own Sub-Auditors (`tenants.allocate_client_to_sub_
  auditor`) is scoped on both ends - the tenant must already be
  `auditor_id == current_user.id`, and the target must be a Sub-Auditor
  with `parent_auditor_id == current_user.id` - so an Auditor can never
  hand a client to someone outside their own office. Reassigning a client
  to any Auditor/Sub-Auditor, or back to unallocated, is otherwise an
  Ultra-Admin-only action (`tenants.reassign_auditor`) from the client's
  own page. "Act as" extends unchanged to the new roles - an Auditor
  impersonating their own Client Admin reaches the existing branch-
  creation route exactly as the Ultra Admin always could, so "Auditor can
  create branches for companies they manage" needed no new UI at all -
  plus a second impersonation path, `tenants.act_as_auditor`, lets the
  Ultra Admin (or an Auditor, for their own Sub-Auditors only) sign
  straight into an Auditor-hierarchy account the same way.
- **Hard expiry removed; billing cycle is changeable; Auditors are
  billed too.** Three follow-up changes to the round above, all driven
  by the firm's own feedback. First, `Tenant.valid_until` is gone
  entirely (column dropped in the migration, along with
  `tenants.renew_access` and the "Renew / extend" box on a client's
  page) - the firm doesn't want a hard subscription end date at all, only
  pending/paused/billing-overdue. Second, `tenants.verify_client` became
  `tenants.set_billing_cycle`: the same one route and the same always-
  visible "Billing cycle [Monthly/Yearly] [Change]" control on a client's
  page both lifts the initial pending gate *and* changes an already-
  active client's cycle at any later time (e.g. Monthly -> Yearly) -
  it's no longer a one-time "verify" step. Third, an Auditor or
  Sub-Auditor's own account now carries the exact same four billing
  fields a `Tenant` does (`User.billing_cycle`/`cycle_anchor_date`/
  `next_billing_due`/`manual_alarm_active`) - the firm bills an
  Auditor's office the same way it bills a client. `app.utils.billing`
  was generalized accordingly: `_billing_status(subject, ...)` runs the
  same pending/paused/overdue logic against either a `Tenant` or a
  `User`, with `tenant_access_status`/`auditor_access_status` as the two
  thin wrappers that supply the right messages; `advance_billing_cycle`
  already took a generic `subject` so needed no change. Enforcement
  follows the same pattern throughout - `app.auth.routes._login_view`
  and `app.__init__._register_request_hooks` now branch on whether the
  signed-in user is tenant-scoped or is an Auditor/Sub-Auditor and call
  the matching status function; `auth.change_password` stamps
  `cycle_anchor_date`/`next_billing_due` on whichever of "the Client
  Admin's tenant" or "the Auditor's own account" actually applies,
  instead of only ever checking the tenant. A new Auditor or Sub-Auditor
  - created by the Ultra Admin or by another Auditor - starts pending
  (`billing_cycle` null) exactly like a new client, with its own
  "Auditors awaiting verification" queue on the Auditors tab and its own
  verify/change-cycle/mark-paid/trigger-alarm/mute-alarm actions on
  `tenants/auditor_detail.html`, all Ultra-Admin-only, mirroring the
  client-side ones exactly. The migration backfills
  `billing_cycle='MONTHLY'` for any Auditor/Sub-Auditor that already
  existed (the role was introduced one migration earlier), for the same
  "don't retroactively lock out someone already set up" reason as the
  tenant backfill.
- **"Mark this period paid" is idempotent, and never disabled.** The
  original version advanced `next_billing_due` by one cycle on every
  single click with no idempotency check at all, so an accidental
  double/triple-click (or four) silently skipped the tenant several
  cycles into the future - a real billing bug, not a cosmetic one. The
  fix adds `Tenant.last_paid_on`/`User.last_paid_on` (same field on
  both, same reason as every other billing column they share) recording
  the live-calendar date the cycle was last actually advanced, and
  `tenants.mark_billing_paid`/`mark_auditor_billing_paid` check it
  first: `last_paid_on == today` means "already recorded a payment just
  now", a flat no-op regardless of what the date math says. That catches
  the one case a plain `next_billing_due > today` comparison can't - a
  tenant overdue by exactly one cycle length advances, on its one
  legitimate click, to a due date that lands exactly on today, not in
  the future, so a same-day repeat click would otherwise slip through.
  `next_billing_due > today` is still checked too, for "paid ahead,
  come back another day". Importantly, the button itself is never
  hidden, disabled, or swapped for plain text in either state - it's
  always safe to click, so there's nothing to get wrong by trying to
  tell whether now is "the right time" to show it.
- **"Resync to live calendar"** (`tenants.resync_billing_cycle` /
  `resync_auditor_billing_cycle`) is a separate, explicit repair action
  for a tenant/Auditor whose `next_billing_due` has already drifted -
  from the bug above, a hand edit, or anything else. Unlike
  `mark_billing_paid`, which always advances relative to whatever
  `next_billing_due` currently holds (correct for an undrifted cycle,
  but compounds a bad value if it already drifted), this recomputes
  from the one date that never drifts: `cycle_anchor_date`.
  `app.utils.billing.next_due_after(anchor, cycle, today)` walks forward
  from the anchor in whole cycle steps to the smallest boundary still in
  the future - a true resync to where the cycle should actually be
  today, not another relative nudge that could compound an existing
  mistake. Requires both `billing_cycle` and `cycle_anchor_date` to
  already be set (there's nothing to resync against otherwise) and is
  Ultra-Admin-only, same as every other billing action.
- **Switching Monthly <-> Yearly now actually moves the renewal date.**
  `tenants.set_billing_cycle`/`set_auditor_billing_cycle` used to only
  write the new `billing_cycle` value and leave `next_billing_due`
  exactly where it was - correct for the first-time pick (nothing to
  recompute yet, the cycle hasn't started), but wrong for changing an
  already-running cycle: the due date kept reflecting the old cadence
  until the next `mark_billing_paid` click, so a client moved from
  Monthly to Yearly still showed next month's date instead of next
  year's. Both routes now call the same `next_due_after(anchor, new_cycle,
  today)` `resync_billing_cycle` uses whenever the change is to an
  already-started cycle (`cycle_anchor_date` already set) rather than a
  first-time verification - recomputed from the anchor under the *new*
  cadence, not left stale or compounded onto the old due date.
- **Ultra Admin resets an Auditor's (or any of their Sub-Auditors') own
  login password** (`tenants.reset_auditor_password`) - the same direct,
  no-OTP "set it yourself, they change it on next sign-in" pattern
  `tenants.reset_client_password` already uses for a Client Admin/Staff
  login, just for the Auditor-hierarchy accounts themselves.
  Ultra-Admin-only and unscoped (no `assert_auditor_owns_auditor` check,
  unlike viewing/act-as) - deliberately reachable for *any* Auditor or
  Sub-Auditor, not just ones an Auditor manages themselves, since this
  is the same kind of action billing already is: it always stays with
  the Ultra Admin regardless of who created or allocated the account.
  Reachable from the auditor's own page and, for an Auditor's view of
  their Sub-Auditors table, from each Sub-Auditor's row directly.
- **Held bills live in a persistent left sidebar on the POS terminal**,
  not a collapsible panel - the whole point is that a parked bill is
  never forgotten mid-shift, so it has to stay visible by default rather
  than behind a toggle a cashier has to remember to open. `pos.terminal`
  already queried every `HELD` bill for the day-book/resume flow; the
  template change is purely layout (`.pos-layout` grows a third,
  leftmost grid column) with no new backend logic - the list "resets"
  only in the sense that a new day's held bills are a new, empty query,
  same as before.
- **Item categories/sub-categories are opt-in per tenant**
  (originally `Tenant.category_wise_items`, now the `categories` feature
  flag - see Phase 1 below; toggled from Settings). Some clients
  want a flat item list, others want to browse by Category ->
  Sub-category, so `Category`/`SubCategory` (`app/models/category.py`)
  are normal normalized tables rather than free-text fields on
  `Product` - letting a tenant manage them once (`products.categories`)
  and reuse consistent names everywhere, including in the POS grouping.
  Both fields stay optional on `Product` even with the toggle on (a
  client's catalog is rarely perfectly categorized on day one), so
  `app.pos.catalog.build_category_tree` buckets every product into
  "in a sub-category", "loose in a category", or "uncategorized" -
  including a product whose category or sub-category was since
  deactivated, which falls back to uncategorized rather than
  disappearing. The POS terminal renders two alternate product
  layouts from the same `PRODUCTS` data (`app/templates/macros.html`'s
  `product_js` macro keeps both in sync): a flat grid when the toggle
  is off or a search is active, and the collapsible Category ->
  Sub-category tree (with "Expand all"/"Always collapse" controls)
  when it's on - both exist in the server-rendered HTML always, since
  Alpine's `x-if` only ever decides which one is *visible*, never which
  one is *sent*.
- **Reports share one filter bar** (`app.reports.filters.parse_report_filters`,
  included via `templates/reports/_filter_bar.html`) across the Sales,
  Stock, and Sales-by-item reports, rather than three separate ad hoc
  date-range forms. A period is one of day/month/custom range (`?mode=`),
  and a branch filter is zero or more Staff logins (`?branch_id=`,
  repeatable) - `None` means "every branch". `app.reports.sales.sales_report_data`,
  `app.reports.stock.stock_report_data` (generalizing the POS
  terminal's today-only, single-branch stock counter in
  `app.pos.stock` to any date range and any branch selection), and the
  new `app.reports.items.item_sales_data` (merging `InvoiceLine` and
  `POSBillLine` by product, falling back to grouping by description for
  a one-off typed line with no `product_id`) all take the same
  `(tenant, start, end, branch_ids)` shape, so the filter bar's query
  string works identically on all three pages and round-trips straight
  into the Excel/PDF export links on the Sales report.
- **The top-nav Live dashboard** (`app/live/`, Client Admin only) is a
  separate thing from Reports on purpose: Reports answers "what
  happened in this period", Live always means "right now, today" -
  there's no filter bar, just a `/live/data.json` snapshot the page
  polls on an interval. `app.live.metrics.branch_leaderboard` ranks
  every active branch by today's total POS revenue (the chosen
  metric - order count rides along for context but never breaks a
  tie), including a branch with zero sales so it visibly shows up as
  idle rather than dropping off the board. The F1-style "watch it
  move" effect is pure CSS: each branch row keeps the same DOM node
  across a refresh (Alpine's `x-for` `:key="b.id"`), so re-sorting the
  underlying array only changes that node's `top` offset, which
  `transition: top 0.6s ease` animates into a slide instead of a jump.
  The best-seller pie chart is a hand-rolled SVG donut (stacked
  `<circle>` segments via `stroke-dasharray`/`stroke-dashoffset`, no
  charting library - this app loads no CDN scripts) built as a markup
  string and injected with `x-html`, because Alpine's `x-for` template
  cloning doesn't handle the SVG namespace correctly (it throws on
  `document.importNode` for SVG children) - `x-html`'s plain
  `innerHTML` assignment sidesteps that entirely. `app.live.metrics.item_sales_today`
  computes each item's revenue share from POS sales only (not
  invoices - a live floor dashboard is about till activity), and the
  stock panel reuses `app.reports.stock.stock_report_data` for today,
  all branches.
- **Fixed the POS cart row's misalignment** - traced to a global form-
  reset rule, `input[type="number"] { width: 100%; min-height: 48px }`
  (`app/static/css/app.css`), which combines an attribute selector with
  the element type and so outranks the single-class `.qty-input`
  selector that was supposed to size it, silently stretching the
  quantity box to fill the row regardless of what was written there.
  `.cart-line` moved from flex to a `minmax(0, 1fr) <fixed> <fixed>`
  grid with the item name ellipsis-truncated to one line (so a long
  name can't make just that row taller than its neighbors), and the
  quantity input is now scoped as `.qty-stepper .qty-input` - two
  classes, enough specificity to actually win.
- **`android/` - ARFA POS, a native Android wrapper for branch logins
  only.** A full-screen WebView around the same server-rendered POS
  pages (not a reimplementation - see `android/README.md`), gated so
  only a Staff login is let in: `MainActivity.kt` reads which page
  `app.auth.routes._post_login_redirect` actually sent the signed-in
  user to (a branch login always lands on `/pos/`) and signs anyone
  else straight back out, rather than duplicating the server's role
  logic. Android's WebView has no built-in PDF viewer or
  `window.print()`, unlike a desktop/mobile browser, so printing a
  receipt there goes through one small additive change in
  `app/templates/pos/terminal.html`'s `printReceipt()`: a
  feature-detected `window.AndroidPrint` bridge (present only inside
  the app's WebView, `addJavascriptInterface`-injected by
  `MainActivity.kt`) that downloads the same server-generated PDF and
  hands it to Android's native `PrintManager`, instead of the hidden-
  iframe `window.print()` every other browser still uses unchanged.
  Built via `.github/workflows/android-apk.yml` on GitHub's own
  runners, not locally - the Android SDK, the Android Gradle Plugin,
  and every AndroidX/Material dependency all resolve through
  `dl.google.com` (`maven.google.com` just redirects there), which this
  project's own sandbox network policy has no route to.

### Platform build - Phase 1 (business profile, pricing, item master)

- **Business type + per-client feature flags** (`app/utils/features.py`).
  `Tenant.business_type` (retail, restaurant, pharmacy, ...) only
  *suggests* modules: picking one at onboarding applies a preset, and in
  Settings a separate "also switch modules" tick re-applies it - changing
  the type alone never flips anything. Flags live in one JSON column,
  `Tenant.features`, rather than a boolean column per module, because
  the next nine phases add a dozen of them. The old
  `category_wise_items` column was folded in as `features.categories`;
  the migration (`306ce885a627`) marks every existing client RESTAURANT
  and copies its categories setting across, so nothing changes for them.
  A flag is enforced in two places: `feature_required(key)` on routes
  (404 when off - the module doesn't exist for that client) and the
  `has_feature()` Jinja global for menus/fields. Only flags whose module
  is actually built are `available` and appear in Settings; the presets
  already store the rest so they light up as their phases ship.
- **Tax-inclusive pricing, in the one GST service.**
  `app.utils.gst.compute_line(..., price_includes_tax=True)` backs GST
  out of the line total instead of adding it: net = qty x rate less
  discount (each rounded half-up), taxable = net x 100 / (100 + rate),
  tax = net - taxable. Intra-state, CGST is rounded and SGST takes the
  remainder, so the bill total is exactly the shelf price even when that
  makes the two differ by a paisa; the exclusive path is untouched
  (CGST = SGST, as before). The flag is stored per line
  (`InvoiceLine`/`POSBillLine`/`NoteLine.price_includes_tax`) so a
  reprint or note always recomputes the same way, and the PDFs mark such
  lines with an asterisk. `app/static/js/gst_calc.js` mirrors the same
  rounding for the on-screen preview (POS cart, invoice and note forms);
  it is display-only - the server always recomputes. Clients default to
  exclusive; `Tenant.default_price_includes_tax` only pre-ticks new items
  and lines.
- **Rates and thresholds live in `app/utils/gst_config.py`**, each with
  its notification and effective date: the rate slabs (including the
  40% slab from 22 Sep 2025), the HSN-digit rule (4 digits up to Rs. 5
  crore AATO, 6 above - Notification 78/2020-CT), and the 45 UQC unit
  codes from the GST portal. Logic never hardcodes a rate or threshold.
  HSN length is checked on the item form and bulk import against the
  client's `Tenant.aato` (blank = treated as under Rs. 5 crore); a
  service must use a SAC (`99...`).
- **Item master** gains SKU and barcode (unique per client, named
  constraints), MRP (sale price can't exceed it once GST is included),
  wholesale/purchase/cost prices, a UQC unit with an optional pack unit
  + conversion factor, low-stock level, "is a service" and "allow billing
  at zero stock" (stored now, enforced when stock tracking ships).
  `default_price` was renamed `sale_price`. Units used to be free text:
  an item keeping an old value (e.g. "plate") can still be saved
  unchanged, but new items and changed units must be a UQC code. The
  extra fields sit in a collapsed "More details" section so a small
  shop's form looks no busier than before.
- **Bulk Excel import/export** (`app/bulk/`). Each kind is one
  `ImportSpec` (columns, parse, save, export) so suppliers can be added
  when that master exists. Upload never saves: it shows every row as
  add / update / skip with the reasons, then "Import" re-reads the file
  (kept in the server-side session, tied to the tenant) and re-validates
  against the database as it is at that moment. Code columns are
  text-formatted in the template, and HSN codes that Excel stripped a
  leading zero from (901 -> 0901) or GST rates entered as a percentage
  cell (0.18 -> 18) are repaired rather than rejected. Rows match an
  existing item by SKU then name, a customer by GSTIN then name -
  always within the tenant only.
- **Per-staff permissions** (`app/utils/permissions.py`, Branches ->
  Permissions): give discounts (with a max %), change price at billing,
  refund, void, see Day-book/Z-report, adjust stock. Defaults equal
  what a branch login could already do, so nothing changes until the
  owner edits them; Client Admin is always allowed everything. They're
  enforced on the server, not just hidden: the POS now ignores a
  client-posted rate unless the login may change prices (and rejects
  products from another client by id), caps discounts, and refund /
  Day-book / Z-report routes return 403. Void and stock permissions are
  stored but have nothing to guard until those flows exist.
- **Bugs fixed along the way:** the POS preview added GST on top for
  composition-scheme clients (the saved bill was right; the cart total
  shown wasn't), and the invoice form's preview treated a Delivery
  Challan as taxable.

### Platform build - Phase 2 (POS for every business)

- **Discounts at the counter, capped by the login.** A line can take a %
  or a rupee discount and the whole bill can too. A rupee amount means
  "off what the customer pays", so on a pre-tax price it is converted
  first (`gst.price_basis_amount`). A bill discount is spread over the
  lines in proportion to what each costs, with the rounding remainder on
  the biggest line (`gst.allocate_amount`), because tax is always worked
  out line by line. What the GST is computed on is stored per line:
  `discount_percent` + `discount_flat` (+ the discount as entered, for
  the receipt and for resuming a held bill). The cap is checked on each
  line's total discount, with one paisa of slack for allocation rounding.
  The cashier only sees discount controls if their login may give them;
  a Client Admin always can.
- **Price edit and price lists.** A line's price is the item's price, or
  its price on the counter's price list, unless the login may change
  prices. Price lists (`multi_price_list`, Items -> Price lists) are
  named sets of item prices, assigned per branch login under Branches ->
  Permissions. Only a login that may change prices can switch lists at
  the counter. An item missing from a list sells at its normal price.
  Switching a list off puts its counters back on normal prices rather
  than blocking billing.
- **Split payments (`POSPayment`).** Every completed bill now has one
  payment row per way it was paid, adding up exactly to the grand total.
  `POSBill.payment_mode` stays filled in (`MIXED` for a split) so
  anything reading it still works. The migration back-fills one row for
  every existing bill, so reports read old and new bills the same way.
  The Z-report splits sales by those rows. A Client Admin picks which
  modes the counter offers in Settings -> Counter; Cash/UPI/Card stay the
  default, so existing counters look the same. Credit (pay later) needs
  the `credit_sales` module and a named customer. Cash received and
  change are stored on the bill and printed.
- **Dynamic UPI QR**: `upi://pay` with the payee, the amount and a note,
  drawn server-side by `segno` (`app/pos/share.py`). No JS library, and
  no payment network is called - the cashier still confirms the money
  arrived. It appears only once a UPI ID is set in Settings. On the
  printed receipt it appears only for an amount still owed (a credit
  part), because a pay-now QR on a paid bill invites paying twice.
- **Customer on a bill**: search by phone/name, or quick-add (name,
  phone, GSTIN optional; state taken from the GSTIN or the shop's own).
  A customer with a GSTIN makes a B2B POS bill, which GSTR-1 already
  files under B2B by its customer snapshot.
- **Barcode and keyboard.** A keyboard-wedge scanner is caught by a
  buffer of very fast keystrokes ending in Enter, outside any input.
  Enter in the search box also adds an exact barcode/SKU match.
  Shortcuts: F2 search, F4 checkout, F8 hold, +/- last line, Esc
  close, `?` shows the cheat sheet. Items in a weighed/measured unit
  (`gst_config.LOOSE_QTY_UQC`: KGS, LTR, MTR...) open a keypad for a
  decimal quantity (Numeric(12,3)).
- **Favourites row** (`favourites`): pinned items (item form) first,
  then the 30-day best sellers, up to 8 (`app.pos.catalog`).
- **Item-wise sales return / exchange (`POSReturn`).** Its own fiscal
  document with its own row-locked series (`SR/<FY>/0001`), never
  deleted, voidable the same day with a reason, and audit-logged. Amounts
  come from `gst.return_line_amounts`: a part return is recomputed from
  the sold line's own inputs (rupee discount pro-rated by quantity), and
  the return that empties a line takes exactly what is left. However the
  goods come back, the returns add up to the original line to the paisa.
  The original bill is never changed. A return reduces sales, item and
  stock reports and GSTR-1/3B in the period the goods came back. A B2B
  return is also listed with the credit notes. The branch stock counter
  counts returned goods as back on the shelf. Paid back by any counter
  mode, or kept as exchange credit that a later bill spends through an
  `EXCHANGE` payment (row-locked, with a running balance). The old
  amount-only refund still works for bills with no item returns.
- **Void** (needs the `void` permission): same day only - or, with day
  close on, only while that counter day is open - and only with no
  returns against the bill. The number stays used; the bill shows as
  cancelled in the GSTR-1 document summary and drops out of sales.
- **Day open / close per counter** (`day_close`, `CashSession` +
  `CashMovement`). With it on, a counter opens the day with its cash
  before billing (enforced in checkout, refund and return).
  Expected cash = opening + cash payments - cash refunds - cash paid back
  on returns + cash in - cash out. Closing stores the count and the
  difference, and locks the day: its bills can't be voided, and goods
  coming back are a return in a later day. A refund lands in the day it
  is paid out (`refund_session_id`). A partial unique index allows one
  open day per counter. The Z-report adds a counter-days table. Off, none
  of this is consulted.
- **Share and copies.** The bill PDF is shared through a signed,
  expiring link (itsdangerous on SECRET_KEY, 7 days,
  `BILL_SHARE_LINK_DAYS`) inside a `wa.me` click-to-chat message - no
  WhatsApp API. The Android app hands `wa.me` links to WhatsApp
  (`MainActivity.shouldOverrideUrlLoading`), since its WebView can't open
  windows. The print straight after checkout asks for the original
  (`original=1`), which is given out once (`original_printed_at`). Every
  later print says DUPLICATE COPY, and the shared copy says CUSTOMER COPY.
  "Reprint last bill" sits in the POS header.
- **Unchanged for existing clients unless they switch something on:**
  the new modules are off, the payment buttons are the same three,
  staff can't discount or edit prices by default, and an old-style
  single `payment_mode` post still works. Deliberate visible changes:
  the cart total now shows the rupee-rounded amount the bill will
  actually charge, and a reprint is marked DUPLICATE COPY.

### Platform build - Phase 3 (inventory and purchases)

- **One append-only stock ledger (`StockMovement`), posted for every
  client.** Stock at a place is the sum of its movements. No other
  column stores a stock number, so nothing can drift. Movements are
  never edited or deleted. Voiding a document posts the opposite of each
  of its movements, with `reversal_of_id` pointing back
  (`ledger.reverse_source`, safe to call twice). Every POS sale, item
  return, refund, void, invoice, purchase, purchase return, transfer and
  adjustment posts here, whether or not the Inventory module is on. That
  way the history is complete on the day a client switches it on (they
  then set their real counts with a physical-count adjustment). The
  module only adds the screens and the "can't sell below zero" rule. So
  existing clients see no change: nothing is refused, and nothing new
  appears on their screens.
- **Locations (`StockLocation`).** Each client has a main store (made the
  first time it's needed), any godowns or stores they add, and one
  location per branch login, made automatically. The main counter and
  invoices sell from the main store. A branch login's counter sells from
  its own location. An invoice billed to a branch is how stock reaches
  it, so it posts as a transfer from the main store into the branch. The
  restaurant's daily branch counter (`BranchStockAllocation`) keeps
  working exactly as before, and is still fed by branch invoices.
- **Documented gaps fixed.** Voiding a branch invoice now takes back its
  daily branch allocation (negative allocation rows, so the allocation
  history stays append-only too) as well as its ledger movements. Item
  returns put goods back on the shelf of the counter that took them back,
  into the batches and serial numbers they were sold from. Voids and
  full refunds reverse the sale.
- **No negative stock, only when asked for.** With Inventory on, an item
  whose "Allow billing even when stock shows zero" box is unticked can't
  be sold, transferred or returned to a supplier below zero at that
  location. This is checked before a number is allocated
  (`ledger.check_available`). Items left ticked (the default) behave as
  before.
- **Weighted-average cost.** Goods coming in move `Product.cost_price` to
  (stock x old cost + qty x new cost) / (stock + qty). Stock at or below
  zero adds nothing to the average, because its cost is unknown. Voiding
  a purchase or returning goods to the supplier undoes this at the cost
  the goods came in at. Goods going out are valued at the current
  average, and stock value is on-hand x average cost.
- **Purchase bills (`PurchaseBill`, `PB/<FY>/0001`)** are posted when
  saved: stock in, cost updated, ITC stored on the bill. They are never
  deleted and are voided with a reason. A bill can't be voided while it
  has returns, or after one of its serial numbers was sold. The line math
  is the same `gst.compute_line` the sales side uses. Purchase-only rules
  are in one module, `app/utils/itc.py`:
  - **ITC eligibility per line**: eligible, ineligible, or blocked under
    s.17(5). Only eligible GST comes back as credit. Ineligible GST
    becomes part of the goods' cost.
  - **Who can claim**: only a regular GST registration with a GSTIN
    (s.16). For unregistered and composition clients
    (`purchases.services.can_claim_itc`), every line is forced to
    "No ITC" and the GST is part of the cost. The form says so.
  - **Reverse charge**: the tax is computed and reported, and credited
    if eligible. What's owed to the supplier excludes it.
  - **Landed cost**: freight and other charges are spread over the lines
    by taxable value (`gst.allocate_amount`). Unit cost = (taxable value
    + share of charges + GST that won't come back) / qty, to 4 decimal
    places.
  - **CGST+SGST or IGST** depends on whether the supplier's state is the
    receiving GSTIN's state.
  - The same supplier invoice number can't be entered twice.
  - The supplier's bill (PDF/JPG/PNG, 5 MB, content-checked) is stored in
    `uploaded_images` with `is_private`. The public `/media/<id>` route
    refuses private rows. It is served only through the bill's own
    tenant-checked route, with `Cache-Control: private, no-store`.
- **Returns to the supplier / debit notes (`PurchaseReturn`,
  `PDN/<FY>/0001`).** Amounts come from `gst.return_line_amounts`, so
  however the goods go back, the returns add up to the bill line to the
  paisa. The credit those goods gave is reversed. Serial-tracked units
  are picked from the bill's unsold ones. A return can be voided, which
  puts the goods back.
- **Purchase orders (`PO/<FY>/0001`)** have no tax effect. "Goods
  arrived" opens a pre-filled purchase bill, and posting it marks the
  order converted. Open orders can be cancelled with a reason. The
  reorder list can start an order with suggested quantities.
- **Suppliers master**: GSTIN (its first two digits must match the
  state), contact, credit days, opening balance. Bulk import/export uses
  the same Excel flow as items and customers. The spec carries
  `feature="purchases"`, so it 404s with the module off.
- **GSTR-2B import and match** (`app/purchases/gstr2b.py`) reads the
  portal's JSON (`data.docdata.b2b`) or Excel (B2B sheet, two-row
  headings, one row per tax rate merged per invoice).
  - **Match rule**: same supplier GSTIN, and the same invoice number
    after normalising (`INV/0042` = `inv-42` = `INV0042`), with values
    within Rs. 1. That gives Matched; a different value gives Mismatch.
  - **Fuzzy match**: an invoice number written differently (difflib
    ratio >= 0.8) is a match with a note.
  - **Only in 2B** means no bill was entered. **Only in books** means a
    posted bill of the month that no row matched.
  - **Reverse charge**: RCM rows are matched only to RCM bills and listed
    separately. RCM bills have their own register too
    (Purchases -> Reverse charge), never mixed into the regular one.
  - **Manual changes**: matches made by hand survive "Match again", and
    any match can be undone.
- **ITC in the GSTR-3B working paper** appears only with Purchases on, so
  everyone else's working paper and its JSON are unchanged. It adds:
  - 3.1(d), reverse-charge inward supplies
  - 4A(3), ITC on those reverse-charge purchases
  - 4A(5), all other ITC
  - 4B(1), ineligible / blocked GST, availed and reversed
  - 4B(2), credit of goods returned to suppliers
  - 4C, the net (4A - 4B)

  Bills count by the date they were entered. The page says to claim only
  what GSTR-2B shows.
- **Stock documents.** Adjustments (`ADJ`) cover opening stock, physical
  count, damage, wastage / expired, found, and other corrections (a note
  is required). A physical count is entered as the counted number, and
  the difference is posted. Transfers (`TDC`) move goods between
  locations, with a Delivery Challan PDF (Rule 55, valued at cost, "not a
  sale"). Both are numbered from the row-locked allocator, never
  deleted, voided with a reason, and audit-logged.
- **Batches and expiry (`batch_expiry`).** For items marked "track
  batches", purchases need a batch number (with mfg/expiry/MRP). Sales
  and transfers take stock earliest-expiry first (FEFO) and skip expired
  batches. A sale that would need expired stock is refused. Near-expiry
  and expired stock is listed per location (Stock -> Expiry). With the
  module off, batches are neither recorded nor enforced.
- **Serial / IMEI (`serial_imei`).** For items marked "track serial",
  purchases need one serial per unit, unique per client. At the counter,
  each such line asks for its serials, from a list of the units in stock.
  The sale checks that each serial exists and is in stock, then marks it
  sold against the bill line. The receipt prints `S/N: ...` under the
  item. Voids put units back in stock. The warranty end date is the
  purchase date + the item's warranty months. Stock -> Serial / IMEI
  answers "where is this IMEI".
- **Variants (`variants`).** An item can be turned into a design with a
  size x colour matrix (two attributes, up to 200 combinations). Each
  variant is a full item of its own: its own SKU (design SKU + codes),
  barcode, price and stock, with `parent_id` pointing at the design. The
  design itself (`has_variants`) is never sold or stocked: it is hidden
  from the POS, the invoice form and purchases. It can only become a
  design while its own stock is zero.
- **Reports** (Stock tab), all read straight off the ledger:
  - stock summary with value at cost, and a low-only filter
  - stock ledger per item, with opening/closing and running balance
  - stock by location (godown-wise)
  - reorder list: items at or below `min_stock_level`, suggesting an
    order back up to twice the minimum, with the last supplier and rate
  - fast / slow / dead stock over 30-365 days: the top third by quantity
    sold is fast, the other sellers slow, and stock that never sold dead

  Low-stock items also show on the Live dashboard (only with Inventory
  on).
- **Barcode labels** (`app/inventory/labels.py`): python-barcode SVG
  inside WeasyPrint. EAN-13 is used when the item's barcode is a valid
  EAN-13, otherwise Code 128. Items with no barcode get their SKU, else
  `ARFA000123`, which the POS scanner also recognises. Layouts are an A4
  sheet (3 x 8 labels of 70 x 37 mm) or a 50 x 25 mm roll. Each label
  has the name, code, price and MRP.
- **Flags.** Every new screen is gated by `feature_required`, which
  returns 404 when the module is off; staff logins get 403. Nav links
  render only with the flag. The product form shows the track-batch /
  track-serial / warranty fields only with their module, and keeps the
  stored values when the module is off.
- **Tenant isolation.** Every new table has `tenant_id`, is read through
  `tenant_query`, and is checked by `tests/test_phase3_isolation.py`. That
  test covers guessed ids in every URL (404), B's ids smuggled into A's
  forms (refused), lists, and `tenant_query` for each model.

### Platform build - Phase 4 (expenses)

- **Expense vouchers (`Expense`).** A posted expense is numbered
  `EXP/<FY>/0001` from the same row-locked allocator as every other
  document. It is never deleted: it is voided with a reason, and every
  post and void is audit-logged. A **draft** has no number. Numbers are
  given only on posting, so drafts never leave gaps in the series. A
  draft that isn't wanted is "skipped", which voids it with a reason, so
  its recurring expense doesn't make it again.
- **Categories (`ExpenseCategory`)** are seeded on first use with Rent,
  Salary & Wages, Electricity, Fuel, Transport, Repairs,
  Telephone/Internet, Professional Fees, Bank Charges, Packing Material,
  Advertisement, Office Expenses and Miscellaneous. They are committed
  straight away, so the ids on the page are real: a first-visit form
  once had uncommitted ids, and the regression test posts using only
  what the page offers. Clients can add, rename and switch off
  categories. A switched-off category keeps its past expenses.
- **An entry records:** date, category, payee (free text or a supplier
  when Purchases is on), amount, payment mode (cash / UPI / card /
  bank / cheque / not paid yet), a reference, the branch (head office
  when empty), notes, and a bill photo/PDF. The bill is stored privately
  like a supplier's bill: the public media route refuses it, and it is
  served only through the expense's tenant-checked route.
- **GST bills use the existing tax module.** GST on an expense is
  computed by `itc.compute_expense_gst`, a thin layer over
  `gst.compute_line`, so all Decimal ROUND_HALF_UP rounding is shared
  with sales and purchase bills. An expense carries one rate.
  - The amount can be the bill total with GST inside it, or the taxable
    value.
  - CGST+SGST or IGST follows the supplier GSTIN's state code.
  - Under reverse charge the payee is paid the taxable value only, and
    the tax is still computed.
  - Eligible / ineligible / blocked u/s 17(5) works per expense, as on a
    purchase line.
  - Only a regular registration with a GSTIN can claim ITC
    (`purchases.services.can_claim_itc`). Composition and unregistered
    clients get "No ITC", and the GST is part of the cost.
  - A forward-charge GST bill needs the supplier's GSTIN, as credit
    requires one on the bill (s.16(2)).
- **Recurring expenses (`RecurringExpense`)**: weekly, monthly,
  quarterly or yearly, with an optional end date.
  - **Drafts**: `generate_due_drafts` makes one draft per due period.
    A unique (`recurring_id`, `due_date`) constraint and the advancing
    `next_due_date` mean it can run any number of times without
    duplicating anything. It catches up at most 36 periods per run.
  - **Due dates**: monthly dates stay on the start day, or the month's
    last day when that month is shorter (31 Jan -> 28 Feb -> 31 Mar).
  - **When it runs**: on every visit to the Expenses page, and from cron
    with `flask expenses-generate-drafts`.
  - **Checking a draft**: the client adjusts the amount, adds the GST
    bill if any, and posts it.
  - **Stopping and restarting**: a restarted recurring expense doesn't
    back-fill the periods it was stopped for.
- **Petty cash from the POS drawer.** With Expenses on, a counter's
  "Cash out" (Phase 2 day open/close) is booked as a posted cash expense
  of that counter. The branch is the counter's own; a client admin's
  counter is head office. It is filed under Miscellaneous unless the
  cashier picks another category. "Not an expense (sent to bank / taken
  by owner)" books nothing. Each cash movement can make at most one
  expense (unique `cash_movement_id`). With Expenses off, cash out works
  exactly as before.
- **Expense report** (Expenses -> Report): posted expenses only. It is
  grouped by category, branch and month, with GST and ITC totals. It
  uses the same period/branch filter bar as the sales reports, plus a
  category filter. The Excel download has the expenses plus by-category,
  by-branch and by-month sheets; there is also a PDF.
- **ITC in GSTR-3B.** A GST-bill expense counts like a purchase bill, by
  its expense date:
  - its tax is availed in 4A(5) (4A(3) and 3.1(d) under reverse charge)
  - whatever isn't credit is reversed in 4B(1)

  The ITC section now shows when Purchases or Expenses is on. With both
  off, the working paper and its JSON are unchanged.
- **Flag.** Every expense screen is behind `feature_required("expenses")`,
  which returns 404 when off; staff logins get 403. The nav link and the
  POS category picker render only with the flag.
- **Tenant isolation.** Each new table has `tenant_id` and
  `tenant_query`, and `tests/test_expenses.py` proves:
  - guessed ids 404 on every route
  - another client's category, branch or registration is refused in a
    form
  - drafts are only ever made from a client's own recurring expenses

### Platform build - Phase 5 (margin scheme, Rule 32(5))

- **Who it's for.** Dealers in second-hand goods (used phones,
  vehicles...). The module is `margin_scheme`. An item is marked "sold
  under the margin scheme" on its item form, which also makes its price
  tax-inclusive: the buyer pays the price and GST is never added on top.
  It only applies to a regular GST registration.
- **One tax module, `app/utils/margin.py`** (Decimal, half-up, fully
  unit-tested):
  - margin = selling price - purchase price
  - a margin of zero or less means taxable 0 and GST 0
  - otherwise the margin is tax-inclusive: taxable = margin x 100 /
    (100 + rate), GST = margin - taxable
  - GST is split CGST/SGST (half-up, with the odd paisa on SGST) or IGST
    by place of supply

  A sale line is worked out lot by lot, so a loss on one unit never
  cancels the tax on another.
- **Purchase price is always tracked (`MarginAllocation`).** Each unit
  sold is tied to the purchase bill line it came from:
  - with `serial_imei`, the IMEI picked at the counter, which sets that
    unit's own price
  - otherwise the oldest margin-scheme purchase with units left (FIFO)

  The unit purchase price is what was paid for the line, GST included
  (no credit is taken on it), per unit. A unit with no purchase on
  record can't be sold under the scheme. One allocation row per (sale
  line, lot) is kept, and that is the margin register:
  - rows are never deleted
  - voids and refunds release them
  - sales returns count the units that came back, and voiding the
    return undoes that
  - a purchase bill can't be voided once its margin goods were sold
  - a purchase return can't send back units already sold
- **No ITC on margin purchases.** A purchase line for a margin-scheme
  item is forced to "ineligible" and flagged `is_margin_scheme`, so it
  never feeds GSTR-3B table 4.
- **Separate invoices only (the client's decision).** A sale that mixes
  margin-scheme and regular items is refused, at the counter and in
  Invoicing, before a number is used, with "bill them in two sales". An
  all-margin sale is an **Invoice - Margin Scheme** in its own series
  `MSI/<FY>/0001`, shared by POS and Invoicing for each GSTIN.
  - **What's stored on a line:** taxable value and tax are the margin's,
    the line total is the selling price, and the purchase value and
    margin are kept too.
  - **Document totals** (`compute_margin_totals`): the grand total is
    the sum of the prices.
  - **Printing:** the POS receipt and the invoice PDF show item and
    price only - no taxable value or GST - under a "Margin Scheme"
    title, with the declaration. The declaration text is editable in
    Settings; blank means the standard wording.
  - **Restrictions:**
    - Invoicing refuses margin goods to a branch, as an export / RCM /
      bill of supply, and serial-tracked margin goods (those are sold at
      the counter, where the IMEI is picked).
    - Credit/debit notes against a margin invoice are refused; void and
      re-bill instead.
- **GSTR-1.** B2B/B2C is split by the buyer's GSTIN as usual, at the
  taxable margin. The HSN summary gets two rows per HSN:
  - the taxable margin at the item's rate, with its quantity and tax
  - a 0% row (no quantity) with the rest of the sale value (sale value -
    margin), labelled "Margin scheme - value other than margin" in the
    page, PDF and Excel

  The two rows' totals add up to the full sale value; returns and
  refunds reduce both. "Documents issued" lists the MSI series once,
  covering POS and invoices. GSTR-3B counts the taxable margin and its
  tax only.
- **Margin register** (Reports -> Margin register, Excel/PDF):
  - columns: item and IMEI, purchase date / bill / supplier / price,
    sale date / invoice / price, margin, taxable margin, CGST/SGST/IGST
  - a loss is shown as such and not taxed
  - returned units are left out, and so are voided and refunded sales
- **Flag.** With `margin_scheme` off, the item flag is ignored: such
  items sell as regular goods, exactly as before. The register returns
  404, and the item-form and Settings fields aren't shown. Tenant
  isolation: lots and register rows are per client
  (`tests/test_margin_scheme.py`).

### Platform build - Phase 6 (accounts, pre-sale documents, recurring invoices)

Five new flags, all off by default; with them off nothing changes (routes
404, no menu, no extra fields, GSTR output byte-for-byte the same):
`receivables`, `payables`, `sales_documents`, `recurring_invoices`,
`cash_bank_book` (Accounts and "Quotes & orders" in the menu).

- **Balances are never stored.** What a customer or supplier owes is
  worked out from the documents each time (`app/accounts/receivables.py`,
  `payables.py`), so it can't drift:
  - customer: opening balance + issued invoices (not branch challans; an
    RCM invoice without the tax the customer pays the government) +
    debit notes - credit notes + the credit part of counter bills -
    credit-mode returns/refunds - receipts + refund vouchers
  - supplier: opening balance + posted purchase bills - purchase returns
    + expenses booked "unpaid" to the supplier - payments
  - receipts / payments are set against bills (`ReceiptAllocation`,
    `PaymentAllocation`: full, part, or opening balance); the rest stays
    "on account". Balance = open bills' outstanding - on account = the
    running ledger's last line (tested both ways).
  - a voided invoice / counter bill / purchase bill / expense releases
    what was received or paid against it back on account.
  - **books opening date** (Accounts -> "Books start date", or the cash
    book page): documents dated before it are left to the opening
    balances, so a client switching on accounts mid-year doesn't see
    every old invoice as unpaid.
- **Vouchers**, each from the row-locked allocator, never deleted, voided
  with a reason, audit-logged: receipt `RCT/`, advance receipt voucher
  `RV/` (Rule 50), refund voucher `RFV/` (Rule 51), payment voucher
  `PAY/`. Modes cash / UPI / card / bank / cheque. A cheque has its own
  status: pending settles the bill but stays out of the bank book;
  cleared lands in the bank book on the clearing date; bounced settles
  nothing (the bill is outstanding again, the ledger shows the bounce).
- **Ageing** 0-30 / 31-60 / 61-90 / 90+ days from the bill date, plus
  overdue by the customer's credit days / supplier's terms. Customer
  statement PDF (balance brought forward, running balance, open bills,
  ageing). **WhatsApp reminder**: a wa.me click-to-chat link (no API)
  with the balance and a signed, 15-day statement link anyone can open
  without signing in (same pattern as the POS bill share link).
- **Credit limit** (customer form, with accounts on) is a warning, never
  a block: flashed after an invoice, shown on the POS "done" screen for
  a credit sale.
- **GST on advances - one module, `app/utils/advance_gst.py`** (Decimal,
  half-up, unit-tested). An advance for services is tax-inclusive:
  taxable = amount x 100 / (100 + rate); CGST/SGST (odd paisa on SGST)
  or IGST by place of supply. Goods advances carry none (Notification
  66/2017); composition / unregistered clients issue the voucher without
  tax. When an advance is set against an invoice or refunded, that
  part's tax is worked out cumulatively (`advance_portion`), so the
  pieces always add back to exactly the tax paid.
  - GSTR-1 (with `receivables` on): tables 11A (advances received) and
    11B (adjusted / refunded) by place of supply and rate, on the page,
    PDF, Excel ("Advances 11A-11B") and JSON; receipt and refund
    vouchers in "Documents issued".
  - GSTR-3B: 3.1(a) includes 11A - 11B, with a note saying so.
- **Expense voucher prefix moved from `EXP` to `EV`.** `EXP` was already
  the export invoice's prefix. The migration renames existing expense
  series; vouchers already issued keep their numbers.
- **Quotation `QTN/`, Proforma `PI/`, Sales Order `SO/`, Delivery
  Challan `DC/`** (`SalesDocument`, `app/sales/`): same line editor as
  the invoice form, same GST service, so converting never changes a
  rupee. None is a tax document (no GSTR-1 sales, no stock movement).
  - **Convert in one click** (issues the invoice with today's date, or
    the date picked) or **Edit & invoice** (opens the invoice form
    pre-filled; the document closes when it's saved). Once only.
  - Challan purposes: job work, sale on approval, stock transfer, other.
    Challans share the `DC` series with branch challans, so no number is
    used twice, and appear in GSTR-1 "Documents issued" (cancelled ones
    counted as cancelled). A challan can be marked "returned".
  - Cancel with a reason; "Copy" starts a new one from it.
- **Void & re-issue** (with `sales_documents` on): the void form on an
  invoice also offers "Void & re-issue a corrected copy", which voids it
  and opens the invoice form pre-filled from it (new number on save).
- **Recurring invoices** (`RecurringInvoice` + `RecurringInvoiceRun`):
  a template (customer, lines, weekly/monthly/quarterly/yearly, start,
  optional end). Each due date becomes a draft run with **no number**
  until it's issued - Issue (one click, dated the due date), Edit &
  issue, or Skip with a reason - so a skipped month leaves no gap.
  Generation is idempotent (unique recurring + due date) and runs when
  the page opens or from cron: `flask recurring-invoices-generate`.
- **Cash book and bank book** (`app/accounts/books.py`), derived from:
  counter payments (cash vs UPI/card/bank/cheque/wallet), drawer cash
  in/out, counter refunds/returns, receipts, refund vouchers, supplier
  payments, expenses (petty-cash expenses are counted once, via the
  drawer movement). Opening cash in hand / bank balance on the books
  opening date. Credit and exchange move no money.
- **Tenant isolation**: every new table carries `tenant_id` (allocations
  through their voucher); tests prove another client can't read, void,
  allocate against, convert or issue anything by guessing ids
  (`tests/test_accounts.py`, `tests/test_sales_documents.py`).

### Platform build - Phase 7 (P&L, business reports, GST returns, Tally, filing)

- **New flag `business_reports`** (off by default; on in new clients'
  business-type presets): with it on the Client Admin lands on a **Home
  dashboard** and Reports gains P&L, gross profit by item and the daily
  summary. Off: the landing page, menus and reports are exactly as before
  and the new routes 404.
- **Profit & loss (`app/finance/pnl.py`)** - computed from the documents,
  no ledger yet, but every figure is an *account line* (code, label,
  amount) so a double-entry ledger can later supply the same codes:
  - revenue = invoices + counter bills + debit notes - credit notes -
    returns / refunds, all excluding GST; margin-scheme sales at full
    selling price with "GST paid on margin" deducted.
  - COGS = opening stock + purchases (net of returns; non-claimable GST is
    cost, claimable isn't) + opening stock entered - closing stock, valued
    from the stock ledger (Σ qty × unit cost, weighted average). Damage,
    wastage and count differences fall in through closing stock. Stock of
    third-party branches (independent buyers) is not the client's.
  - expenses by category at cost (excluding claimable GST); gross profit,
    GP %, net profit; comparison with the previous period or the same
    period last year; one location or all (counter sales by the till's
    location, invoices at the main store, purchases at their location,
    expenses by branch); Excel and PDF.
  - without Inventory: Revenue - Purchases - Expenses, labelled a
    cash-basis estimate.
- **Gross profit by item / category** (`app/finance/sales_analysis.py`):
  sales excluding GST vs the stock ledger's cost of the units that went
  out; typed-in lines and services show "no cost". Excel.
- **Daily sales summary**: counter sales by payment mode net of money paid
  back, by hour (bar), by cashier (bills, discounts, returns, voids),
  discounted bills, and every void / return / refund with reason. PDF.
- **Dashboard**: sales and money received today / this month, cash in
  hand (cash book, or today's counter cash), dues both ways, biggest
  debtors, top items, low stock, expiring batches - each tile only with
  its module on.
- **GSTR-3B net payable - one module, `app/utils/gst_setoff.py`** (Rule
  88A, unit-tested): IGST credit first (IGST, then CGST/SGST, spread to
  minimise cash), then CGST/SGST credit on their own head then IGST, never
  across; reverse-charge tax always in cash; credit carried forward.
  Table 6.1 on the page, PDF, Excel and JSON - with Purchases or Expenses
  on (ITC excludes blocked, ineligible and margin-scheme purchases).
- **GSTR-1 B2CL**: inter-state invoices to unregistered buyers above
  `gst_config.B2CL_INVOICE_VALUE_THRESHOLD` (Rs. 1 lakh, Notification
  12/2024-CT, from 01.08.2024) listed invoice by invoice; the rest stays
  B2C Small. **HSN summary split B2B / B2C** (the combined table stays).
  New Excel sheets B2CL, HSN B2B, HSN B2C.
- **Purchase register / expense register** (Excel, one month; Reports
  and the monthly pack): bill-wise per GST rate with supplier invoice,
  GSTIN, tax and ITC taken (blocked / margin lines show none); returns as
  minus rows; expenses with GST bill details.
- **Tally export** (`app/finance/journal.py` + `tally.py`): every sale,
  counter bill, note, return, purchase, purchase return, receipt, payment,
  refund and expense becomes a balanced voucher (rounding to "Round
  Off"; a test asserts debits = credits) on ledgers in Tally's standard
  groups (Sundry Debtors / Creditors, Sales @ rate, Purchases @ rate,
  Output / Input CGST-SGST-IGST, Cash, Bank...). A ZIP with
  `1_masters.xml` and `2_vouchers.xml` (TallyPrime Import Data XML,
  debit = negative amount with ISDEEMEDPOSITIVE Yes). Admin console.
- **Filing tracker** (`FilingStatus`, Filing in the admin menu): per
  client GSTIN and month, GSTR-1 (due 11th) and GSTR-3B (due 20th) -
  dates from `gst_config` - with document count, data ready (the client
  marks it under Reports -> GST filing, or the firm does) and filed with
  date and ARN; overdue in red. Auditors see only their own clients.
- **Monthly pack** (client page in the admin console, and the bulk GST
  export now sends one per client): GSTR-1 and GSTR-3B (Excel, PDF,
  JSON) per GSTIN, HSN summary (all / B2B / B2C), purchase and expense
  registers (when recorded), P&L, Tally files.
- Tenant isolation: filing rows are per client - another client can't
  read or change them, an auditor can't reach clients that aren't theirs
  (`tests/test_phase7_reports.py`).

### Platform build - Phase 8 (e-Invoice IRN and e-Way Bill)

- **Two new flags, `einvoice` and `eway_bill`** (off by default). Off:
  invoices, notes, challans and transfers look exactly as before and every
  `/einvoice/...` route 404s; staff get 403. On: an **e-Invoice** (or
  **e-Way bills**) menu with the IRN desk, e-way bill list, call log and
  settings.
- **When an IRN is needed - `app/einvoice/rules.py`**, every number from
  `gst_config`: a Regular client whose AATO (Settings) is above
  `EINVOICE_AATO_THRESHOLD` (Rs. 5 crore, Notification 10/2023-CT, from
  01.08.2023), for tax / reverse-charge invoices to a buyer with a GSTIN,
  export invoices (with IGST or under LUT) and credit / debit notes
  against them. Never B2C, bills of supply, challans or branch transfers.
  At AATO of `EINVOICE_REPORTING_LIMIT_AATO` (Rs. 10 crore) and above, the
  IRP refuses documents older than `EINVOICE_REPORTING_DAYS` (30): the
  page warns 5 days before and says plainly once it's too late. The
  "IRN needed" panel shows only on documents that need one.
- **INV-01 JSON (`app/einvoice/schema.py`)**, schema 1.1, built from the
  document and checked in Python before anything is sent: document
  number format (16 characters), GSTINs, both addresses and PIN codes,
  HSN digits, GST slab, and the IRP's arithmetic (price x qty = amount,
  amount - discount = taxable, taxable + tax = line total, lines = totals
  + round off). Problems are listed in plain words on the invoice.
  **Download e-Invoice JSON** (one document, or every ready document of
  a month from the IRN desk) is the file for the IRP's offline /
  bulk-upload tool; the IRN, Ack No / date and signed QR it returns are
  then recorded on the document. This path needs no credentials.
- **Providers (`app/einvoice/providers/`)** - one interface
  (`authenticate`, `generate_irn`, `get_irn`, `cancel_irn`, `ewb_by_irn`,
  `generate_ewb`, `update_part_b`, `cancel_ewb`), picked per GSTIN under
  e-Invoice settings:
  - **NIC sandbox / NIC direct**: the published NIC protocol - login with
    the NIC public key (RSA), session key (Sek) decrypted with our random
    AppKey, payloads AES-256 encrypted with the Sek; standalone e-way
    bills through the e-way bill system's own API (address and paths are
    settings, from the client's NIC onboarding mail).
  - **GSP / ASP**: plain JSON to the GSP's address with its own headers
    and paths as settings - so changing GSP is a settings change; a GSP
    that works differently is one small subclass in the `PROVIDERS`
    registry.
  - Tested against a simulated NIC server (a real RSA / AES round trip)
    and a fake GSP. **Not run against the live sandbox** - no sandbox
    login was available. HTTP is always mocked in pytest.
- **Logins are stored encrypted** (`app/einvoice/vault.py`, Fernet) with
  `EINVOICE_SECRET_KEY` from the environment, or a key derived from
  `SECRET_KEY`. Changing the key makes saved logins unreadable - the
  client re-enters them; nothing else is lost. Secrets are never shown
  again; leaving one blank keeps it.
- **Every government call goes through `services.call`**: logs in when
  the session has expired, retries once on an expired token, and writes a
  `gov_api_calls` row (action, our request id, success or the error code
  and message, the plain-language line, time taken) plus an audit entry
  - committed even when the call fails. `errors.py` turns IRP messages
  into a pointer to what to fix (customer GSTIN, PIN code, HSN, login...)
  by words in the message, never by guessing what a code means; the
  original code and message are always shown too. The one code used is
  2150 (duplicate IRN): the app fetches the existing IRN instead of
  failing, which also covers a retry after a timeout.
- **IRN on the document**: IRN, Ack No / date (Indian time) and the
  signed QR string are stored; the QR is drawn at view time (segno) on
  the invoice / note page, the PDF and the admin console. **Cancel within
  `IRN_CANCEL_HOURS`** (24) with the IRP's reason codes and a remark -
  through the API or recorded as done on the portal - then the document
  is voided with the same reason. Not while an e-way bill on it is
  active. After 24 hours the page says to issue a credit note and links
  to it. A plain void is refused while the IRN is active.
- **LUT export numbering**: `EXP-LUT/2026-27/0001` is 20 characters and
  the IRP takes 16, so with `einvoice` on, LUT exports start an `EXL`
  series (`EXL/2026-27/0001`). Numbers already issued are untouched;
  clients without the flag keep `EXP-LUT`.
- **e-Way bills (`app/einvoice/eway.py`)** for invoices (including a
  branch's delivery-challan invoice), delivery challans (job work,
  approval, exhibition...) and stock transfers (valued at cost, to the
  client's own GSTIN) worth more than `EWAY_BILL_VALUE_THRESHOLD`
  (Rs. 50,000, Rule 138(1)). Part A comes from the document (both
  addresses editable - ship-to, dispatch-from, exports); Part B is mode,
  distance, vehicle or transporter ID, and the LR / RR / AWB number for
  rail, air or ship. Then: **download the JSON** for the portal's bulk
  tool and record the EWB number, or **generate** through the provider -
  from the IRN when the invoice has one (IRP "EWB by IRN"), standalone
  otherwise. **Validity** follows Rule 138(10): one day per 200 km (20 km
  for over-dimensional cargo), each day ending at midnight Indian time,
  starting once Part B is in; a Part-A-only bill says it isn't valid for
  movement yet. **Vehicle update** (with reason) and **cancel** within
  `EWAY_BILL_CANCEL_HOURS` (24), through the API or recorded as done on
  the portal. Never deleted.
- Tenant isolation: logins, call log and e-way bills are per client -
  guessing another client's ids gives 404 and changes nothing
  (`tests/test_phase8_einvoice.py`).

### Platform build - Phase 9 (business-type modules)

- **Eight modules, each behind its own flag** (`restaurant_mode`,
  `weighing`, `pharmacy`, `electronics`, `jewellery`, `job_cards`,
  `wholesale`, `loyalty`), on by default only in the matching business
  type's preset for **new** clients. Some clients onboarded since Phase 1
  already had `restaurant_mode` / `weighing` / `job_cards` / `loyalty` in
  their saved flags from a preset, where they did nothing; the migration
  switches those off for every existing client, so no module appears that
  a client didn't turn on. Flag off: every route 404s, nothing reaches
  the counter (`MODULES` is `{"quote": false}`) and a bill is exactly as
  before. Admin-only pages are 403 for staff.
- **One hook module for the counter, `app/pos/business.py`**, called
  from `app/pos/services.py` (sold-out check, pack units, wholesale /
  jewellery prices, prescription, old gold / points payments, warranty,
  KOT after checkout, release on void). **`/pos/quote`** prices the cart
  exactly as checkout will - item master, price lists, order type,
  slabs, packs, today's metal rate, restaurant charges - so the screen
  never guesses; a price the cashier typed is kept.
- **Restaurant** (`app/restaurant/`): tables (sections, on/off) and a
  Tables screen (free / running with amount, time and KOT count); a
  dine-in order is a HELD counter bill carrying `table_id`. **Send KOT**
  saves the order and prints a kitchen ticket with only what changed
  since the last one - a lower quantity prints as a struck-through
  CANCEL line - numbered per day (not a tax series). Move, merge, cancel
  (with a reason). Billing the table marks its tickets billed and frees
  it. Takeaway / delivery / Swiggy / Zomato bill straight from the counter,
  each from the price list set for it, and print one KOT. Item notes
  ("less spicy") go on the KOT and the receipt. **Packing charge** (per
  order or per item, on chosen order types) and **service charge** (a %
  of the food, dine-in, only when the cashier ticks it - CCPA guidelines
  04.07.2022, `gst_config.SERVICE_CHARGE_DEFAULT_ON`) are lines taxed at
  the restaurant rate (`RESTAURANT_SERVICE_GST_RATE`, 5%, Notification
  11/2017-CT(R) item 7(ii)). **Sold out today** per item, back on sale the
  next day by itself.
- **Weighing** (`app/utils/scale_barcode.py`): scale labels (EAN-13 in the
  20-29 range: prefix + item code + weight in grams or price in paise +
  check digit) scanned at the counter fill the item and weight; prefixes
  and code length are settings; items carry their scale code (PLU).
  Quick 250 g / 500 g / 1 kg / 2 kg buttons on the weight keypad.
- **Pharmacy**: every medicine batch-tracked, expiry required on purchase;
  **Schedule H / H1 / X** (`gst_config.DRUG_SCHEDULES`, Drugs and
  Cosmetics Rules) can't be billed without the doctor's and patient's
  names, plus the doctor's registration number and the patient's address
  for H1 / X; stored on the bill, printed on the receipt, and listed in
  the **scheduled drug register** (with batch; Excel). **Substitutes**
  with the same composition (normalised), cheapest in stock first.
  **Sell by the strip** (the item's pack unit): price x pack size, stock
  moves in tablets (`POSBillLine.unit_factor`) - also on void and return.
- **Electronics**: IMEI picked for every unit (as before), the customer's
  warranty counted from the sale date, and a **warranty check** by IMEI
  (sold when, to whom, in / out of warranty, past repairs) that opens a
  repair job card.
- **Jewellery** (`app/utils/jewellery.py` - the one pricing module, unit
  tested): today's rate per metal and purity (set daily; a purity not set
  is derived from the purest grade set via `gst_config.METAL_PURITIES`);
  a piece's price = net weight x rate + making (per gram / % of metal /
  flat) + stones, before GST; **3% GST** on the whole
  (`JEWELLERY_GST_RATE`, Notification 1/2017-CT(R) Sch. V); the breakdown
  prints under the line. **Making charges on the customer's own gold** are
  job work: a service item at **5%, SAC 9988** (`JEWELLERY_JOB_WORK_*`,
  Notification 11/2017-CT(R) item 26 as amended) - the firm's decision.
  **Old gold** bought from a customer is a numbered purchase voucher
  (series OG, never deleted, voided with a reason only while unused) and
  its value pays for the new bill as the "Old gold" payment - **after
  GST**, which stays on the new jewellery's full value (the firm's
  decision). A voided bill puts the value back on the voucher.
- **Job cards** (`app/jobs/`): numbered (series JOB); customer (or a new
  one), item, IMEI, accessories, complaint, estimate, promised date,
  technician; Received -> In progress -> Ready -> Delivered (delivery
  needs the invoice when there are charges), or cancelled with a reason.
  Parts come out of stock as they're added and go back when removed or
  cancelled; **Make the invoice** hands them to the invoice (the job's
  entries are reversed, the invoice takes them - counted once) with the
  labour lines (SAC). **WhatsApp "ready for pickup"** link (wa.me, no paid
  SMS).
- **Wholesale** (`app/wholesale/pricing.py`): customer groups each buy at
  a price list; quantity slabs per item (the biggest slab reached, only
  if cheaper - a slab never raises a price); the counter and the invoice
  form use the same rule (the invoice form asks the server and fills the
  rate, which stays editable); salesmen on counter bills, invoices and
  receipts, and **sales & collections by salesman** (Excel). Credit
  limits are the existing customer-account warning.
- **Loyalty** (`app/loyalty/points.py`): points earned on counter bills
  with a customer, on what was paid (not on points, exchange credit or
  old gold); spent as a payment (`PaymentMode.LOYALTY`) - GST stays on the
  full bill; a minimum balance before spending; void / refund / return
  take back what they should. Customer page: purchases (bills and
  invoices) and the points ledger.
- **Receipts and screens**: order type / table, salesman, prescription,
  notes and jewellery breakdown print on the receipt; Tables, Jobs and
  Rates in the menu with their modules; module settings under Settings
  (restaurant setup, scale label format, points rules, groups &
  salesmen, today's rates).
- Tenant isolation: tables, KOTs, metal rates, old gold vouchers, job
  cards (and lines), customer groups, slabs, salesmen and loyalty entries
  are per client - guessing ids gives 404, another client's table /
  voucher / salesman / customer points can't be used at a counter, and
  another client's metal rate never prices an item
  (`tests/test_phase9_modules.py`).

### Platform build - Phase 10 (platform, Android, security, polish)

- **Offline billing (`offline_pos`, opt-in) - each device numbers its own
  series.** The owner's choice at the [ASK]: every counter device (a
  browser, or the Android app) registers once and gets a code `O1`,
  `O2`... and its own `InvoiceSeries` (`device_id` set - the counter's
  normal POS series filters `device_id IS NULL`, so it is untouched).
  Offline, the device numbers each bill itself, `O1/2026-27/0001`
  (within GST's 16 characters, the FY by the Indian calendar on the
  device), so the number on the customer's paper is final - nothing is
  renumbered later. Bills wait in IndexedDB and go to `/pos/offline/sync`
  oldest first when the connection is back (on the `online` event, every
  20 s while any wait, or "Send now"); each is booked exactly once by its
  uuid, under the same row lock as every number (`numbering.claim_number`
  moves the series past it, never back). What the device sent is kept in
  `OfflineBill` whatever happens: a bill the server can't book (item
  deleted, clock in the future, ...) stays there, number accounted for,
  for the Client Admin to **retry** or **cancel with a reason** (a VOIDED
  POS bill with that number, zero value - the series has no gap). The
  device's total is compared with the server's; a difference is booked
  at the server's figure and flagged. Only plain bills go offline:
  catalog items at the device's price, one payment by cash / UPI / card,
  no discounts, credit, returns, points, serial / margin / jewellery /
  Schedule H items (`offline.blocked_products`) - the counter says why it
  must wait. Stock goes out even past zero (the goods have left).
- **A lost reply is never billed twice.** Every online checkout sends a
  `checkout_uuid` (unique per client); sending it again returns the same
  bill. If the reply was lost and the cashier billed it offline, the
  offline copy carries the same uuid, so the server flags it ("reached
  the server online as POS/...") instead of booking a second sale.
- **Service worker** (`static/js/pos_sw.js`, served at `/pos/sw.js` with
  scope `/pos/`, registered only with the flag on): the POS screen,
  `/static/` and item photos are fetched from the network as usual and a
  copy kept; with no network the copy is used. Any other `/pos/` page
  offline gets a plain "You're offline - only billing works" page. The
  sign-in page drops the cached POS screen (another login may use the
  browser); queued bills stay with the login that made them. Device
  registration takes the client's row lock (two counters at once can't
  both get O1) and a cross-tab lock in the browser.
- **Receipts as data, ESC/POS in the page.** `app/pos/receipt_doc.py`
  turns a bill into the same content as the PDF receipt, as JSON
  (`/pos/bills/<id>/receipt.json`, same original / DUPLICATE rule); an
  offline bill builds the identical shape on the device.
  `static/js/escpos.js` lays it out for a 58 mm (32 col) or 80 mm (48 col)
  roll in plain ASCII ESC/POS (init, centred bold header, two-column
  pairs, feed and cut) - one formatter for online and offline bills,
  tested with Node.
- **Android app** (`android/`): direct printing to a **Bluetooth (SPP)
  or USB (bulk OUT)** thermal printer - the app only moves the bytes the
  page made; the PDF -> Android print dialog stays as the fallback (no
  printer chosen, or the printer fails - the page is told and prints the
  HTML receipt instead). **Cash drawer** kick (`ESC p 0 25 250`) after a
  cash sale's original print, and an "Open drawer" button. **Kiosk mode**
  (Android lock task; a dedicated / device-owner tablet locks straight
  away, any other asks to pin the screen) with a code to unlock.
  **Update check** against the `android-latest` release's `version.json`
  (the CI build's run number is the versionCode). Signed in before, the
  app opens straight on the POS so the cached screen works offline, and
  its offline screen offers "Open billing without internet". Settings
  live on the device (`PrinterSettings`): printer, paper width, drawer,
  kiosk.
- **Two-step sign-in (TOTP, RFC 6238)** - `app/auth/security.py`, no
  library, no service called. Compulsory for the Ultra Admin and the
  Auditor hierarchy (they can open every client they manage; already
  signed-in admins are sent to set it up), optional for a Client Admin
  (Account security), not offered to branch counters (they get cashier
  PINs). Nothing is signed in until the second step passes. The secret
  is stored encrypted (Fernet from SECRET_KEY); a used step can't be
  replayed; eight one-time recovery codes (hashes stored); five wrong
  codes send you back to the password and count as a wrong password.
  The firm resets a lost phone from the client / Auditor page;
  `flask reset-2fa EMAIL` for the Ultra Admin's own. Tests run with
  `ADMIN_2FA_REQUIRED = False` except the 2FA tests.
- **Lockout and limits.** `LOGIN_LOCKOUT_ATTEMPTS` (8) wrong passwords
  in a row lock the account for `LOGIN_LOCKOUT_MINUTES` (15), audit-logged;
  any new password (an admin's reset included) opens it. One network
  (IP) gets `LOGIN_IP_LIMIT` failed sign-ins per window across all
  accounts. `PROXY_FIX_X_FOR` trusts the proxy's X-Forwarded-For (set it
  to 1 on Render) so that is the real client IP.
- **Idle sign-out.** The admin hierarchy after `ADMIN_IDLE_MINUTES` (30);
  a client's own logins only if its Client Admin picks a limit under
  Settings -> Idle sign-out (off by default - unchanged behaviour). Pages
  asked for in the background (`X-Background`, the offline sync) don't
  count as activity; an XHR gets a 401 JSON instead of a redirect.
- **Cashier PINs (`cashier_pin`, opt-in).** Cashiers belong to a counter
  login (a branch, or the Client Admin's own counter); each has a 4-6
  digit PIN, unique at that counter, stored as an HMAC keyed by
  SECRET_KEY (a PIN picks its cashier). With any cashier set up, the POS
  is covered by a PIN pad until someone taps in; checkout refuses
  without a cashier; the bill, receipt and daily summary carry the
  cashier. Lock / switch, auto-lock after the Client Admin's idle
  minutes, five wrong PINs = a minute's wait. Offline, the cashier on the
  counter stays (a PIN needs the server).
- **Monitoring.** Sentry only when `SENTRY_DSN` is set (`app/monitoring.py`;
  user id and role only, no email or form data; tracing off unless
  `SENTRY_TRACES_SAMPLE_RATE`). `/healthz` checks the database and Redis
  and answers 503 naming the part that's down.
- **First-run checklist** (`app/onboarding.py`) for a new Client Admin -
  business profile (Settings saved once), GSTIN / location, first item,
  first customer, first bill, printer test (a sample receipt on the
  chosen roll) - each ticking itself from the client's data, closable.
  Clients that existed before were marked dismissed by the migration.
- **Platform billing** (`app/subscriptions/`): the firm's own GST
  invoices to clients and Auditor offices from the existing billing
  cycle - a subscription fee per subject (Ultra Admin), a billing profile
  (name, GSTIN, state, UPI ID, SAC 997331 / 18% from gst_config, days
  before renewal, prefix). Raised by `flask platform-invoices-generate`
  (daily) or by hand, numbered `SUB/2026-27/0001` under the row-locked
  allocator (`allocate_number` now takes any series table), taxed through
  `app.utils.gst` (CGST+SGST in the firm's state, IGST outside, none
  while the firm has no GSTIN), UPI link and QR. **Marking paid** moves
  the subscriber's cycle through the same idempotent `mark_period_paid`
  the "Mark this period paid" buttons now use (moved out of the routes):
  an invoice pays exactly its period, even before the due date, and only
  while that period is still the one due - paying twice, or after the
  button, changes nothing. Voided with a reason, never deleted, every
  step audit-logged. A Client Admin (Settings) or Auditor sees only their
  own invoices.
- **Admin drill-downs** for POS bills and credit / debit notes, like the
  invoice one: read-only, scoped by `assert_auditor_owns_tenant`, PDF
  (the firm gets a DUPLICATE COPY of a receipt), audit-logged.
- **Daily owner summary (`daily_summary`; on in new clients' presets
  with the dashboard, off for existing ones)** - `app/finance/owner_summary.py`
  on top of the daily summary and gross profit: sales, payment modes,
  counters, top items, low stock and stock **below zero** by location,
  offline bills waiting; on the dashboard, its own page, a WhatsApp
  click-to-chat message, and email over SMTP when `SMTP_HOST` /
  `SMTP_FROM` are set (`flask owner-summary-send` each evening).
- Fixed on the way: Alpine 3 calls a component's `init()` itself, so the
  POS screen's `x-init="init()"` ran it twice (watchers and the offline
  start-up doubled) - removed there and on the new PIN pad.
- Tenant isolation: counter devices, offline bills, cashiers and the
  subscription invoices a client sees are per client - another client's
  device (even with its token), bill, cashier or invoice id gives 403 /
  404 (`tests/test_phase10_offline.py`, `test_phase10_security.py`,
  `test_phase10_platform.py`).

### Interface redesign - stage 1 (the frame, menus and look)

The plan (six stages) grouped every screen around the jobs people do,
moved the menus out of one long top row, and set one look for the whole
app. Stage 1 is the frame every page sits in; the pages themselves keep
their content and are reworked in later stages.

- **Grouped side menu** (`app/navigation.py`, `base.html`): Home, Sell,
  Stock & buying, People & money, Reports & GST, Business. Each entry
  names the flag that guards its screen, exactly as the route does, so a
  module that's off never shows anywhere; a group with nothing left is
  dropped. The menu folds to an icon "rail" (remembered per browser; the
  counter and screens narrower than a laptop start folded) and slides in
  from the left on tablets and phones. The firm's console (Ultra Admin,
  Auditors, Sub-Auditors) gets the same frame with its own groups.
- **Branch (Staff) logins keep the simple layout** - no menu, search,
  New or bell; the counter app on Android looks the same apart from the
  fonts and toasts.
- **Search (Ctrl K)** jumps to any screen or action this login may open:
  every menu screen, every "New" action, and screens one level down (tabs
  and report cards, e.g. GSTR-2B matching, Reorder list, Drug register).
  The list is built on the server per request, so switched-off modules
  never reach it.
- **One green New button** for everything created day to day (counter
  bill, tax invoice, note, quotation, job card, item, customer, purchase
  bill, expense, receipt, payment, stock adjustment / transfer), each
  only with its module on.
- **The bell** (`/needs-you`, loaded after the page so it never slows
  one down): offline bills that couldn't be saved, items at or below
  minimum stock, batches expired / expiring in 30 days, recurring
  invoices ready to issue - only for modules that are on, only the
  signed-in client's own rows (tested with a second client's stock).
- **Toasts** replace the coloured bars: the same flashed messages slide
  in at the corner; successes leave after 5 seconds, errors and warnings
  stay until closed. Sign-in pages keep their inline messages.
- **Phones**: a bottom bar (Home, Sell, New, Reports, More) for Client
  Admins, with New opening a sheet of actions and More opening the menu.
- **Look**: Figtree for text, Bricolage Grotesque for headings, JetBrains
  Mono for codes - stored in `app/static/fonts` (OFL licences beside
  them), never loaded from the internet. Icons are Lucide (ISC licence)
  in one sprite, `app/static/img/icons.svg`, used through the `icon()`
  template helper. Colours keep the `--color-*` names a client's theme
  overrides, so a client's own button colour, background and font still
  apply; the "Default" font choice is now Figtree, and a client who chose
  another font gets it for headings too.
- **Motion**: page content rises into place (cards a beat apart), menus
  grow from their button, toasts slide in, the active menu entry draws
  its marker, the bell's dot pulses when something needs you, buttons
  lift on hover and press in on tap, and `data-count-up` figures count up
  once. Everything is 0.1 - 0.7 s, nothing loops for decoration, and all
  of it switches off for anyone whose device asks for reduced motion.
- **Unchanged**: every URL, permission, feature flag, form field, report
  figure and printed document. The offline counter's service worker
  already caches everything under `/static/`, so the new fonts, icons
  and `shell.js` work offline with no change.
- Tests: `tests/test_redesign_shell.py` (menu groups and flags, search
  and New lists, active entry, branch layout, console groups, toasts,
  vendored assets, bell scoping and flags, theme fonts).

### Interface redesign - stage 2 (Settings in sections)

Settings was one 4,900-pixel form with a single Save at the bottom. It is
now seven short sections, each with its own Save, under one Settings menu
(`app/settings/sections.py`, `settings/_layout.html`):

- **Business** (`/settings/`): type of business, turnover, the
  GST-included default for new items, "switch modules to the recommended
  set", and the branded sign-in link with a Copy button.
- **Appearance** (`/settings/appearance`): logo (previewed as soon as a
  file is picked), colours and font, with a live preview of a screen.
- **Modules** (`/settings/features`): every module as a switch, grouped
  by purpose - Selling, Stock & buying, Money & reports, GST, Your
  industry, At the counter - each with its icon, what it does, a "3 of 5
  on" count per group and a search box. A module that's on shows a "Set
  up" link to its own screen (categories, suppliers, scale labels,
  cashiers...); the link only appears once its screen exists, so it is
  never a 404 (Expiry and Serial numbers also need Inventory). A module
  added later without a group still appears, under "Other".
- **Counter** (`/settings/counter`): payment modes as tap-on chips, the
  UPI scan-to-pay ID, and tiles to the counter's own screens (branches,
  cashiers & PINs, offline devices, restaurant setup, scale labels,
  printer test) - each only with its module on.
- **Documents & printing** (`/settings/documents`): POS bill print size
  (invoices stay A4) and, with the margin scheme on, its declaration.
- **Sign-in & security** (`/settings/security`, the address idle sign-out
  already had): two-step sign-in status and link, change password, idle
  sign-out and the counter lock.
- **Subscription**: the client's invoices for the software
  (`/admin/subscriptions/mine`), shown inside Settings for a Client
  Admin; an Auditor's office sees it on its own as before.
- Scale-label and loyalty rules (`/settings/modules`, same address and
  same 404 when both modules are off) open inside Settings too, reached
  from their modules' "Set up" links.

How saving works:
- Each section posts only its own fields (separate small forms in
  `app/settings/forms.py`); saving one section never changes another.
  The save bar slides up once something changes, with Discard, and the
  browser asks before leaving with unsaved changes. Without JavaScript the
  Save is simply always shown.
- Every section save is audit-logged (`settings_saved` with the section);
  a change of modules is still logged as `tenant_features_changed` with
  before / after, and the first-run "Business profile" step ticks on the
  first save of any section.
- The single all-in-one post to `/settings/` that the old page made still
  works exactly as before (`TenantSettingsForm` is all the section forms
  together, saved by the same helpers), so nothing that posts it breaks.
- Search (Ctrl K) finds each section by name ("modules", "upi", "print
  size", "logo"...).
- Fixed on the way: search listed Expiry and Serial numbers when only
  their own module was on, but those screens also need Inventory - they
  now need both, like their routes.
- Tests: `tests/test_redesign_settings.py` (sections open in the frame,
  branch logins refused, each section saves only its own fields, preset,
  module audit, set-up links only when reachable, payment-mode and UPI
  checks, margin wording only with the module, colour validation, no
  effect on another client, search entries). One older test now looks
  for the print sizes on the Documents & printing section.

### Interface redesign - stage 3 (page patterns)

Every page now follows one of three shapes - a list, a single record or a
form - with the same header, toolbar, cards and tables, so people stop
hunting for buttons. Mostly shared CSS plus two small scripts, so all 200
screens changed without rewriting each one:

- **One main action per page.** A page header with three or more actions
  keeps its green (main) button in view and folds the others into a
  "More" menu (`shell.js`). On a template, `data-keep` keeps an action
  out of More and `data-more` sends one in; `data-no-more` on the header
  turns it off. The counter (POS) is left for stage 5.
- **Toolbars**: the search/filter bar above a list is one card with a
  search icon in its box.
- **Tabs** (Stock, Purchases, Accounts...) are underlined tabs that
  scroll sideways on a phone instead of wrapping. **Credit & debit notes**
  are now a tab inside **Invoices** (`invoicing/_tabs.html`) rather than
  their own menu entry; search still finds them.
- **Records**: "label / value" details in a card read as small labels
  over bold values.
- **Tables**: quieter headers, row hover, status dots. On a phone a list
  table (one without inputs) becomes a stack of cards, each value with its
  column name (`shell.js` adds the labels); a table that holds inputs
  scrolls inside its own box instead of widening the page.
- **Empty lists** show a small illustration above their message.
- **Form controls** (checkboxes, radios, file pickers) use the client's
  button colour.
- **Inline styles**: 775 one-off `style="..."` attributes became shared
  classes with exactly the same values (`!important`, so each wins where
  its inline style did) - spacing (`mt-16`, `mb-20`, `gap-10`...),
  layout (`grow`, `items-end`, `justify-between`...), sizes (`maxw-180`
  ...) and figures (`figure`, `figure-xl` for big totals). 46 one-offs
  remain on screen templates; printed documents (PDFs, receipts, KOTs)
  and the two partials shared with the GSTR-1 PDF are untouched, since
  WeasyPrint never loads app.css.
- Fixed on the way: the menu's "GST returns" (and search's GSTR-1 /
  GSTR-3B) opened a 404 because those pages needed a GSTIN in the
  address - they now open the client's primary active GSTIN when none is
  given (an explicit one, or another client's, behaves as before). "New
  credit or debit note" came off the New menu: a note always starts from
  an issued invoice, so that link was a 404.
- Tests: `tests/test_redesign_pages.py` (inline styles stay retired,
  every utility class used exists in app.css, notes tab, no dead New
  action, GST returns opens the primary GSTIN and still refuses another
  client's, every menu and search link opens with all modules on).

### Interface redesign - stage 4 (Home dashboard and Reports hub)

**Home** (`reports/dashboard.html`, data from `app/finance/dashboard.py`):
- Headline tiles - sales today (the lead, with a 14-day sparkline), money
  received, cash in hand, what customers owe (with overdue) and what you
  owe suppliers - each figure counting up once as the page opens, with a
  comparison where there is one: today against the same weekday last
  week, and this month against the same days of last month.
- **Sales, last 14 days**: a bar per day, today in the accent, the
  highest and today labelled, the exact figure on hover or keyboard focus,
  and a hidden table of the same figures for screen readers. One series,
  so no legend - the title names it. The days come from
  `daily_sales()`, which applies exactly the rule behind "Sales today"
  (`_sales`: counter bills less refunds and returns, plus issued sales
  invoices) in four queries for the whole fortnight; a test checks every
  day against `_sales` and the fortnight against its total.
- **Needs you** (the bell's list, `app.navigation.needs_you`), or "All
  clear" when nothing is waiting.
- Top items with bars in proportion to the best seller; biggest dues, low
  stock and expiring batches as tidy lists with a link to the full screen;
  the end-of-day card unchanged.
- Money on Home reads the Indian way - 1,64,944.00, and 1.6L on chart
  labels - from new template filters `inr` / `inr_compact`
  (`app/utils/money.py`, display only; figures are still computed in
  Decimal by the module that owns them).

**Reports hub** (`app/reports/hub.py`, `reports/index.html`): every report
in one place, grouped by what it answers - Sales, Profit, Stock, GST,
Accounts & registers, Your industry - with a "Find a report" box and
jump links to each group. Each report sits behind the same feature flag as
its route, so a switched-off module's report never shows; the GST group
keeps the GSTR-1 / GSTR-3B working-paper form, and Accounts & registers
the monthly purchase / expense registers. Reports that used to hide
behind a module's own screen (fast/slow/dead stock, reorder, expiry,
expense report, e-invoice log, cash and bank books) are now listed here
too.

Tests: `tests/test_redesign_home.py` (Indian formatting, comparisons,
trend days add up to the sales figure, another client's sales never
counted, dashboard shows the trend / screen-reader table / Needs you,
hub shows only reports whose modules are on and every tile opens).

### Interface redesign - stage 5 (the counter)

The billing screen (`pos/terminal.html`) was rebuilt around its existing
logic - cart, prices, checkout, offline queue, PINs, printing and every
keyboard shortcut work exactly as before; only the layout and a few small
helpers in `posTerminal()` are new.

- **Full screen**: on the billing screen the site menu steps aside into a
  drawer (the menu button opens it), so the items and the bill get the
  whole width. Day-book, Z-report and the other counter pages keep the
  normal frame.
- **One bar on top**: "Held bills" with its count (saffron and pulsing
  while any are waiting), the screen's name, and one **Counter** menu
  holding the occasional jobs - Reprint last bill, Day-book, Z-report,
  Cash in/out & close day, Jobs, Buy old gold, Sold out, the Android
  app's Open drawer / Printer, Keyboard shortcuts. Each only with its
  module or permission, as before.
- **Held bills in a drawer** from the left (Esc closes it) - the same
  Resume / Discard as the old column, which gave the items its width.
- **Items**: a big search box (F2), categories as one row of chips (All,
  each category, "Other" for items without one) with sub-category chips
  under a picked category, the favourites row, then larger tiles. A
  search always looks through every item. The chips replace the
  expand/collapse tree.
- **The bill**: a count badge, a big total, and the first three payment
  modes as **one-tap buttons** - Cash, UPI, Card go straight to the
  confirm step (cash tendered, UPI QR); "Checkout" (F4) still opens every
  way to pay - split, credits, old gold, points. A prescription, when the
  bill needs one, is still asked first. Hold bill (F8) sits beside it.
- **Phones**: a bar at the bottom keeps the bill's count and total in
  reach with a Pay button; tapping it scrolls to the bill.
- **Motion**: a tapped item flies to the bill total, which bumps; the
  confirm sheet rises in; on "Printed" a tick draws itself (saffron when
  the bill was queued offline) and the change to give rises in large.
  All off for reduced motion.
- **Restaurant tables** as cards with a green (free) or saffron (busy)
  edge; the **PIN pad** with large round keys over a blurred counter.
- Tests: `tests/test_redesign_counter.py` - full-screen frame only on the
  billing screen, the bar and Counter menu, held bills in the drawer with
  their count, chips only with categories on, branch logins get the same
  counter, and the page's own script run in Node: chips filter by
  category / sub-category / "Other", search overrides them, and a one-tap
  payment lands on the confirm step with the right mode.

### Interface redesign - stage 6 (dark mode, sign-in, firm console, accessibility)

- **Dark mode** follows the device (phone or computer set to dark) unless
  someone picks **Light** or **Dark** under *Appearance* in their account
  menu (branch logins: the sun/moon button by their name). The choice is
  kept on that device only (`localStorage` "arfa.theme") and applied
  before the page paints (`_theme_boot.html`). The dark colours live in
  `app.css` as the same `--color-*` tokens, only inside `@media screen`,
  so anything printed from the browser stays light; PDFs never used
  `app.css`. A client's own button colour stays in the dark (links and
  picked menu entries use a light shade of it); their chosen page
  background and text colour apply to light screens only. Fixed colours
  that were in the stylesheet (warnings, toasts, overlays, the save bar,
  restaurant table cards) are tokens now so they switch too.
- **Sign-in pages** (sign in, two-step, set password, recovery codes,
  error pages when signed out) are split: the brand on a panel in the
  button colour - the client's own logo, name and colour on their branded
  link (`/auth/login/<slug>`), ARFA's otherwise - and the form on the
  other side; on a phone the brand shrinks to a strip on top. The form
  gets a heading, a show/hide password button, the right autocomplete
  hints for password managers, and a wrong password is announced to
  screen readers. Sign-in security (rate limits, lockout, two-step) is
  untouched.
- **Firm console**: the Clients page opens with four counts you can tap
  to narrow the list - All, Active, Renewal due, Can't sign in (paused,
  overdue past grace, awaiting verification) - a search by name or GSTIN,
  and rows with the client's initial and status. A client's page has jump
  links to its sections (Access & billing, Allocation, Branding,
  Locations, Users, Month-end, Recent activity) and Indian-format
  amounts. Who sees which client, and every action, is as before.
- **Accessibility pass**: every client screen (85 of them) was checked in
  light and dark for text contrast (WCAG AA) and fixed where short; the
  focus ring is lighter in the dark; form errors and help text are tied
  to their field (`aria-describedby`, `aria-invalid`); fields with no
  label of their own (filter bars, invoice and purchase lines) are named
  for screen readers from their column heading or placeholder
  (`shell.js`); pop-up menus move with the arrow keys, Home and End, and
  open onto their first entry with the down arrow; the saffron badge on
  held bills uses dark text.
- Tests: `tests/test_redesign_polish.py` - dark blocks only on screen, a
  client's colours in the dark, the saved theme applied before the
  stylesheet, the Appearance picker for client admins and branch logins,
  the split sign-in page and a client's branded one, the wrong-password
  alert, the two-step heading, the console's counts by state, the client
  page's jump links and Indian amounts, and errors tied to their fields.
- **Printing fix**: newer Chrome shows a PDF in a frame the page may not
  reach into, so printing a receipt from the counter's hidden frame threw
  "Blocked a frame ... from accessing a cross-origin frame". Every PDF
  print (counter, day-book, receipt, return, printer test) now goes
  through `static/js/print.js`: it still prints from the frame where the
  browser allows it, and otherwise opens the receipt in a new tab to
  print - or, if a pop-up blocker stops that, shows a note with a link.
  Tests: `tests/test_print_fallback.py`.

### Packages and powers - stage 1 (the rules underneath)

The plan (approved with the recommendations): the Ultra Admin decides what
each account may use. Five layers decide a module or setting, each only
able to narrow the one before it - the client's **package**, the Ultra
Admin's **exceptions for that client**, the **client's own switches**
(only for modules left to them), a **branch's** narrowing and a
**login's** powers. Stage 1 puts the rules in place without changing what
anyone sees.

- **Packages** (`app/models/package.py`, table `packages`): each module
  *Off* (hidden, its pages 404), *Included* (always on, the client can't
  switch it off) or *Client's choice*; each setting "Client can change"
  or "Only ARFA"; limits on branches, branch logins and GSTINs; own
  branding yes/no; monthly and yearly price. A package belongs to the
  firm, so it has no `tenant_id` (like `platform_profile`).
- **A client's package and exceptions** (`tenant_packages`, one row per
  client, with `tenant_id`): the package, and any single module, setting
  or limit changed for this client only. `read_only_modules` lists
  modules taken away while they held data (stage 3 fills it).
- **Auditor powers** (`auditor_powers`, one row per auditor): onboard
  clients, choose packages, switch a client's Client's-choice modules,
  pause, act as, reset logins, month-end exports, create Sub-Auditors -
  plus the packages they may give and client limits. No row means the
  role's powers as before (`ROLE_DEFAULT_POWERS`).
- **One answer for every check**: `app/utils/entitlements.py`.
  `module_mode()` says on / off / read-only, and `Tenant.has_feature()`
  asks it - so menus, search, route guards and saves all follow the
  package. `client_may_switch()`, `setting_locked()`, `limit_of()`,
  `own_branding()` and `auditor_can()` answer the rest. Every change
  (`assign_package`, `set_module_exception`, `set_setting_exception`,
  `set_auditor_powers`) is audit-logged with who made it.
- **Settings > Modules** saves only the modules left to the client; an
  Included or Off module can't be changed from there, even by a crafted
  post.
- **Starting packages** (all editable later): Current setup, Billing,
  Retail, Restaurant, Pharmacy, Mobiles & Electronics, Wholesale,
  Everything - built from the business-type presets, prices left blank.
- **Nothing changes for anyone**: the migration puts every existing
  client on *Current setup* (every module their own choice, every
  setting theirs, no limits); a client with no package row behaves the
  same; no auditor has a powers row, so every auditor keeps today's
  powers.
- Tests: `tests/test_entitlements.py` - no change without a package or on
  Current setup, the migration's packages match the code, package /
  exception / client switch layering, read-only, a module switched off
  by the package is a 404, Settings saves only the client's choices,
  settings locks / limits / branding, the audit trail, one client's
  package never touching another's, auditor power defaults and per-
  auditor powers.

## Roadmap

- **Platform build (ARFA general-purpose POS), phase by phase.**
  Done (see Design notes):
  - Phase 1: business type + feature flags, tax-inclusive pricing, item
    master, HSN/AATO rule, bulk import/export, `flask backup`, per-staff
    permissions.
  - Phase 2: POS for every business.
  - Phase 3: stock ledger and locations, purchases with ITC, supplier
    returns, purchase orders, GSTR-2B match, ITC in 3B, batches/FEFO,
    serial/IMEI, variants, stock documents, stock reports, barcode
    labels.
  - Phase 4: expense vouchers with GST/ITC, categories, recurring
    drafts, POS petty cash, expense report, expense ITC in 3B.
  - Phase 5: margin scheme (Rule 32(5)) - margin math, lots by IMEI /
    FIFO, separate MSI invoices, no ITC on margin purchases, GSTR-1 HSN
    margin + 0% rows, margin register.
  - Phase 6: customer and supplier accounts (receipts, payments,
    allocations, cheques, ageing, statements, WhatsApp reminder, credit
    warning), advance receipt / refund vouchers with GST (GSTR-1
    11A/11B, 3B), quotation / proforma / sales order / delivery challan
    with one-click invoicing, void & re-issue, recurring invoices, cash
    and bank book.
  - Phase 7: P&L (COGS from the stock ledger), gross profit by item,
    daily summary, dashboard, GSTR-3B set-off, GSTR-1 B2CL and HSN
    B2B/B2C, purchase/expense registers, Tally XML, filing tracker,
    monthly pack.
  - Phase 8: e-invoice (applicability, INV-01 JSON + validator, offline
    upload path, NIC / GSP providers with encrypted logins, IRN + QR,
    cancel within 24 h) and e-way bills (invoices, challans, transfers;
    JSON, API by IRN or standalone, vehicle update, cancel, validity).
  - Phase 9: restaurant (tables, KOT, order types, charges, sold out),
    weighing-scale labels, pharmacy (schedules, register, substitutes,
    strips), electronics warranty, jewellery (metal rates, 3% / 5% job
    work, old gold after GST), job cards, wholesale (groups, slabs,
    salesmen), loyalty.

  - Phase 10: offline billing (per-device series, service worker,
    retry queue), Android ESC/POS printing / drawer / kiosk / update
    check, two-step sign-in, lockout, idle sign-out, cashier PINs,
    Sentry + /healthz, first-run checklist, platform subscription
    invoices, POS bill / note drill-downs, daily owner summary.

- **Interface redesign** (plan approved with the recommendations: side
  menu, dark mode following the device, full POS re-layout keeping the
  shortcuts, the three bundled fonts), in six stages:
  - Stage 1 (done): design tokens, bundled fonts and icons, grouped side
    menu with rail and phone drawer, search (Ctrl K), New menu, bell,
    account menu, phone bottom bar, toasts, motion with reduced-motion
    respected.
  - Stage 2 (done): Settings in sections (Business, Appearance, Modules,
    Counter, Documents & printing, Sign-in & security, Subscription),
    module switches grouped by purpose with "Set up" links, per-section
    save with a sliding save bar.
  - Stage 3 (done): list / record / form patterns on every template, one
    primary action per page with the rest under More, the inline styles
    replaced by shared classes, empty states, phone-friendly tables,
    notes as a tab inside Invoices.
  - Stage 4 (done): Home dashboard (trend, comparisons, Needs you,
    count-up figures, Indian money format) and the Reports hub (grouped,
    searchable).
  - Stage 5 (done): the counter - full screen, category chips, larger
    tiles, held bills in a drawer, Counter menu, one-tap payment buttons,
    fly-to-bill and paid animations, phone bill bar; restaurant tables and
    the PIN pad to match (KOT slips print as before).
  - Stage 6 (done): dark mode following the device with a per-device
    Light / Dark choice, split-screen branded sign-in pages, the firm
    console's Clients page and client page, accessibility pass.

- **Packages and powers** (plan approved with the recommendations: keep
  every existing client's setup until a package is assigned, list
  modules outside the package as "Ask ARFA to add", read-only for a
  removed module that holds data, auditors choose only from packages
  allowed to them, packages and auditor powers without a client id), in
  six stages:
  - Stage 1 (done): packages, each client's package and exceptions,
    auditor powers, and the one set of rules every check uses; every
    existing client on "Current setup" - no change for anyone.
  - Stage 2: packages in the console - the package editor, a client's
    "Package & modules" with exceptions, onboarding by package, price
    into the subscription fee.
  - Stage 3: clients see their package - locked modules and settings,
    "Ask ARFA to add", read-only for removed modules with data.
  - Stage 4: auditor powers on each auditor's page, enforced on every
    console page and in "Act as".
  - Stage 5: branch logins by role, modules per branch, the Ultra
    Admin's per-login switches, limits with usage, branding by package.
  - Stage 6: change history per client, checks, hand-over.

  Stage 1 limits:
  - Stage 3 changed pages through shared CSS and two small scripts, not
    template by template, so a page's content order is as it was; pages
    that need more than that are reworked in stages 4 and 5.
  - The bell counts what the modules already know (offline bills, low
    stock, expiry, recurring drafts); overdue customer balances stay on
    the dashboard.

  Phase 10 limits:
  - The Android app compiles only in GitHub Actions (this sandbox can't
    reach Google's Maven); the Bluetooth / USB printing code has not been
    run on a real printer here - try "Print a test page" first.
  - Offline: plain bills only (see Design notes); a PIN can't be checked
    offline, so the cashier on the counter stays; a device whose storage
    is wiped while bills wait loses those bills (their numbers then show
    as gaps until cancelled on the Offline bills page).
  - A day closed (day open/close) before its offline bills arrive: they
    are booked to the counter's open day, if any.
  - The update check reads a public release; with a private repository
    the app can't see it (download the APK from Actions instead).
  - Email needs an SMTP server (`SMTP_HOST`, `SMTP_FROM`, optional
    `SMTP_USERNAME` / `SMTP_PASSWORD`); WhatsApp sharing needs nothing.
  - The invoice form, note form and Live dashboard still call Alpine's
    `init()` twice (harmless there, not changed in this phase).

  Phase 9 limits:
  - KOTs print through the browser's print dialog like receipts - pick
    the kitchen printer there; direct printing to a kitchen printer
    comes with Phase 10's ESC/POS.
  - Prescriptions, pack units, old gold, points and jewellery pricing are
    at the counter (POS); invoices take the price you type (the wholesale
    price is suggested).
  - Service and packing charges use one GST rate (the restaurant setting).
  - Old gold bought isn't added to stock as bullion; the voucher register
    is the record.
  - A table's running order can't be split into separate bills.

  Phase 8 limits:
  - The NIC and GSP adapters follow the published protocol but have only
    been run against simulated servers - the first real sandbox login
    should be tried with "Test login" before going live.
  - Counter (POS) bills to a registered buyer are not e-invoiced - B2B
    sales have to go through Invoices.
  - SEZ supplies (SEZWP / SEZWOP), deemed exports, export shipping
    details (port, shipping bill) and the buyer's country code aren't
    captured; ship-to / dispatch-from on the e-invoice itself aren't sent
    (the e-way bill has both).
  - No Part-B update through the API for bills recorded from the portal
    unless a login is saved; no e-way bill extension, consolidated EWB or
    multi-vehicle movement (do those on the portal).
  - The bulk e-way bill JSON uses the portal tool's format version
    1.0.0621 (`eway.BULK_VERSION`) - update it if the portal asks.

  Phase 7 limits:
  - Purchases in COGS are dated by when the stock arrived in the ledger,
    so a bill entered late lands in the month it was entered.
  - Round-off on documents is not a P&L line.
  - Returns / credit notes against B2C sales stay in B2C Small (no CDNUR
    table); B2CL lists invoices only.
  - The filing tracker is for monthly filers (no QRMP dates).
  - The Tally export uses one "Bank" and one "Cash" ledger; advances are
    plain receipts (no GST-on-advance entries in Tally).

  Phase 3 limits, stated plainly:
  - Purchase orders have no PDF.
  - The GSTR-2B Excel reader handles the B2B sheet only (not CDNR/ISD).
  - Batch/serial pickers are at the POS only: invoices sell FEFO
    automatically and don't carry serials.
  - Stock valuation is weighted average only (no FIFO).

  Phase 5 limits:
  - The margin-scheme flag is per item, not per individual IMEI unit.
  - Margin goods need a purchase bill (Purchases module); opening stock
    entered as an adjustment has no purchase price to work from.
  - The HSN 0% remainder row carries no quantity (the units are on the
    margin row).

  Phase 6 limits:
  - Ageing is by bill date; on-account money is shown beside the
    buckets rather than set against the oldest bill automatically.
  - A delivery challan doesn't move stock (stock moves when it is
    invoiced; own-location moves use Stock -> Transfers).
  - Sales documents can't be edited once saved - cancel and copy.
  - An advance received and adjusted in the same month is reported in
    both 11A and 11B (the net is the same; GSTN allows leaving it out).
  - Bank book is one account (no per-bank books, no reconciliation).

  Phase 4 limits:
  - One GST rate per expense: a multi-rate bill is entered as one
    expense per rate.
  - Expense bills aren't matched against GSTR-2B (purchase bills are).
  - Drafts appear when the Expenses page is opened or the cron command
    runs; there is no built-in scheduler.
- **Document types** - all done: Tax Invoice, Bill of Supply,
  Export/RCM, margin-scheme invoice, credit/debit notes, delivery
  challans (own-location transfers since Phase 3; job work / approval
  since Phase 6), receipt and refund vouchers for advances (Phase 6).
- **Onboarding walkthrough polish** - still just the plain onboarding form;
  a guided first-run checklist for a new Client Admin (add a product, add a
  customer, issue the first invoice) hasn't been built.
- **Done, with explicit scope limits:**
  - **GSTR-1/3B** (`app/reports/gstr.py`) are *working papers* computed
    from this tenant's own invoices/POS bills/notes, not an auto-filer or
    an exact replica of the government JSON schema. **Input Tax Credit**
    is only there for clients with the Purchases or Expenses module
    (table 4 and the 6.1 set-off; for everyone else 3B shows gross output
    tax only). **B2C Large** (inter-state, unregistered buyer, invoice
    above Rs. 1 lakh - `gst_config.B2CL_INVOICE_VALUE_THRESHOLD`,
    Notification 12/2024-CT, from 01.08.2024) is listed invoice by
    invoice since Phase 7; returns against B2C supplies stay in B2C Small
    (no separate CDNUR table). Everything else - taxable value, CGST/SGST/
    IGST, the B2B/B2C split by whether the customer has a GSTIN on file,
    HSN-wise summary, and the document-number-series summary - is exact,
    computed straight from the tenant's own records. Excel/PDF/JSON
    export all three.
  - **Admin console cross-client drill-down**: Super Admin can read a
    client's invoices in full (line items, totals, linked notes, PDF) and
    see recent POS bills/notes in summary, all audit-logged and never
    editable from the admin side. POS bills and notes don't have their
    own full drill-down pages yet - only invoices do.
  - **Bulk GST export**: Super Admin picks a month and any number of
    Regular-scheme clients with a GSTIN on file, downloads one ZIP with
    a GSTR-1 and GSTR-3B workbook per client.
  - **e-Invoice**: since Phase 8 the IRN columns are filled - by the
    client's own NIC / GSP login, or by recording what the IRP's offline
    tool returned. Without a login there is still no "generate" button:
    nothing pretends to call an API it can't reach.
