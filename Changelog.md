# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- TCP keepalive on upstream connections (idle 5 s, interval 1 s, count 5) for faster dead-peer detection after cable pulls

### Fixed

- CLIENT mode upstreams no longer receive `BLAECK.DEACTIVATE` during discovery, preventing a deadlock where nobody re-ACTIVATEs
- Removed duplicate `BLAECK.ACTIVATE` after reconnect in CLIENT mode — the downstream client owns activation

## [2.0.0] - 2026-04-08

Complete rewrite. Version 1.0.0 was a test release — treat 2.0.0 as the first production version.

### Added

- `replay_commands` parameter on `add_tcp()` and `add_serial()` to replay custom commands after upstream restart or reconnect
- "No signals" empty-state indicators on the status page
- Collapsible local signals section on the status page
- Warning logs for failed `send_command` calls during forwarding and replay

### Fixed

- Zero-signal upstreams no longer raise `ValueError` during discovery
- Restart detection for serial upstreams (BlaeckSerial commands-only sketches)
- Consistent log ordering: cause message appears before replay/interval in all restart and reconnect paths

## [1.0.0] - 2026-03-17

Initial release (test).

[2.0.0]: https://github.com/sebaJoSt/blaecktcpy/compare/1.0.0...2.0.0
[1.0.0]: https://github.com/sebaJoSt/blaecktcpy/releases/tag/1.0.0
