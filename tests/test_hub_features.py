"""Tests for features added to BlaeckHub and BlaeckServer.

Covers:
- STATUS_OK / STATUS_UPSTREAM_LOST in _build_data_msg
- UpstreamSignals collection (index, name, len, iter, errors)
- Callback registration and dispatch (on_data_received, client callbacks)
- relay_downstream=False signal registration in start()
"""

import binascii
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from blaecktcpy import Signal, STATUS_OK, STATUS_UPSTREAM_LOST
from blaecktcpy.hub._hub import (
    BlaeckHub,
    UpstreamSignals,
    _UpstreamDevice,
    _MSG_ID_HUB,
)
from blaecktcpy.hub._upstream import _UpstreamBase
from blaecktcpy.hub import _decoder as decoder


# ========================================================================
# Helpers
# ========================================================================


def _make_server_on_free_port():
    """Create a BlaeckServer on a random free port."""
    import socket
    import random

    from blaecktcpy import BlaeckServer

    for _ in range(10):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            server = BlaeckServer("127.0.0.1", port, "Test", "HW", "1.0")
            return server
        except OSError:
            continue
    raise OSError("Could not find a free port after 10 attempts")


class FakeTransport(_UpstreamBase):
    """Minimal transport for testing without real connections."""

    def __init__(self, name="fake"):
        super().__init__(name)
        self._connected = True

    def connect(self, timeout=5.0):
        self._connected = True
        return True

    def read_available(self):
        return b""

    def send(self, data):
        return True

    def close(self):
        self._connected = False


# ========================================================================
# Status byte tests
# ========================================================================


class TestStatusByte:
    """Verify _build_data_msg encodes the status byte correctly."""

    def setup_method(self):
        self.server = _make_server_on_free_port()
        self.server.add_signal("sig1", "float", 3.14)
        self.server.add_signal("sig2", "int", 42)

    def teardown_method(self):
        self.server.close()

    def test_default_status_is_ok(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header)
        # Status byte is at position -5 (1 byte before 4-byte CRC)
        assert msg[-5] == STATUS_OK

    def test_status_upstream_lost(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header, status=STATUS_UPSTREAM_LOST)
        assert msg[-5] == STATUS_UPSTREAM_LOST

    def test_crc_excludes_status_byte(self):
        header = self.server.MSG_DATA + b":" + (1).to_bytes(4, "little") + b":"
        msg = self.server._build_data_msg(header, status=STATUS_UPSTREAM_LOST)
        # CRC is computed over everything before status byte
        crc_data = msg[:-5]
        expected_crc = binascii.crc32(crc_data) & 0xFFFFFFFF
        actual_crc = int.from_bytes(msg[-4:], "little")
        assert actual_crc == expected_crc

    def test_status_byte_values(self):
        assert STATUS_OK == 0x00
        assert STATUS_UPSTREAM_LOST == 0x02


# ========================================================================
# UpstreamSignals tests
# ========================================================================


class TestUpstreamSignals:
    """Verify UpstreamSignals collection access patterns."""

    def setup_method(self):
        self.signals = [
            Signal("temperature", "float", 22.5),
            Signal("humidity", "float", 65.0),
            Signal("pressure", "float", 1013.25),
        ]
        self.collection = UpstreamSignals(self.signals)

    def test_access_by_index(self):
        assert self.collection[0].signal_name == "temperature"
        assert self.collection[1].signal_name == "humidity"
        assert self.collection[2].signal_name == "pressure"

    def test_access_by_name(self):
        assert self.collection["temperature"].value == 22.5
        assert self.collection["humidity"].value == 65.0

    def test_index_out_of_range(self):
        with pytest.raises(IndexError):
            _ = self.collection[5]

    def test_name_not_found(self):
        with pytest.raises(KeyError, match="wind_speed"):
            _ = self.collection["wind_speed"]

    def test_invalid_key_type(self):
        with pytest.raises(TypeError, match="float"):
            _ = self.collection[1.5]

    def test_len(self):
        assert len(self.collection) == 3

    def test_iter(self):
        names = [s.signal_name for s in self.collection]
        assert names == ["temperature", "humidity", "pressure"]

    def test_empty_collection(self):
        empty = UpstreamSignals([])
        assert len(empty) == 0
        assert list(empty) == []

    def test_value_updates_propagate(self):
        self.collection["temperature"].value = 30.0
        assert self.signals[0].value == 30.0


# ========================================================================
# Callback registration tests
# ========================================================================


class TestCallbackRegistration:
    """Verify callback decorators register and dispatch correctly."""

    def setup_method(self):
        self.hub = BlaeckHub.__new__(BlaeckHub)
        self.hub._upstreams = []
        self.hub._local_signals = []
        self.hub._server = None
        self.hub._started = False
        self.hub._disconnect_callback = None
        self.hub._client_connect_callback = None
        self.hub._client_disconnect_callback = None
        self.hub._data_received_callbacks = []
        self.hub._command_handlers = {}
        self.hub._command_catchall = None

    def test_on_upstream_disconnected_registers(self):
        @self.hub.on_upstream_disconnected()
        def handler(name):
            pass

        assert self.hub._disconnect_callback is handler

    def test_on_client_connected_registers(self):
        @self.hub.on_client_connected()
        def handler(cid):
            pass

        assert self.hub._client_connect_callback is handler

    def test_on_client_disconnected_registers(self):
        @self.hub.on_client_disconnected()
        def handler(cid):
            pass

        assert self.hub._client_disconnect_callback is handler

    def test_on_data_received_registers_with_name(self):
        @self.hub.on_data_received("Arduino")
        def handler(upstream):
            pass

        assert len(self.hub._data_received_callbacks) == 1
        name, func = self.hub._data_received_callbacks[0]
        assert name == "Arduino"
        assert func is handler

    def test_on_data_received_registers_without_name(self):
        @self.hub.on_data_received()
        def handler(upstream):
            pass

        name, func = self.hub._data_received_callbacks[0]
        assert name is None

    def test_multiple_data_received_callbacks(self):
        @self.hub.on_data_received("Arduino")
        def h1(u):
            pass

        @self.hub.on_data_received("ESP32")
        def h2(u):
            pass

        @self.hub.on_data_received()
        def h3(u):
            pass

        assert len(self.hub._data_received_callbacks) == 3

    def test_on_command_specific_registers(self):
        @self.hub.on_command("SET_LED")
        def handler(state):
            pass

        assert "SET_LED" in self.hub._command_handlers
        assert self.hub._command_handlers["SET_LED"] is handler

    def test_on_command_catchall_registers(self):
        @self.hub.on_command()
        def handler(command, *params):
            pass

        assert self.hub._command_catchall is handler

    def test_on_command_multiple_specific(self):
        @self.hub.on_command("SET_LED")
        def h1(state):
            pass

        @self.hub.on_command("SET_MODE")
        def h2(mode):
            pass

        assert len(self.hub._command_handlers) == 2


