"""Tests for features of the unified BlaeckTCPy class.

Covers:
- STATUS_OK / STATUS_UPSTREAM_LOST in _build_data_msg
- SignalList collection (index, name, len, iter, errors)
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

from blaecktcpy import Signal, SignalList, STATUS_OK, STATUS_UPSTREAM_LOST, IntervalMode, TimestampMode, BlaeckTCPy
from blaecktcpy._server import _UpstreamDevice, _MSG_ID_HUB, _IntervalTimer
from blaecktcpy.hub._upstream import _UpstreamBase
from blaecktcpy.hub import _decoder as decoder


# ========================================================================
# Helpers
# ========================================================================


def _make_server_on_free_port():
    """Create an unstarted BlaeckTCPy that will bind to a free port on start()."""
    return BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")


def _start_retry(device, attempts=1):
    """Start device; after start, update _port from the actual bound address."""
    device.start()
    # Port 0 means OS picks a free port — read the actual port after bind
    device._port = device._server_socket.getsockname()[1]


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
        _start_retry(self.server)

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
# SignalList tests
# ========================================================================


class TestSignalList:
    """Verify SignalList collection access patterns."""

    def setup_method(self):
        self.signals = [
            Signal("temperature", "float", 22.5),
            Signal("humidity", "float", 65.0),
            Signal("pressure", "float", 1013.25),
        ]
        self.collection = SignalList(self.signals)

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
        empty = SignalList([])
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
        self.device = BlaeckTCPy.__new__(BlaeckTCPy)
        self.device._upstreams = []
        self.device._started = False
        self.device._upstream_disconnect_callback = None
        self.device._connect_callback = None
        self.device._disconnect_callback = None
        self.device._data_received_callbacks = []
        self.device._command_handlers = {}
        self.device._read_callback = None

    def test_on_upstream_disconnected_registers(self):
        @self.device.on_upstream_disconnected()
        def handler(name):
            pass

        assert self.device._upstream_disconnect_callback is handler

    def test_on_client_connected_registers(self):
        @self.device.on_client_connected()
        def handler(cid):
            pass

        assert self.device._connect_callback is handler

    def test_on_client_disconnected_registers(self):
        @self.device.on_client_disconnected()
        def handler(cid):
            pass

        assert self.device._disconnect_callback is handler

    def test_on_data_received_registers_with_name(self):
        @self.device.on_data_received("Arduino")
        def handler(upstream):
            pass

        assert len(self.device._data_received_callbacks) == 1
        name, func = self.device._data_received_callbacks[0]
        assert name == "Arduino"
        assert func is handler

    def test_on_data_received_registers_without_name(self):
        @self.device.on_data_received()
        def handler(upstream):
            pass

        name, func = self.device._data_received_callbacks[0]
        assert name is None

    def test_multiple_data_received_callbacks(self):
        @self.device.on_data_received("Arduino")
        def h1(u):
            pass

        @self.device.on_data_received("ESP32")
        def h2(u):
            pass

        @self.device.on_data_received()
        def h3(u):
            pass

        assert len(self.device._data_received_callbacks) == 3

    def test_on_command_specific_registers(self):
        @self.device.on_command("SET_LED")
        def handler(state):
            pass

        assert "SET_LED" in self.device._command_handlers
        assert self.device._command_handlers["SET_LED"] is handler

    def test_on_command_catchall_registers(self):
        @self.device.on_command()
        def handler(command, *params):
            pass

        assert self.device._read_callback is handler

    def test_on_command_multiple_specific(self):
        @self.device.on_command("SET_LED")
        def h1(state):
            pass

        @self.device.on_command("SET_MODE")
        def h2(mode):
            pass

        assert len(self.device._command_handlers) == 2


class TestCommandHandlerDispatch:
    """Verify on_command handlers are actually called when commands arrive."""

    def _make_device(self):
        import socket

        device = _make_server_on_free_port()
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client

    def test_specific_command_handler_fires(self):
        device, client = self._make_device()
        received = []

        @device.on_command("SET_LED")
        def handler(*params):
            received.append(params)

        try:
            import time

            client.sendall(b"<SET_LED,1>")
            time.sleep(0.05)
            device.read()
            assert len(received) == 1
            assert received[0] == ("1",)
        finally:
            client.close()
            device.close()

    def test_catchall_handler_fires(self):
        device, client = self._make_device()
        received = []

        @device.on_command()
        def handler(command, *params):
            received.append((command, params))

        try:
            import time

            client.sendall(b"<MY_CMD,42>")
            time.sleep(0.05)
            device.read()
            assert len(received) == 1
            assert received[0] == ("MY_CMD", ("42",))
        finally:
            client.close()
            device.close()

    def test_both_specific_and_catchall_fire(self):
        device, client = self._make_device()
        specific = []
        catchall = []

        @device.on_command("SET_LED")
        def h1(*params):
            specific.append(params)

        @device.on_command()
        def h2(command, *params):
            catchall.append((command, params))

        try:
            import time

            client.sendall(b"<SET_LED,1>")
            time.sleep(0.05)
            device.read()
            assert len(specific) == 1
            assert len(catchall) == 1
        finally:
            client.close()
            device.close()


class TestFireDataReceived:
    """Verify _fire_data_received dispatches to correct callbacks."""

    def setup_method(self):
        self.device = BlaeckTCPy.__new__(BlaeckTCPy)
        self.device._data_received_callbacks = []

    def _make_upstream(self, name):
        transport = FakeTransport(name)
        return _UpstreamDevice(device_name=name, transport=transport)

    def test_fires_matching_name(self):
        calls = []

        @self.device.on_data_received("Arduino")
        def handler(upstream):
            calls.append(upstream.device_name)

        arduino = self._make_upstream("Arduino")
        self.device._fire_data_received(arduino)
        assert calls == ["Arduino"]

    def test_skips_non_matching_name(self):
        calls = []

        @self.device.on_data_received("Arduino")
        def handler(upstream):
            calls.append(upstream.device_name)

        esp = self._make_upstream("ESP32")
        self.device._fire_data_received(esp)
        assert calls == []

    def test_global_callback_fires_for_any(self):
        calls = []

        @self.device.on_data_received()
        def handler(upstream):
            calls.append(upstream.device_name)

        self.device._fire_data_received(self._make_upstream("Arduino"))
        self.device._fire_data_received(self._make_upstream("ESP32"))
        assert calls == ["Arduino", "ESP32"]

    def test_mixed_callbacks(self):
        specific_calls = []
        global_calls = []

        @self.device.on_data_received("Arduino")
        def specific(upstream):
            specific_calls.append(upstream.device_name)

        @self.device.on_data_received()
        def global_handler(upstream):
            global_calls.append(upstream.device_name)

        self.device._fire_data_received(self._make_upstream("Arduino"))
        self.device._fire_data_received(self._make_upstream("ESP32"))

        assert specific_calls == ["Arduino"]
        assert global_calls == ["Arduino", "ESP32"]


# ========================================================================
# relay_downstream=False tests
# ========================================================================


class TestRelayFalseRegistration:
    """Verify relay_downstream=False signals go to internal storage, not device.signals."""

    def test_relay_true_registers_on_server(self):
        device = _make_server_on_free_port()
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

            offset = len(device.signals)
            for i, sym in enumerate(upstream.symbol_table):
                sig_type = decoder.DTYPE_TO_SIGNAL_TYPE.get(sym.datatype_code, "float")
                device.add_signal(sym.name, sig_type)
                upstream._signals.append(device.signals[offset])
                upstream.index_map[i] = offset
                offset += 1
            upstream._upstream_signals = SignalList(upstream._signals)

            assert len(device.signals) == 2
            assert upstream.index_map == {0: 0, 1: 1}
            assert device.signals[0].signal_name == "temp"
            # relay_downstream=True: upstream._signals references device signals
            assert upstream._signals[0] is device.signals[0]
            assert upstream.signals["temp"] is device.signals[0]
        finally:
            device.close()

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
        upstream._upstream_signals = SignalList(upstream._signals)

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
        upstream._upstream_signals = SignalList(upstream._signals)

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
        upstream._upstream_signals = SignalList(upstream._signals)

        # Now accessible and cached
        s1 = upstream.signals
        s2 = upstream.signals
        assert s1 is s2
        assert s1["temp"].signal_name == "temp"

    def test_relay_true_transform_modifies_server_signal(self):
        """Modifying upstream.signals for relay_downstream=True changes the device signal."""
        device = _make_server_on_free_port()
        try:
            upstream = _UpstreamDevice(
                device_name="Arduino",
                transport=FakeTransport("Arduino"),
                relay_downstream=True,
            )
            device.add_signal("temp_f", "float", 212.0)
            upstream._signals.append(device.signals[0])
            upstream.index_map[0] = 0
            upstream._upstream_signals = SignalList(upstream._signals)

            # Transform via upstream reference
            upstream.signals["temp_f"].value = (upstream.signals["temp_f"].value - 32) * 5 / 9

            # Device signal reflects the change (same object)
            assert device.signals[0].value == pytest.approx(100.0)
        finally:
            device.close()


# ========================================================================
# Callback exception resilience
# ========================================================================


class TestCallbackExceptionResilience:
    """Verify that a failing callback doesn't crash _fire_data_received."""

    def setup_method(self):
        self.device = BlaeckTCPy.__new__(BlaeckTCPy)
        self.device._data_received_callbacks = []

    def test_exception_does_not_prevent_other_callbacks(self):
        calls = []

        @self.device.on_data_received()
        def bad_callback(upstream):
            raise ValueError("oops")

        @self.device.on_data_received()
        def good_callback(upstream):
            calls.append(upstream.device_name)

        upstream = _UpstreamDevice(device_name="Arduino", transport=FakeTransport("Arduino"))
        # _fire_data_received calls each callback independently;
        # but currently it doesn't catch per-callback — the outer
        # try/except in _poll_upstreams handles it.
        # This test documents that a single exception stops later callbacks.
        with pytest.raises(ValueError, match="oops"):
            self.device._fire_data_received(upstream)
        assert calls == []


