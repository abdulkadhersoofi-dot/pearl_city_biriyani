# ARFA — Project Context for a New Claude Code Session

This file exists purely to onboard a **fresh Claude Code chat** onto this
project quickly. Paste/attach this file at the start of a new session so
it doesn't need to rediscover any of this from scratch. It is a snapshot
written at a specific point in the project's life — always cross-check
anything load-bearing against the actual repo state (`git log`, `git
status`, the code itself) before acting on it, since the repo keeps
moving after this file is written.

**Read `README.md` and `android/README.md` in full before doing
anything else.** They are this project's own living documentation —
every shipped feature has a dense, technically precise "Design notes"
bullet in `README.md` explaining what it does and *why* it's built the
way it is (not just what). This file does not repeat that content; it
gives you the parts README.md doesn't cover: the chronological history
of how the project got here, the operational/tooling knowledge, the
conventions that were established along the way, and what's honestly
still outstanding.

---

## 1. What this project is

A multi-tenant, GST-compliant billing and POS web application, built
for a Chartered Accountant / GST practice firm (the "platform owner")
to provide to its own small-business clients (e.g. restaurants). The
platform is branded **ARFA**. One of those clients, used as the
working example throughout this repo/session, is "Pearl City
Biriyani" / "Spice Route Kitchen" — hence the repo name.

- **Repo**: `abdulkadhersoofi-dot/pearl_city_biriyani` on GitHub
- **Working branch**: `claude/epic-keller-g2gvbr` (all work in this
  session happened here — check `git branch`/`git status` in a new
  session to confirm this is still current)
- **Live deployment**: `https://gst-billing-app-prdx.onrender.com`
  (Render.com free-tier Blueprint deploy — see README.md's Deployment
  section). The user intends to move to their own domain eventually;
  `android/app/build.gradle`'s `SERVER_BASE_URL` is the one place that
  needs updating when that happens.
- **User's own account email** (for attribution/identification only):
  `abdulkadhersoofi@gmail.com`

## 2. Stack (see README.md "Stack" for the authoritative version)

- Python 3.11 / Flask, server-rendered HTML + HTMX + Alpine.js for
  in-page interactivity. **Alpine/HTMX/every JS dependency is vendored
  locally** under `app/static/js/vendor/` — nothing loads from a CDN,
  by design, so the app keeps working on a client's offline/LAN
  network. Any new client-side feature must follow this rule.
- PostgreSQL, one shared database, every business table carries
  `tenant_id` (multi-tenancy by row-filtering, not separate schemas/DBs).
- Flask-Login + Redis-backed server-side sessions.
- WeasyPrint for all PDF generation (invoices, notes, POS receipts,
  GST export PDFs).
- Gunicorn + Nginx for self-hosted deploys; Render Blueprint
  (`render.yaml`) for the quick-start deploy currently in use.
- Alembic (Flask-Migrate) for migrations.
- pytest against a **real Postgres database**, not sqlite (Postgres-
  specific behavior — row locking, native JSON/Enum columns — is
  exercised faithfully).

## 3. Roles and exact powers

Five roles, strictly hierarchical: **Ultra Admin → Auditor →
Sub-Auditor → Client Admin → Staff/Branch**. The stored enum value for
Ultra Admin is still `SUPER_ADMIN` (`UserRole.SUPER_ADMIN`) — "Ultra
Admin" is purely the UI label, don't go looking for a `ULTRA_ADMIN`
role in the code. Every login's password is set directly by whoever
creates it (no OTP, no email step anywhere in the app); the new user
is forced to change it on their first sign-in.

### Ultra Admin (Super Admin) — unrestricted, the only billing authority

- Sees and can act on **every** Auditor, every client, every branch —
  no scoping at all (`auditor_tenant_ids()` returns `None` for this
  role specifically, meaning "no filter").
- **The only role that can touch billing**, for both clients and
  Auditors/Sub-Auditors: verify a pending client/Auditor and set its
  initial billing cycle, change an already-active one's cycle later
  (Monthly ⇄ Yearly), mark a period paid, resync a drifted cycle back
  to the live calendar, and trigger/mute the manual billing alarm.
  Nobody else — not an Auditor, not a Client Admin — can do any of
  this, for anyone, ever. This was deliberately repeated and reinforced
  across several rounds of this session (§5.2–§5.5) specifically
  because the firm rejected any version where billing authority
  leaked to another role.
- **Reassigns a client between Auditors**, or back to unallocated
  (`tenants.reassign_auditor`) — an Auditor can only move a client
  *they already have* to *their own* Sub-Auditor; moving a client
  anywhere else, or taking it away from an Auditor entirely, is
  Ultra-Admin-only.
- **Resets an Auditor's (or any of their Sub-Auditors') own login
  password** (`tenants.reset_auditor_password`) — deliberately
  *unscoped*: reachable for any Auditor/Sub-Auditor in the system, not
  just ones a particular Auditor manages, for the same reason billing
  is unscoped (§5.6).
- Resets a Client Admin's or a Staff/branch login's password directly
  (`tenants.reset_client_password`).
- Activates/deactivates any single user anywhere — a specific branch,
  or a Client Admin itself — directly from that client's admin page.
- **"Act as"** any Client Admin or branch login (`tenants.act_as`),
  *and* any Auditor-hierarchy account (`tenants.act_as_auditor`) —
  signs in as that user via `flask_login.login_user`, stashing the
  admin's own id in `session['impersonator_id']`, so every existing
  tenant-scoped route/template/business-logic function just works
  unmodified, no parallel "pretend tenant_id" plumbing anywhere.
  Impersonation deliberately **bypasses the tenant-access gate** (a
  paused or billing-overdue client can still be reached this way — the
  Ultra Admin needs to actually get in to fix the problem, e.g. mark
  it paid). Both starting and stopping impersonation are written to
  `AuditLog`.
- Full audit log, cross-client GST exports (bulk ZIP of GSTR-1+3B for
  any number of Regular-scheme clients at once).
- Onboards clients/Auditors too (same `tenants.onboard()` any role can
  call), but — critically — onboarding **never** sets a billing cycle
  for anyone, including when the Ultra Admin does it. Every new
  client/Auditor always lands "pending" first; a *separate*,
  deliberate Ultra-Admin action is required to lift that.

