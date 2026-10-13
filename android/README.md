# ARFA POS - Android branch app

A thin native Android wrapper around the same POS screens branch (Staff)
logins already see on the website - not a reimplementation. It is a
full-screen WebView locked to this one app's server, so billing, GST
math, held bills, Day-book, Z-report and receipt printing are exactly
the server-rendered pages already built and tested in `app/`. Nothing
in this directory, or in the one CSS/one JS change made elsewhere in
this repo to support it (see below), changes how the website behaves
for a browser.

## What it does

- Opens straight to `/auth/login` on the configured server.
- After a successful sign-in, the app checks which page the server
  actually sent the user to. A branch (Staff) login lands on `/pos/`
  and the app lets them stay; any other role (Client Admin, Super
  Admin, Auditor, ...) lands somewhere else (Invoices, the admin
  console, ...), and the app immediately signs them back out with a
  "branch logins only" message. This mirrors the server's own
  `_post_login_redirect` in `app/auth/routes.py` - the app doesn't
  duplicate that logic, it just reads its result.
- Everything else (cart, checkout, hold/resume, Day-book, Z-report) is
  the same pages a branch login already has, full-screen, with no
  browser chrome (no address bar, no tabs).
- Receipt printing goes through Android's native print dialog instead
  of a browser print preview - see "Printing" below.
- A native splash screen, offline/"can't reach the server" screen, and
  back-button handling, so it reads as a normal installed app rather
  than a bookmarked website.

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
ARFA POS Android APK" -> Run workflow). It:

1. Builds `app-debug.apk` with the pinned Android SDK platform 34 /
   build-tools 34.0.0.
2. Uploads it as a run artifact (Actions tab -> the run -> Artifacts;
   expires after 90 days).
3. Publishes/updates a GitHub Release tagged `android-latest` with the
   APK attached - a stable, permanent link, replaced by the APK from
   whichever push built it most recently.

This is a **debug-signed build**, meant for direct installation
("sideloading"), not a Play Store release - there's no separate release
keystore to manage. To install: on the phone, enable "install unknown
apps" for whichever browser/file manager downloads it, download
`ARFA-POS-debug.apk` from the release, open it, install.

## Building locally (if you have Android Studio / the SDK already)

```
cd android
./gradlew assembleDebug
```

Output: `app/build/outputs/apk/debug/app-debug.apk`.

## Project layout

- `app/src/main/java/com/arfa/pos/MainActivity.kt` - the entire app:
  WebView setup, role gating, offline screen, back button, the print
  bridge.
- `app/src/main/res/` - native chrome only (splash, launcher icon,
  offline screen strings/colors/theme). All POS UI itself is server-
  rendered HTML from `app/templates/pos/` - nothing here duplicates it.
- `minSdk 26` (Android 8.0+, 2017 onward) - chosen so the launcher icon
  can be a single adaptive vector (no separate legacy PNG fallback set)
  and so the real AndroidX SplashScreen API is available rather than
  its reduced compat shim for older devices.
