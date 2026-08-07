# Changelog

All notable changes to this project will be documented in this file.

## [3.0.0] - 2026-08-08

Feature parity with BlaeckTCP 7.0.0 (server mode): string signals, typed
commands, command acknowledgement, and the message frame.

### Breaking

- **Client compatibility:** string signals introduce a new signal value type
  (`0xA`) in the binary data frame. Hosts decoding a stream must support this
  variable-length type to parse any frame containing a string signal; decoders
  without support may lose sync or drop data. Devices using only numeric signals
  remain compatible. This new wire requirement is why 3.0.0 is a major release.

### Added

- **String signals:** `add_signal(name, "string", value)` registers a
  text-valued signal (type code `0xA`). In the data frame the value is encoded
  as a 1-byte length (max 255 bytes) followed by the UTF-8 bytes, so a host can
  log it to a text column or mirror it as a Home Assistant text sensor.
  `write(...)` / `update(...)` accept `str` values, and hub mode decodes string
  signals from upstream devices for relaying.
- **Typed command registration** for Home Assistant MQTT Discovery:
  `on_number_command(...)`, `on_switch_command(...)`, `on_select_command(...)`,
  `on_button_command(...)` and `on_text_command(...)` register a command
  together with the metadata a dashboard needs (kind, plus where applicable a
  numeric range/step/unit, select options, free-text max length, and a mirrored
  state signal). On `BLAECK.WRITE_COMMANDS` the device emits a `0xE0` "Command
  List" frame describing every typed command; plain `on_command(...)`
  registrations are advertised as kind PLAIN. `write_commands([msg_id])` sends
  the catalog on demand. Typed values are validated before the handler runs:
  out-of-range numbers, non-`0/1` switch payloads and out-of-range select values
  are rejected. Select payloads accept an option name (case-insensitive) or a
  numeric index and are normalized to the index. Text values are percent-decoded
  and length-checked.
- **Command acknowledgement (`0xF0` frame):** after dispatching a typed command
  the device replies to the sending client with an FNV-1a hash of the received
  command plus an accept/reject status and reason code. Like `0xE0`, the frame
  carries no CRC.
- **Message frame (`0x90`):** `write_message(channel_name, text[, msg_id])`
  sends a fire-and-forget, named free-text status/log line from the device to
  every connected host. The payload is `name` (NUL-terminated) followed by a
  2-byte little-endian length and the UTF-8 text (up to 65535 bytes, truncated
  beyond). Carries no CRC and is broadcast regardless of the data mask; never
  logged as signal data. A host can surface each channel as its own Home
  Assistant text sensor.
- Added the `server/waveform_generator.py` example: a dashboard-friendly
  waveform generator (frequency, amplitude, offset, shape, on/off) controllable
  live over typed commands via Loggbok.

### Fixed

- Declared `_last_custom_commands` on the internal `HubHost` protocol so static
  type checkers can verify the hub command-replay path.

## [2.0.1] - 2026-04-30

### Fixed

- Include `py.typed` marker in package distribution so type checkers (basedpyright, mypy) resolve the package on non-editable installs

## [2.0.0] - 2026-04-21

Complete rewrite. Version 1.0.0 was a test release — treat 2.0.0 as the first production version.

## [1.0.0] - 2026-03-17

Initial release (test).

[3.0.0]: https://github.com/sebaJoSt/blaecktcpy/compare/2.0.1...3.0.0
[2.0.1]: https://github.com/sebaJoSt/blaecktcpy/compare/2.0.0...2.0.1
[2.0.0]: https://github.com/sebaJoSt/blaecktcpy/compare/1.0.0...2.0.0
[1.0.0]: https://github.com/sebaJoSt/blaecktcpy/releases/tag/1.0.0
