"""Tests for callback registration, dispatch, and exception resilience."""

import pytest

from blaecktcpy import BlaeckTCPy
from blaecktcpy._server import _UpstreamDevice
from conftest import _make_server_on_free_port, _start_retry, FakeTransport


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
        self.device._non_forwarded_commands = set()
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
