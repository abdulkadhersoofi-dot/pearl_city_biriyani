# ARFA Warehouse (Android)

The pickers' app for warehouse dispatch by serial scan (the website's module
`warehouse_dispatch`; server side in `app/warehouse` and `app/warehouse_api`).
A separate Gradle project from `android/` (ARFA Counter, ARFA Scan), which it
never touches. `applicationId com.arfa.warehouse`, minSdk 26.

## Build and release

`.github/workflows/warehouse-apk.yml` builds on every push that touches
`warehouse-android/`: JVM unit tests, the instrumented test (compiled), the
release APK (R8, signed with the same shared key as the other apps - a Play
Store key is a separate decision). It publishes `ARFA-Warehouse.apk` and
`warehouse-version.json` to arfa.live/apps (the private apps bucket) and the
`warehouse-latest` GitHub release. The server URL is `https://arfa.live`
(`WAREHOUSE_SERVER_URL` overrides it at build time).

Run the instrumented scan -> sync -> confirm test on a device or emulator:
`./gradlew :app:connectedDebugAndroidTest`.

## How it works

- **Sign-in**: business code + login id + password once per phone (refresh
  token in EncryptedSharedPreferences), then a PIN on open and after 5 idle
  minutes. Logins are made on the website (Warehouse > App logins).
- **Pipeline**: every open order for my warehouse, oldest first; Start
  picking claims it (only one phone can win); My picks; Done today; a
  warehouse switcher for logins in more than one.
- **Scanner** (`scan/`): barcodes on every frame, text recognition after 2 s
  with no match; both matched exactly (after normalising look-alike
  characters) against the line's `allowed_serials`. "Read label with ARFA"
  sends the frame to the server's label reader. The label frame is kept as the
  pick's proof photo (under 1 MB). Rugged scanners: DataWedge / Honeywell
  intent output set to the action `com.arfa.warehouse.SCAN`, or keyboard wedge
  (Settings > Rugged scanner phone).
- **Offline** (`data/Db.kt`, `sync/`): opening an order caches it; scans are
  queued in Room and sent by WorkManager; the server's answer is final - a
  refused scan puts its line back to "needs scan" with a notification.
  Confirm unlocks only when every unit is server-confirmed.
- **Pushes** (`push/`): the Firebase settings come from
  `/api/wh/v1/app/config` (set by `scripts/gcp/warehouse_push_setup.sh`); a new
  order arrives as a high-priority data message and rings on the "New orders"
  channel even with the app closed. Without push the pipeline polls every 30 s.
- **Receive / Count**: scan a purchase order's units in; count every serial on
  the floor (the result is on the website).

## EMI sales (stage 5B)

- EMI orders are pinned first in red with "customer waiting N min" and ring on
  the **EMI sales** channel (alarm sound - audible on silent). Confirm reads
  **Lock for EMI**.
- **Deliver** tab: invoiced orders; each unit is scanned again and must be the
  invoiced serial (shown with its rack). **Locate** beeps on the box being
  looked for. A damaged box: **Ask for a swap** (approved on the website).
- Racks: in Receive, scan a rack label (`RACK:C3`) and the units scanned next
  are put on it; Receive > **Move units to a rack** re-shelves them.
