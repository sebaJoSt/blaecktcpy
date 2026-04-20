"""Tests for custom command forwarding to upstreams."""

import pytest

from blaecktcpy._server import UpstreamDevice
from conftest import _make_server_on_free_port, _start_retry, RecordingTransport


class TestForwardCommandRegistration:
    """Verify on_command(forward=False) opt-out registration."""

    def setup_method(self):
        self.device = _make_server_on_free_port()

    def test_on_command_forward_true_does_not_opt_out(self):
        @self.device.on_command("SET_LED", forward=True)
        def handler(state):
            pass

        assert "SET_LED" not in self.device._non_forwarded_commands
        assert "SET_LED" in self.device._command_handlers

    def test_on_command_forward_false_opts_out(self):
        @self.device.on_command("SET_LED", forward=False)
        def handler(state):
            pass

        assert "SET_LED" in self.device._non_forwarded_commands
        assert "SET_LED" in self.device._command_handlers

    def test_on_command_catchall_ignores_forward(self):
        @self.device.on_command(forward=True)
        def handler(command, *params):
            pass

        assert len(self.device._non_forwarded_commands) == 0
        assert self.device._read_callback is handler

    def test_on_command_forward_non_bool_raises(self):
        with pytest.raises(TypeError, match="forward must be True or False"):
            @self.device.on_command("SET_LED", forward=1)
            def handler(state):
                pass


class TestCustomCommandForwarding:
    """Verify custom commands are forwarded to opted-in upstreams."""

    def _make_hub_with_upstream(self, forward_custom_commands=True):
        """Create a hub with one recording upstream and a TCP client."""
        import socket
        import time

        device = _make_server_on_free_port()

        transport = RecordingTransport("ESP32")
        upstream = UpstreamDevice(
            device_name="ESP32",
            transport=transport,
            relay_downstream=True,
            forward_custom_commands=forward_custom_commands,
        )
        device._hub._upstreams.append(upstream)
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client, transport

    def test_unknown_command_forwarded_by_default(self):
        device, client, transport = self._make_hub_with_upstream()
        # No handler registered — command should still be forwarded

        try:
            import time

            client.sendall(b"<RESET>")
            time.sleep(0.05)
            device.read()
            assert b"<RESET>" in transport.sent
        finally:
            client.close()
            device.close()

    def test_unknown_command_with_params_forwarded(self):
        device, client, transport = self._make_hub_with_upstream()

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

        try:
            import time

            client.sendall(b"<RESET>")
            time.sleep(0.05)
            device.read()
            assert b"<RESET>" not in transport.sent
        finally:
            client.close()
            device.close()

    def test_builtin_commands_not_double_forwarded(self):
        """Built-in BLAECK.* commands must not be forwarded via the custom path."""
        device, client, transport = self._make_hub_with_upstream()

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
        upstream_a = UpstreamDevice(
            device_name="ArduinoA",
            transport=transport_a,
            relay_downstream=True,
            forward_custom_commands=True,
        )

        transport_b = RecordingTransport("ArduinoB")
        upstream_b = UpstreamDevice(
            device_name="ArduinoB",
            transport=transport_b,
            relay_downstream=True,
            forward_custom_commands=False,
        )

        device._hub._upstreams.extend([upstream_a, upstream_b])
        _start_retry(device)

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
        """on_command with forward=False should NOT forward."""
        device, client, transport = self._make_hub_with_upstream()
        received = []

        @device.on_command("MOTOR", forward=False)
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

    def test_forward_custom_commands_list_filter(self):
        """Only commands in the list are forwarded to that upstream."""
        import socket
        import time

        device = _make_server_on_free_port()

        transport_a = RecordingTransport("LED")
        upstream_a = UpstreamDevice(
            device_name="LED",
            transport=transport_a,
            relay_downstream=True,
            forward_custom_commands=["SET_LED"],
        )

        transport_b = RecordingTransport("Motor")
        upstream_b = UpstreamDevice(
            device_name="Motor",
            transport=transport_b,
            relay_downstream=True,
            forward_custom_commands=["SET_SPEED"],
        )

        device._hub._upstreams.extend([upstream_a, upstream_b])
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        try:
            client.sendall(b"<SET_LED,1>")
            time.sleep(0.05)
            device.read()
            assert b"<SET_LED,1>" in transport_a.sent
            assert len(transport_b.sent) == 0

            client.sendall(b"<SET_SPEED,255>")
            time.sleep(0.05)
            device.read()
            assert b"<SET_SPEED,255>" in transport_b.sent
            assert b"<SET_SPEED,255>" not in transport_a.sent

            # Unknown command goes to neither (both have lists)
            client.sendall(b"<RESET>")
            time.sleep(0.05)
            device.read()
            assert b"<RESET>" not in transport_a.sent
            assert b"<RESET>" not in transport_b.sent
        finally:
            client.close()
            device.close()


