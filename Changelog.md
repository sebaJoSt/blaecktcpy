# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- `IntervalMode` enum — `IntervalMode.OFF` (-1) disables timed data, `IntervalMode.CLIENT` (-2) lets the client control the rate (default). Values ≥ 0 are fixed intervals in milliseconds.
- `SignalList` class — a `list` subclass with name-based access (`signals["temperature"]`). Replaces the old `UpstreamSignals` class.
- `BlaeckServer.set_interval(interval_ms)` — lock the server to a fixed timed data rate. Client `ACTIVATE`/`DEACTIVATE` commands are ignored while locked. Pass `IntervalMode.OFF` to disable, `IntervalMode.CLIENT` to return to client control.
- `BlaeckServer.add_signals(signals)` — add multiple signals at once.
- `BlaeckServer.delete_signals()` — remove all signals.
- `BlaeckServer.on_before_write()` decorator — fires before every data send.
- `HubLocalSignals` class — groups all local signal operations under `hub.local`.
- `hub.local.add_signal(signal_or_name, datatype, value)` — add a local signal. Accepts a `Signal` object or name/type/value.
- `hub.local.add_signals(signals)` — add multiple local signals at once.
- `hub.local.delete_signals()` — remove all local signals.
- `hub.local.set_interval(interval_ms)` — set a fixed sending rate for local signals using `IntervalMode`.
- `hub.local.signals` — property returning a `SignalList` of local signals with name-based access.
- `hub.local.on_before_write()` decorator — fires before every local data send.
- `hub.local.write(key, value)` — update a local signal and immediately send it downstream.
- `hub.local.update(key, value)` — update a local signal and mark it as updated (no send).
- `hub.local.mark_signal_updated(key)` — mark a local signal as updated without changing its value.
- `hub.local.mark_all_signals_updated()` — mark all local signals as updated.
- `hub.local.clear_all_update_flags()` — clear the updated flag on all local signals.
- `hub.local.has_updated_signals` — property that returns True if any local signal is marked as updated.
- `hub.local.write_all_data(msg_id)` — immediately send all local signal data downstream.
- `hub.local.write_updated_data(msg_id)` — immediately send only updated local signals downstream.
- `hub.local.timed_write_all_data(msg_id)` — send all local signals if the timer interval has elapsed. Returns `bool`.
- `hub.local.timed_write_updated_data(msg_id)` — send only updated local signals if the timer interval has elapsed. Returns `bool`.
- `hub.local.tick(msg_id)` — convenience alias for `timed_write_all_data()`. Returns `bool`.
- `hub.local.tick_updated(msg_id)` — convenience alias for `timed_write_updated_data()`. Returns `bool`.
- `BlaeckHub.read()` — read and process downstream client commands without polling upstreams.
- `BlaeckHub.commanding_client` — property returning the socket of the currently commanding client (passthrough to server).
- `BlaeckHub.on_command()` decorator — register custom command handlers on the hub (dispatched locally, not forwarded to upstreams).

### Changed

- `BlaeckHub.tick()` no longer sends local signal data. It now only reads downstream commands and polls upstreams. Use `hub.local.tick()` for timed local data.
- `hub.add_tcp()` / `hub.add_serial()` `interval_ms` parameter now defaults to `IntervalMode.CLIENT` and accepts `IntervalMode` values.
- `BlaeckServer.tick()` / `tick_updated()` now return `bool` (True if data was sent).
- `BlaeckServer.timed_write_all_data()` / `timed_write_updated_data()` now return `bool`.
- Timer interval elapsed check now skips missed intervals instead of firing a burst of catchup frames.
- Improved error handling for non-relayed upstream data callbacks.
- Removed unused `timestamp` parameter from `BlaeckServer.write_updated_data()`.

### Fixed

- Hub custom command handlers now fire correctly. Previously, handlers registered via `hub.on_command()` were never dispatched.

### Removed

- `BlaeckServer.timed_activated` property — internal only now.
- `BlaeckServer.set_timed_data()` — use `set_interval()` instead.
- `UpstreamSignals` class — replaced by `SignalList`.
- `BlaeckHub.add_signal()` — use `hub.local.add_signal()`.
- `BlaeckHub.set_local_interval()` — use `hub.local.set_interval()`.
- `BlaeckHub.tick_updated()` — use `hub.tick()` + `hub.local.tick_updated()`.

## [2.0.0] - 2026-03-26

Complete rewrite. Version 1.0.0 was a test release — treat 2.0.0 as the first production version.

## [1.0.0] - 2026-03-17

Initial release (test).

[2.0.0]: https://github.com/sebaJoSt/blaecktcpy/compare/1.0.0...2.0.0
[1.0.0]: https://github.com/sebaJoSt/blaecktcpy/releases/tag/1.0.0