### Auditor — same operational power as Ultra Admin, but scoped to their own allocated clients

- Can onboard clients, pause/reactivate clients, and **"act as"** a
  client (which is also how an Auditor creates branches for a client
  they manage — there's no separate "Auditor creates a branch" UI, it
  reuses the exact impersonation path the Ultra Admin already had).
- **Scope**: only clients with `Tenant.auditor_id` pointing at this
  Auditor, *plus* every client allocated to any of their own
  Sub-Auditors. Every admin-console route checks this via
  `assert_auditor_owns_tenant`/`assert_auditor_owns_auditor` before
  acting — an Auditor can never read or act on a client outside this
  set, even by guessing an id in the URL.
- **Never sets a billing cycle for anyone** — a client an Auditor
  onboards lands pending exactly like one the Ultra Admin onboards;
  only the Ultra Admin can lift that.
- **Can create their own Sub-Auditors**, and can allocate any client
  *currently allocated to them* to one of *their own* Sub-Auditors
  (`tenants.allocate_client_to_sub_auditor`) — checked on both ends
  (the tenant must already be theirs; the target must be a Sub-Auditor
  with `parent_auditor_id` pointing at this Auditor), so an Auditor can
  never hand a client to someone outside their own office.
- Can **"act as"** their own Sub-Auditors too (`tenants.act_as_auditor`).
- Has its own billing fields (`billing_cycle`, `cycle_anchor_date`,
  `next_billing_due`, `manual_alarm_active`) — **the firm bills an
  Auditor's own office the same way it bills a client**, including
  starting "pending" until the Ultra Admin verifies them.
- Sees the shared admin console (`tenants/directory.html`) exactly as
  the Ultra Admin does, just pre-filtered to their own scope — no
  separate template to maintain.

### Sub-Auditor — scoped to only what one specific Auditor hands them

- Created by exactly one Auditor ("within their office") —
  `User.parent_auditor_id` names that Auditor.
- **Scope**: only the clients that Auditor has *explicitly* allocated
  to them (`Tenant.auditor_id` pointing at this Sub-Auditor) — never
  that Auditor's full client pool automatically, and never another
  Sub-Auditor's allocated clients.
- Otherwise runs the same console as an Auditor for whatever's in that
  allocated set.
- Also starts "pending" with its own billing fields, same as an
  Auditor — the hierarchy is exactly two levels deep (Auditor →
  Sub-Auditor); nothing in the code supports a Sub-Auditor creating a
  further level under themselves.

### Client Admin — the business owner, full control of their own tenant only

- Owns their tenant's GSTIN(s), invoice numbering series (own editable
  prefix per GSTIN/doc-type/financial-year), product/customer catalog.
