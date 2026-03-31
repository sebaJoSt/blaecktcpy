# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed (v3.0 — Breaking)

- **Unified class**: `BlaeckServer` and `BlaeckHub` merged into `BlaeckTCPy`.
  A device with no upstreams is a pure server; add `add_tcp()`/`add_serial()` for hub mode.
- **`start()` required**: Call `start()` after setup (add_signal, add_tcp, set_interval)
  before using tick/read/write. The constructor no longer binds the socket.
- **No more `hub.local`**: All local signal methods (`write()`, `update()`, `set_interval()`,
  `tick()`, etc.) are directly on the `BlaeckTCPy` instance.
- **Single `tick()`**: `hub.tick()` + `hub.local.tick()` replaced by one `device.tick()` call
  that reads commands, polls upstreams, and sends timed data.

### Removed

- `BlaeckServer` class — use `BlaeckTCPy`
- `BlaeckHub` class — use `BlaeckTCPy` with `add_tcp()`/`add_serial()`
- `HubLocalSignals` class — methods now on `BlaeckTCPy` directly
- `hub.local` namespace

## [2.0.0] - 2026-03-26

Complete rewrite. Version 1.0.0 was a test release — treat 2.0.0 as the first production version.

## [1.0.0] - 2026-03-17

Initial release (test).

[2.0.0]: https://github.com/sebaJoSt/blaecktcpy/compare/1.0.0...2.0.0
[1.0.0]: https://github.com/sebaJoSt/blaecktcpy/releases/tag/1.0.0