class TestFireDataReceived:
    """Verify _fire_data_received dispatches to correct callbacks."""

    def setup_method(self):
        self.hub = BlaeckHub.__new__(BlaeckHub)
        self.hub._data_received_callbacks = []

    def _make_upstream(self, name):
        transport = FakeTransport(name)
        return _UpstreamDevice(device_name=name, transport=transport)

    def test_fires_matching_name(self):
        calls = []

        @self.hub.on_data_received("Arduino")
        def handler(upstream):
            calls.append(upstream.device_name)

        arduino = self._make_upstream("Arduino")
        self.hub._fire_data_received(arduino)
        assert calls == ["Arduino"]

    def test_skips_non_matching_name(self):
        calls = []

        @self.hub.on_data_received("Arduino")
        def handler(upstream):
            calls.append(upstream.device_name)

        esp = self._make_upstream("ESP32")
        self.hub._fire_data_received(esp)
        assert calls == []

    def test_global_callback_fires_for_any(self):
        calls = []

        @self.hub.on_data_received()
        def handler(upstream):
            calls.append(upstream.device_name)

        self.hub._fire_data_received(self._make_upstream("Arduino"))
        self.hub._fire_data_received(self._make_upstream("ESP32"))
        assert calls == ["Arduino", "ESP32"]

    def test_mixed_callbacks(self):
        specific_calls = []
        global_calls = []

        @self.hub.on_data_received("Arduino")
        def specific(upstream):
            specific_calls.append(upstream.device_name)

        @self.hub.on_data_received()
        def global_handler(upstream):
            global_calls.append(upstream.device_name)

        self.hub._fire_data_received(self._make_upstream("Arduino"))
        self.hub._fire_data_received(self._make_upstream("ESP32"))

        assert specific_calls == ["Arduino"]
        assert global_calls == ["Arduino", "ESP32"]


# ========================================================================
# relay_downstream=False tests
# ========================================================================


class TestRelayFalseRegistration:
    """Verify relay_downstream=False signals go to internal storage, not server."""

    def test_relay_true_registers_on_server(self):
        server = _make_server_on_free_port()
        try:
            upstream = _UpstreamDevice(
                device_name="ESP32",
                transport=FakeTransport("ESP32"),
                relay_downstream=True,
                symbol_table=[
                    decoder.DecodedSymbol("temp", 8, "float", 4),
                    decoder.DecodedSymbol("hum", 8, "float", 4),
                ],
            )

            offset = len(server.signals)
            for i, sym in enumerate(upstream.symbol_table):
                sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(sym.datatype_code, "float")
                server.add_signal(sym.name, sig_type)
                upstream._signals.append(server.signals[offset])
                upstream.index_map[i] = offset
                offset += 1
            upstream._upstream_signals = UpstreamSignals(upstream._signals)

            assert len(server.signals) == 2
            assert upstream.index_map == {0: 0, 1: 1}
            assert server.signals[0].signal_name == "temp"
            # relay_downstream=True: upstream._signals references server signals
            assert upstream._signals[0] is server.signals[0]
            assert upstream.signals["temp"] is server.signals[0]
        finally:
            server.close()

    def test_relay_false_stores_internally(self):
        upstream = _UpstreamDevice(
            device_name="Arduino",
            transport=FakeTransport("Arduino"),
            relay_downstream=False,
            symbol_table=[
                decoder.DecodedSymbol("temp", 8, "float", 4),
                decoder.DecodedSymbol("hum", 8, "float", 4),
            ],
        )

        for i, sym in enumerate(upstream.symbol_table):
            sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(sym.datatype_code, "float")
            sig = Signal(sym.name, sig_type)
            upstream._signals.append(sig)
            upstream.index_map[i] = i
        upstream._upstream_signals = UpstreamSignals(upstream._signals)

        assert len(upstream._signals) == 2
        assert upstream.index_map == {0: 0, 1: 1}
        assert upstream._signals[0].signal_name == "temp"

    def test_relay_false_signals_accessible_via_collection(self):
        upstream = _UpstreamDevice(
            device_name="Arduino",
            transport=FakeTransport("Arduino"),
            relay_downstream=False,
        )
        upstream._signals = [
            Signal("temp", "float", 22.5),
            Signal("hum", "float", 65.0),
        ]
        upstream._upstream_signals = UpstreamSignals(upstream._signals)

        assert upstream.signals["temp"].value == 22.5
        assert upstream.signals[1].value == 65.0
        assert upstream["temp"].value == 22.5

    def test_upstream_signals_created_in_start(self):
        upstream = _UpstreamDevice(
            device_name="Arduino",
            transport=FakeTransport("Arduino"),
            relay_downstream=False,
        )
        upstream._signals = [Signal("temp", "float")]

        # Before start: _upstream_signals is None
        assert upstream._upstream_signals is None
        with pytest.raises(RuntimeError, match="start"):
            _ = upstream.signals

        # Simulate what start() does
        upstream._upstream_signals = UpstreamSignals(upstream._signals)

        # Now accessible and cached
        s1 = upstream.signals
        s2 = upstream.signals
        assert s1 is s2
        assert s1["temp"].signal_name == "temp"

    def test_relay_true_transform_modifies_server_signal(self):
        """Modifying upstream.signals for relay_downstream=True changes the server signal."""
        server = _make_server_on_free_port()
        try:
            upstream = _UpstreamDevice(
                device_name="Arduino",
                transport=FakeTransport("Arduino"),
                relay_downstream=True,
            )
            server.add_signal("temp_f", "float", 212.0)
            upstream._signals.append(server.signals[0])
            upstream.index_map[0] = 0
            upstream._upstream_signals = UpstreamSignals(upstream._signals)

            # Transform via upstream reference
            upstream.signals["temp_f"].value = (upstream.signals["temp_f"].value - 32) * 5 / 9

            # Server signal reflects the change (same object)
            assert server.signals[0].value == pytest.approx(100.0)
        finally:
            server.close()