class TestReplayCommandRegistration:
    """Verify replay_commands on add_tcp/add_serial."""

    def setup_method(self):
        self.device = _make_server_on_free_port()

    def test_replay_commands_default_is_empty(self):
        transport = RecordingTransport("ESP32")
        upstream = UpstreamDevice(
            device_name="ESP32", transport=transport,
        )
        assert upstream.replay_commands == []

    def test_replay_commands_set_on_upstream(self):
        transport = RecordingTransport("ESP32")
        upstream = UpstreamDevice(
            device_name="ESP32", transport=transport,
            replay_commands=["SET_LED", "SET_MOTOR"],
        )
        assert upstream.replay_commands == ["SET_LED", "SET_MOTOR"]

    def test_add_tcp_replay_commands_rejects_non_list(self):
        with pytest.raises(TypeError, match="replay_commands must be a list"):
            self.device.add_tcp("127.0.0.1", 9999, replay_commands=True)

    def test_add_serial_replay_commands_rejects_non_list(self):
        with pytest.raises(TypeError, match="replay_commands must be a list"):
            self.device.add_serial("COM99", replay_commands="SET_LED")


class TestCustomCommandReplay:
    """Verify replayable commands are stored and replayed on reconnect."""

    def _make_hub_with_upstream(self, forward_custom_commands=True,
                                 replay_commands=None):
        """Create a hub with one recording upstream and a TCP client."""
        import socket
        import time

        device = _make_server_on_free_port()

        transport = RecordingTransport("ESP32")
        upstream = UpstreamDevice(
            device_name="ESP32",
            transport=transport,
            relay_downstream=True,
            forward_custom_commands=forward_custom_commands,
            replay_commands=replay_commands or [],
        )
        device._hub._upstreams.append(upstream)
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        return device, client, transport, upstream

    def test_replayable_command_is_stored(self):
        device, client, transport, upstream = self._make_hub_with_upstream(
            replay_commands=["SET_LED"]
        )
        try:
            import time

            client.sendall(b"<SET_LED,ON>")
            time.sleep(0.05)
            device.read()
            assert device._last_custom_commands == {"SET_LED": "SET_LED,ON"}
        finally:
            client.close()
            device.close()

    def test_non_replayable_command_not_stored(self):
        device, client, transport, upstream = self._make_hub_with_upstream(
            replay_commands=[]
        )
        try:
            import time

            client.sendall(b"<TRIGGER>")
            time.sleep(0.05)
            device.read()
            assert "TRIGGER" not in device._last_custom_commands
        finally:
            client.close()
            device.close()

    def test_last_invocation_wins(self):
        device, client, transport, upstream = self._make_hub_with_upstream(
            replay_commands=["SET_LED"]
        )
        try:
            import time

            client.sendall(b"<SET_LED,ON>")
            time.sleep(0.05)
            device.read()
            client.sendall(b"<SET_LED,OFF>")
            time.sleep(0.05)
            device.read()
            assert device._last_custom_commands == {"SET_LED": "SET_LED,OFF"}
        finally:
            client.close()
            device.close()

    def test_replay_on_reconnect(self):
        device, client, transport, upstream = self._make_hub_with_upstream(
            replay_commands=["SET_LED"]
        )
        try:
            import time

            client.sendall(b"<SET_LED,ON>")
            time.sleep(0.05)
            device.read()
            transport.sent.clear()

            device._hub._replay_custom_commands(upstream)
            assert b"<SET_LED,ON>" in transport.sent
        finally:
            client.close()
            device.close()

    def test_replay_respects_forward_whitelist(self):
        """Command in replay_commands but not in forward_custom_commands is skipped."""
        device, client, transport, upstream = self._make_hub_with_upstream(
            forward_custom_commands=["SET_MOTOR"],
            replay_commands=["SET_LED", "SET_MOTOR"],
        )
        try:
            import time

            # SET_LED won't be forwarded (not in forward list), but stored
            # via the upstream's replay_commands
            client.sendall(b"<SET_MOTOR,100>")
            time.sleep(0.05)
            device.read()
            transport.sent.clear()

            device._hub._replay_custom_commands(upstream)
            assert b"<SET_LED,ON>" not in transport.sent
            assert b"<SET_MOTOR,100>" in transport.sent
        finally:
            client.close()
            device.close()

    def test_replay_skips_when_forward_false(self):
        device, client, transport, upstream = self._make_hub_with_upstream(
            forward_custom_commands=False,
            replay_commands=["SET_LED"],
        )
        try:
            import time

            client.sendall(b"<SET_LED,ON>")
            time.sleep(0.05)
            device.read()
            # Command stored because another upstream might want it
            # but this upstream has forward=False
            device._hub._replay_custom_commands(upstream)
            assert len(transport.sent) == 0
        finally:
            client.close()
            device.close()

    def test_replay_before_activate(self):
        """Replay commands are sent before BLAECK.ACTIVATE."""
        device, client, transport, upstream = self._make_hub_with_upstream(
            replay_commands=["SET_LED"]
        )
        try:
            import time

            client.sendall(b"<SET_LED,ON>")
            time.sleep(0.05)
            device.read()
            transport.sent.clear()

            device._hub._replay_custom_commands(upstream)
            device._hub._resend_activate(upstream)
            # SET_LED should appear before any BLAECK command
            led_idx = None
            for i, msg in enumerate(transport.sent):
                if b"SET_LED" in msg:
                    led_idx = i
                    break
            assert led_idx == 0, f"SET_LED should be first, got index {led_idx}"
        finally:
            client.close()
            device.close()

    def test_no_replay_without_prior_command(self):
        """If no command was ever sent, nothing is replayed."""
        device, client, transport, upstream = self._make_hub_with_upstream(
            replay_commands=["SET_LED"]
        )
        try:
            device._hub._replay_custom_commands(upstream)
            assert len(transport.sent) == 0
        finally:
            client.close()
            device.close()
