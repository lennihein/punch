# Punch! Wear OS Client

Native Kotlin + Jetpack Compose for Wear OS app & Jetpack Tile for daily punching against your Punch! server (e.g. `https://punch.example.com`).

## Features

- **6-Digit Pairing Flow:** Enter the 6-digit numeric code generated under `/settings` → **Pair Wear OS Device**. The returned Watch Bearer token is stored in `EncryptedSharedPreferences`.
- **Main Punch Screen & Tile (`PunchTileService`):** Matches across both the watch app and widget: status pill (`● REMOTE` / `● OFFICE` / `○ OFF DUTY` + warning notification dot), live `HH:MM` current duration, `Today HH:MM · Break Xm` status line, and direct `Remote` / `Office` / `Clock Out` / `→ Office` / `→ Remote` actions without leaving the Tile.
- **Offline Queue & Auto-Sync (Wi-Fi-only Watch):**
  - Over Bluetooth to your paired Android phone, Wear OS automatically tunnels HTTPS requests using the phone's connection.
  - If both Wi-Fi and Bluetooth are unavailable when you punch, the punch is persisted locally with its exact `client_ts` and unique idempotency `event_id`, the watch updates optimistically, and `WorkManager` (`PunchSyncWorker`, `NetworkType.CONNECTED`) flushes the queue in FIFO order as soon as connectivity returns.

## Build & Sideload onto Pixel Watch (via `adb`)

1. Build the APK inside the Nix Wear shell:
   ```bash
   nix develop .#wear --impure --command bash -c "cd wear && gradle assembleDebug --no-daemon"
   ```
   Output: `wear/build/outputs/apk/debug/wear-debug.apk`.
2. On your **Pixel Watch**:
   - Open **Settings → System → About → Versions** and tap **Build number** 7 times to enable Developer options.
   - Open **Settings → Developer options**, turn on **ADB debugging** and **Wireless debugging**.
3. Connect & install:
   ```bash
   adb pair <watch-ip>:<pair-port>
   adb connect <watch-ip>:<connect-port>
   adb install -r wear/build/outputs/apk/debug/wear-debug.apk
   ```
4. Open **Punch!** on the watch, generate a 6-digit code in **Settings → Pair Wear OS Device** at `https://<your-punch-host>/settings`, and enter it on the watch.