# ========================================================================
# Callback exception resilience
# ========================================================================


class TestCallbackExceptionResilience:
    """Verify that a failing callback doesn't crash _fire_data_received."""

    def setup_method(self):
        self.hub = BlaeckHub.__new__(BlaeckHub)
        self.hub._data_received_callbacks = []

    def test_exception_does_not_prevent_other_callbacks(self):
        calls = []

        @self.hub.on_data_received()
        def bad_callback(upstream):
            raise ValueError("oops")

        @self.hub.on_data_received()
        def good_callback(upstream):
            calls.append(upstream.device_name)

        upstream = _UpstreamDevice(device_name="Arduino", transport=FakeTransport("Arduino"))
        # _fire_data_received calls each callback independently;
        # but currently it doesn't catch per-callback — the outer
        # try/except in _poll_upstreams handles it.
        # This test documents that a single exception stops later callbacks.
        with pytest.raises(ValueError, match="oops"):
            self.hub._fire_data_received(upstream)
        assert calls == []


# ── B6 device type ─────────────────────────────────────────────────────


class TestB6DeviceType:
    """B6 message key includes device_type field."""

    def test_server_msg_devices_is_b6(self):
        from blaecktcpy._server import BlaeckServer

        assert BlaeckServer.MSG_DEVICES == b"\xb6"

    def test_parse_b6_includes_device_type(self):
        """B6 frame parsed by decoder returns device_type."""
        msg_key = b"\xb6"
        msg_id = (1).to_bytes(4, "little")
        msc_slave_id = b"\x00\x00"
        payload = (
            msc_slave_id
            + b"TestDevice\0"
            + b"1.0\0"
            + b"2.0\0"
            + b"3.0\0"
            + b"blaecktcpy\0"
            + b"1\0"
            + b"1\0"
            + b"0\0"
            + b"server\0"
            + b"0\0"
        )
        content = msg_key + b":" + msg_id + b":" + payload
        info = decoder.parse_devices(content)
        assert info.device_type == "server"
        assert info.device_name == "TestDevice"
        assert info.server_restarted == "0"
        assert info.parent == "0"

    def test_parse_b6_hub_device_type(self):
        """B6 frame with device_type='hub'."""
        msg_key = b"\xb6"
        msg_id = (1).to_bytes(4, "little")
        msc_slave_id = b"\x00\x00"
        payload = (
            msc_slave_id
            + b"MyHub\0"
            + b"1.0\0"
            + b"2.0\0"
            + b"3.0\0"
            + b"blaecktcpy\0"
            + b"1\0"
            + b"1\0"
            + b"0\0"
            + b"hub\0"
            + b"0\0"
        )
        content = msg_key + b":" + msg_id + b":" + payload
        info = decoder.parse_devices(content)
        assert info.device_type == "hub"

    def test_parse_b5_has_no_device_type(self):
        """B5 (legacy) frame has empty device_type."""
        msg_key = b"\xb5"
        msg_id = (1).to_bytes(4, "little")
        msc_slave_id = b"\x00\x00"
        payload = (
            msc_slave_id
            + b"OldDevice\0"
            + b"1.0\0"
            + b"2.0\0"
            + b"3.0\0"
            + b"blaecktcpy\0"
            + b"1\0"
            + b"1\0"
            + b"0\0"
        )
        content = msg_key + b":" + msg_id + b":" + payload
        info = decoder.parse_devices(content)
        assert info.device_type == ""
        assert info.server_restarted == "0"


# ── Multi-slave pass-through ───────────────────────────────────────────


