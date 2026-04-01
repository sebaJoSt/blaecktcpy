# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **Custom command forwarding**: Custom commands can now be forwarded to upstream
  devices. Use `forward_custom_commands=True` on `add_tcp()`/`add_serial()` to
  opt-in per upstream, and `forward=True` on `@on_command()` or `forward_command()`
  to mark which commands should be forwarded.
- **Timestamps**: Data frames can now include timestamps via `timestamp_mode`
  property (`TimestampMode.MICROS` for µs since start, `TimestampMode.UNIX` for
  µs since epoch). Write methods accept `unix_timestamp` (float seconds or int µs)
  and `micros_timestamp` (int µs) overrides matched to their respective modes.
  Wire format is uint64 (8 bytes).
- **`interval_ms` property**: Replaces `set_interval()` method.
- **`start_time` property**: Exposes the `time.time()` value captured at `start()`.
- **`TimestampMode` enum**: `NONE`, `MICROS`, `UNIX`.

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
