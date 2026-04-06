# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **TCP auto-reconnect**: Hub upstreams can now automatically reconnect after
  connection loss. Pass `auto_reconnect=True` to `add_tcp()`. On disconnect the
  hub zeros the upstream's signals, sends a `STATUS_UPSTREAM_LOST` (0x80) data
  frame (with auto-reconnect flag in StatusPayload), and retries every 5 seconds.
  On successful reconnect a `STATUS_UPSTREAM_RECONNECTED` (0x81) data frame is
  sent. If the upstream device restarted, a 0xC0 restart notification follows.
- **0xC0 upstream restart frame**: When a hub-connected upstream restarts
  (detected via `server_restarted` in device info or the data frame restart flag),
  the hub builds and sends a 0xC0 frame downstream with the upstream's device
  name, hardware/firmware versions, and library info.
- **`STATUS_UPSTREAM_RECONNECTED` (0x81)**: New data frame status byte indicating
  an upstream device has reconnected after being lost.
- **D2 schema hash**: Every D2 data frame now includes a 2-byte CRC16-CCITT
  hash of the signal schema (names + datatype codes). Hubs compare this hash
  per-upstream on each frame; on mismatch the upstream is paused, a fresh
  `WRITE_SYMBOLS` + `GET_DEVICES` is requested, and relay resumes only after
  the signal table is rebuilt. Prevents silent data corruption when an upstream
  device adds, removes, or reorders signals at runtime. D1/B1 frames use a
  signal-count fallback for partial detection.
- **Custom command forwarding**: Custom commands can now be forwarded to upstream
  devices. Use `forward_custom_commands=True` on `add_tcp()`/`add_serial()` to
  opt-in per upstream, and `forward=True` on `@on_command()` or `forward_command()`
  to mark which commands should be forwarded.
- **`TimestampMode.MICROS` removed from user API**: `TimestampMode.MICROS` can no
  longer be set on blaecktcpy servers. Use `TimestampMode.UNIX` instead. The enum
  value is retained internally for protocol parsing and hub relay of upstream
  Arduino devices. The `micros_timestamp` parameter has been removed from all
  write methods.
- **Timestamps**: Data frames can now include timestamps via `timestamp_mode`
  property (`TimestampMode.UNIX` for µs since epoch). Write methods accept
  `unix_timestamp` (float seconds or int µs) overrides.
  Wire format is uint64 (8 bytes).
- **`local_interval_ms` property**: Replaces `set_interval()` method.
- **`start_time` property**: Exposes the `time.time()` value captured at `start()`.
- **`TimestampMode` enum**: `NONE`, `MICROS` (protocol-level only, not user-facing), `UNIX`.

### Changed (v3.0 — Breaking)

- **Unified class**: `BlaeckServer` and `BlaeckHub` merged into `BlaeckTCPy`.
  A device with no upstreams is a pure server; add `add_tcp()`/`add_serial()` for hub mode.
- **`start()` required**: Call `start()` after setup (add_signal, add_tcp, interval_ms)
  before using tick/read/write. The constructor no longer binds the socket.
- **No more `hub.local`**: All local signal methods (`write()`, `update()`,
  `tick()`, etc.) are directly on the `BlaeckTCPy` instance.
- **Single `tick()`**: `hub.tick()` + `hub.local.tick()` replaced by one `device.tick()` call
  that reads commands, polls upstreams, and sends timed data.
- **`set_interval()` removed**: Use the `interval_ms` property instead.

### Removed

- `BlaeckServer` class — use `BlaeckTCPy`
- `BlaeckHub` class — use `BlaeckTCPy` with `add_tcp()`/`add_serial()`
- `HubLocalSignals` class — methods now on `BlaeckTCPy` directly
- `hub.local` namespace
- `set_interval()` method — use `interval_ms` property

## [2.0.0] - 2026-03-26

Complete rewrite. Version 1.0.0 was a test release — treat 2.0.0 as the first production version.

## [1.0.0] - 2026-03-17

Initial release (test).

[2.0.0]: https://github.com/sebaJoSt/blaecktcpy/compare/1.0.0...2.0.0
[1.0.0]: https://github.com/sebaJoSt/blaecktcpy/releases/tag/1.0.0
