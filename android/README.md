# ARFA Counter - the Android app for a branch's counter

A small native Android app around the ARFA Counter screens the server
renders at `/app/` (pairing, Bill, Tables & KOT, Held, Today, Kitchen) -
not a reimplementation. It is a full-screen WebView locked to this one
app's server, so billing, GST, numbering, KOTs and offline queues are the
server-rendered pages and scripts in `app/` (see "Counter app" in the
main README). This directory adds the native frame and what a web page
can't do on its own.

## What it does

- **Opens on the app's start, `/app/?start=1`.** A phone that isn't
  paired sees the pairing screen: the owner taps *Pair a phone* on the
  website's Branches page and the 6-digit code is typed once - no email
  or password at the counter. A paired phone signs itself back in with
  its own secret and opens what it is for.
- **Counter, Waiter or Kitchen.** A restaurant's phone is asked, once,
  what it is for; the app then opens straight on the Bill screen, the
  Tables, or the Kitchen screen (changeable on the app's home screen).
  Any other business's phone is the counter.
- **Tells the server it is the app**: the WebView's user agent ends with
  `ARFACounter/<version>`, so a branch login that signs in by email
  inside the app still lands on the app's screens (`/app/`), not the
  website's counter. Any other login (owner, auditor...) is signed out
  with a "this app is for a branch's counter" message.
- **Works without internet**: the app's screens, items and the bills /
  KOTs made offline are kept by the web page (service worker, IndexedDB,
  localStorage) - the app enables DOM storage and its "can't reach the
  server" screen offers **Open the counter without internet**.
- **The kitchen tablet stays lit** while the Kitchen screen is open.
- Receipts and KOT slips print **straight to a Bluetooth or USB thermal
  printer** (ESC/POS), with Android's print dialog as the fallback; a
  cash drawer on the printer opens after a cash sale. *Printer, cash
  drawer & app settings* is on the app's home screen.
- **Kiosk mode** keeps a counter tablet on the app (Android lock task).
- **Update check** against the `android-latest` release.
- Native splash screen, launcher icon (the smiling receipt), offline
  screen and back-button handling.

It is the same Android app as before ("ARFA POS", `com.arfa.pos`) under
its new name, so a phone with the old app takes the new one as an
update - see "Signing" below for the one-time reinstall.

## Server URL

Set once, in `app/build.gradle`:

```groovy
buildConfigField "String", "SERVER_BASE_URL", "\"https://gst-billing-app-prdx.onrender.com\""
```

This is the Render.com URL in use right now. When a real domain
replaces it, change this one line (and bump `versionCode`/`versionName`
just above it) and let the GitHub Actions workflow below rebuild - no
other file references the server URL.

## Printing

The web app normally prints a receipt by loading a server-generated PDF
into a hidden `<iframe>` and calling `window.print()` on it - desktop
and mobile browsers handle that natively. Android's WebView has no
built-in PDF viewer and no `window.print()` support to drive that way,
so `MainActivity.kt` injects a small JS bridge, `window.AndroidPrint`,
that the web template (`app/templates/pos/terminal.html`) calls instead
*when running inside this app* (feature-detected - `window.AndroidPrint`
is `undefined` everywhere else, so browser behavior is unchanged). The
app downloads the same PDF the browser path would have (reusing the
WebView's own session cookie) and hands it to Android's `PrintManager`,
which opens the real system print dialog - that's what actually talks
to a thermal receipt printer via whatever Android print service the
printer's manufacturer provides.

### Direct thermal printing (ESC/POS) - Phase 10

The page lays the receipt out itself (`app/static/js/escpos.js`, from
the bill's receipt document `/pos/bills/<id>/receipt.json`, or an
offline bill's) and hands the bytes to `window.AndroidPrint.printRaw`.
`EscPosPrinter.kt` only moves them: over Bluetooth (the Serial Port
Profile, UUID 00001101-...) to a *paired* printer, or over USB to the
printer's bulk OUT endpoint (with an OTG cable on a phone). Nothing
scans for devices.

Set it up from **Printer, cash drawer & app settings** on the app's home
screen (or the full counter's Printer button) - only shown inside the
app:

- *Receipt printer*: Bluetooth (pick from paired printers - pair it in
  Android's Bluetooth settings first; Android 12+ asks for "Nearby
  devices"), USB (Android asks to allow the device), or none (the PDF
  path above, as before).
- *Paper*: 2 inch (58 mm, 32 characters) or 3 inch (80 mm, 48).
- *Cash drawer*: kick it (ESC p 0 25 250) when a cash sale's original
  prints; "Open the cash drawer now" for change.
- *Print a test page*.

If the printer doesn't answer, the app shows why and the page prints the
same receipt through Android's print dialog instead.

### Kiosk mode

*Kiosk mode* in the same settings starts Android's lock task: no home
screen, no other apps. On a tablet set up as a dedicated device (device
owner, with this app allowed) it locks straight away; on any other phone
Android asks to pin the screen. Turning it on asks for a code of 4+
digits; the same code turns it off. The back button never leaves the
app while it's on.

### Offline

Offline billing is a web feature, always on for a paired phone (the flag
`offline_pos` now covers only the website counter - see the main README,
"Six fixes"): the app's screens register a service worker (`/app/sw.js`)
that opens them from the phone at once and refreshes them behind, every
request gives up after 8-10 s, and bills are queued in IndexedDB - each numbered in this phone's own series
(O1/2026-27/0001) - and KOTs in localStorage (the phone's own slip
number, O1-1), and send them when the connection is back, KOTs first.
The app's part: it enables DOM storage, always opens `/app/?start=1`
(so the saved screens load with no connection), and its "can't reach the
server" screen offers **Open the counter without internet**. The splash
waits at most 1.5 s (`SPLASH_MAX_MS`) and the page shows as soon as it is
drawn (`onPageCommitVisible`), so the app never sits on the splash
waiting for a slow server.

### Server address (moving the server without reinstalling)

The address built in is `SERVER_BASE_URL` (app/build.gradle). The release's
`version.json` also carries `server_url`, taken from `android/server.json`:
set it to a new https address and push, and each phone moves on its next
update check (at most 12 hours) - `ServerAddress.kt` keeps the address,
and `MainActivity` copies everything the app keeps on the old address
("arfa.*" in localStorage: pairing, mode, held bills, waiting KOTs) and
writes it back on the new one. It moves only when no offline bill is
waiting on the phone (they are sent first, while online) and only to an
address whose `/healthz` answers.

### Update check

`.github/workflows/android-apk.yml` builds with `ANDROID_VERSION_CODE`
= the workflow run number and publishes `version.json` next to the APK
on the `android-latest` release. `UpdateChecker.kt` reads it at start
(at most twice a day) or from settings, and offers the download when its
versionCode is higher than the installed one. The repository is set by
`RELEASE_REPO` in `app/build.gradle`; a private repository's releases
can't be read without signing in, so the check then just finds nothing.

## Why this isn't built locally

The sandbox this project was written in has no route to `dl.google.com`
(and `maven.google.com`, which redirects there) - the host the Android
SDK, the Android Gradle Plugin, and every AndroidX/Material dependency
are fetched from. That means this project could be *written* but not
*compiled or dependency-resolved* in that environment. The Gradle
wrapper (`gradlew`, `gradle/wrapper/*`) was generated for real with a
locally available plain Gradle install, but the first real build of
this project happens in CI.

## Building (GitHub Actions)

`.github/workflows/android-apk.yml` builds a debug-signed APK on every
push that touches `android/**`, or on demand (Actions tab -> "Build
ARFA Counter Android APK" -> Run workflow). It:

1. Builds `app-debug.apk` with the pinned Android SDK platform 34 /
   build-tools 34.0.0.
2. Uploads it as a run artifact (Actions tab -> the run -> Artifacts;
   expires after 90 days).
3. Publishes/updates a GitHub Release tagged `android-latest` with the
   APK attached as `ARFA-Counter.apk` (and, for links handed out before
   the rename, `ARFA-POS-debug.apk`) - a stable, permanent link, replaced
   by the APK from whichever push built it most recently.

### Signing

Android only installs an update signed with the same key as the app on
the phone. A fresh GitHub runner makes a new debug key every build, so
every build is now signed with one shared key kept in
`app/arfa-counter-debug.keystore` (`app/build.gradle`, `signingConfigs.shared`):
each new APK installs over the last one, keeping the phone's pairing
and anything waiting to be sent.

- **Once, for phones with an APK built before this change**: it was
  signed with a throw-away key, so Android won't update it. On that
  phone, first let any offline bills go (open the app online until
  "waiting to be sent" is gone), then uninstall it and install
  `ARFA-Counter.apk`; pair it again with a new code.
- **Your own key** (recommended before handing the app to many shops -
  anyone with this repository could sign an "update" with the shared
  key): make a keystore, then add repository secrets
  `ANDROID_KEYSTORE_BASE64` (the file, base64), `ANDROID_KEYSTORE_PASSWORD`,
  `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`. The workflow uses them
  instead (phones then need the one-time reinstall again).

To install: on the phone, enable "install unknown apps" for whichever
browser/file manager downloads it, download `ARFA-Counter.apk` from the
`android-latest` release, open it, install, then pair it with the
owner's code.

## Building locally (if you have Android Studio / the SDK already)

```
cd android
./gradlew assembleDebug
```

Output: `app/build/outputs/apk/debug/app-debug.apk`.

## Project layout

- `app/src/main/java/com/arfa/pos/MainActivity.kt` - WebView setup,
  role gating, offline screen, back button, the JS bridge
  (`window.AndroidPrint`: printReceipt, hasPrinter, printerColumns,
  printRaw, printHtml, openDrawer, openSettings, appVersion), the
  printer / kiosk settings dialogs.
- `EscPosPrinter.kt` - bytes to a Bluetooth or USB printer; drawer kick;
  test page.
- `PrinterSettings.kt` - what this device is set up with
  (SharedPreferences).
- `UpdateChecker.kt` - the `android-latest` version check.
- `app/src/main/res/` - native chrome only (splash, launcher icon,
  offline screen strings/colors/theme). All the app's screens are
  server-rendered HTML from `app/templates/counterapp/` (and the full
  counter, `app/templates/pos/`) - nothing here duplicates them.
- `minSdk 26` (Android 8.0+, 2017 onward) - chosen so the launcher icon
  can be a single adaptive vector (no separate legacy PNG fallback set)
  and so the real AndroidX SplashScreen API is available rather than
  its reduced compat shim for older devices.