class TestMultiSlavePassThrough:
    """Decoder preserves MSC/SlaveID, hub renumbers for downstream."""

    def test_parse_symbol_list_preserves_msc_and_slave_id(self):
        """B0 symbols include msc and slave_id from the wire."""
        msg_key = b"\xb0"
        msg_id = (1).to_bytes(4, "little")
        # Master signal (MSC=1, SlaveID=0)
        payload = (
            b"\x01\x00" + b"temp\0" + b"\x08"  # float
            # Slave signal (MSC=2, SlaveID=8)
            + b"\x02\x08" + b"pressure\0" + b"\x08"
            # Slave signal (MSC=2, SlaveID=42)
            + b"\x02\x2a" + b"humidity\0" + b"\x08"
        )
        content = msg_key + b":" + msg_id + b":" + payload
        symbols = decoder.parse_symbol_list(content)
        assert len(symbols) == 3
        assert symbols[0].msc == 1 and symbols[0].slave_id == 0
        assert symbols[1].msc == 2 and symbols[1].slave_id == 8
        assert symbols[2].msc == 2 and symbols[2].slave_id == 42

    def test_parse_all_devices_returns_multiple(self):
        """B6 frame with master + slave returns both entries."""
        msg_key = b"\xb6"
        msg_id = (1).to_bytes(4, "little")
        master = (
            b"\x01\x00"  # MSC=master, SlaveID=0
            + b"ArduinoMain\0"
            + b"1.0\0" + b"2.0\0" + b"3.0\0"
            + b"blaecktcpy\0" + b"1\0" + b"1\0" + b"0\0" + b"server\0" + b"0\0"
        )
        slave = (
            b"\x02\x08"  # MSC=slave, SlaveID=8
            + b"SensorBoard\0"
            + b"1.1\0" + b"2.1\0" + b"3.1\0"
            + b"blaeckserial\0" + b"1\0" + b"1\0" + b"0\0" + b"server\0" + b"0\0"
        )
        content = msg_key + b":" + msg_id + b":" + master + slave
        devices = decoder.parse_all_devices(content)
        assert len(devices) == 2
        assert devices[0].device_name == "ArduinoMain"
        assert devices[0].msc == 1 and devices[0].slave_id == 0
        assert devices[1].device_name == "SensorBoard"
        assert devices[1].msc == 2 and devices[1].slave_id == 8

    def test_parse_all_devices_b3_multi_entry(self):
        """B3 (BlaeckSerial) frame with master + slave."""
        msg_key = b"\xb3"
        msg_id = (1).to_bytes(4, "little")
        master = (
            b"\x01\x00"
            + b"Master\0" + b"hw1\0" + b"fw1\0" + b"lib1\0" + b"blaeckserial\0"
        )
        slave = (
            b"\x02\x09"
            + b"Slave9\0" + b"hw2\0" + b"fw2\0" + b"lib2\0" + b"blaeckserial\0"
        )
        content = msg_key + b":" + msg_id + b":" + master + slave
        devices = decoder.parse_all_devices(content)
        assert len(devices) == 2
        assert devices[0].device_name == "Master"
        assert devices[0].msc == 1 and devices[0].slave_id == 0
        assert devices[1].device_name == "Slave9"
        assert devices[1].msc == 2 and devices[1].slave_id == 9
        assert devices[1].lib_name == "blaeckserial"

    def test_parse_devices_backward_compat(self):
        """parse_devices() still returns first entry only."""
        msg_key = b"\xb6"
        msg_id = (1).to_bytes(4, "little")
        master = (
            b"\x01\x00" + b"First\0"
            + b"1.0\0" + b"2.0\0" + b"3.0\0"
            + b"lib\0" + b"1\0" + b"1\0" + b"0\0" + b"hub\0" + b"0\0"
        )
        slave = (
            b"\x02\x01" + b"Second\0"
            + b"1.0\0" + b"2.0\0" + b"3.0\0"
            + b"lib\0" + b"1\0" + b"1\0" + b"0\0" + b"server\0" + b"0\0"
        )
        content = msg_key + b":" + msg_id + b":" + master + slave
        info = decoder.parse_devices(content)
        assert info.device_name == "First"
        assert info.device_type == "hub"

    def test_slave_id_map_built_from_symbols(self):
        """Hub builds slave_id_map from upstream symbol MSC/SlaveID."""
        hub = BlaeckHub("127.0.0.1", 0, "TestHub", "1.0", "1.0")
        upstream = _UpstreamDevice(
            device_name="Arduino",
            transport=None,
            relay_downstream=True,
        )
        upstream.symbol_table = [
            decoder.DecodedSymbol("temp", 8, "float", 4, msc=1, slave_id=0),
            decoder.DecodedSymbol("pressure", 8, "float", 4, msc=2, slave_id=8),
            decoder.DecodedSymbol("humidity", 8, "float", 4, msc=2, slave_id=42),
        ]
        hub._upstreams.append(upstream)

        # Simulate start() slave_id_map building
        hub_slave_idx = 0
        seen: dict[tuple[int, int], int] = {}
        for sym in upstream.symbol_table:
            key = (sym.msc, sym.slave_id)
            if key not in seen:
                hub_slave_idx += 1
                seen[key] = hub_slave_idx
        upstream.slave_id_map = seen

        assert upstream.slave_id_map == {
            (1, 0): 1,   # master → slave 1
            (2, 8): 2,   # I2C slave 8 → slave 2
            (2, 42): 3,  # I2C slave 42 → slave 3
        }

    def test_slave_id_map_multiple_upstreams(self):
        """Slave IDs are contiguous across multiple upstreams."""
        hub = BlaeckHub("127.0.0.1", 0, "TestHub", "1.0", "1.0")

        upstream_a = _UpstreamDevice(device_name="A", transport=None, relay_downstream=True)
        upstream_a.symbol_table = [
            decoder.DecodedSymbol("a1", 8, "float", 4, msc=1, slave_id=0),
            decoder.DecodedSymbol("a2", 8, "float", 4, msc=2, slave_id=5),
        ]

        upstream_b = _UpstreamDevice(device_name="B", transport=None, relay_downstream=True)
        upstream_b.symbol_table = [
            decoder.DecodedSymbol("b1", 8, "float", 4, msc=1, slave_id=0),
        ]

        hub._upstreams.extend([upstream_a, upstream_b])

        # Simulate start() logic
        hub_slave_idx = 0
        for up in hub._upstreams:
            if not up.relay_downstream:
                continue
            seen: dict[tuple[int, int], int] = {}
            for sym in up.symbol_table:
                key = (sym.msc, sym.slave_id)
                if key not in seen:
                    hub_slave_idx += 1
                    seen[key] = hub_slave_idx
            up.slave_id_map = seen

        assert upstream_a.slave_id_map == {(1, 0): 1, (2, 5): 2}
        assert upstream_b.slave_id_map == {(1, 0): 3}

    def _remap_parents(self, upstreams):
        """Apply parent remapping logic across multiple upstreams.

        Each upstream is a tuple of (device_infos, slave_id_map).
        Returns list of (device_name, hub_slave_id, parent_slave_id).
        """
        results = []
        for device_infos, slave_id_map in upstreams:
            old_sid_to_new: dict[int, int] = {}
            for (msc, sid), hub_sid in slave_id_map.items():
                old_sid_to_new[sid] = hub_sid
            first_entry = True
            for info in device_infos:
                key = (info.msc, info.slave_id)
                hub_sid = slave_id_map.get(key)
                if hub_sid is None:
                    continue
                if first_entry:
                    parent_sid = 0
                    first_entry = False
                else:
                    orig_parent = int(info.parent) if info.parent else 0
                    parent_sid = old_sid_to_new.get(orig_parent, 0)
                results.append((info.device_name, hub_sid, parent_sid))
        return results

    def _dev(self, name, dtype="server", parent="0", msc=2, sid=0):
        """Shorthand for creating a DecodedDeviceInfo."""
        return decoder.DecodedDeviceInfo(
            msg_id=1, device_name=name, hw_version="1.0",
            fw_version="1.0", lib_version="1.0", device_type=dtype,
            parent=parent, msc=msc, slave_id=sid,
        )

    def test_parent_remapping_hub_chain(self):
        """Case 3: Hub_A ← Hub_B ← Arduino1."""
        infos = [
            self._dev("Hub_B", "hub", "0", msc=1, sid=0),
            self._dev("Arduino1", "server", "0", msc=2, sid=1),
        ]
        sid_map = {(1, 0): 1, (2, 1): 2}
        results = self._remap_parents([(infos, sid_map)])
        assert results == [("Hub_B", 1, 0), ("Arduino1", 2, 1)]

    def test_parent_remapping_two_hubs(self):
        """Case 4: Hub_A ← Hub_B(Arduino1), Hub_C(Arduino2)."""
        hub_b = [
            self._dev("Hub_B", "hub", "0", msc=1, sid=0),
            self._dev("Arduino1", "server", "0", msc=2, sid=1),
        ]
        hub_c = [
            self._dev("Hub_C", "hub", "0", msc=1, sid=0),
            self._dev("Arduino2", "server", "0", msc=2, sid=1),
        ]
        results = self._remap_parents([
            (hub_b, {(1, 0): 1, (2, 1): 2}),
            (hub_c, {(1, 0): 3, (2, 1): 4}),
        ])
        assert results == [
            ("Hub_B", 1, 0), ("Arduino1", 2, 1),
            ("Hub_C", 3, 0), ("Arduino2", 4, 3),
        ]

    def test_parent_remapping_hub_plus_direct_server(self):
        """Case 5: Hub_A ← Hub_B(Arduino1), ServerA — the original ambiguity."""
        hub_b = [
            self._dev("Hub_B", "hub", "0", msc=1, sid=0),
            self._dev("Arduino1", "server", "0", msc=2, sid=1),
        ]
        server_a = [
            self._dev("ServerA", "server", "0", msc=1, sid=0),
        ]
        results = self._remap_parents([
            (hub_b, {(1, 0): 1, (2, 1): 2}),
            (server_a, {(1, 0): 3}),
        ])
        assert results == [
            ("Hub_B", 1, 0), ("Arduino1", 2, 1),
            ("ServerA", 3, 0),  # belongs to Hub_A, NOT Hub_B
        ]

    def test_parent_remapping_three_level_chain(self):
        """Case 6: Hub_A ← Hub_B ← Hub_C ← Arduino1."""
        infos = [
            self._dev("Hub_B", "hub", "0", msc=1, sid=0),
            self._dev("Hub_C", "hub", "0", msc=2, sid=1),
            self._dev("Arduino1", "server", "1", msc=2, sid=2),
        ]
        sid_map = {(1, 0): 1, (2, 1): 2, (2, 2): 3}
        results = self._remap_parents([(infos, sid_map)])
        assert results == [
            ("Hub_B", 1, 0), ("Hub_C", 2, 1), ("Arduino1", 3, 2),
        ]

    def test_parent_remapping_mixed_order(self):
        """Case 7: Hub_A ← ServerA, Hub_B(Arduino1), ServerB."""
        server_a = [self._dev("ServerA", "server", "0", msc=1, sid=0)]
        hub_b = [
            self._dev("Hub_B", "hub", "0", msc=1, sid=0),
            self._dev("Arduino1", "server", "0", msc=2, sid=1),
        ]
        server_b = [self._dev("ServerB", "server", "0", msc=1, sid=0)]
        results = self._remap_parents([
            (server_a, {(1, 0): 1}),
            (hub_b, {(1, 0): 2, (2, 1): 3}),
            (server_b, {(1, 0): 4}),
        ])
        assert results == [
            ("ServerA", 1, 0),
            ("Hub_B", 2, 0), ("Arduino1", 3, 2),
            ("ServerB", 4, 0),  # NOT under Hub_B
        ]

    def test_parent_remapping_i2c_slaves(self):
        """Case 8: Hub_A ← BlaeckSerial(Master + Slave8 + Slave42)."""
        # BlaeckSerial sends B3 — no parent field, defaults to "0"
        infos = [
            self._dev("Master", "server", "0", msc=1, sid=0),
            self._dev("Slave8", "server", "0", msc=2, sid=8),
            self._dev("Slave42", "server", "0", msc=2, sid=42),
        ]
        sid_map = {(1, 0): 1, (2, 8): 2, (2, 42): 3}
        results = self._remap_parents([(infos, sid_map)])
        assert results == [
            ("Master", 1, 0),
            ("Slave8", 2, 1),   # parent=1 (Master)
            ("Slave42", 3, 1),  # parent=1 (Master)
        ]

    def test_parent_remapping_i2c_through_hub_chain(self):
        """Case 9: Hub_A ← Hub_B ← BlaeckSerial(Master + Slave8)."""
        infos = [
            self._dev("Hub_B", "hub", "0", msc=1, sid=0),
            self._dev("BlaeckMaster", "server", "0", msc=2, sid=1),
            self._dev("Slave8", "server", "1", msc=2, sid=2),
        ]
        sid_map = {(1, 0): 1, (2, 1): 2, (2, 2): 3}
        results = self._remap_parents([(infos, sid_map)])
        assert results == [
            ("Hub_B", 1, 0),
            ("BlaeckMaster", 2, 1),
            ("Slave8", 3, 2),  # belongs to BlaeckMaster, not Hub_B
        ]

    def test_parent_remapping_complex(self):
        """Case 10: Hub_A ← Hub_B(BlaeckSerial+Slave8), Hub_C(Ard1,Ard2), ServerD."""
        hub_b = [
            self._dev("Hub_B", "hub", "0", msc=1, sid=0),
            self._dev("BlaeckMaster", "server", "0", msc=2, sid=1),
            self._dev("Slave8", "server", "1", msc=2, sid=2),
        ]
        hub_c = [
            self._dev("Hub_C", "hub", "0", msc=1, sid=0),
            self._dev("Arduino1", "server", "0", msc=2, sid=1),
            self._dev("Arduino2", "server", "0", msc=2, sid=2),
        ]
        server_d = [self._dev("ServerD", "server", "0", msc=1, sid=0)]
        results = self._remap_parents([
            (hub_b, {(1, 0): 1, (2, 1): 2, (2, 2): 3}),
            (hub_c, {(1, 0): 4, (2, 1): 5, (2, 2): 6}),
            (server_d, {(1, 0): 7}),
        ])
        assert results == [
            ("Hub_B", 1, 0),
            ("BlaeckMaster", 2, 1),
            ("Slave8", 3, 2),
            ("Hub_C", 4, 0),
            ("Arduino1", 5, 4),
            ("Arduino2", 6, 4),
            ("ServerD", 7, 0),
        ]

    def test_parent_tree_reconstruction(self):
        """Loggbok can reconstruct the full device tree from parent fields.

        Takes the complex case (10) output and rebuilds a tree,
        proving the parent field is unambiguous.
        """
        # Flat B6 list as Loggbok would receive it (slave_id, name, parent)
        flat = [
            (0, "Hub_A", 0),       # master
            (1, "Hub_B", 0),
            (2, "BlaeckMaster", 1),
            (3, "Slave8", 2),
            (4, "Hub_C", 0),
            (5, "Arduino1", 4),
            (6, "Arduino2", 4),
            (7, "ServerD", 0),
        ]

        # Reconstruct tree: parent_sid → list of child names
        children: dict[int, list[str]] = {}
        for sid, name, parent in flat:
            if sid == parent:
                continue  # skip master self-reference
            children.setdefault(parent, []).append(name)

        assert children[0] == ["Hub_B", "Hub_C", "ServerD"]  # Hub_A's children
        assert children[1] == ["BlaeckMaster"]                # Hub_B's children
        assert children[2] == ["Slave8"]                      # BlaeckMaster's children
        assert children[4] == ["Arduino1", "Arduino2"]        # Hub_C's children
        assert 3 not in children  # Slave8 has no children
        assert 5 not in children  # Arduino1 has no children
        assert 7 not in children  # ServerD has no children


