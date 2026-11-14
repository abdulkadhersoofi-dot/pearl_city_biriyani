# Cutover from Render to Google Cloud - runbook and rollback

> **Not used for the first move (decided 24 Oct 2026):** Render held only
> demo data, so Google Cloud started with an empty database and nothing
> was copied (`PRODUCTION_ENABLED="1"` from the start). This runbook - and
> `transfer.sh`, `transfer_back.sh`, the read-only switch - stays ready for
> any later move that has to carry real data.

Written **before** the cutover, so nothing is decided under pressure. Each
step names who does it. Abdul says **"go"** before step C1; nothing in
section C starts without it.

Render stays up, **read-only**, for **two full weeks** after the cutover -
as the reference copy and the way back. It is switched off only after a
second "go" (section E).

---

## A. Ready before the day (Claude, with the setup guide done)

| # | Check | Pass when |
|---|---|---|
| A1 | `scripts/gcp/setup.sh database`, `connections`, `scheduler`, `trigger` have run | each ends without error; `MAX_INSTANCES` committed from the live `max_connections` |
| A2 | Rehearsal copy: `scripts/gcp/transfer.sh staging` | prints **IDENTICAL**; note how long it took (sets the window length) |
| A3 | Staging on the copy: https://PROJECT_ID-staging.web.app | sign in as Ultra Admin, a client admin, a branch; open the latest invoice of 3 clients; their PDFs; day-book; GSTR-1 of last month for 2 clients **equals Render's** figures |
| A4 | Proxies: staging logs (`scripts/gcp/logs.sh staging`) show `sign-in from <your IP>` | the IP is the real one, not Google's - else fix `PROXY_FIX_X_FOR` |
| A5 | A spare phone paired to staging bills offline and syncs | the bill appears once in staging's day-book |
| A6 | Phones already on https://arfa.live (setup guide step 8a) | every counter phone shows arfa.live; `android/server.json` is set |
| A7 | Firebase has a certificate for arfa.live ready (setup guide step 8b, ownership TXT record added in advance) | `scripts/gcp/setup.sh domain` shows the certificate ACTIVE or the records to add |
| A8 | This document's rollback (R1-R3) rehearsed on staging once | `transfer_back.sh` tried against a scratch Render copy, or reviewed line by line if no scratch copy exists |

Pick the window: a quiet night (for example 23:30-01:00 IST), not between
the 10th and the 20th (GSTR-1 / 3B filing days), not month-end.
Tell clients the day before (WhatsApp): *"ARFA will be in view-only mode
from 23:30 to about 01:00 tonight for an upgrade."*

## B. The numbers that must carry over exactly

`deploy/gcp/verify_counts.sql`, compared by `transfer.sh`:
every table's row count; per business, the number **and value** of
invoices, counter bills and notes; every numbering series' next number;
the schema version. **Any difference stops the cutover.**

## C. Cutover (after Abdul's "go")

| # | Who | Step | Takes |
|---|---|---|---|
| C1 | Claude | Commit `deploy/render-read-only`, push. Render redeploys (3-5 min) into read-only: every page shows "This copy is read-only"; saving anything is refused (503); phones keep their offline bills queued | 5 min |
| C2 | Claude | Check Render refuses a change (a POST answers 503) and no request is still writing (Render's last `audit_logs` row stops moving) | 2 min |
| C3 | Claude | `scripts/gcp/transfer.sh production` - copy and compare | from A2 |
| C4 | Claude | Set `PRODUCTION_ENABLED="1"` in `deploy/gcp/settings.env`, push. Cloud Build: migrate production (no-op), deploy with no traffic, health check, traffic, then Firebase Hosting for both sites. Then `scripts/gcp/setup.sh scheduler` again: it binds and resumes the nightly job (created paused before production existed) | 10 min |
| C5 | Claude | On https://PROJECT_ID.web.app: sign in (3 roles), latest invoice numbers of 5 clients **equal Render's**, a PDF opens, `/healthz` shows the new version | 5 min |
| C6 | Abdul | At the registrar: replace the Render records for arfa.live with the ones `setup.sh domain` printed | 5 min + DNS |
| C7 | Claude | Watch: `logs.sh errors`, `/healthz` on arfa.live, the first counter bills arriving, queued phone bills syncing (each once) | 2 h |
| C8 | Claude + Abdul | **Acceptance** at C6 + 2 h, when none of R3's triggers has fired | - |

## D. Rollback - triggers decided in advance

**R1 - the copy isn't identical (C3), or setup breaks before C4.**
Nothing has changed anywhere yet.
Do: delete `deploy/render-read-only`, push - Render is writable again in 5
minutes. Tell clients the upgrade is postponed. Fix, rehearse again (A2).

**R2 - production fails its health check or C5's checks (DNS not yet changed).**
Cloud Build keeps the failed revision out of traffic by itself; nobody has
used Google Cloud yet.
Do: as R1 (remove the read-only flag). Production's database is not used;
before the next attempt Claude empties it (stated as destructive, staging
procedure).

**R3 - after the DNS change, before acceptance (C6 + 2 h), any of:**
- clients can't sign in, for more than 10 minutes;
- more than 5% of requests fail (5xx) for 10 minutes;
- a document number repeats or skips, or a tax total differs from Render's;
- a phone's offline bill is lost or booked twice.

Do, in this order:
1. Claude: make Google Cloud read-only (set `READ_ONLY_MODE=1` on the
   Cloud Run service) - no new documents on either side.
2. Claude: `scripts/gcp/transfer_back.sh --replace-render-database` -
   Cloud SQL's data (including any document issued on Google Cloud since
   C6) is copied over Render's database, and compared; it must print
   **IDENTICAL**. Numbers issued on Google Cloud are kept, so no number is
   ever issued twice.
3. Abdul: at the registrar, put the Render records for arfa.live back.
4. Claude: delete `deploy/render-read-only`, push - Render takes over.
5. Claude: set `PRODUCTION_ENABLED="0"`, push; write up what failed.

**After acceptance** there is no going back to Render: Google Cloud is the
books. A problem is fixed forward, and data is restored from Cloud SQL's
own backups (nightly, 14 kept) or point-in-time recovery (any minute of
the last 7 days, into a new instance first - never over the live one).

## E. Two weeks later - switching Render off (after Abdul's second "go")

1. Claude: one last `verify_counts.sql` on Render, kept in the transfer
   bucket with the final dump (the bucket deletes files after 30 days - the
   nightly Cloud SQL backups take over).
2. Abdul: Render dashboard -> **gst-billing-app** -> Settings -> **Delete**;
   the database **gst-billing-db** -> **Delete**; the Redis
   **gst-billing-redis** -> **Delete**. (Render's billing stops.)
3. Claude: remove `render.yaml`, the Render-only parts of
   `deploy/entrypoint.sh`, and `.github/workflows/db-backup.yml`'s Render
   secret (backups are Cloud SQL's now); push.
