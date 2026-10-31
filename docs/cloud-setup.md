# Moving ARFA to Google Cloud - what Abdul does, step by step

Everything below is done **once**. After it, every change works the way it
does now: you describe it, Claude changes the code and pushes, and it goes
live by itself - Google Cloud Build tests it on staging first, then puts it
live. Claude also reads the logs and works on the database itself. Nothing
here needs to be repeated.

Allow about **2 hours** for steps 1-7 (most of it waiting). Step 8 (the
domain) and step 9 (the phones) come later, when Claude asks.

Never paste a password, key or database address into the Claude chat.
Where a step stores one, it says where.

> **Fresh start (decided 24 Oct 2026).** Render held only demo data, so
> nothing is copied across: Google Cloud starts with an empty database and
> clients are created fresh there. Step 0 (keeping Render's data) and the
> Render database URL in step 7 are therefore optional, there is no
> cutover, and Render is simply switched off once Google Cloud is live.
> `docs/cloud-cutover.md` stays for any later move that does carry data.

---

## Step 0 - TODAY: keep the Render data safe

Your clients' invoices exist **only** in the Render database. Downloading
the code to your laptop saves the code, not the data. Render deletes a free
database 30 days after it was made - that is now. Do **both**:

**A. Upgrade the Render database (keeps it alive; needed anyway - Render
stays up read-only for two weeks after the move).**
1. Open https://dashboard.render.com and sign in.
2. Click the database **gst-billing-db**.
3. Click **Upgrade** (or *Change plan*) and pick the cheapest paid plan
   (Basic). Confirm.

**B. Turn on the nightly backup (a copy outside Render).**
1. In the Render database page: **Connect** -> **External Database URL** ->
   copy it (it starts `postgresql://` and ends `.render.com/...`).
2. Open https://github.com/abdulkadhersoofi-dot/pearl_city_biriyani ->
   **Settings** -> **Secrets and variables** -> **Actions** ->
   **New repository secret**.
3. Name: `BACKUP_DATABASE_URL`. Secret: paste the URL. **Add secret**.
4. **Actions** tab -> **Database backup** (left) -> **Run workflow** -> **Run workflow**.
5. After about 2 minutes the run shows a green tick; open it, and under
   **Artifacts** download **database-backup**. Keep that file safe (it is
   every client's data). From now on it runs every night at 02:00.

---

## Step 1 - Create the Google Cloud account and billing

1. Open https://console.cloud.google.com and sign in with your Google (Gmail)
   account.
2. If it offers **Start free** / **Activate**: click it. New accounts get
   free credit (about $300 for 90 days) - it covers the first months.
3. Country: **India**. Account type: **Business** - enter your firm's name
   and **GSTIN** if it asks, so Google's invoices carry it (input tax credit).
4. Add a payment method: a credit or debit card that allows online
   recurring payments (an e-mandate), or another method it offers. Google
   may make a small test charge that is refunded.

## Step 2 - Create the project

1. In the console, top bar: click the project picker (next to the Google
   Cloud logo) -> **New project**.
2. Project name: `ARFA`. Under the name it shows a **Project ID** - click
   **Edit** and set something like `arfa-gst-prod` (it must be unique in all
   of Google; if taken, add digits: `arfa-gst-prod-1`).
3. **Write the Project ID down** - Claude needs it. Click **Create**.
4. Menu (three lines, top left) -> **Billing** -> if it says the project has
   no billing account, **Link a billing account** -> pick the one from step 1.

## Step 3 - Add Firebase to the project (for arfa.live and its free certificate)

1. Open https://console.firebase.google.com -> **Create a project** (or
   *Add project*).
2. Choose **Add Firebase to an existing Google Cloud project** (or type the
   name and pick your `ARFA` project from the list) -> accept the terms.
3. Google Analytics: switch it **off** (not needed) -> **Continue** /
   **Add Firebase**.
4. In the left menu: **Build** -> **Hosting** -> **Get started** -> click
   **Next** through the steps (ignore the commands it shows) -> **Continue to
   console**.

## Step 4 - Run the setup script in Cloud Shell (about 10 minutes)

Cloud Shell is a terminal inside the browser, already signed in as you -
nothing to install.

1. In https://console.cloud.google.com, select your `ARFA` project (top bar),
   then click the **Activate Cloud Shell** button (a `>_` icon, top right).
   A terminal opens at the bottom. Click **Authorize** if asked.
2. Get the script into Cloud Shell - either:
   - type (works if the repository is public):
     ```
     git clone -b claude/epic-keller-g2gvbr https://github.com/abdulkadhersoofi-dot/pearl_city_biriyani.git
     cd pearl_city_biriyani
     ```
   - or, if that asks for a password: in the Cloud Shell window click the
     three-dot menu -> **Upload** -> choose `scripts/gcp/bootstrap.sh` from the
     code on your laptop. It lands in your home folder.
3. Run it, with **your** Project ID:
   ```
   bash scripts/gcp/bootstrap.sh arfa-gst-prod
   ```
   (or `bash bootstrap.sh arfa-gst-prod` if you uploaded it).
4. It prints `== 1/9` ... `== 9/9` and ends with **Done**. If step 9/9 says
   **"Key creation was refused"** (an account in a Google Workspace
   organisation, which blocks keys by default), run
   `bash scripts/gcp/allow_key.sh YOUR_PROJECT_ID`, wait 2 minutes, and run
   bootstrap.sh again - it lifts the block for this one project only. If it stops with a
   message instead, copy the **last 20 lines** (not keys) to Claude.

What it did: turned on the Google services, made five service accounts
each with only the permissions it needs, the image store, a transfer
bucket, empty secret boxes, the **Rs.15,000/month budget alert** (emails
you at 50%, 90%, 100%), and Claude's key. Claude's account can deploy, read
logs and change data, but can **not** delete the database, its backups or
the project.

## Step 5 - Connect GitHub to Cloud Build (push = deploy)

1. Console menu -> **Cloud Build** -> **Repositories**. If there are two
   tabs, use **2nd gen**.
2. **Create host connection** -> Region: `asia-south1 (Mumbai)` -> Name:
   `github` (exactly this) -> **Connect**.
3. A GitHub window opens: sign in, **Authorize**, then **Install** the
   *Google Cloud Build* app on your account - choose **Only select
   repositories** -> `pearl_city_biriyani` -> **Install**.
4. Back in Cloud Build: **Link repository** -> connection `github` ->
   repository `pearl_city_biriyani` -> **Link**.

## Step 6 - Give Claude its key (in the Claude Code environment, not the chat)

1. Back in Cloud Shell, make the key one line and download it:
   ```
   base64 -w0 ~/claude-deployer-key.json > ~/key-oneline.txt
   cloudshell download ~/key-oneline.txt
   ```
   Your browser downloads `key-oneline.txt`.
2. Open https://claude.ai/code, open this project's session, click the
   **environment** name in the session's title bar -> **Edit**.
3. Under **Environment variables**, add a new line:
   `GCP_SA_KEY_JSON=` followed by the whole contents of `key-oneline.txt` -
   one long line of letters and digits (open it in Notepad / Kate, select
   all, copy, paste right after the `=`).
   If the settings offer a **Network secrets** / **API credentials** section
   instead, put it there under the same name. **Save**.
4. Delete the key from Cloud Shell and your laptop:
   ```
   rm ~/claude-deployer-key.json ~/key-oneline.txt
   ```
   and delete `key-oneline.txt` from your Downloads folder.
5. **Start a new Claude Code session** for this project (a running session
   doesn't see new settings) and say: *"The Google Cloud setup is done -
   project ID arfa-gst-prod"*.

## Step 7 - Two values from your notepad, stored in Google (not in chat)

In Cloud Shell, run each command; it asks you to paste the value (nothing
shows while you paste - that is on purpose, and it isn't kept in the
terminal's history), then press Enter:

1. Render's **SECRET_KEY** (Render -> web service **gst-billing-app** ->
   **Environment** -> SECRET_KEY -> reveal). Saved e-invoice logins are
   locked with it, so the new server needs the same one:
   ```
   read -rsp "Paste Render's SECRET_KEY, then Enter: " V && printf '%s' "$V" | gcloud secrets versions add arfa-einvoice-key --data-file=- && unset V
   ```
2. Render's **External Database URL** (Render -> database -> **Connect**),
   used once to copy the data across:
   ```
   read -rsp "Paste the External Database URL, then Enter: " V && printf '%s' "$V" | gcloud secrets versions add arfa-render-db-url --data-file=- && unset V
   ```
3. Only if you use them today (they are in your notepad as SENTRY_DSN,
   SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM) - the same command
   with the secret's name changed: `arfa-sentry-dsn`, `arfa-smtp-host`,
   `arfa-smtp-username`, `arfa-smtp-password`, `arfa-smtp-from`.

**4. Your Ultra Admin login on the new server** (its database starts empty,
so this is how your first account gets made; you sign in with it and turn
on two-step sign-in, then create clients from there):
1. In Cloud Shell, get the latest scripts and re-run the setup - it only
   adds the two new boxes (and skips everything already done, the key too):
   ```
   cd ~/pearl_city_biriyani && git pull && bash scripts/gcp/bootstrap.sh YOUR_PROJECT_ID
   ```
2. In Secret Manager
   (https://console.cloud.google.com/security/secret-manager - signed in as
   the project's owner), the same way as above (**+ New version**, paste,
   **Add new version**):
   - `arfa-super-admin-email`: the email you will sign in with;
   - `arfa-super-admin-password`: a strong password (12+ characters) -
     keep it in your notepad.

Tell Claude which of these you filled: Claude lists them in
`deploy/gcp/settings.env` (`OPTIONAL_SECRETS`) so the deploy passes them on.

Tell Claude "step 7 done". From here Claude builds the database, staging,
the nightly job and the push trigger itself (`scripts/gcp/setup.sh`), puts
the app live on Google Cloud, and reports back.

## Step 8 - The domain (when Claude asks - one visit to your domain registrar)

Claude runs `scripts/gcp/setup.sh domain`, which prints the DNS records
Firebase needs for arfa.live. At the company you bought arfa.live from
(GoDaddy, Hostinger, Namecheap, BigRock ...): **DNS settings** for
arfa.live -> remove any old records pointing at Render -> add exactly the
records Claude gives you -> save. The certificate follows by itself
(minutes to a few hours).

## Step 9 - The phones (once)

Install the new APK on each counter phone (in the app: **Settings** ->
**Check for updates**, or download
https://github.com/abdulkadhersoofi-dot/pearl_city_biriyani/releases/tag/android-latest)
and pair it again with a code from the new server (Branches -> the branch ->
Pair a phone) - the demo pairings stay behind on Render. Claude points the
app at https://arfa.live first (`android/server.json`); after this, an
address change never needs the phones.

---

## VVIP sign-in domains (any time)

A sign-in domain (for example `pcbinvoice.com`) shows only a sign-in page.
You decide which clients may use it: every login of those clients - the
owner's, staff and every branch login - can sign in there, and so can you
as Ultra Admin. After signing in, everyone carries on at
https://arfa.live. Anyone else is sent to https://arfa.live. Any client
can be put on a domain at any time, whatever their package - that is how
a client becomes a VVIP client.

1. The domain must be **owned** by you or the client (GoDaddy or similar).
   One domain can serve several clients; each client is on at most one.
2. Ultra Admin: menu **Sign-in domains** -> type the domain -> **Add
   domain**.
3. On the domain's page: **Add client** for each client who may use it
   (or, from any client's page, **Logo** card -> **Sign-in domain** -> pick
   it -> **Save**). Choose whose logo and colours the page wears under
   **Look and status** (with one client, theirs).
4. Claude runs `scripts/gcp/setup.sh domain THE-DOMAIN` and gives you its
   DNS records; add exactly those at the registrar. The certificate
   follows by itself within a few hours.

To pause a domain: its page -> switch **On** off -> **Save**. To stop using
it: **Remove domain** (its clients go back to arfa.live; ask Claude to
disconnect it from hosting).

---

## Scan bills: the AI key (once)

The Scan bills module reads photos of bills with Google's Gemini AI. It
needs one API key for the whole server; until it has one, bills can be
uploaded but wait to be read.

1. Open https://aistudio.google.com, sign in with the Google account that
   owns the ARFA project, and click **Get API key** -> **Create API key** ->
   choose the `ARFA` project. Copy the key.
2. Make sure billing is on for that key (AI Studio shows "Paid tier"). On
   the free tier Google may use what it reads to improve its models - not
   acceptable for your clients' bills.
3. In Cloud Shell (signed in as the owner), run these three commands; the
   last one asks you to paste the key (nothing shows while you paste):
   ```
   gcloud secrets create arfa-gemini-key --replication-policy user-managed --locations asia-south1
   gcloud secrets add-iam-policy-binding arfa-gemini-key --member serviceAccount:arfa-run@arfa-gst-prod-1.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor
   read -rsp "Paste the Gemini API key, then Enter: " V && printf '%s' "$V" | gcloud secrets versions add arfa-gemini-key --data-file=- && unset V
   ```
4. Tell Claude "the Gemini key is in". Claude adds `gemini` to
   `OPTIONAL_SECRETS` in `deploy/gcp/settings.env` and deploys.

Then, per client: **Settings -> Modules -> Scan bills** (or the Ultra Admin
under the client's package). Each client can read up to 300 bills a day
(`SCAN_DAILY_LIMIT`), which caps the cost.

---

## THINGS ONLY ABDUL CAN DO

1. (Optional - demo data only) the GitHub secret `BACKUP_DATABASE_URL` (step 0).
2. Create the Google Cloud account and billing, with your GSTIN (step 1).
3. Create the project and note its ID (step 2).
4. Add Firebase to the project and open Hosting once (step 3).
5. Run `scripts/gcp/bootstrap.sh` in Cloud Shell (step 4).
6. Connect GitHub to Cloud Build (step 5).
7. Put Claude's key in the Claude Code environment as `GCP_SA_KEY_JSON`, then
   start a new session (step 6).
8. Store the e-mail settings and your Ultra Admin email + password in Google (step 7).
9. Add DNS records at your domain registrar (step 8).
10. Install the new APK on each phone and pair it again, once (step 9).
11. Say **"go"** for switching Render off once Google Cloud is live.

Everything else is Claude's to run.

---

## Optional: database access from your own Fedora laptop

Only if you want to run queries yourself or run Claude Code on the laptop.
Claude's cloud sessions don't need any of this.

```
sudo tee /etc/yum.repos.d/google-cloud-sdk.repo <<'EOF'
[google-cloud-cli]
name=Google Cloud CLI
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-x86_64
enabled=1
gpgcheck=1
repo_gpgcheck=0
gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg
EOF
sudo dnf install -y google-cloud-cli postgresql nodejs
gcloud init                                   # pick the ARFA project
gcloud auth application-default login         # once; a browser opens
mkdir -p ~/.local/bin
curl -o ~/.local/bin/cloud-sql-proxy \
  https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.18.2/cloud-sql-proxy.linux.amd64
chmod +x ~/.local/bin/cloud-sql-proxy
npm install -g firebase-tools@15.32.1
```
Your Google account (the project owner) already has the access it needs.
Then `scripts/gcp/db.sh` opens psql on production (`scripts/gcp/db.sh
staging` for staging). After `gcloud auth application-default login` no
browser step comes back.

## How Claude works once this is set up

| What | Command (Claude runs it) |
|---|---|
| Deploy | `git push` (Cloud Build: staging -> checks -> production -> Firebase) |
| Deploy now, by hand | `scripts/gcp/deploy.sh` |
| Build / deploy logs | `scripts/gcp/logs.sh builds`, `scripts/gcp/logs.sh build` |
| App logs, errors | `scripts/gcp/logs.sh app`, `scripts/gcp/logs.sh errors` |
| Look at data | `scripts/gcp/sql.sh "SELECT ..."` |
| Change data | `scripts/gcp/sql.sh --destructive "UPDATE ..."` (an on-demand backup is taken first) |
| Schema change | a migration in `migrations/versions/`, run by the deploy |
| Secrets | `scripts/gcp/secret.sh list / read / set / rotate` |
| Firebase Hosting | `scripts/gcp/hosting.sh` (also part of every deploy) |

Backups: automatic every night (kept 14 days) plus 7 days of point-in-time
recovery - the database can be brought back to any minute of the last week.
Deletion protection is on: the database can't be deleted by mistake, by
Claude or anyone, without first turning that off in the console.