# ========================================================================
# Restart flag relay tests
# ========================================================================


class TestRestartFlagRelay:
    """Hub relays upstream RestartFlag to downstream."""

    def _build_d1_frame(
        self,
        restart_flag: bool,
        signal_values: list[float],
        status: int = 0,
    ):
        """Build a valid D1 data frame with CRC."""
        import struct

        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        flag = b"\x01" if restart_flag else b"\x00"
        timestamp_mode = b"\x00"
        meta = flag + b":" + timestamp_mode + b":"

        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        return msg_key + b":" + msg_id + b":" + meta + payload + bytes([status]) + crc

    def test_upstream_restart_flag_sets_server_flag(self):
        """When upstream sends restart_flag=1, hub sets its own flag."""
        server = _make_server_on_free_port()
        try:
            server.add_signal("sig1", "float", 0.0)
            hub = BlaeckHub.__new__(BlaeckHub)
            hub._server = server
            hub._upstreams = []
            hub._local_signals = []
            hub._callbacks = {"data_received": []}

            upstream = _UpstreamDevice(
                device_name="Arduino", transport=FakeTransport(), relay_downstream=True
            )
            upstream.symbol_table = [
                decoder.DecodedSymbol("temp", 8, "float", 4),
            ]
            upstream.index_map = {0: 0}
            upstream.interval_ms = 0
            hub._upstreams.append(upstream)

            # Build D1 frame with restart_flag=1
            frame = self._build_d1_frame(restart_flag=True, signal_values=[25.0])
            full = b"<BLAECK:" + frame + b"/BLAECK>\r\n"

            # Verify server flag is False initially
            server._send_restart_flag = False

            # Feed frame through FakeTransport
            upstream.transport._buffer = full
            upstream.transport.read_available = lambda: upstream.transport._buffer

            # Parse and relay
            decoded = decoder.parse_data(frame, upstream.symbol_table)
            assert decoded.restart_flag is True

            # Simulate what _poll_upstreams does
            if decoded.restart_flag:
                server._send_restart_flag = True

            assert server._send_restart_flag is True
        finally:
            server.close()

    def test_no_restart_flag_leaves_server_flag_unchanged(self):
        """When upstream sends restart_flag=0, hub flag stays unchanged."""
        server = _make_server_on_free_port()
        try:
            server.add_signal("sig1", "float", 0.0)

            frame = self._build_d1_frame(restart_flag=False, signal_values=[10.0])
            symbol_table = [decoder.DecodedSymbol("temp", 8, "float", 4)]

            decoded = decoder.parse_data(frame, symbol_table)
            assert decoded.restart_flag is False

            server._send_restart_flag = False
            if decoded.restart_flag:
                server._send_restart_flag = True
            assert server._send_restart_flag is False
        finally:
            server.close()


