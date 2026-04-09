"""Tests for relay registration, B6 device type parsing, and multi-slave pass-through."""

import pytest

from blaecktcpy import Signal, SignalList, BlaeckTCPy
from blaecktcpy._server import _UpstreamDevice
from blaecktcpy.hub import _decoder as decoder
from conftest import _make_server_on_free_port, _start_retry, FakeTransport


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
            + b"Loggbok\0"
            + b"app\0"
        )
        content = msg_key + b":" + msg_id + b":" + payload
        info = decoder.parse_devices(content)
        assert info.device_type == "server"
        assert info.device_name == "TestDevice"
        assert info.server_restarted == "0"
        assert info.parent == "0"
        assert info.client_name == "Loggbok"
        assert info.client_type == "app"

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
            + b"\0"
            + b"unknown\0"
        )
        content = msg_key + b":" + msg_id + b":" + payload
        info = decoder.parse_devices(content)
        assert info.device_type == "hub"
        assert info.client_name == ""
        assert info.client_type == "unknown"

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
        assert info.client_name == ""
        assert info.client_type == ""


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
            + b"Loggbok\0" + b"app\0"
        )
        slave = (
            b"\x02\x08"  # MSC=slave, SlaveID=8
            + b"SensorBoard\0"
            + b"1.1\0" + b"2.1\0" + b"3.1\0"
            + b"blaeckserial\0" + b"1\0" + b"1\0" + b"0\0" + b"server\0" + b"0\0"
            + b"Loggbok\0" + b"app\0"
        )
        content = msg_key + b":" + msg_id + b":" + master + slave
        devices = decoder.parse_all_devices(content)
        assert len(devices) == 2
        assert devices[0].device_name == "ArduinoMain"
        assert devices[0].msc == 1 and devices[0].slave_id == 0
        assert devices[0].client_name == "Loggbok"
        assert devices[0].client_type == "app"
        assert devices[1].device_name == "SensorBoard"
        assert devices[1].msc == 2 and devices[1].slave_id == 8
        assert devices[1].client_name == "Loggbok"
        assert devices[1].client_type == "app"

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
            + b"\0" + b"unknown\0"
        )
        slave = (
            b"\x02\x01" + b"Second\0"
            + b"1.0\0" + b"2.0\0" + b"3.0\0"
            + b"lib\0" + b"1\0" + b"1\0" + b"0\0" + b"server\0" + b"0\0"
            + b"\0" + b"unknown\0"
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
