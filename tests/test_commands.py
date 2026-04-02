"""Tests for custom command forwarding to upstreams."""

import pytest

from blaecktcpy import BlaeckTCPy
from blaecktcpy._server import _UpstreamDevice
from conftest import _make_server_on_free_port, _start_retry, RecordingTransport


class TestForwardCommandRegistration:
    """Verify on_command(forward=False) opt-out registration."""

    def setup_method(self):
        self.device = BlaeckTCPy.__new__(BlaeckTCPy)
        self.device._upstreams = []
        self.device._started = False
        self.device._command_handlers = {}
        self.device._non_forwarded_commands = set()
        self.device._read_callback = None

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
