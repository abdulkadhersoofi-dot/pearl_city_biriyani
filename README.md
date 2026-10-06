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
| Super Admin (the firm) | Onboard/deactivate clients, read-only cross-tenant view + export, full audit log, sets/resets Client Admin passwords directly |
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
├── pos/             # POS module: terminal, hold/resume, refunds, Z-report
├── notes/           # Credit/Debit notes, created from an existing invoice
├── reports/         # gstr.py (aggregation), export.py (Excel/PDF/JSON) -
│                     # GSTR-1/3B working papers, see Roadmap for scope
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
- **Tenant access gating** (`app/utils/billing.py`, `Tenant.valid_until` /
  `Tenant.next_billing_due`) - three independent gates, any one of which
  cuts a tenant's Client Admin and every branch under it off from
  signing in, checked both at login and on every request of an already
  open session (`app.__init__._register_request_hooks`), not just at
  login: a Super Admin's manual **pause** (`Tenant.is_active`, pre-existing,
  now actually enforced - previously this flag was set by the
  deactivate/activate routes but nothing ever checked it outside the
  tenant's own login-slug lookup, so a deactivated client's users could
  still sign in and work); a hard **expiry date** (`valid_until`, required
  at onboarding, moved forward only by a Super Admin's "Renew / extend"
  on the client's own page - nothing else changes it); and a recurring
  **monthly billing reminder** (`next_billing_due`, set to one month from
  onboarding and advanced by exactly one more month, from whatever it
  already was, only by a Super Admin's "Mark this month paid" - a
  calendar-correct `add_one_month()` so e.g. the 31st of a 31-day month
  lands on the 28th/29th of February rather than overflowing into
  March). Monthly billing has a `BILLING_GRACE_DAYS` (3) window after
  the due date: an in-app banner (`billing_notice` in `base.html`, shown
  to that tenant's own users on every page) warns during the grace
  window without blocking anything; only once the grace window itself
  elapses unpaid does it also block, the same way the other two gates
  do. A tenant onboarded before this feature existed has both fields
  unset and is never blocked on either until a Super Admin opts it in -
  the migration adding these columns deliberately backfills nothing, so
  shipping this never silently locks out an existing client.
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

## Roadmap

- **Remaining document types** - Delivery Challan and Receipt/Refund
  Voucher don't fit the itemized-invoice shape the Invoicing module uses
  (no GST calc the same way, or tied to an advance payment rather than a
  sale) and still need their own flow. Tax Invoice, Bill of Supply, and
  both Export/RCM variants already work through Invoicing today.
- **Onboarding walkthrough polish** - still just the plain onboarding form;
  a guided first-run checklist for a new Client Admin (add a product, add a
  customer, issue the first invoice) hasn't been built.
- **Done, with explicit scope limits:**
  - **GSTR-1/3B** (`app/reports/gstr.py`) are *working papers* computed
    from this tenant's own invoices/POS bills/notes, not an auto-filer or
    an exact replica of the government JSON schema. Two things it can't
    do: **Input Tax Credit isn't tracked** (this app records sales only,
    so GSTR-3B shows gross output tax, not the net cash payable - add ITC
    from purchase records before filing), and **B2C is one aggregated
    bucket**, not split into Large/Small by GSTR-1's Rs. 2.5 lakh
    inter-state threshold. Everything else - taxable value, CGST/SGST/
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
  - **e-Invoice IRN/QR hook**: `Invoice.irn`/`irn_ack_number`/
    `irn_ack_date`/`qr_code_data` columns exist and the invoice view/PDF
    already render them when set - but nothing populates them. This is
    deliberately just the extension point: a real integration needs NIC
    IRP (government e-invoice portal) credentials this deployment doesn't
    have, so there's no fake "generate e-Invoice" button pretending to
    call an API that isn't there.