# ── B6 device type ─────────────────────────────────────────────────────


class TestB6DeviceType:
    """B6 message key includes device_type field."""

    def test_server_msg_devices_is_b6(self):
        assert BlaeckTCPy.MSG_DEVICES == b"\xb6"

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
        device = BlaeckTCPy("127.0.0.1", 0, "TestHub", "1.0", "1.0")
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
        device._upstreams.append(upstream)

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
        device = BlaeckTCPy("127.0.0.1", 0, "TestHub", "1.0", "1.0")

        upstream_a = _UpstreamDevice(device_name="A", transport=None, relay_downstream=True)
        upstream_a.symbol_table = [
            decoder.DecodedSymbol("a1", 8, "float", 4, msc=1, slave_id=0),
            decoder.DecodedSymbol("a2", 8, "float", 4, msc=2, slave_id=5),
        ]

        upstream_b = _UpstreamDevice(device_name="B", transport=None, relay_downstream=True)
        upstream_b.symbol_table = [
            decoder.DecodedSymbol("b1", 8, "float", 4, msc=1, slave_id=0),
        ]

        device._upstreams.extend([upstream_a, upstream_b])

        # Simulate start() logic
        hub_slave_idx = 0
        for up in device._upstreams:
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
    """Device relays upstream RestartFlag to downstream."""

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
        """When upstream sends restart_flag=1, device sets its own flag."""
        device = _make_server_on_free_port()
        device.add_signal("sig1", "float", 0.0)
        _start_retry(device)
        try:
            upstream = _UpstreamDevice(
                device_name="Arduino", transport=FakeTransport(), relay_downstream=True
            )
            upstream.symbol_table = [
                decoder.DecodedSymbol("temp", 8, "float", 4),
            ]
            upstream.index_map = {0: 0}
            upstream.interval_ms = 0
            device._upstreams.append(upstream)

            # Build D1 frame with restart_flag=1
            frame = self._build_d1_frame(restart_flag=True, signal_values=[25.0])
            full = b"<BLAECK:" + frame + b"/BLAECK>\r\n"

            # Verify device flag is False initially
            device._restart_flag_pending = False

            # Feed frame through FakeTransport
            upstream.transport._buffer = full
            upstream.transport.read_available = lambda: upstream.transport._buffer

            # Parse and relay
            decoded = decoder.parse_data(frame, upstream.symbol_table)
            assert decoded.restart_flag is True

            # Simulate what _poll_upstreams does
            if decoded.restart_flag:
                device._restart_flag_pending = True

            assert device._restart_flag_pending is True
        finally:
            device.close()

    def test_no_restart_flag_leaves_server_flag_unchanged(self):
        """When upstream sends restart_flag=0, device flag stays unchanged."""
        device = _make_server_on_free_port()
        device.add_signal("sig1", "float", 0.0)
        _start_retry(device)
        try:
            frame = self._build_d1_frame(restart_flag=False, signal_values=[10.0])
            symbol_table = [decoder.DecodedSymbol("temp", 8, "float", 4)]

            decoded = decoder.parse_data(frame, symbol_table)
            assert decoded.restart_flag is False

            device._restart_flag_pending = False
            if decoded.restart_flag:
                device._restart_flag_pending = True
            assert device._restart_flag_pending is False
        finally:
            device.close()


