# ARFA made simple: the plan

**Progress:** Phases 0 and 1 are live. Phase 2 (New sale), Phase 3 (purchase
orders billed in parts, scanned bills matched to their order), Phase 4 (bill
reader training with heading lessons) and Phase 5 (the SAP link, tested
against a stand-in SAP; a real connection needs the client's SAP team) are
built and tested, and so is Phase 6 (the 3D dashboard), all waiting for
approval to go live. Phase 7 follows.

**What this is:** a plan to make ARFA look and feel minimal (few buttons, one
way to do each job) without removing a single feature, while making its
numbers provably correct. It was written from the code as it stood on
13 November 2026; the progress line above says what is built since.

**The promises this plan keeps:**

1. **No feature is removed.** Every screen that exists today stays reachable.
   Appendix A maps each one to its new place, and old web addresses keep working.
2. **The system decides the paperwork.** People say what they are doing
   ("selling this", "this bill arrived"). ARFA works out whether that is an
   invoice, a sales order, a purchase bill against an order, and so on.
3. **The books are checked, not trusted.** Every figure on the dashboard and
   in every report is tested against the records behind it, every night, and
   a failed check is shown and emailed. See section 7 for what "0 errors"
   can honestly mean.
4. **A person always confirms before anything is posted.** ARFA proposes;
   the user sees it in plain words and presses one button.

---

## 1. What is wrong today

The features are all there. They are spread over too many doors, and the
same job is done in different ways in different places.

| Area | Today | Why it is hard |
|---|---|---|
| Side menu (owner) | 7 groups, up to 29 entries | Too many choices on every screen |
| Selling | 3 separate forms: POS counter, Tax invoice, Quotes & orders (4 document kinds) | The user must know which document they need before they start |
| Sales orders | Inside Sell > Quotes & orders > "Sales orders" tab | The showroom-to-warehouse flow, the busiest one, is three clicks deep |
| Purchases | 6 tabs: Purchase bills, Returns, Supplier notes, Purchase orders, Suppliers, GSTR-2B match. Scan bills is a separate menu entry | An order becomes a bill by opening the order and retyping, or by scanning with no link to the order |
| Stock | 10 tabs under Stock, plus Warehouse (6 tabs) as a separate entry | Two places for one subject |
| Money | Accounts has 6 tabs; Profit & loss lives under Reports; GST lives in three places | Related figures are far apart |
| Reports | 25 reports in 6 groups | Fine as a library, but the daily answers are buried |
| Whole app | 278 screens, 436 web addresses | No shared pattern: lists, forms and buttons differ from module to module |

Two things found in the code that matter for accuracy:

- The dashboard's "Sales today" (`app/finance/dashboard.py`) and the Sales
  report (`app/reports/sales.py`) calculate sales in **separate code**. They
  agree today, but nothing forces them to keep agreeing.
- A purchase order is marked "converted" by its **first** bill
  (`app/purchases/services.py`). A part delivery closes the whole order.

---

## 2. Design rules (apply to every screen)

1. **Seven doors, no more.** Home, Sell, Buy, Stock, Money, Reports, Setup.
2. **One big button per page.** The thing people do most (New sale, Add a
   bill). Everything else is smaller or behind "More".
3. **Say what will happen before it happens.** A plain sentence above the
   save button: "This will bill 2 items now and order 1 from Chennai
   warehouse."
4. **One pattern for every list:** title, one main button, a search box,
   filter chips (Waiting, Today, All...), the table. Click a row to open it.
5. **One pattern for every record:** what it is, a progress line (Ordered >
   Picked > Billed > Delivered), and one "next step" button.
6. **Plain words.** Sale, Order, Bill, Money in, Money out. The GST names
   (Tax invoice, Bill of supply, Delivery challan) appear on the printed
   document and in reports, where the law needs them.
7. **Search finds everything.** The search box (Ctrl K) already exists; it
   will also find records (an invoice number, a customer, a serial number),
   not only screens.
8. **The same on a phone.** Every page works at phone width with no sideways
   scrolling.

---

## 3. The new menu

### Owner

| Door | What opens | Today's entries it gathers |
|---|---|---|
| **Home** | The dashboard (section 6) with "Needs you" | Dashboard, Live |
| **Sell** | One **New sale** button and one list of all sales | POS, Tables, Invoices, Quotes & orders, Recurring invoices, Job cards, Old gold, Second-hand goods (5 entries) |
| **Buy** | One **Add a bill** button (scan or type) and one list of all buying | Purchases (6 tabs), Scan bills, Expenses |
| **Stock** | Items and what is where | Items, Stock (10 tabs), Warehouse (6 tabs), Metal rates |
| **Money** | Who owes you, whom you owe, cash and bank | Customers, Accounts (6 tabs), Wholesale, Loyalty |
| **Reports** | The daily answers first, then the full library, then GST | Reports (25), GST returns, e-Invoice & e-Way bills |
| **Setup** | People, phones, modules, training | Branches, Settings, Phone apps, Teach the bill reader |

Each door opens a **hub page** built the same way: the main button, a
"waiting for you" strip (for example "3 orders ready to bill", "2 bills to
check"), then tiles for the rest. A tile appears only when its module is
switched on, exactly as menu entries do today.

### Branch login (showroom or counter)

Three doors: **Counter** (the POS, as today), **New sale** (the one form in
section 4), **Today** (day-book, Z-report, close the day). Sales orders stop
being a separate entry because New sale makes them.

### Console (Ultra Admin and auditors)

Unchanged in this plan. It was tidied recently and is a different audience.

### How this is built

`app/navigation.py` already defines the whole menu in one place (`_client_groups`,
`_branch_groups`, `_more_screens`). The change is a new set of groups plus
seven hub templates. No route is renamed or deleted.

---

## 4. One form to sell

### What the user does

1. Tap **New sale**.
2. Pick the customer (search, or add one in place). A walk-in needs no name.
3. Add items by search, barcode or serial number. Next to each item ARFA
   shows where it is: **"Here: 3 · Chennai warehouse: 12"**.
4. Read the sentence at the bottom, and press the button.

There is no "what kind of document?" question.

### How ARFA decides

"Here" means the stock location of the person selling: a branch login's own
showroom, or for the owner the store chosen in "Selling from" (default: the
main store).

| What the lines look like | What ARFA makes | Button says |
|---|---|---|
| Every item is here, in stock | **A sale**: the invoice now, stock leaves now | Bill now |
| Every item comes from a warehouse | **A sales order** to that warehouse. The warehouse scans the serial numbers and the invoice follows, as it does today | Send order to warehouse |
| Some here, some from a warehouse | **Both**: an invoice for what is here and an order for the rest, shown together as one sale | Bill 2 now, order 1 |
| Items in two warehouses | One order per warehouse (the split that exists today) | Send 2 orders |
| Not in stock anywhere | An order marked **Waiting for stock** | Take the order |
| "Only a price for now" is ticked | A **quotation** (or proforma). Nothing moves | Save quote |
| "Send goods without a bill" is ticked | A **delivery challan** | Send goods |
| Sold on EMI | The existing EMI rules: never split, billed at the showroom, goods held until the finance is confirmed | Take EMI order |

Details that keep this safe:

- **Serial-numbered items here** need the serial scanned to bill now. If it
  isn't in hand, the line switches to "order from warehouse".
- **Part quantities:** wanting 5 with 2 here becomes 2 billed now and 3 ordered.
- **The user can always override:** each line has a small "from" picker, and
  a "send everything as one order" switch exists for the mixed case.
- **Which warehouse:** the nearest one with stock, using the pincode rules
  already built (`app/warehouse/nearest.py`).
- **GST is untouched:** the invoice is the same invoice, numbered from the
  same series; a sales order still has no tax effect.

### How this is built

A new decision service (proposed `app/sell/decide.py`) takes the lines and the
seller's location and returns a plan made of parts: "invoice these",
"order these from warehouse X". Saving runs the parts **in one database
transaction** through the code that exists: `create_invoice`
(`app/invoicing/services.py`) and `create_sales_document`
(`app/sales/services.py`). No new document type. The parts of one sale share
a group id (the `split_group_id` column exists), which lets the Sell list
show one row with one progress line.

The POS counter stays as it is for fast walk-in billing. The old Tax invoice
form and Quotes & orders form stay reachable (Sell > More) until the new
form has proved itself, then become redirects.

---

## 5. Buying: the scanned bill finds its order

### What the user does

1. Tap **Add a bill** and drop the supplier's invoice in (photo, PDF, or
   from the ARFA Scan phone app). Typing a bill by hand stays possible.
2. ARFA reads it, finds the purchase order it belongs to, and shows the two
   side by side with any differences marked.
3. The user checks and presses **Post**.

### Where purchase orders come from

- Typed in ARFA, as today.
- **Fetched from the client's SAP S/4HANA** through SAP's purchase-order API.

### Fetching from SAP

| Point | Plan |
|---|---|
| Which SAP interface | SAP's standard Purchase Order OData service. SAP marked the older version (`API_PURCHASEORDER_PROCESS_SRV`, OData V2) as deprecated in release 2308 and offers `API_PURCHASEORDER_2` (OData V4) as its successor. The client's SAP team confirms which one their system exposes |
| Direction | Phase 1: read only. ARFA pulls orders; it never changes SAP |
| How often | A scheduled pull (every 15 minutes is a sensible start) plus a "Fetch now" button |
| No duplicates | Each order is stored with its SAP number. Fetching the same order again updates it |
| Paging | Fetched in pages. An SAP note reports a 1,000-record limit per request in one older release |
| Suppliers | Matched by GSTIN, then by SAP vendor code. Unknown suppliers wait in a "to map" list |
| Items | Matched by SAP material number to ARFA SKU. Unknown items wait in the same list. Nothing is guessed silently |
| SAP orders in ARFA | Shown with an "SAP" tag, not editable in ARFA |
| Security | As in the security overview: a dedicated machine login (OAuth or certificate), SAP addresses allow-listed, credentials in Google Secret Manager, every call logged |
| Later (needs a decision) | Sending the goods receipt or the posted bill back to SAP |

This is new code (proposed `app/integrations/sap/`). It cannot be finished
without the client's SAP details; see section 10.

### How the bill finds its order

The reader already extracts "Your order (PO) no." from a bill
(`po_number` in `app/scan/services.py`). Matching runs in this order:

1. **PO number printed on the bill.** Compared after cleaning (spaces,
   leading zeros, prefixes like "PO-") against that supplier's open orders.
2. **No number, or no hit:** score each open order of that supplier
   (found by GSTIN) on:
   - the same items (mapped product, then HSN, then name likeness; the
     name-matching code exists);
   - quantities within tolerance;
   - rates within tolerance;
   - bill date on or after the order date.
   The best order is proposed only if it scores high **and** is clearly ahead
   of the second best. Otherwise the top three are shown to choose from.
3. **Still nothing:** the bill is posted without an order, as today, or
   parked as "waiting for its order".

### The check screen

The existing scan review screen gains a panel: **"Matched to order
4500012345"** and one row per item:

| Item | Ordered | Received (warehouse scan) | Billed | Difference |
|---|---|---|---|---|

Differences are named in words: billed more than ordered, rate higher than
ordered, item not on the order, GST rate differs. Tolerances are a setting
(proposed start: quantity exact, rate within 1% or Rs. 1).

**Post** then does what a typed bill does today, in one step: the purchase
bill linked to its order, stock in, input tax credit, the supplier's
balance. Matched bills with no differences can be posted together ("Post 12
matched bills"), still by a person.

### What must change underneath

- **Part deliveries.** Orders get per-line "received" and "billed"
  quantities and three states: Open, Partly billed, Closed. Several bills
  can then settle one order.
- **Three-way match.** The warehouse app's Receive mode already scans
  serials against an order (`WarehouseReceipt.purchase_order_id`). The check
  screen reads it for the "Received" column.

---

## 6. Home: the 3D dashboard

**The rule:** 3D is how the numbers are *shown*. Every figure is still
printed as exact text, and every figure is a link to the report that proves it.

What the owner sees:

- **Five tiles** (as today): sales today, money received, cash in hand,
  customers owe you, you owe suppliers. Each lifts and tilts slightly under
  the pointer and opens its report on a click.
- **The branch skyline:** each branch is a 3D tower. Height is today's
  sales, colour is "ahead of / behind the same day last week". It turns
  slowly; tap a tower for that branch's figures. This replaces a flat bar
  chart and makes Live part of Home.
- **The 14-day sales ribbon:** the trend as a 3D ribbon with the exact
  value on each day.
- **Needs you:** the list that exists, moved to the top right.

Engineering choices:

| Point | Plan |
|---|---|
| Library | three.js, stored in ARFA's own files (no outside server, which the security policy requires). Roughly 170 KB compressed, loaded only on Home and only after the numbers are on screen |
| Fallback | A flat version appears automatically on a weak phone, with "reduce motion" set, or with no 3D support. A "Flat view" switch is always there |
| Speed | The numbers paint first; the 3D scene arrives after. Home must not get slower to read |
| Accessibility | Screen readers get the same figures as a table |
| Phones in the Android apps | Tested in the app's built-in browser before release |

A lighter first step is possible: the same skyline drawn with CSS 3D only,
with no library at all. See decision 3 in section 10.

---

## 7. "0 errors": how the numbers are kept right

No software can honestly promise zero errors by saying so. What ARFA can do
is make an error **impossible to miss**: every number is calculated in one
place, checked against the records every night, and a failed check blocks
the month from being marked ready to file.

### One calculation per number

Each figure gets a single function that the dashboard, the report, the Excel
export and the GST return all call. First to fix: sales (two code paths
today, see section 1). Then collections, receivables, payables, stock value.

### The Books check (new page under Reports, runs nightly and on demand)

| Check | Must be true |
|---|---|
| Sales | Sales report total = invoices + counter bills − returns − credit notes |
| GSTR-1 | Total and tax by rate = the same invoices as the Sales report |
| GSTR-3B | Outward supplies = GSTR-1; input credit = eligible credit in the purchase register |
| Stock quantity | For every item and place: opening + in − out = on hand. Never below zero |
| Serial numbers | For serial-numbered items: units on hand = serials in stock |
| Customers | Invoices − receipts − credit notes = each customer's balance |
| Suppliers | Bills − payments − debit notes = each supplier's balance |
| Cash and bank | Opening + money in − money out = closing, each day |
| Profit & loss | Revenue = Sales report before tax; cost of goods = value of stock that left |
| Journal | Total debits = total credits (the journal builder used for the Tally export exists) |
| Dashboard | Every tile = its report for the same period |

Each row shows a tick, or the difference in rupees with a link to the
records that cause it. A failed check emails the owner through the alerting
already set up, and the month cannot be marked "ready to file" until it is
resolved or explained.

### A test business with known answers

A fixed set of test data (sales, returns, part payments, transfers, EMI
sales, split orders, part deliveries, a credit note across months) whose
correct totals are worked out by hand once. Every report and the dashboard
must match it to the paisa. It runs on every code change, before any deploy.

### Closing a period (new)

Once a month's GST is marked filed, its documents can no longer be edited or
back-dated; corrections go through credit and debit notes. The code has no
such lock today.

---

## 8. Bill reader training, in Settings

**Where:** Setup > Settings > **Bill reader training**. The page that exists
(`/scan/teach`, "Teach ARFA") moves here; its address keeps working.

**What exists:** ARFA remembers rows the user had to add by hand, per
supplier, and takes free-text notes ("the items continue on page 2").

**What is added: teaching headings.** Suppliers print the same thing under
different names. The training page lets the user say which heading means what.

| ARFA's field | Headings a supplier might print |
|---|---|
| Invoice number | Bill No., Doc No., Inv #, Voucher No. |
| Your order (PO) number | Cust. PO, Buyer's Order No., Our Ref., Order Ref. |
| Quantity | Qty, Nos, Pcs, Billed Qty |
| Rate | Price, Unit Rate, Basic |
| Taxable value | Assessable Value, Net Amt, Basic Amount |

How training works:

1. Pick a supplier (or "every supplier") and open one of their bills.
2. Click the heading on the picture of the bill, then choose what it means.
   Or type the heading into the table.
3. Press **Read again** to see the result before and after, side by side.
4. Save. Every later bill from that supplier is read with the lesson.

How it is stored: a new kind of lesson beside the two that exist
(`ScanLesson`). It is given to the reader as a hint **and** applied as a
fixed rule after reading, so a taught heading is not left to chance. Each
supplier's row shows how many bills needed no correction, so the owner can
see training working. Lessons can be switched off or deleted.

The PO-number heading is the one that matters most for section 5: it is what
lets a bill find its order by number.

---

## 9. Build order

Each phase goes to the test copy first, is checked in a browser and by the
test suite, and reaches the live site only on your approval.

Phase 0 found and fixed three errors in the figures (see README, "Books
check"): a branch stock transfer counted as a sale and as GST turnover; a
refund changing the figures of an earlier, possibly filed, month; and the
Sales report leaving out credit and debit notes.

| Phase | What | Depends on | Size |
|---|---|---|---|
| 0 | **Accuracy first:** one calculation per number, the test business, the Books check page, period closing | Nothing | Large |
| 1 | **The seven doors:** new menu and hub pages. No behaviour changes; a "Classic menu" switch stays for now | Nothing | Medium |
| 2 | **New sale:** the one form and the decision rules | Phase 1 | Large |
| 3 | **Buying:** part deliveries on orders, bill-to-order matching, the check panel | Phase 1 | Large |
| 4 | **Bill reader training** in Settings, with heading lessons | Phase 3 (uses the same reader fields) | Medium |
| 5 | **SAP purchase orders** | Phase 3, and the client's SAP details | Large, partly outside our control |
| 6 | **3D dashboard** | Phase 0 (its numbers must already be the checked ones) | Medium |
| 7 | **Finish:** record search, branch login, phone polish, retire the Classic switch | All | Medium |

Phase 0 comes first on purpose: the redesign moves numbers to new places,
and they should be proven right before they are made prominent.

Rollout safety: the new shell is a per-business switch, so it can be turned
on for one business first. Old web addresses redirect to their new home.

---

## 10. Decisions

**Decided on 13 November 2026: every recommendation below is accepted.**
Decision 6 (SAP) still needs the client's SAP team; phases 0 to 4 and 6 do
not wait for it.


1. **Mixed sale** (some items here, some in a warehouse): bill what is here
   now and order the rest (recommended), or always send one order for
   everything?
2. **Door names:** Home, Sell, Buy, Stock, Money, Reports, Setup. Any you
   want worded differently?
3. **3D dashboard:** full 3D with three.js (recommended, richer), or the
   lighter CSS-only version first?
4. **Matching tolerance** for a bill against its order: quantity exact and
   rate within 1% or Rs. 1 (recommended), or stricter?
5. **Period closing:** lock a month once its GST is marked filed
   (recommended), or leave months open?
6. **SAP, from the client's team:**
   - S/4HANA Cloud or on-premise, and which release?
   - Do they use SAP Integration Suite, or should ARFA call SAP directly?
   - Which system is the book of record for purchase bills: does ARFA send
     the posted bill back to SAP, or only read orders?
   - Their SAP material numbers and vendor codes, to map to ARFA's items and
     suppliers.
7. **Classic menu:** keep the old menu as a switch for one month after the
   new one goes live (recommended), or switch everyone at once?

---

## 11. Risks

| Risk | How the plan handles it |
|---|---|
| Staff used to the old menu get lost | Classic switch for a month; search finds every old name; a one-screen "what moved where" guide |
| The sale form bills when it should have ordered, or the reverse | The outcome is stated in words before saving; each line can be overridden; the rules are covered by tests for every row of the table in section 4 |
| A bill is matched to the wrong order | A match is only proposed, never posted, without a person; a weak match shows choices instead of one answer |
| SAP details arrive late | Phases 0 to 4 and 6 do not depend on SAP |
| 3D slows the dashboard or drains phones | Numbers first, 3D after; automatic flat fallback |
| A Books check fails on existing data | That is the check working. Phase 0 includes time to investigate each difference before the redesign starts |

---

## Appendix A: where every screen goes

Nothing is deleted. "Chip" means a filter button on a list; "tile" means a
button on a hub page.

### Sell

| Today | New place |
|---|---|
| POS | Sell > Counter (tile); Counter door for a branch |
| Tables, Kitchen, Sold out today, Restaurant menu and setup | Sell > Tables (tile) |
| Invoices; New tax invoice | Sell list, chip "Bills"; made by New sale |
| Credit & debit notes | Sell list, chip "Returns & notes"; started from a bill |
| Quotations, Proforma invoices | Sell list, chip "Quotes"; made by New sale > "Only a price for now" |
| Sales orders | Sell list, chips "Waiting at warehouse" and "Ready to bill"; made by New sale |
| Delivery challans | Sell list, chip "Goods sent"; made by New sale > "Send goods without a bill" |
| Recurring invoices | Sell > Recurring (tile) |
| Job cards, Warranty check | Sell > Job cards (tile) |
| Old gold | Sell > Old gold (tile) |
| Second-hand: Overview, Buy used goods, Used stock, Sell, e-Way bills | Sell > Second-hand goods (tile, its five tabs kept) |
| Day-book, Z-report, Close the day, Offline bills | Sell > Counter > Today; Today door for a branch |

### Buy

| Today | New place |
|---|---|
| Scan bills | Buy > **Add a bill** (the main button) and chip "To check" |
| Purchase bills; New purchase bill | Buy list, chip "Bills"; "Type a bill by hand" under Add a bill |
| Purchase orders | Buy list, chip "Orders" (with the SAP tag where fetched) |
| Returns / debit notes | Buy list, chip "Returns" |
| Supplier credit / debit notes | Buy list, chip "Supplier notes" |
| GSTR-2B match | Buy > GSTR-2B match (tile); also Reports > GST |
| Suppliers; Bulk add suppliers | Buy > Suppliers (tile) |
| Expenses, categories, recurring expenses | Buy > Expenses (tile) |
| Warehouse > Receiving | Buy list, chip "Goods received"; also Stock > Warehouse |

### Stock

| Today | New place |
|---|---|
| Items, Categories, Price lists, Bulk add items | Stock > Items (tile) |
| Stock, By location | Stock (the hub's main list, with a place picker) |
| Adjustments, Transfers | Stock > Move or adjust (tile) |
| Locations | Setup > Places |
| Reorder, Fast / slow / dead, Expiry | Stock > Alerts (tile); also in Reports |
| Serial / IMEI | Stock > Serial numbers (tile); found by search |
| Barcode labels | Stock > Labels (tile) |
| Warehouse: Ready to invoice | Sell list, chip "Ready to bill"; Stock > Warehouse |
| Warehouse: Racks, Reports | Stock > Warehouse (tile) |
| Warehouse: App logins | Setup > Phones & logins |
| Warehouse: Financiers, settlement | Money > Finance companies (tile) |
| Metal rates | Stock > Metal rates (tile) |

### Money

| Today | New place |
|---|---|
| Customers; Bulk add customers | Money > Customers (tile) |
| Customers owe, Receipts, refunds | Money > Money in (the hub's first list) |
| You owe suppliers, Payments | Money > Money out |
| Cash book, Bank book | Money > Cash & bank (tile) |
| Wholesale: groups, slabs, salesmen | Money > Wholesale (tile) |
| Loyalty | Money > Loyalty (tile) |

### Reports

| Today | New place |
|---|---|
| All 25 reports in the Reports library | Reports > Library (unchanged, with its search) |
| Profit & loss, Sales report, Stock on hand, Customer accounts | Also pinned at the top of Reports as "The daily four" |
| GST returns: GSTR-1, GSTR-3B, Filing status | Reports > GST (tile) |
| e-Invoice & e-Way bills, e-Invoice log | Reports > GST > e-Invoice & e-Way |
| Margin register | Reports > GST |
| (new) Books check | Reports > Books check |

### Setup

| Today | New place |
|---|---|
| Branches, permissions, cashiers & PINs | Setup > Branches |
| Billing phones & tablets, Phone apps, Warehouse app logins | Setup > Phones & logins |
| Settings: business profile, appearance, documents, counter, security, printer test | Setup > Settings (unchanged inside) |
| Modules and features | Setup > Modules |
| Teach ARFA (bill reader) | Setup > Settings > Bill reader training |
| Stock locations | Setup > Places |
| Subscription | Setup > Subscription |

---

## Appendix B: where the work lands in the code

| Change | Main files |
|---|---|
| Menu and hubs | `app/navigation.py`, new hub templates, `app/templates/base.html` |
| New sale and its rules | new `app/sell/`, calling `app/invoicing/services.py`, `app/sales/services.py`, `app/warehouse/nearest.py`, `app/warehouse/dispatch.py` |
| Order part deliveries | `app/models/purchase.py`, `app/purchases/services.py`, one migration |
| Bill-to-order matching | new `app/purchases/matching.py`, `app/scan/services.py`, `app/templates/scan/review.html` |
| SAP orders | new `app/integrations/sap/`, a scheduled job, a mapping table |
| Bill reader training | `app/scan/lessons.py`, `app/models/scan.py`, `app/settings/`, `app/templates/scan/teach.html` |
| One calculation per number | `app/finance/dashboard.py`, `app/reports/sales.py`, `app/finance/pnl.py`, `app/reports/gstr.py` |
| Books check | new `app/finance/checks.py`, the nightly job, `app/monitoring.py` for the alert |
| Period closing | `app/finance/filing.py`, the document services, one migration |
| 3D dashboard | `app/templates/reports/dashboard.html`, new `app/static/js/dashboard3d.js`, three.js in `app/static/js/vendor/` |
