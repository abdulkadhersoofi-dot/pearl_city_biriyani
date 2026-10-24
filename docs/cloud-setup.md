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
4. It prints `== 1/9` ... `== 9/9` and ends with **Done**. If it stops with a
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

Tell Claude "step 7 done". From here Claude builds the database, staging,
the nightly job and the push trigger itself (`scripts/gcp/setup.sh`), copies
Render's data into **staging** to rehearse, and reports back.

## Step 8 - The domain (when Claude asks - two short visits to your domain registrar)

**8a. Point arfa.live at Render first** (so the phones move to their
permanent address once, while Render still runs everything):
1. Render -> **gst-billing-app** -> **Settings** -> **Custom Domains** ->
   **Add Custom Domain** -> `arfa.live` -> Save. Render shows one or two DNS
   records.
2. At the company you bought arfa.live from (GoDaddy, Hostinger, Namecheap,
   BigRock ...): **DNS settings** for arfa.live -> add exactly those records
   -> save. Back in Render, wait until it says **Verified** and the
   certificate is issued (minutes to a few hours).
3. Tell Claude. Claude then sets the phones' address to https://arfa.live
   (`android/server.json`) - each phone moves by itself within 12 hours,
   keeping its pairing.

**8b. At the cutover**, Claude runs `scripts/gcp/setup.sh domain`, which
prints Firebase's records for arfa.live. You replace the Render records with
those at your registrar. Phones need nothing - they already use arfa.live.

## Step 9 - The phones (once)

Install the new APK on each counter phone (in the app: **Settings** ->
**Check for updates**, or download
https://github.com/abdulkadhersoofi-dot/pearl_city_biriyani/releases/tag/android-latest).
It keeps its pairing. After this, an address change never needs the phones.

---

## THINGS ONLY ABDUL CAN DO

1. Today: upgrade the Render database to a paid plan, and add the GitHub
   secret `BACKUP_DATABASE_URL` (step 0).
2. Create the Google Cloud account and billing, with your GSTIN (step 1).
3. Create the project and note its ID (step 2).
4. Add Firebase to the project and open Hosting once (step 3).
5. Run `scripts/gcp/bootstrap.sh` in Cloud Shell (step 4).
6. Connect GitHub to Cloud Build (step 5).
7. Put Claude's key in the Claude Code environment as `GCP_SA_KEY_JSON`, then
   start a new session (step 6).
8. Store Render's SECRET_KEY and External Database URL in Google (step 7).
9. Add DNS records at your domain registrar, twice (step 8).
10. Install the new APK on each phone, once (step 9).
11. Say **"go"** for the cutover, and later **"go"** for switching Render off
    (docs/cloud-cutover.md).

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