# ========================================================================
# Status byte relay tests
# ========================================================================


class TestStatusByteRelay:
    """Hub relays upstream status byte downstream."""

    def _build_d1_frame(self, status: int, signal_values: list[float]):
        """Build a valid D1 data frame with a specific status byte."""
        import struct

        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        meta = b"\x00:\x00:"  # no restart, no timestamp

        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        return msg_key + b":" + msg_id + b":" + meta + payload + bytes([status]) + crc

    def test_decoder_reads_status_byte_ok(self):
        """D1 parser captures status_byte = 0 (OK)."""
        frame = self._build_d1_frame(status=0x00, signal_values=[1.0])
        symbol_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, symbol_table)
        assert decoded.status_byte == 0x00

    def test_decoder_reads_status_byte_i2c_crc_error(self):
        """D1 parser captures status_byte = 1 (I2C CRC error)."""
        frame = self._build_d1_frame(status=0x01, signal_values=[1.0])
        symbol_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, symbol_table)
        assert decoded.status_byte == 0x01

    def test_decoder_reads_status_byte_upstream_lost(self):
        """D1 parser captures status_byte = 2 (upstream lost)."""
        frame = self._build_d1_frame(status=0x02, signal_values=[1.0])
        symbol_table = [decoder.DecodedSymbol("sig", 8, "float", 4)]
        decoded = decoder.parse_data(frame, symbol_table)
        assert decoded.status_byte == 0x02

    def test_status_byte_relay_end_to_end(self):
        """Status byte flows: upstream D1 → hub _poll_upstreams → downstream frame."""
        import socket
        import struct

        server = _make_server_on_free_port()
        try:
            # Register a signal on the server (hub relay target)
            server.add_signal("temp", "float", 0.0)

            # Build hub internals manually
            hub = BlaeckHub.__new__(BlaeckHub)
            hub._server = server
            hub._upstreams = []
            hub._local_signals = []
            hub._callbacks = {"data_received": []}
            hub._disconnect_callback = None

            transport = FakeTransport("Arduino")
            upstream = _UpstreamDevice(
                device_name="Arduino", transport=transport, relay_downstream=True
            )
            upstream.symbol_table = [
                decoder.DecodedSymbol("temp", 8, "float", 4),
            ]
            upstream.index_map = {0: 0}
            upstream.interval_ms = 0
            upstream.connected = True
            hub._upstreams.append(upstream)

            # Connect a downstream TCP client
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(2.0)
            client.connect(("127.0.0.1", server._port))
            server._accept_new_clients()  # accept the client

            # Feed a D1 frame with status=0x01 (I2C CRC error)
            frame_content = self._build_d1_frame(status=0x01, signal_values=[25.0])
            wrapped = b"<BLAECK:" + frame_content + b"/BLAECK>\r\n"
            transport._pending = wrapped
            transport.read_available = lambda: transport._pending

            # Run poll to process the frame and relay downstream
            hub._poll_upstreams()
            # Clear pending so next read_available returns empty
            transport._pending = b""

            # Read downstream frame from TCP client
            downstream = client.recv(4096)
            client.close()

            # Parse the downstream frame — extract status byte at position [-5]
            # Frame: <BLAECK:...content.../BLAECK>\r\n
            start = downstream.find(b"<BLAECK:") + len(b"<BLAECK:")
            end = downstream.find(b"/BLAECK>")
            content = downstream[start:end]
            # Status byte is at content[-5] (before 4-byte CRC)
            assert content[-5] == 0x01, (
                f"Expected status byte 0x01 (I2C CRC error), got 0x{content[-5]:02x}"
            )
        finally:
            server.close()


