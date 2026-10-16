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
│                     # open/close), share.py (UPI QR, share links)
├── notes/           # Credit/Debit notes, created from an existing invoice
├── reports/         # gstr.py (aggregation), export.py (Excel/PDF/JSON) -
│                     # GSTR-1/3B working papers, see Roadmap for scope
├── einvoice/        # e-Invoice IRN + e-way bills (Phase 8): rules.py
│                     # (when they apply), schema.py (INV-01 JSON),
│                     # providers/ (NIC sandbox/direct, GSP), vault.py
│                     # (encrypted logins), services.py, eway.py, errors.py
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

  Next, in order: 9 business-type modules, 10 platform polish.

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