# ========================================================================
# Status byte relay tests
# ========================================================================


class TestStatusByteRelay:
    """Device relays upstream status byte downstream."""

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
        """Status byte flows: upstream D1 → device _poll_upstreams → downstream frame."""
        import socket
        import struct

        device = _make_server_on_free_port()
        _start_retry(device)
        try:
            # Connect a downstream TCP client
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(2.0)
            client.connect(("127.0.0.1", device._port))
            device._accept_new_clients()  # accept the client

            # Manually wire upstream (simulating relay of 1 signal)
            transport = FakeTransport("Arduino")
            upstream = _UpstreamDevice(
                device_name="Arduino", transport=transport, relay_downstream=True
            )
            upstream.symbol_table = [
                decoder.DecodedSymbol("temp", 8, "float", 4),
            ]
            upstream.interval_ms = 0
            upstream.connected = True

            # Add relay signal to device
            sig = Signal("temp", "float", 0.0)
            device.signals.append(sig)
            upstream._signals.append(device.signals[0])
            upstream.index_map = {0: 0}
            upstream._upstream_signals = SignalList(upstream._signals)
            device._upstreams.append(upstream)

            # Feed a D1 frame with status=0x01 (I2C CRC error)
            frame_content = self._build_d1_frame(status=0x01, signal_values=[25.0])
            wrapped = b"<BLAECK:" + frame_content + b"/BLAECK>\r\n"
            transport._pending = wrapped
            transport.read_available = lambda: transport._pending

            # Run poll to process the frame and relay downstream
            device._poll_upstreams()
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
            device.close()


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

    def _make_device_with_two_upstreams(self):
        """Create a device with two fake upstreams (A: 2 signals, B: 2 signals)."""
        import socket

        device = _make_server_on_free_port()
        _start_retry(device)  # _local_signal_count = 0, no local signals

        # Manually add upstream signals to device.signals
        for name in ["A_sig0", "A_sig1", "B_sig0", "B_sig1"]:
            device.signals.append(Signal(name, "float", 0.0))

        transport_a = FakeTransport("UpstreamA")
        upstream_a = _UpstreamDevice(
            device_name="UpstreamA", transport=transport_a, relay_downstream=True
        )
        upstream_a.symbol_table = [
            decoder.DecodedSymbol("A_sig0", 8, "float", 4),
            decoder.DecodedSymbol("A_sig1", 8, "float", 4),
        ]
        upstream_a._signals.append(device.signals[0])
        upstream_a._signals.append(device.signals[1])
        upstream_a.index_map = {0: 0, 1: 1}
        upstream_a._upstream_signals = SignalList(upstream_a._signals)
        upstream_a.interval_ms = 300
        upstream_a.connected = True
        device._upstreams.append(upstream_a)

        transport_b = FakeTransport("UpstreamB")
        upstream_b = _UpstreamDevice(
            device_name="UpstreamB", transport=transport_b, relay_downstream=True
        )
        upstream_b.symbol_table = [
            decoder.DecodedSymbol("B_sig0", 8, "float", 4),
            decoder.DecodedSymbol("B_sig1", 8, "float", 4),
        ]
        upstream_b._signals.append(device.signals[2])
        upstream_b._signals.append(device.signals[3])
        upstream_b.index_map = {0: 2, 1: 3}
        upstream_b._upstream_signals = SignalList(upstream_b._signals)
        upstream_b.interval_ms = 300
        upstream_b.connected = True
        device._upstreams.append(upstream_b)

        # Connect a downstream TCP client
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client, upstream_a, upstream_b, transport_a, transport_b

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
        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
        )
        try:
            # Clear the device's initial restart flag
            device._restart_flag_pending = False

            # Upstream A: restart_flag=True
            frame_a = self._build_d1_frame(restart_flag=True, signal_values=[1.0, 2.0])
            tr_a._pending = b"<BLAECK:" + frame_a + b"/BLAECK>\r\n"
            tr_a.read_available = lambda: tr_a._pending

            # Upstream B: restart_flag=False
            frame_b = self._build_d1_frame(restart_flag=False, signal_values=[5.0, 6.0])
            tr_b._pending = b"<BLAECK:" + frame_b + b"/BLAECK>\r\n"
            tr_b.read_available = lambda: tr_b._pending

            device._poll_upstreams()

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
            device.close()

    def test_status_byte_does_not_leak_across_upstreams(self):
        """Upstream A status=0x01 must not appear in upstream B's relay frame."""
        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
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

            device._poll_upstreams()
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
            device.close()

    def test_upstream_lost_frame_scoped_to_upstream(self):
        """STATUS_UPSTREAM_LOST frame only contains the disconnected upstream's signals."""
        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
        )
        try:
            # Mark upstream A's signals as updated (simulates _zero_upstream_signals)
            device.signals[0].value = 0
            device.signals[0].updated = True
            device.signals[1].value = 0
            device.signals[1].updated = True

            # Also mark B's signals as updated (from normal data)
            device.signals[2].value = 99.0
            device.signals[2].updated = True
            device.signals[3].value = 99.0
            device.signals[3].updated = True

            # Send upstream-lost for A only
            device._send_upstream_lost_frame(up_a)

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
            assert device.signals[2].updated is True
            assert device.signals[3].updated is True
        finally:
            client.close()
            device.close()

    def test_upstream_lost_frame_sent_only_once(self):
        """STATUS_UPSTREAM_LOST is sent once on disconnect, not on subsequent ticks."""
        import time

        device, client, up_a, up_b, tr_a, tr_b = (
            self._make_device_with_two_upstreams()
        )
        try:
            # Disconnect upstream A
            tr_a.close()
            up_a.connected = True  # simulate it was connected before

            # First poll: should detect disconnect and send lost frame
            device._poll_upstreams()
            time.sleep(0.05)
            downstream1 = client.recv(8192)

            assert b"/BLAECK>" in downstream1, "Expected a lost frame on first poll"
            content = downstream1[downstream1.find(b"<BLAECK:") + 8:downstream1.find(b"/BLAECK>")]
            assert content[-5] == 0x02, "First poll should send STATUS_UPSTREAM_LOST"

            # connected should now be False
            assert up_a.connected is False

            # Second poll: should NOT send another lost frame
            device._poll_upstreams()
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
            device.close()