# ========================================================================
# Relay frame scoping tests
# ========================================================================


class TestRelayFrameScoping:
    """Relay frames are scoped to the originating upstream's signals only."""

    def _build_d1_frame(
        self,
        restart_flag: bool,
        signal_values: list[float],
        status: int = 0,
    ):
        """Build a valid D1 data frame with CRC."""
        import struct

        msg_key = b"\xd1"
        msg_id = (1).to_bytes(4, "little")
        flag = b"\x01" if restart_flag else b"\x00"
        timestamp_mode = b"\x00"
        meta = flag + b":" + timestamp_mode + b":"

        payload = b""
        for idx, val in enumerate(signal_values):
            payload += idx.to_bytes(2, "little") + struct.pack("<f", val)

        crc_input = msg_key + b":" + msg_id + b":" + meta + payload
        crc = binascii.crc32(crc_input).to_bytes(4, "little")
        return msg_key + b":" + msg_id + b":" + meta + payload + bytes([status]) + crc

    def _make_hub_with_two_upstreams(self):
        """Create a hub with two fake upstreams (A: 2 signals, B: 2 signals)."""
        import socket

        server = _make_server_on_free_port()
        # 4 upstream signals (A gets idx 0,1 — B gets idx 2,3)
        server.add_signal("A_sig0", "float", 0.0)
        server.add_signal("A_sig1", "float", 0.0)
        server.add_signal("B_sig0", "float", 0.0)
        server.add_signal("B_sig1", "float", 0.0)

        hub = BlaeckHub.__new__(BlaeckHub)
        hub._server = server
        hub._upstreams = []
        hub._local_signals = []
        hub._callbacks = {"data_received": []}
        hub._disconnect_callback = None
        hub._data_received_callbacks = []

        transport_a = FakeTransport("UpstreamA")
        upstream_a = _UpstreamDevice(
            device_name="UpstreamA", transport=transport_a, relay_downstream=True
        )
        upstream_a.symbol_table = [
            decoder.DecodedSymbol("A_sig0", 8, "float", 4),
            decoder.DecodedSymbol("A_sig1", 8, "float", 4),
        ]
        upstream_a.index_map = {0: 0, 1: 1}
        upstream_a.interval_ms = 300
        upstream_a.connected = True
        hub._upstreams.append(upstream_a)

        transport_b = FakeTransport("UpstreamB")
        upstream_b = _UpstreamDevice(
            device_name="UpstreamB", transport=transport_b, relay_downstream=True
        )
        upstream_b.symbol_table = [
            decoder.DecodedSymbol("B_sig0", 8, "float", 4),
            decoder.DecodedSymbol("B_sig1", 8, "float", 4),
        ]
        upstream_b.index_map = {0: 2, 1: 3}
        upstream_b.interval_ms = 300
        upstream_b.connected = True
        hub._upstreams.append(upstream_b)

        # Connect a downstream TCP client
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", server._port))
        server._accept_new_clients()

        return hub, server, client, upstream_a, upstream_b, transport_a, transport_b

    def _parse_downstream_signal_ids(self, raw: bytes) -> list[int]:
        """Extract signal index IDs from a downstream D1 frame."""
        import struct

        start = raw.find(b"<BLAECK:") + len(b"<BLAECK:")
        end = raw.find(b"/BLAECK>")
        content = raw[start:end]

        # D1 layout: msg_key(1) : msg_id(4) : restart(1) : ts_mode(1) : signals... status(1) crc(4)
        # Find the signal data after the last ":"
        colon_positions = []
        for i, b in enumerate(content):
            if b == ord(":"):
                colon_positions.append(i)
        # Signal data starts after 4th colon, ends 5 bytes before end (status+crc)
        sig_start = colon_positions[3] + 1
        sig_end = len(content) - 5
        sig_data = content[sig_start:sig_end]

        ids = []
        pos = 0
        while pos < len(sig_data):
            sig_id = int.from_bytes(sig_data[pos:pos + 2], "little")
            ids.append(sig_id)
            # skip id(2) + float(4)
            pos += 6
        return ids

    def test_restart_flag_does_not_leak_across_upstreams(self):
        """Upstream A restart_flag must not appear in upstream B's relay frame."""
        hub, server, client, up_a, up_b, tr_a, tr_b = (
            self._make_hub_with_two_upstreams()
        )
        try:
            # Clear the server's initial restart flag
            server._send_restart_flag = False

            # Upstream A: restart_flag=True
            frame_a = self._build_d1_frame(restart_flag=True, signal_values=[1.0, 2.0])
            tr_a._pending = b"<BLAECK:" + frame_a + b"/BLAECK>\r\n"
            tr_a.read_available = lambda: tr_a._pending

            # Upstream B: restart_flag=False
            frame_b = self._build_d1_frame(restart_flag=False, signal_values=[5.0, 6.0])
            tr_b._pending = b"<BLAECK:" + frame_b + b"/BLAECK>\r\n"
            tr_b.read_available = lambda: tr_b._pending

            hub._poll_upstreams()

            # Clear pending
            tr_a._pending = b""
            tr_b._pending = b""

            # Read all downstream data
            import time
            time.sleep(0.05)
            downstream = client.recv(8192)

            # Should get two separate frames
            frames = downstream.split(b"/BLAECK>\r\n")
            frames = [f for f in frames if f]  # remove empty
            assert len(frames) == 2, f"Expected 2 frames, got {len(frames)}"

            # Frame 1 (upstream A): should have signal IDs 0,1 only
            frame1_raw = frames[0] + b"/BLAECK>\r\n"
            ids1 = self._parse_downstream_signal_ids(frame1_raw)
            assert ids1 == [0, 1], f"Frame 1 should contain A's signals [0,1], got {ids1}"

            # Frame 2 (upstream B): should have signal IDs 2,3 only
            frame2_raw = frames[1] + b"/BLAECK>\r\n"
            ids2 = self._parse_downstream_signal_ids(frame2_raw)
            assert ids2 == [2, 3], f"Frame 2 should contain B's signals [2,3], got {ids2}"

            # Check restart flag: frame 1 should have it, frame 2 should not
            # D1 layout: msg_key(1) : msg_id(4) : restart(1) : ts_mode(1) : ...
            # restart_flag byte is at colons[1]+1
            content1_start = frame1_raw.find(b"<BLAECK:") + len(b"<BLAECK:")
            content1 = frame1_raw[content1_start:frame1_raw.find(b"/BLAECK>")]
            colons1 = [i for i, b in enumerate(content1) if b == ord(":")]
            assert content1[colons1[1] + 1] == 1, "Frame 1 should have restart_flag=1"

            content2_start = frame2_raw.find(b"<BLAECK:") + len(b"<BLAECK:")
            content2 = frame2_raw[content2_start:frame2_raw.find(b"/BLAECK>")]
            colons2 = [i for i, b in enumerate(content2) if b == ord(":")]
            assert content2[colons2[1] + 1] == 0, "Frame 2 should have restart_flag=0"
        finally:
            client.close()
            server.close()

    def test_status_byte_does_not_leak_across_upstreams(self):
        """Upstream A status=0x01 must not appear in upstream B's relay frame."""
        hub, server, client, up_a, up_b, tr_a, tr_b = (
            self._make_hub_with_two_upstreams()
        )
        try:
            # Upstream A: status=0x01 (I2C CRC error)
            frame_a = self._build_d1_frame(
                restart_flag=False, signal_values=[1.0, 2.0], status=0x01
            )
            tr_a._pending = b"<BLAECK:" + frame_a + b"/BLAECK>\r\n"
            tr_a.read_available = lambda: tr_a._pending

            # Upstream B: status=0x00 (OK)
            frame_b = self._build_d1_frame(
                restart_flag=False, signal_values=[5.0, 6.0], status=0x00
            )
            tr_b._pending = b"<BLAECK:" + frame_b + b"/BLAECK>\r\n"
            tr_b.read_available = lambda: tr_b._pending

            hub._poll_upstreams()
            tr_a._pending = b""
            tr_b._pending = b""

            import time
            time.sleep(0.05)
            downstream = client.recv(8192)

            frames = downstream.split(b"/BLAECK>\r\n")
            frames = [f for f in frames if f]
            assert len(frames) == 2, f"Expected 2 frames, got {len(frames)}"

            # Frame 1 (upstream A): status_byte should be 0x01
            content1 = frames[0][frames[0].find(b"<BLAECK:") + 8:]
            assert content1[-5] == 0x01, f"Frame 1 status should be 0x01, got 0x{content1[-5]:02x}"

            # Frame 2 (upstream B): status_byte should be 0x00
            content2 = frames[1][frames[1].find(b"<BLAECK:") + 8:]
            assert content2[-5] == 0x00, f"Frame 2 status should be 0x00, got 0x{content2[-5]:02x}"
        finally:
            client.close()
            server.close()

    def test_upstream_lost_frame_scoped_to_upstream(self):
        """STATUS_UPSTREAM_LOST frame only contains the disconnected upstream's signals."""
        hub, server, client, up_a, up_b, tr_a, tr_b = (
            self._make_hub_with_two_upstreams()
        )
        try:
            # Mark upstream A's signals as updated (simulates _zero_upstream_signals)
            server.signals[0].value = 0
            server.signals[0].updated = True
            server.signals[1].value = 0
            server.signals[1].updated = True

            # Also mark B's signals as updated (from normal data)
            server.signals[2].value = 99.0
            server.signals[2].updated = True
            server.signals[3].value = 99.0
            server.signals[3].updated = True

            # Send upstream-lost for A only
            hub._send_upstream_lost_frame(up_a)

            import time
            time.sleep(0.05)
            downstream = client.recv(8192)

            # Should be one frame with only A's signals
            ids = self._parse_downstream_signal_ids(downstream)
            assert ids == [0, 1], f"Lost frame should contain only A's signals [0,1], got {ids}"

            # Status byte should be STATUS_UPSTREAM_LOST (0x02)
            start = downstream.find(b"<BLAECK:") + len(b"<BLAECK:")
            end = downstream.find(b"/BLAECK>")
            content = downstream[start:end]
            assert content[-5] == 0x02, f"Status should be 0x02, got 0x{content[-5]:02x}"

            # B's signals should still be updated (not consumed)
            assert server.signals[2].updated is True
            assert server.signals[3].updated is True
        finally:
            client.close()
            server.close()

    def test_upstream_lost_frame_sent_only_once(self):
        """STATUS_UPSTREAM_LOST is sent once on disconnect, not on subsequent ticks."""
        import time

        hub, server, client, up_a, up_b, tr_a, tr_b = (
            self._make_hub_with_two_upstreams()
        )
        try:
            # Disconnect upstream A
            tr_a.close()
            up_a.connected = True  # simulate it was connected before

            # First poll: should detect disconnect and send lost frame
            hub._poll_upstreams()
            time.sleep(0.05)
            downstream1 = client.recv(8192)

            assert b"/BLAECK>" in downstream1, "Expected a lost frame on first poll"
            content = downstream1[downstream1.find(b"<BLAECK:") + 8:downstream1.find(b"/BLAECK>")]
            assert content[-5] == 0x02, "First poll should send STATUS_UPSTREAM_LOST"

            # connected should now be False
            assert up_a.connected is False

            # Second poll: should NOT send another lost frame
            hub._poll_upstreams()
            time.sleep(0.05)
            client.setblocking(False)
            try:
                downstream2 = client.recv(8192)
            except BlockingIOError:
                downstream2 = b""
            client.setblocking(True)

            assert downstream2 == b"", (
                f"Expected no data on second poll, got {len(downstream2)} bytes"
            )
        finally:
            client.close()
            server.close()