- Creates/deactivates branch (Staff) logins and **sets/resets their
  passwords directly** (same "set it yourself, they change it on next
  sign-in" pattern used one level up).
- Full invoicing, POS, Reports, and the Live dashboard — scoped
  automatically to their own `tenant_id` via `tenant_query()`; can
  never see another tenant's data even by guessing an id.
- Settings: branding (logo upload, display name, their own
  `/auth/login/<slug>` branded sign-in page), full site theme
  (button/background/text colors, font), the category/sub-category
  on/off toggle, default POS print size.
- **Their own first successful login** (after changing the password
  the Ultra Admin set for them) is the one moment that stamps their
  tenant's `cycle_anchor_date`/`next_billing_due` for the first time —
  never re-stamped on any later password change.
- Cannot change their own billing cycle, trigger/mute their own
  billing alarm, or reassign themselves to a different Auditor — all
  Ultra-Admin-only, see above.

### Staff / Branch — POS billing only, nothing else

- **No nav bar at all** in the web app — POS is the only destination,
  by design (this is exactly the constraint the Android app in §6
  enforces natively, by reading which page the server's own post-login
  redirect sent the user to).
- A branch is typed at creation (`User.branch_type`, set by the Client
  Admin): **PCB-owned** (the kitchen's own branch — any invoice billed
  to it is always forced to a Delivery Challan with no GST, regardless
  of what document type was picked in the form) or **third-party** (an
  independent reseller — always forced to a Tax Invoice with GST
  applied normally). This override happens server-side in
  `create_invoice` itself, never left to the operator's choice.
- Sees their own running "today's stock" counter (view-only — it's
  informational, never a checkout limit; a branch can sell past its
  allocation and the counter is allowed to go negative, shown in red,
  rather than silently floored at zero).
- Cannot see any other branch's data, any other tenant, or anything
  above POS billing.
- **Per-login permissions** (Branches -> Permissions, Phase 1 of the
  platform build): give discounts (+ max %), change price at billing,
  refund, void, see Day-book/Z-report, adjust stock. Defaults equal the
  old behaviour (refund + Day-book/Z-report on, the rest off) and are
  enforced server-side (`app/utils/permissions.py`).

## 4. Full feature inventory (what's actually shipped)

Everything below exists in working, tested code right now. For full
rationale/implementation detail on any of these, search README.md's
"Design notes" section (each bullet names the exact files involved) —
this list is just the index.

- **Tenant/user management**: onboarding, directory, role hierarchy,
  audit log, impersonation ("Act as"), per-user activate/deactivate,
  password resets (direct-set, no OTP/email anywhere in the app).
- **Multi-tenancy & access gating**: `tenant_scope.tenant_query()`,
  `auditor_scope.auditor_tenant_ids()`, pending/paused/billing-overdue
  gates with a grace-period banner, checked at login *and* on every
  request of an already-open session.
- **Billing/subscription model**: Monthly/Yearly cycles, changeable at
  any time, no hard expiry date, idempotent "mark period paid",
  separate explicit "Resync to live calendar" repair action, manual
  alarm trigger/mute, all mirrored identically for a Tenant (client)
  and for an Auditor/Sub-Auditor's own account.
- **Invoicing**: Tax Invoice / Bill of Supply / Export / RCM Invoice,
  gated by registration type, PDF generation, per-tenant/per-GSTIN/
  per-FY/per-document-type numbering with row-locked allocation, never
  hard-deleted (void + reason instead).
- **Credit/Debit Notes**: always tied to an existing invoice, inherit
  its GST treatment, own numbering series.
- **POS**: touchscreen checkout, 3-tap flow, direct-to-printer thermal
  receipts (58mm/80mm) + A4 copies sized in real inches (not a broken
  CSS "auto"), hold/resume with a **persistent left sidebar** (not
  collapsible), refunds (two-entry model, not a flipped status),
  Day-book, Z-report, category/sub-category grouping (opt-in per
  tenant, optional even when on).
- **Branches & stock**: a branch is PCB-owned (forced Delivery
  Challan, no GST) or third-party (forced Tax Invoice, GST applied) —
  decided server-side in `create_invoice`, never the operator's
  choice. Stock is an append-only ledger, no checkout limit ever
  (remaining can go negative, shown honestly).
- **Reports**: Sales report (any range, both invoices+POS), Stock
  report, Sales-by-item report — **all three share one filter bar**
  (day/month/range period + single-or-multi branch), Excel/PDF/JSON
  exports. GSTR-1/GSTR-3B working papers (explicit scope limits: no
  ITC tracking, B2C not threshold-split — see README Roadmap).
- **Live dashboard** (top-nav "Live", Client Admin only): F1-style
  branch leaderboard ranked by today's revenue with a live CSS-driven
  reordering animation, best-seller pie chart (hand-rolled SVG, no
  charting library), today's stock — polls a JSON snapshot endpoint.
- **Branding/theming**: platform branding (ARFA) vs. per-tenant
  branding (logo, display name, per-tenant login slug, full color/font
  theme), uploaded images stored in the database (not local disk —
  survives restarts/redeploys), product photos auto-cropped to 512×512.
- **Admin console**: Super Admin cross-client drill-down (invoices in
  full; POS bills/notes in summary only — full drill-down for those is
  still outstanding), bulk GST export (ZIP of GSTR-1+3B per client).
- **Android app** (`android/`): see §6.
- **Platform build Phase 1** (from `ARFA_BUILD_PROMPT.md`'s 10-phase
  plan): business type + per-tenant feature flags (`Tenant.features`
  JSON, `app/utils/features.py`; `category_wise_items` folded into
  `features.categories`), tax-inclusive pricing per item/line
  (`gst.compute_line(price_includes_tax=...)`), GST rates/HSN/UQC in
  `app/utils/gst_config.py`, item master (sku, barcode, mrp, prices,
  UQC unit, pack unit, service flag; `default_price` -> `sale_price`),
  Excel import/export (`app/bulk/`), `flask backup`, per-staff
  permissions.
- **Platform build Phase 2** (POS for every business): line/bill
  discounts (% or Rs., capped per login), price edit + price lists,
  split payments (`POSPayment`), cash tendered/change, UPI QR (segno),
  customer lookup/quick-add (B2B POS bills), barcode/keyboard shortcuts,
  decimal quantities for KGS/LTR..., favourites row, item-wise sales
  returns/exchange (`POSReturn`, series SR), same-day voids, day
  open/close per counter (`CashSession`), WhatsApp share links,
  DUPLICATE COPY on reprints.
- **Platform build Phase 3** (inventory & purchases, flags `inventory`,
  `purchases`, `variants`, `batch_expiry`, `serial_imei`): append-only
  stock ledger `StockMovement` posted for every client (flags only add
  screens + the no-negative-stock rule), locations incl. auto branch
  locations, weighted-average cost, stock adjustments (ADJ) and
  transfers (TDC + Delivery Challan PDF), suppliers (+ bulk import),
  purchase bills (PB) with per-line ITC eligibility / RCM / landed cost
  / private attachment (`app/utils/itc.py`; no ITC for unregistered or
  composition clients), supplier returns (PDN), purchase orders (PO),
  GSTR-2B JSON/Excel import + match (`app/purchases/gstr2b.py`), ITC in
  the GSTR-3B working paper (3.1(d), table 4 - only with Purchases on),
  batches/FEFO/expired-sale block, serial/IMEI capture + POS picker +
  receipt print, size/colour variants, stock reports, barcode labels.
- **Platform build Phase 4** (expenses, flag `expenses`): expense
  vouchers (EXP series, drafts, voids) with GST bills computed by
  `app/utils/itc.compute_expense_gst`, seeded editable categories,
  recurring expenses making drafts (`flask expenses-generate-drafts`),
  POS cash-out booked as petty-cash expense, expense report
  (category/branch/month, Excel/PDF), expense ITC in GSTR-3B table 4.
- **Platform build Phase 5** (margin scheme, flag `margin_scheme`):
  `app/utils/margin.py` (Rule 32(5) math), `app/margin/services.py`
  (lots by IMEI / FIFO via `MarginAllocation`, release on void/refund,
  returns), separate MSI-series margin invoices at POS and in Invoicing
  (mixed sales refused - the user's decision), no ITC on margin
  purchases, GSTR-1 HSN margin row + 0% remainder row, margin register
  (Excel/PDF), editable declaration in Settings.
- **Platform build Phase 6** (flags `receivables`, `payables`,
  `sales_documents`, `recurring_invoices`, `cash_bank_book`):
  `app/accounts/` (ledgers computed from documents, receipts RCT /
  advance receipt vouchers RV / refund vouchers RFV / payment vouchers
  PAY with allocations and cheque status, ageing, statement PDF,
  WhatsApp reminder with signed statement link, credit-limit warning,
  cash & bank book), `app/utils/advance_gst.py` (GST on advances,
  GSTR-1 11A/11B and 3B net via `app/reports/advances.py`), `app/sales/`
  (quotation QTN, proforma PI, sales order SO, delivery challan DC -
  one-click or edit-then invoice; recurring invoices as numberless
  draft runs, `flask recurring-invoices-generate`), void & re-issue.
  Expense vouchers moved from prefix EXP to EV.
- **Platform build Phase 7** (flag `business_reports` for the client
  side): `app/finance/` - `pnl.py` (P&L from documents as account lines,
  COGS from the stock ledger, comparison, per location, cash-basis
  fallback), `sales_analysis.py` (item / category GP, daily summary),
  `dashboard.py`, `journal.py` + `tally.py` (balanced vouchers, TallyPrime
  XML), `filing.py` (due dates, tracker, monthly pack);
  `app/utils/gst_setoff.py` (Rule 88A set-off, GSTR-3B 6.1);
  `app/reports/registers.py`; GSTR-1 B2CL (Rs. 1 lakh in gst_config) and
  HSN B2B/B2C; `FilingStatus` table; admin Filing page, monthly pack and
  Tally export; bulk GST export now ships monthly packs.
- **Platform build Phase 8** (flags `einvoice`, `eway_bill`):
  `app/einvoice/` - `rules.py` (AATO > Rs. 5 crore, B2B / export and
  their notes; 30-day IRP limit at Rs. 10 crore; e-way bill > Rs. 50,000,
  validity per Rule 138(10) - all numbers in gst_config), `schema.py`
  (INV-01 1.1 builder + validator, plain-language problems), JSON
  download for the IRP offline tool and recording the IRN it returns,
  `providers/` (one interface; NIC sandbox / direct with RSA + AES-256
  Sek, generic GSP via settings; tested against simulated servers only),
  `vault.py` (Fernet, `EINVOICE_SECRET_KEY` or derived from SECRET_KEY),
  `services.py` (every call logged in `gov_api_calls` + audit, one
  re-login retry, duplicate IRN 2150 fetched), IRN + signed QR rendered
  with segno, cancel within 24 h then void (blocked while an e-way bill
  is active; plain void blocked while the IRN is active; after 24 h ->
  credit note), `eway.py` (invoices, delivery challans, stock transfers;
  bulk JSON, generate by IRN or standalone, Part B, cancel). Tables
  `einvoice_credentials`, `gov_api_calls`, `eway_bills`; Gstin got city
  and PIN. With `einvoice` on, LUT exports use an `EXL` series (16-char
  IRP limit).
- **Platform build Phase 9** (flags `restaurant_mode`, `weighing`,
  `pharmacy`, `electronics`, `jewellery`, `job_cards`, `wholesale`,
  `loyalty`; the migration switched the four that presets already
  carried off for existing clients): `app/pos/business.py` is the one
  place the counter calls into the modules, `/pos/quote` prices the cart
  server-side for the screen. `app/restaurant/` (tables = HELD bills with
  table_id, delta KOTs, move/merge/cancel, order-type price lists,
  packing/service charge lines at 5%, sold-out-today), `app/utils/
  scale_barcode.py`, `app/pharmacy/` (schedules + register, substitutes;
  strips via POSBillLine.unit_factor), electronics warranty from the sale
  (`/jobs/warranty`), `app/utils/jewellery.py` + `app/jewellery/` (metal
  rates, 3% jewellery, 5% SAC 9988 job work, OldGoldPurchase vouchers
  series OG paid as PaymentMode.OLD_GOLD after GST), `app/jobs/` (JobCard
  series JOB, parts from stock, invoice hand-off), `app/wholesale/`
  (groups, slabs, salesmen, report), `app/loyalty/` (LoyaltyEntry ledger,
  PaymentMode.LOYALTY). Settings in `Tenant.module_settings`
  (`app/utils/module_settings.py`).
- **Platform build Phase 10** (flags `offline_pos`, `cashier_pin` opt-in;
  `daily_summary` in new clients' dashboard presets): offline billing
  with a per-device series (owner's [ASK] answer) - `CounterDevice`
  (code O1..O99, its own `InvoiceSeries.device_id`), `OfflineBill` (what
  each device sent, SYNCED / FAILED / VOIDED), `app/pos/offline.py` +
  `app/pos/counter.py` routes, `static/js/pos_offline.js` (IndexedDB
  queue, device numbering) + `pos_sw.js` (service worker at /pos/sw.js);
  `POSBill.checkout_uuid` makes checkout idempotent. Receipts as data
  (`app/pos/receipt_doc.py`) + `static/js/escpos.js`; Android app gained
  Bluetooth/USB ESC/POS, drawer kick, kiosk (lock task), update check
  (`android/app/src/main/java/com/arfa/pos/`). `app/auth/security.py`:
  TOTP 2FA (compulsory for the admin hierarchy, `ADMIN_2FA_REQUIRED`),
  lockout, IP limit, idle sign-out, cashier PIN digests; `Cashier`
  model + `app/pos/cashiers.py`. `app/monitoring.py` (Sentry on DSN),
  richer /healthz. `app/onboarding.py` (`Tenant.first_run`, migration
  dismissed it for existing clients). `app/subscriptions/`
  (`PlatformProfile`, `PlatformSeries`, `PlatformInvoice`; fee on
  Tenant/User; `app.utils.billing.mark_period_paid` is now the one
  mark-paid path). Admin drill-downs for POS bills and notes.
  `app/finance/owner_summary.py` (dashboard, WhatsApp, SMTP email).
  CLI: `platform-invoices-generate`, `owner-summary-send`, `reset-2fa`.
- **Interface redesign, stage 1 of 6** (plan approved by the owner with
  all four recommendations: side menu, dark mode following the device,
  full POS re-layout keeping the shortcuts, the three bundled fonts).
  `app/navigation.py` is the one place menus are defined: grouped side
  menu, New menu, search palette entries (Ctrl K), phone bottom bar and
  the bell (`needs_you`, served at `/needs-you`). Every entry carries the
  same feature flag as its route. `base.html` is the new frame (side
  menu + rail + phone drawer, top bar with search / New / bell / account
  menu, toasts via `_toasts.html`); `static/js/shell.js` holds its
  Alpine components; branch (Staff) logins keep the simple top bar.
  Fonts in `static/fonts` (Figtree, Bricolage Grotesque, JetBrains Mono,
  OFL), icons in `static/img/icons.svg` (Lucide, ISC) via the `icon()`
  template global. CSS tokens keep the `--color-*` names tenant themes
  override. Stages 2-6 (Settings sections, page patterns, Home/Reports,
  the counter, console/sign-in/dark mode/accessibility) are in README's
  Roadmap.
- **Interface redesign, stage 2: Settings in sections.**
  `app/settings/sections.py` (section menu, module groups with icons and
  set-up links), small per-section forms in `app/settings/forms.py`
  (`TenantSettingsForm` = all of them, still accepted at `/settings/` for
  the old all-in-one post), one route per section in
  `app/settings/routes.py` (`index` = Business, `appearance`, `features`
  = the module switches, `counter`, `documents`, `security`; `modules`
  stays the weighing/loyalty setup page). Templates under
  `templates/settings/` extend `settings/_layout.html`;
  `settingsForm` in `static/js/shell.js` drives the save bar.
- **Interface redesign, stage 3: page patterns.** Shared CSS in
  `static/css/app.css` ("Page patterns" and "Utilities" sections) and two
  scripts at the end of `static/js/shell.js` (header actions into a
  "More" menu - opt out with `data-keep` / `data-no-more`; list tables
  labelled for phone stacking). Inline styles were converted to utility
  classes by a one-off script (not in the repo); keep new templates on
  the classes - `tests/test_redesign_pages.py` caps inline styles and
  checks every utility class exists. Notes are a tab in Invoices
  (`invoicing/_tabs.html`). GSTR-1/3B views default to the primary
  active GSTIN when no `gstin_id` is given (`reports.routes._resolve_gstin`).
- **Interface redesign, stage 4: Home and Reports.** `daily_sales()` and
  `change()` in `app/finance/dashboard.py` (trend and comparisons, same
  rule as `_sales`); `inr` / `inr_compact` filters (`app/utils/money.py`);
  the hub's groups in `app/reports/hub.py` (add a report there, with its
  feature flags, and it appears in the hub).
- **Interface redesign, stage 5: the counter.** `pos/terminal.html`
  markup rebuilt (bar + Counter menu, chips, held drawer, quick pay,
  phone bill bar); new `posTerminal()` members: `activeCat`, `activeSub`,
  `heldOpen`, `afterRx`, `pickCat()`, `activeSubs()`, `shownProducts()`,
  `quickPay()`, `flyFrom()`. The old category tree helpers are still
  there but unused. `base.html` marks only `pos.terminal` as
  `is-counter` (menu as a drawer there). `tests/test_redesign_counter.py`
  runs the page's scripts in Node to drive `posTerminal()`.
- **Interface redesign, stage 6: dark mode, sign-in, console, a11y.**
  Dark tokens in `app.css` under `@media screen` (device setting) and
  `:root[data-theme="dark"]` (picked); `_theme_boot.html` (head of both
  bases) and `_theme_pick.html` + `themePick` in `shell.js`. New colour
  tokens for states/overlays (`--color-warn-*`, `--color-ink`,
  `--color-inverse`, `--color-scrim`, `--color-glass`, `--color-focus`,
  `--color-primary-hover`, `--color-chart-quiet`) - use them rather than
  fixed hex values. `auth_base.html` is the split layout with
  `{% block heading %}`. `tenants/directory.html` computes each client's
  state in the template from `tenant_access_status`. `field()` in
  `macros.html` sets `aria-describedby` / `aria-invalid`; `shell.js` names
  unlabelled fields and adds arrow keys to `role="menu"`.
- **Packages and powers, stage 1: the rules underneath.** Tables
  `packages` (firm-level, no tenant_id), `tenant_packages` (per client:
  package + module/setting/limit exceptions + read-only modules) and
  `auditor_powers` (per auditor). `app/utils/entitlements.py` is the one
  place that decides: `module_mode()` (Tenant.has_feature calls it),
  `client_may_switch()`, `setting_locked()`, `limit_of()`,
  `own_branding()`, `auditor_can()`, and the audit-logged setters. A new
  module check still uses `has_feature`; a new setting that ARFA may
  lock gets a key in `SETTINGS`; a new console power gets a key in
  `AUDITOR_POWERS` with its role defaults. Migration `a7c3f9d21b40`
  carries a frozen copy of `STARTING_PACKAGES` (a test keeps the two
  equal) and puts every existing client on "Current setup".
- **Packages and powers, stage 2: the console.** Routes in
  `app/tenants/packages.py` (registered on `tenants_bp`): `/admin/packages`,
  `/admin/packages/new[?copy=]`, `/admin/packages/<id>` (+ `/active`),
  `/admin/clients/<id>/package` (+ `/preview`, HTMX) and
  `/admin/clients/<id>/exceptions`, all Ultra Admin only. The client page
  section is `tenants/_client_package.html`, fed by
  `client_package_context()`. `preview_package()`, `package_summary()`,
  `package_price_for()`, `BUSINESS_TYPE_PACKAGE`, `set_limit_exception()`
  and `set_branding_exception()` are in `app/utils/entitlements.py`.
- **Packages and powers, stage 3: the client's side.** `feature_required`
  lets GET/HEAD through for a read-only module (sets `g.read_only_module`,
  which base.html turns into a notice and `body.is-read-only`) and 403s
  anything else. `Tenant.uses_module()` = on or read-only - use it (not
  `has_feature`) wherever existing records must keep counting (GST
  returns, filing pack, P&L, registers). Read-only is recomputed by
  `refresh_read_only()` after every package / exception / package-edit
  change, from `app/utils/module_data.MODULE_RECORDS`. Settings locks:
  `locked_for_client()` (stands aside for the Ultra Admin acting as the
  client); each Settings route checks `_locked(key)`; templates get
  `locked` and use `lock_note()` + `fieldset.lock-fieldset[disabled]`.
  Module requests: `ModuleRequest`, `request_module()`,
  `resolve_module_request()`, `navigation.admin_needs_you()` (the Ultra
  Admin's bell).
- **Packages and powers, stage 4: auditor powers.** Guard console routes
  with `@power_required("<power>")` (after the role decorator) and gate
  buttons with the `console_can("<power>")` Jinja global; menu links take
  `Link(power=...)`. `auditor_can()` caps a Sub-Auditor by their parent;
  `grantable_powers()`, `allowed_packages()`, `client_room()`,
  `sub_auditor_room()` and `acting_console_user()` (the auditor behind
  an "Act as") are in `app/utils/entitlements.py`. The Powers editor is
  `app/tenants/powers.py` + `tenants/_auditor_powers.html`;
  `client_package_context(tenant, viewer)` returns None for viewers who
  may neither assign packages nor switch choices. New console actions
  need a power (or an explicit decision that every auditor gets them).
- **Packages and powers, stage 5: branches, logins and style.** Branch
  login permissions go through `permissions.user_can` ->
  `entitlements.branch_permission` (ARFA's `LoginGrant` switch, else the
  User column capped by `counter_permissions_allowed`) - never read the
  `can_*` columns directly. Roles: `permissions.BRANCH_ROLES`,
  `role_of()`. Per-branch modules: `entitlements.BRANCH_MODULES`,
  `login_hides()`; `features.current_tenant_has` (templates, routes,
  menus, the counter's panels) follows the login, while services keep
  `Tenant.has_feature` (business rules). Limits: `limit_usage`,
  `limit_meter`, `may_add` (Ultra Admin passes) and the `limit_meter`
  macro. Sign-in minimums: `security_minimum`, `strictest_minutes`,
  used by `auth.security.idle_limit_minutes`, `pos.cashiers.lock_minutes`
  and `two_factor_required`. Branding: `own_branding`, `brand_logo_id`
  (use it for any printed logo), `tenant_theme_css` returns "" without
  own branding.
- **Packages and powers, stage 6: history and checks.** The Changes list
  is built from the audit log by `app/utils/changes.py` (`CLIENT_ACTIONS`,
  `describe()`) - a new package / module / setting / login action should
  be added there to show up. `feature_required` tags its view with
  `required_feature`; `tests/test_packages_checks.py` sweeps every such
  route and fails on a module page without the guard (exceptions listed in
  `GUARDED_IN_VIEW`). The README has a hand-over guide for the Ultra Admin
  ("Packages and powers - how to run it").

- **Second-hand goods module.** Pages in `app/secondhand/` (blueprint
  `margin`, `/second-hand/...`); rules in `app/margin/buying.py` (purchase
  voucher = a PurchaseBill of kind `margin_voucher`, series
  `DocumentType.MARGIN_PURCHASE` "SHP"), `app/margin/selling.py` (used
  stock, preview, sale through `invoicing.services.create_invoice`, whose
  `LineInput.serials` carries IMEI units), `app/margin/services.py` (lots)
  and `app/utils/margin.py` (math). e-Way bills: `eway.draft_from` sets
  `other_value` for MSI invoices and builds inward bills from vouchers
  (`source_type` "purchase_bill"). Returns: `gstr._margin_remainder_exempt`
  / `_table_8` (setting `module_settings(tenant, "margin")["remainder_as"]`).
  Postgres stores `document_type` by member NAME - add enum values in
  migrations by name (`'MARGIN_PURCHASE'`).

## 5. Chronological development history (this session)

This is the part README.md doesn't capture — the actual sequence of
user requests, bugs reported, and how they were resolved. Read this to
understand *why* some things are shaped the way they are, not just
what they do.

**Note on a prior compaction**: this session was very long and the
conversation was summarized partway through. The hierarchy/billing
rounds below (up through the Auditor/Sub-Auditor build-out) come from
that earlier summary; everything from "Round B" onward is from direct
conversation. Both are reflected accurately in README.md's Design
notes regardless, since that file was updated at the end of every
round.

1. **Auditor/Ultra-Admin hierarchy build-out.** Added the `AUDITOR`/
   `SUB_AUDITOR` roles, `auditor_scope.py`, moved billing-cycle
   selection entirely off the onboarding step (every new client/
   Auditor starts "pending" until the Ultra Admin sets a cycle), added
   the Auditors console tab, client allocation/reassignment, "Act as"
   extended to the new roles. Full detail in README's "Auditor /
   Sub-Auditor hierarchy and the billing-cycle overhaul" bullet.

2. **Round B** — user reported: *"mark this period as paid... if I
   accidentally press the button 3 or 4 times the month skips for each
   press... fix that button... or else let's remove it."* First pass
   also removed the hard `Tenant.valid_until` expiry entirely (the
   firm didn't want a hard cutoff date) and made the billing cycle
   type changeable at any time, not just at onboarding. Commit `114ad3d`.

3. **Round C** — the double-click skip bug was still reproducible;
   root-caused and fixed with an idempotency guard (`last_paid_on`),
   but this first fix **hid the button** once already paid. Commit
   `dce0018`.

4. **Round D** — user explicitly rejected hiding the button: *"instead
   of replacing the button with plain text... there should be a UPDATE
   button that tracks the live month and corrects the billing cycle
   for clients and auditors whose cycle are already mashed up."*
   Reworked so the button stays **always visible and clickable**
   (idempotent via `last_paid_on`, never disabled/hidden/swapped), and
   added a separate explicit **"Resync to live calendar"** repair
   action for already-drifted cycles. This is the shape that shipped.
   Commit `3d71f31`.

5. **Round E** — user reported: switching a tenant from Monthly to
   Yearly billing didn't move the renewal date. Fixed by recomputing
   `next_billing_due` from the anchor under the *new* cadence whenever
   changing an already-started cycle. Commit `892ea9e`.

6. **Round F** — user asked for the Ultra Admin to be able to reset an
   Auditor's (and their Sub-Auditors') own login password, mirroring
   the existing client-password-reset pattern. Added
   `reset_auditor_password`, Ultra-Admin-only, deliberately unscoped
   (reachable for any Auditor/Sub-Auditor, not just ones a specific
   Auditor manages). Commit `96d35e1`.

7. **Round G** — a large 4-part feature request delivered with two
   screenshots (current POS layout + a hand-drawn mockup): (1)
   category/sub-category items with a per-tenant on/off toggle and a
   collapsible POS category tree; (2) Stock/Sales/Sales-by-item
   reports all sharing one day/month/range + branch filter; (3) a new
   top-nav "Live" dashboard with an F1-style branch leaderboard; (4)
   held bills restructured to a persistent, non-collapsible left
   sidebar. Clarified scope via `AskUserQuestion` first (sequencing →
   "all four in one session"; leaderboard metric → "today's total
   sales revenue"; category required-or-optional → "optional even
   when on"). Built as five tracked tasks (held-bills sidebar →
   categories → reports filters → Live dashboard → full verification),
   all four features plus full-suite/browser verification committed
   together as `b0b1948` ("Add item categories, report filters, Live
   dashboard, and a persistent held-bills sidebar").

8. **Cart alignment bug + Android app request.** User reported (with a
   screenshot) that the POS cart's rows were visibly uneven — item
   names wrapping, the quantity box stretched oddly — and in the same
   message asked for a **native Android APK**, branch-logins-only,
   full POS billing, pushed to GitHub for download, without breaking
   the website.
   - **Cart fix first** (independent, concrete, no blockers): root
     cause was `input[type="number"] { width: 100%; min-height: 48px }`
     (a global form-reset rule) outranking the single-class
     `.qty-input` selector on CSS specificity — the quantity box had
     been silently stretching to fill the row the entire time,
     regardless of what was written in `.qty-input`. Took several
     genuine debugging iterations to actually find this (flex→grid
     didn't fix it alone; `auto`→`max-content` grid columns didn't fix
     it either, because a bare `<input>` contributes its *UA-default*
     preferred size, not its CSS width, to a flex/grid ancestor's
     intrinsic-size calculation — only pinning the column to a fixed
     px width plus re-scoping the selector to `.qty-stepper .qty-input`
     actually fixed it). Committed separately as `3d69e21` ("Fix
     misaligned POS cart rows").
   - **Clarified the Android work via `AskUserQuestion`** before
     starting (three real blockers, none guessable): production server
     URL → user supplied `https://gst-billing-app-prdx.onrender.com`
     (own domain later); architecture → user asked "does a WebView
     wrapper look like a URL whipped up or a proper app?" — answered
     (full-screen WebView with no browser chrome reads as native;
     what gives WebView wrappers away is sloppy details — address bar,
     the browser's ugly offline page, wrong status bar color, no
     splash — all of which were built properly here) and proceeded
     with the recommended WebView-wrapper approach; APK build/delivery
     path → user chose GitHub Actions CI (recommended), after being
     told upfront that this sandbox cannot compile Android locally.
   - **Discovered mid-build**: this sandbox's network policy blocks
     `dl.google.com` *and* `maven.google.com` (which just redirects to
     `dl.google.com`) — meaning not just the Android SDK, but the
     Android Gradle Plugin and every single AndroidX/Material
     dependency, are all unreachable from inside this sandbox. This
     was discovered and confirmed (via `curl`) **before** committing
     to an approach, which is why GitHub Actions (not local build) was
     the only real option.
   - **Built the full `android/` project**: a Kotlin/Gradle
     single-Activity app (`com.arfa.pos`, `MainActivity.kt`) — full-
     screen WebView, native splash screen, native offline/error
     screen, back-button handling, and **branch-only enforcement that
     reads the server's own post-login redirect** (a branch login
     always lands on `/pos/`; any other role is immediately signed
     back out with a "branch logins only" dialog) rather than
     duplicating the server's role logic client-side. Also built a
     native receipt-printing bridge (`window.AndroidPrint`,
     `addJavascriptInterface`-injected) since Android's WebView has no
     built-in PDF viewer or `window.print()` — it downloads the same
     server-generated PDF (reusing the WebView's session cookie) and
     hands it to Android's native `PrintManager`. This required **one
     small, additive, feature-detected change** to the website itself
     (`app/templates/pos/terminal.html`'s `printReceipt()`) — verified
     to have zero effect on browser behavior (full suite green, plus a
     live Playwright checkout/print run showing zero JS errors and the
     identical "sent to the printer" flow).
   - The Gradle wrapper (`gradlew`, `gradle/wrapper/*`) was generated
     for real using a locally pre-installed plain Gradle, from an
     *empty scratch directory* (not `android/` itself — evaluating
     `android/build.gradle` directly requires resolving the AGP plugin,
     which needs the blocked Google Maven, so even wrapper generation
     had to route around that).
   - `.github/workflows/android-apk.yml` builds a debug-signed APK
     (direct-install/sideload, not a Play Store release — no separate
     signing key to manage) on GitHub's own runners on every push to
     `android/**`, uploads it as a 90-day build artifact, and
     publishes/updates a GitHub Release tagged `android-latest` with
     the APK attached as a stable, permanent download link.
   - Committed as `9ab8f79` ("Add ARFA POS: a native Android app for
     branch logins only"). **The GitHub Actions build succeeded on the
     first real compile attempt** — confirmed via the GitHub API
     (workflow run `38356984514`, conclusion `success`) and the
     published release at
     `https://github.com/abdulkadhersoofi-dot/pearl_city_biriyani/releases/tag/android-latest`.

9. **This file** — the user asked for a single markdown file capturing
   the entire project so a brand-new Claude Code chat can be seeded
   with it and work as efficiently as this session did.

## 6. The Android app in detail

See `android/README.md` for the canonical reference (build
instructions, printing mechanism, why it isn't built locally, project
layout). Key facts worth repeating here:

- Package `com.arfa.pos`, `minSdk 26` (Android 8.0+), `compileSdk
  34`/`targetSdk 34`, AGP `8.5.2`, Gradle `8.7`, Kotlin `1.9.24`, JDK 17.
- `BuildConfig.SERVER_BASE_URL` (set in `android/app/build.gradle`) is
  the **one place** the server URL lives — change it, bump
  `versionCode`/`versionName`, push, CI rebuilds.
- Role gating lives entirely in `MainActivity.handlePageFinished()` —
  it never duplicates server-side role logic, it just reads which page
  the server actually sent the signed-in user to.
- Printing: `window.AndroidPrint.printReceipt(url)` → downloads the
  PDF with the WebView's session cookie attached manually (raw
  `HttpURLConnection`, not the WebView itself) → Android's
  `PrintManager` with a custom `PrintDocumentAdapter` that just streams
  the raw PDF bytes — works with whatever thermal/other printer the
  device has an Android print service for.
- This sandbox cannot resolve `dl.google.com`/`maven.google.com` at
  all — confirmed by direct `curl` tests during this session. Any
  future Android work in a similarly-sandboxed environment should
  expect the same and plan on GitHub Actions as the only real build/
  verification path, the same way this session did.
- Latest known-good build: GitHub Actions run `38356984514`, release
  tag `android-latest`.

## 7. Engineering conventions established this session

Follow these in a new session unless the user says otherwise:

- **Testing Alpine `x-if`/`x-show` markup is a trap.** Jinja always
  renders *both* branches of an `x-if`/`x-show` into the raw HTTP
  response regardless of the Alpine condition's truth value — Alpine
  only decides visibility client-side, in a real browser, which the
  Flask test client never runs. **Never assert on the presence/
  absence of Alpine-templated markup in a pytest test.** Assert on
  genuinely server-computed values instead — a JS literal like `const
  CATEGORY_WISE = {{ value|tojson }};`, or a Jinja `{% if %}` block
  that's genuinely conditional server-side. This bit a test in
  `tests/test_categories.py` directly — see the fix there for the
  pattern.
- **CSS specificity bites quietly.** An attribute selector + element
  type (`input[type="number"] { ... }`) outranks a single class
  selector (`.qty-input { ... }`) — this caused the whole cart-
  alignment saga in §5.8. When a styled element isn't rendering the
  way its own rule says it should, check for a more specific rule
  elsewhere (`grep` the property name across `app.css`) before
  assuming the rule itself is wrong.
- **Migration discipline**: every migration in this session was
  generated via `flask db migrate`, then hand-reviewed — in
  particular, Alembic autogenerate leaves new foreign-key constraints
  **unnamed** (`batch_op.create_foreign_key(None, ...)`), which breaks
  `drop_constraint` on downgrade; these were always hand-named. Every
  migration was verified upgrade → downgrade → upgrade against a real
  local Postgres instance before being considered done. New NOT NULL
  columns always get a `server_default` (non-breaking for existing
  rows); new enum values use `ALTER TYPE ... ADD VALUE` inside
  Alembic's `autocommit_block()` (Postgres can't add an enum value
  inside a normal transaction).
- **Commit convention**: a detailed body explaining the bug/feature
  and the fix/design (not just "what changed" — "why", with enough
  detail that someone reading `git log` later understands the
  reasoning), ending with:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_019qLax1xk76ZSp3ZevziK9A
  ```
  (that session ID is specific to *this* session — a new session gets
  its own trailer, don't reuse this one verbatim).
- **README.md's "Design notes" section gets a new bullet for every
  feature/fix round**, in the same dense, technically-precise prose
  style as the rest of the file (file paths and function names named
  explicitly, the *why* always stated, not just the *what*). This is
  the project's real living documentation — keep it current.
- **Full test suite + live browser verification before every commit
  that touches templates/JS/CSS.** Playwright (`chromium`, launched
  with `executable_path="/opt/pw-browsers/chromium"`), scripts written
  to the scratchpad directory, screenshots read back with the `Read`
  tool to actually look at the result rather than trusting a green
  test run alone for anything visual.

## 8. Dev environment bootstrap (exact commands used this session)

```bash
# Start services (container-local, not persistent across sessions)
service postgresql start   # prints "online" or starts fresh
service redis-server start # the "ulimit: error setting limit" line is expected/harmless

# Migrations / dev server, against the main dev DB
export FLASK_APP=app:create_app
export DATABASE_URL="postgresql+psycopg2://gst_app:gst_app@localhost:5432/gst_billing"
export SECRET_KEY=dev-secret
flask db upgrade
nohup flask run --port 5055 > /tmp/.../flask_dev.log 2>&1 & disown

# Tests, against a SEPARATE database
export DATABASE_URL="postgresql+psycopg2://gst_app:gst_app@localhost:5432/gst_billing_test"
python -m pytest -q   # full suite, all passing at each phase commit
```

Background long-running commands (the full suite takes ~3 minutes)
rather than blocking the foreground — this harness auto-backgrounds
anything exceeding its foreground timeout and notifies on completion;
don't poll with `sleep` loops, wait for the notification.

## 9. Repo structure

See README.md's "Folder structure" section — it's accurate and
current. One addition worth flagging explicitly: `android/` (the
native app, §6) and `.github/workflows/android-apk.yml` (its CI build)
are both new as of this session and intentionally isolated from the
Flask app — nothing in either can affect the website.

## 10. What's genuinely still outstanding

Trust **README.md's own "Roadmap" section** over any stale task-
tracker state (see the warning in §11) — as of this file:

- **Document types**: all built (job-work / approval challans and
  advance receipt / refund vouchers since Phase 6).
- **Onboarding walkthrough polish**: still just a plain form, no
  guided first-run checklist.
- **Admin console drill-down**: POS bills/notes don't have full
  drill-down pages yet — only invoices do.
- **e-Invoice / e-way bill live calls**: built in Phase 8 to NIC's
  published protocol and a configurable GSP, but only exercised against
  simulated servers - no sandbox login was available. The first real
  login should be checked with "Test login". The JSON-download path
  works without any credentials.
- **GSTR-1/3B explicit, by-design limits** (not bugs, don't "fix"
  these without the user asking): Input Tax Credit appears in GSTR-3B
  only for clients with the Purchases module on (from their purchase
  bills; everyone else sees gross output tax only, unchanged); B2C Large
  uses the ₹1 lakh threshold (Notification 12/2024-CT) since Phase 7, but
  returns on B2C supplies stay in B2C Small (no CDNUR table).
- **Android app**: debug-signed only — no Play Store release signing
  key exists or was asked for. If the user wants a Play Store release
  later, that's a new, separate decision (key generation/management,
  not something to just do unprompted).

## 11. Important caveat: the task tracker is stale in places

This session used a `TaskCreate`/`TaskUpdate` tracked task list (tasks
#1–#41) across its full lifetime. **Several entries near the end of
that list are stale** — e.g. #31 ("Rebrand to ARFA") shows
`in_progress`, #32/#33/#34/#36 (tenant branding, Settings page, product
images, POS print-size default) show `pending` — but README.md's
Design notes section clearly documents all of these as **already
shipped and working** (platform-vs-tenant branding, per-tenant theme
Settings, product image upload/cropping, POS checkout printing
directly with no size prompt). The task statuses simply weren't
closed out even though the work was done and verified. **Do not
re-implement anything the task list calls "pending" without first
checking README.md and the actual code** — it is the more trustworthy
source for "is this really done."

## 12. How to pick up work in a new session

1. Read this file, then `README.md`, then `android/README.md` in full.
2. `git log --oneline -20` and `git status` to confirm branch/commit
   state matches what's described here (this file can go stale too).
3. Bootstrap the dev environment per §8 if you need to run/verify
   anything.
4. For any new feature or fix: implement → write/extend tests →
   run the full suite → if it touches UI, verify live in a real
   browser via Playwright (screenshots, actually look at them) → add a
   README.md Design notes bullet → commit with the established message
   style → push → (if asked) verify any GitHub Actions workflow that
   should have triggered actually succeeded.
5. Ask before doing anything destructive, before pushing without being
   asked to, and before guessing on a genuinely unknowable fact (a
   production URL, a credential, an architecture decision with real
   cost trade-offs) — this session asked `AskUserQuestion` at several
   points for exactly this reason, and it worked well.