# ========================================================================
# Device local signal write / update tests
# ========================================================================


class TestHubWriteUpdate:
    """Verify write(), update(), and related methods on BlaeckTCPy local signals."""

    def _make_device_with_local_signals(self):
        """Create a device with two local signals and a connected downstream client."""
        import socket

        device = _make_server_on_free_port()

        sig_a = Signal("SigA", "float", 1.0)
        sig_b = Signal("SigB", "float", 2.0)
        device.add_signal(sig_a)
        device.add_signal(sig_b)

        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client, sig_a, sig_b

    def _parse_signal_data(self, raw: bytes):
        """Parse signal (id, value) pairs from a downstream data frame."""
        import struct

        start = raw.find(b"<BLAECK:") + len(b"<BLAECK:")
        end = raw.find(b"/BLAECK>")
        content = raw[start:end]

        colon_positions = [i for i, b in enumerate(content) if b == ord(":")]
        sig_start = colon_positions[3] + 1
        sig_end = len(content) - 5  # exclude status(1) + crc(4)
        sig_data = content[sig_start:sig_end]

        signals = []
        pos = 0
        while pos < len(sig_data):
            sig_id = int.from_bytes(sig_data[pos:pos + 2], "little")
            value = struct.unpack("<f", sig_data[pos + 2:pos + 6])[0]
            signals.append((sig_id, value))
            pos += 6
        return signals

    # ---- write() ----

    def test_write_sends_single_signal_by_name(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write("SigA", 42.0)
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (0, 42.0)
            assert sig_a.value == 42.0
        finally:
            client.close()
            device.close()

    def test_write_sends_single_signal_by_index(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write(1, 99.0)
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (1, 99.0)
            assert sig_b.value == 99.0
        finally:
            client.close()
            device.close()

    def test_write_updates_value(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write("SigB", 7.5)
            assert sig_b.value == 7.5
        finally:
            client.close()
            device.close()

    def test_write_noop_when_no_client(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            client.close()
            import time
            time.sleep(0.05)
            device.read()  # process disconnect
            # Should not raise even with no clients
            device.write("SigA", 10.0)
            assert sig_a.value == 10.0
        finally:
            device.close()

    def test_write_rejects_invalid_name(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            with pytest.raises(KeyError):
                device.write("NonExistent", 1.0)
        finally:
            client.close()
            device.close()

    def test_write_rejects_out_of_range_index(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            with pytest.raises(IndexError):
                device.write(5, 1.0)
        finally:
            client.close()
            device.close()

    def test_write_before_start_raises(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        with pytest.raises(KeyError):
            device.write("x", 1.0)

    # ---- update() ----

    def test_update_sets_value_and_flag(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.update("SigA", 55.0)
            assert sig_a.value == 55.0
            assert sig_a.updated is True
        finally:
            client.close()
            device.close()

    def test_update_does_not_send(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.update("SigB", 66.0)
            import time
            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    def test_update_before_start_raises(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        with pytest.raises(KeyError):
            device.update("x", 1.0)

    # ---- mark_signal_updated() ----

    def test_mark_signal_updated_by_name(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert sig_a.updated is False
            device.mark_signal_updated("SigA")
            assert sig_a.updated is True
            assert sig_b.updated is False
        finally:
            client.close()
            device.close()

    def test_mark_signal_updated_by_index(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.mark_signal_updated(1)
            assert sig_b.updated is True
            assert sig_a.updated is False
        finally:
            client.close()
            device.close()

    # ---- mark_all_signals_updated() / clear_all_update_flags() ----

    def test_mark_all_and_clear_all(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.mark_all_signals_updated()
            assert sig_a.updated is True
            assert sig_b.updated is True

            device.clear_all_update_flags()
            assert sig_a.updated is False
            assert sig_b.updated is False
        finally:
            client.close()
            device.close()

    # ---- has_updated_signals ----

    def test_has_updated_signals(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert device.has_updated_signals is False
            sig_a.updated = True
            assert device.has_updated_signals is True
        finally:
            client.close()
            device.close()

    # ---- write_all_data() ----

    def test_write_all_data_sends_all_local(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write_all_data()
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 2
            assert signals[0] == (0, 1.0)
            assert signals[1] == (1, 2.0)
        finally:
            client.close()
            device.close()

    def test_write_all_data_noop_when_no_client(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            client.close()
            import time
            time.sleep(0.05)
            device.read()  # process disconnect
            # Should not raise
            device.write_all_data()
        finally:
            device.close()

    # ---- write_updated_data() ----

    def test_write_updated_data_sends_only_updated(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            sig_b.updated = True
            device.write_updated_data()
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (1, 2.0)
            # updated flag should be cleared after send
            assert sig_b.updated is False
        finally:
            client.close()
            device.close()

    def test_write_updated_data_noop_when_none_updated(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.write_updated_data()
            import time
            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    # ---- tick() ----

    def test_tick_sends_all_on_timer(self):
        import time

        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 50
            device._timed_activated = True
            device._timer.activate(50)

            # Wait for timer to elapse
            time.sleep(0.06)
            device.tick()

            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 2
            assert signals[0] == (0, 1.0)  # SigA
            assert signals[1] == (1, 2.0)  # SigB
        finally:
            client.close()
            device.close()

    def test_tick_noop_before_interval(self):
        import time

        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 500
            device._timed_activated = True
            device._timer.activate(500)

            # Consume the first-tick send
            device.tick()
            client.recv(4096)

            # Don't wait — timer hasn't elapsed
            device.tick()

            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    # ---- tick_updated() ----

    def test_tick_updated_sends_only_updated_on_timer(self):
        import time

        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 50
            device._timed_activated = True
            device._timer.activate(50)

            sig_a.updated = True

            # Wait for timer to elapse
            time.sleep(0.06)
            device.tick_updated()

            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0][0] == 0  # SigA index
        finally:
            client.close()
            device.close()

    def test_tick_updated_noop_when_no_updated(self):
        import time

        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device._fixed_interval_ms = 50
            device._timed_activated = True
            device._timer.activate(50)

            time.sleep(0.06)
            device.tick_updated()

            time.sleep(0.05)
            client.setblocking(False)
            try:
                data = client.recv(4096)
            except BlockingIOError:
                data = b""
            client.setblocking(True)
            assert data == b""
        finally:
            client.close()
            device.close()

    # ---- read() ----

    def test_read_before_start_raises(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        with pytest.raises(AttributeError):
            device.read()

    def test_read_processes_write_data_command(self):
        """read() handles a WRITE_DATA command and sends local signals."""
        import time

        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            # Send WRITE_DATA command from the downstream client
            client.sendall(b"<BLAECK.WRITE_DATA,1>")
            time.sleep(0.05)
            device.read()
            time.sleep(0.05)
            downstream = client.recv(4096)
            assert b"<BLAECK:" in downstream
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 2
        finally:
            client.close()
            device.close()

    # ---- resolve edge cases ----

    def test_resolve_index_empty_signals(self):
        """Index access with no local signals gives a clear error."""
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        device._started = True
        device._local_signal_count = 0
        with pytest.raises(IndexError, match="Signal index 0 out of range"):
            device._resolve_signal(0)

    # ---- add_signals() / delete_signals() ----

    def test_add_signals_bulk_before_start(self):
        device = BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")
        device.add_signals([
            Signal("A", "float"),
            Signal("B", "int"),
        ])
        assert len(device.signals) == 2
        assert device.signals[0].signal_name == "A"
        assert device.signals[1].signal_name == "B"

    def test_add_signal_after_start_inserts_in_server(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert len(device.signals) == 2
            sig_c = device.add_signal("SigC", "float", 3.0)
            assert len(device.signals) == 3
            assert device.signals[2] is sig_c
        finally:
            client.close()
            device.close()

    def test_delete_signals_after_start_removes_from_server(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            assert len(device.signals) == 2
            device.delete_signals()
            assert len(device.signals) == 0
        finally:
            client.close()
            device.close()

    def test_delete_then_add_after_start(self):
        device, client, sig_a, sig_b = self._make_device_with_local_signals()
        try:
            device.delete_signals()
            sig_x = device.add_signal("X", "float", 99.0)
            assert len(device.signals) == 1
            assert device.signals[0] is sig_x

            # Verify we can still send data
            device.write("X", 42.0)
            downstream = client.recv(4096)
            signals = self._parse_signal_data(downstream)
            assert len(signals) == 1
            assert signals[0] == (0, 42.0)
        finally:
            client.close()
            device.close()


# ========================================================================
# Server interval_ms property tests
# ========================================================================


class TestServerIntervalProperty:
    """Verify BlaeckTCPy.interval_ms property locked/unlocked behaviour."""

    def _make_server_with_client(self):
        """Return (device, client_socket) with one connected client."""
        import socket

        device = _make_server_on_free_port()
        device.add_signal("temp", "float", 0.0)
        _start_retry(device)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()
        return device, client

    def test_set_interval_activates_timed_data(self):
        """interval_ms = >0 should activate timed data immediately."""
        server, client = self._make_server_with_client()
        try:
            assert not server._timed_activated
            server.interval_ms = 500
            assert server._timed_activated
            assert server.interval_ms == 500
        finally:
            client.close()
            server.close()

    def test_set_interval_zero_locks_at_zero(self):
        """interval_ms = 0 should lock at 0ms (fastest possible)."""
        server, client = self._make_server_with_client()
        try:
            server.interval_ms = 0
            assert server.interval_ms == 0
            assert server._timed_activated
        finally:
            client.close()
            server.close()

    def test_interval_client_releases_lock(self):
        """interval_ms = IntervalMode.CLIENT should release the lock."""
        server, client = self._make_server_with_client()
        try:
            server.interval_ms = 500
            assert server.interval_ms == 500
            server.interval_ms = IntervalMode.CLIENT
            assert server.interval_ms == IntervalMode.CLIENT
        finally:
            client.close()
            server.close()

    def test_interval_off_deactivates(self):
        """interval_ms = IntervalMode.OFF should deactivate timed data."""
        server, client = self._make_server_with_client()
        try:
            server.interval_ms = 500
            assert server._timed_activated
            server.interval_ms = IntervalMode.OFF
            assert not server._timed_activated
            assert server.interval_ms == IntervalMode.OFF
        finally:
            client.close()
            server.close()

    def test_locked_ignores_client_activate(self):
        """When locked, client ACTIVATE command should be ignored."""
        server, client = self._make_server_with_client()
        try:
            server.interval_ms = 500
            # Send ACTIVATE command from client with different interval
            activate_cmd = "<BLAECK.ACTIVATE,208,7,0,0>"
            client.sendall(activate_cmd.encode())
            import time
            time.sleep(0.05)
            server.read()
            # Lock still active, interval unchanged
            assert server.interval_ms == 500
            assert server._timed_activated
        finally:
            client.close()
            server.close()

    def test_locked_ignores_client_deactivate(self):
        """When locked, client DEACTIVATE should be ignored."""
        server, client = self._make_server_with_client()
        try:
            server.interval_ms = 500
            assert server._timed_activated
            # Send DEACTIVATE from client
            deactivate_cmd = "<BLAECK.DEACTIVATE>"
            client.sendall(deactivate_cmd.encode())
            import time
            time.sleep(0.05)
            server.read()
            # Still activated because lock is on
            assert server._timed_activated
        finally:
            client.close()
            server.close()

    def test_unlocked_allows_client_activate(self):
        """When unlocked (default), client ACTIVATE works normally."""
        server, client = self._make_server_with_client()
        try:
            assert not server._timed_activated
            # interval=1000 → 0x03E8 → bytes 232,3,0,0
            activate_cmd = "<BLAECK.ACTIVATE,232,3,0,0>"
            client.sendall(activate_cmd.encode())
            import time
            time.sleep(0.05)
            server.read()
            assert server._timed_activated
        finally:
            client.close()
            server.close()

    def test_locked_timed_write_works_after_reconnect(self):
        """After client disconnect+reconnect, locked interval keeps working."""
        import socket
        import time

        server, client = self._make_server_with_client()
        try:
            server.interval_ms = 50
            assert server._timed_activated

            # Disconnect
            client.close()
            time.sleep(0.05)
            try:
                server.read()
            except Exception:
                pass

            # _timed_activated may be False after disconnect, but fixed_interval
            # should make timed_write still work after reconnect
            client2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client2.settimeout(2.0)
            client2.connect(("127.0.0.1", server._port))
            server._accept_new_clients()

            # Wait for timer to fire
            time.sleep(0.1)
            result = server.timed_write_all_data()
            assert result is True
        finally:
            try:
                client2.close()
            except Exception:
                pass
            server.close()



# ========================================================================
# Custom command forwarding tests
# ========================================================================


class RecordingTransport(_UpstreamBase):
    """Transport that records all sent data for verification."""

    def __init__(self, name="recording"):
        super().__init__(name)
        self._connected = True
        self.sent: list[bytes] = []

    def connect(self, timeout=5.0):
        self._connected = True
        return True

    def read_available(self):
        return b""

    def send(self, data):
        self.sent.append(data)
        return True

    def close(self):
        self._connected = False


class TestForwardCommandRegistration:
    """Verify forward_command() and on_command(forward=True) register correctly."""

    def setup_method(self):
        self.device = BlaeckTCPy.__new__(BlaeckTCPy)
        self.device._upstreams = []
        self.device._started = False
        self.device._command_handlers = {}
        self.device._forwarded_commands = set()
        self.device._read_callback = None

    def test_forward_command_registers(self):
        self.device.forward_command("RESET")
        assert "RESET" in self.device._forwarded_commands

    def test_forward_command_multiple(self):
        self.device.forward_command("RESET")
        self.device.forward_command("CALIBRATE")
        assert self.device._forwarded_commands == {"RESET", "CALIBRATE"}

    def test_on_command_forward_true_registers(self):
        @self.device.on_command("SET_LED", forward=True)
        def handler(state):
            pass

        assert "SET_LED" in self.device._forwarded_commands
        assert "SET_LED" in self.device._command_handlers

    def test_on_command_forward_false_does_not_register(self):
        @self.device.on_command("SET_LED")
        def handler(state):
            pass

        assert "SET_LED" not in self.device._forwarded_commands
        assert "SET_LED" in self.device._command_handlers

    def test_on_command_catchall_ignores_forward(self):
        @self.device.on_command(forward=True)
        def handler(command, *params):
            pass

        assert len(self.device._forwarded_commands) == 0
        assert self.device._read_callback is handler


class TestCustomCommandForwarding:
    """Verify custom commands are forwarded to opted-in upstreams."""

    def _make_hub_with_upstream(self, forward_custom_commands=True):
        """Create a hub with one recording upstream and a TCP client."""
        import socket
        import time

        device = _make_server_on_free_port()

        transport = RecordingTransport("ESP32")
        upstream = _UpstreamDevice(
            device_name="ESP32",
            transport=transport,
            relay_downstream=True,
            forward_custom_commands=forward_custom_commands,
        )
        device._upstreams.append(upstream)
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client, transport

    def test_forward_command_sent_to_upstream(self):
        device, client, transport = self._make_hub_with_upstream()
        device.forward_command("RESET")

        try:
            import time

            client.sendall(b"<RESET>")
            time.sleep(0.05)
            device.read()
            assert b"<RESET>" in transport.sent
        finally:
            client.close()
            device.close()

    def test_forward_command_with_params(self):
        device, client, transport = self._make_hub_with_upstream()
        device.forward_command("SET_LED")

        try:
            import time

            client.sendall(b"<SET_LED,1,on>")
            time.sleep(0.05)
            device.read()
            assert b"<SET_LED,1,on>" in transport.sent
        finally:
            client.close()
            device.close()

    def test_on_command_forward_true_sends_and_handles(self):
        device, client, transport = self._make_hub_with_upstream()
        received = []

        @device.on_command("SET_LED", forward=True)
        def handler(*params):
            received.append(params)

        try:
            import time

            client.sendall(b"<SET_LED,1>")
            time.sleep(0.05)
            device.read()
            # Local handler fires
            assert len(received) == 1
            assert received[0] == ("1",)
            # AND forwarded to upstream
            assert b"<SET_LED,1>" in transport.sent
        finally:
            client.close()
            device.close()

    def test_not_forwarded_when_upstream_opted_out(self):
        device, client, transport = self._make_hub_with_upstream(
            forward_custom_commands=False
        )
        device.forward_command("RESET")

        try:
            import time

            client.sendall(b"<RESET>")
            time.sleep(0.05)
            device.read()
            assert b"<RESET>" not in transport.sent
        finally:
            client.close()
            device.close()

    def test_not_forwarded_when_command_not_registered(self):
        device, client, transport = self._make_hub_with_upstream()
        # No forward_command("SET_LED") registered

        try:
            import time

            client.sendall(b"<SET_LED,1>")
            time.sleep(0.05)
            device.read()
            assert len(transport.sent) == 0
        finally:
            client.close()
            device.close()

    def test_builtin_commands_not_double_forwarded(self):
        """Built-in BLAECK.* commands must not be forwarded via the custom path."""
        device, client, transport = self._make_hub_with_upstream()
        device._forwarded_commands.add("BLAECK.WRITE_DATA")

        try:
            import time

            client.sendall(b"<BLAECK.WRITE_DATA,1,0,0,0>")
            time.sleep(0.05)
            device.read()
            # Built-in forwarding sends it once (via normal hub path);
            # the custom forward path must NOT send it again.
            blaeck_sends = [s for s in transport.sent if b"BLAECK.WRITE_DATA" in s]
            assert len(blaeck_sends) == 1
        finally:
            client.close()
            device.close()

    def test_selective_forwarding_multiple_upstreams(self):
        """Only upstreams with forward_custom_commands=True receive the command."""
        import socket
        import time

        device = _make_server_on_free_port()

        transport_a = RecordingTransport("ArduinoA")
        upstream_a = _UpstreamDevice(
            device_name="ArduinoA",
            transport=transport_a,
            relay_downstream=True,
            forward_custom_commands=True,
        )

        transport_b = RecordingTransport("ArduinoB")
        upstream_b = _UpstreamDevice(
            device_name="ArduinoB",
            transport=transport_b,
            relay_downstream=True,
            forward_custom_commands=False,
        )

        device._upstreams.extend([upstream_a, upstream_b])
        _start_retry(device)
        device.forward_command("RESET")

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        try:
            client.sendall(b"<RESET>")
            time.sleep(0.05)
            device.read()
            assert b"<RESET>" in transport_a.sent
            assert b"<RESET>" not in transport_b.sent
        finally:
            client.close()
            device.close()

    def test_forward_skips_disconnected_upstream(self):
        device, client, transport = self._make_hub_with_upstream()
        device.forward_command("RESET")
        transport._connected = False

        try:
            import time

            client.sendall(b"<RESET>")
            time.sleep(0.05)
            device.read()
            assert len(transport.sent) == 0
        finally:
            client.close()
            device.close()

    def test_local_handler_fires_without_forwarding(self):
        """on_command without forward=True should NOT forward."""
        device, client, transport = self._make_hub_with_upstream()
        received = []

        @device.on_command("MOTOR")
        def handler(*params):
            received.append(params)

        try:
            import time

            client.sendall(b"<MOTOR,255,forward>")
            time.sleep(0.05)
            device.read()
            assert len(received) == 1
            assert received[0] == ("255", "forward")
            assert len(transport.sent) == 0
        finally:
            client.close()
            device.close()


# ========================================================================
# Timestamp tests
# ========================================================================


class TestTimestampModeEnum:
    """Verify TimestampMode enum values."""

    def test_values(self):
        assert TimestampMode.NONE == 0
        assert TimestampMode.MICROS == 1
        assert TimestampMode.RTC == 2

    def test_is_int(self):
        assert isinstance(TimestampMode.NONE, int)


class TestTimestampProperties:
    """Verify timestamp_mode, start_time properties."""

    def test_default_timestamp_mode_is_none(self):
        device = _make_server_on_free_port()
        assert device.timestamp_mode == TimestampMode.NONE

    def test_set_timestamp_mode(self):
        device = _make_server_on_free_port()
        device.timestamp_mode = TimestampMode.RTC
        assert device.timestamp_mode == TimestampMode.RTC

    def test_start_time_set_at_start(self):
        import time

        device = _make_server_on_free_port()
        before = time.time()
        _start_retry(device)
        after = time.time()
        try:
            assert before <= device.start_time <= after
        finally:
            device.close()

    def test_start_time_zero_before_start(self):
        device = _make_server_on_free_port()
        assert device.start_time == 0.0


class TestTimestampInDataFrames:
    """Verify timestamps are encoded in outgoing data frames."""

    def _make_device(self):
        import socket

        device = _make_server_on_free_port()
        device.add_signal("temp", "float", 3.14)
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client

    def _recv_frame(self, client):
        """Receive one full BlaeckTCP frame."""
        import time
        time.sleep(0.05)
        data = b""
        while True:
            try:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"/BLAECK>" in data:
                    break
            except Exception:
                break
        return data

    def test_no_timestamp_mode_sends_mode_zero(self):
        """Default NO_TIMESTAMP mode should send mode byte 0x00."""
        device, client = self._make_device()
        try:
            device.write_all_data()
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x00
        finally:
            client.close()
            device.close()

    def test_rtc_mode_sends_8_byte_timestamp(self):
        """RTC mode should include an 8-byte timestamp."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.RTC
            device.write_all_data()
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
            ts_bytes = ts_mode_section[1:9]
            ts = int.from_bytes(ts_bytes, "little")
            import time
            now_us = int(time.time() * 1_000_000)
            assert abs(ts - now_us) < 5_000_000  # within 5 seconds
        finally:
            client.close()
            device.close()

    def test_micros_mode_sends_relative_timestamp(self):
        """MICROS mode should send us since start()."""
        import time

        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.MICROS
            time.sleep(0.05)
            device.write_all_data()
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x01
            ts_bytes = ts_mode_section[1:9]
            ts = int.from_bytes(ts_bytes, "little")
            assert 10_000 < ts < 5_000_000
        finally:
            client.close()
            device.close()

    def test_explicit_timestamp_overrides_auto(self):
        """Explicit timestamp_us should override auto-generated value."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.RTC
            explicit_ts = 1234567890_000000
            device.write_all_data(timestamp_us=explicit_ts)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
            ts_bytes = ts_mode_section[1:9]
            ts = int.from_bytes(ts_bytes, "little")
            assert ts == explicit_ts
        finally:
            client.close()
            device.close()

    def test_explicit_timestamp_with_no_mode_uses_none(self):
        """With NONE mode, even explicit timestamp should not be sent."""
        device, client = self._make_device()
        try:
            assert device.timestamp_mode == TimestampMode.NONE
            device.write_all_data(timestamp_us=1234567890)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x00
        finally:
            client.close()
            device.close()

    def test_write_updated_data_includes_timestamp(self):
        """write_updated_data should also include timestamps."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.RTC
            device.update("temp", 42.0)
            device.write_updated_data()
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
        finally:
            client.close()
            device.close()

    def test_write_single_signal_includes_timestamp(self):
        """write() should also include timestamps."""
        device, client = self._make_device()
        try:
            device.timestamp_mode = TimestampMode.RTC
            device.write("temp", 42.0)
            frame = self._recv_frame(client)
            start = frame.find(b"<BLAECK:") + len(b"<BLAECK:")
            content = frame[start:]
            parts = content.split(b":", 4)
            ts_mode_section = parts[3]
            assert ts_mode_section[0] == 0x02
        finally:
            client.close()
            device.close()
