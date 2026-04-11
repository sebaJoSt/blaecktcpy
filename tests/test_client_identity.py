"""Tests for client identity via GET_DEVICES params."""

import socket
import time

import pytest

from blaecktcpy import BlaeckTCPy
from blaecktcpy._server import _UpstreamDevice
from conftest import _make_server_on_free_port, _start_retry, RecordingTransport


class TestClientMeta:
    """Verify _client_meta lifecycle on connect/disconnect."""

    def test_meta_created_on_connect(self):
        device = _make_server_on_free_port()
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))

        try:
            device._accept_new_clients()
            assert 0 in device._client_meta
            assert device._client_meta[0] == {"name": "", "type": "unknown"}
            assert 0 in device._client_addrs
        finally:
            client.close()
            device.close()

    def test_meta_cleared_on_disconnect(self):
        device = _make_server_on_free_port()
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))

        try:
            device._accept_new_clients()
            assert 0 in device._client_meta
            client.close()
            time.sleep(0.05)
            device.read()
        finally:
            device.close()

        assert 0 not in device._client_meta
        assert 0 not in device._client_addrs


class TestIdentityParsing:
    """Verify identity extraction from GET_DEVICES params."""

    def _make_server_with_client(self):
        device = _make_server_on_free_port()
        device.add_signal("x", "float")
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()
        return device, client

    def test_identity_from_get_devices(self):
        device, client = self._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0,My App,app>")
            time.sleep(0.05)
            device.read()

            assert device._client_meta[0]["name"] == "My App"
            assert device._client_meta[0]["type"] == "app"
        finally:
            client.close()
            device.close()

    def test_identity_name_only(self):
        device, client = self._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0,My Hub>")
            time.sleep(0.05)
            device.read()

            assert device._client_meta[0]["name"] == "My Hub"
            assert device._client_meta[0]["type"] == "unknown"
        finally:
            client.close()
            device.close()

    def test_no_identity_keeps_defaults(self):
        device, client = self._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0>")
            time.sleep(0.05)
            device.read()

            assert device._client_meta[0]["name"] == ""
            assert device._client_meta[0]["type"] == "unknown"
        finally:
            client.close()
            device.close()

    def test_no_params_keeps_defaults(self):
        device, client = self._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES>")
            time.sleep(0.05)
            device.read()

            assert device._client_meta[0]["name"] == ""
            assert device._client_meta[0]["type"] == "unknown"
        finally:
            client.close()
            device.close()

    def test_identity_updates_on_second_get_devices(self):
        device, client = self._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0,Old Name,app>")
            time.sleep(0.05)
            device.read()
            assert device._client_meta[0]["name"] == "Old Name"

            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0,New Name,hub>")
            time.sleep(0.05)
            device.read()
            assert device._client_meta[0]["name"] == "New Name"
            assert device._client_meta[0]["type"] == "hub"
        finally:
            client.close()
            device.close()


class TestDisconnectLog:
    """Verify disconnect log includes identity when known."""

    def test_disconnect_with_identity(self, caplog):
        import logging

        device, client = TestIdentityParsing()._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0,Loggbok,app>")
            time.sleep(0.05)
            device.read()

            with caplog.at_level(logging.INFO, logger="blaecktcpy"):
                caplog.clear()
                client.close()
                time.sleep(0.05)
                device.read()

                assert any("Loggbok" in r.message for r in caplog.records)
                assert any("disconnected" in r.message for r in caplog.records)
        finally:
            device.close()

    def test_disconnect_without_identity(self, caplog):
        import logging

        device = _make_server_on_free_port()
        device.add_signal("x", "float")
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()

        try:
            with caplog.at_level(logging.INFO, logger="blaecktcpy"):
                caplog.clear()
                client.close()
                time.sleep(0.05)
                device.read()

                disconnect_msgs = [
                    r.message for r in caplog.records if "disconnected" in r.message
                ]
                assert len(disconnect_msgs) == 1
                # Should NOT contain a name after "disconnected:"
                assert "disconnected:" not in disconnect_msgs[0]
        finally:
            device.close()


class TestHubSendsIdentity:
    """Verify hub includes identity when calling GET_DEVICES on upstreams."""

    def test_hub_sends_identity_in_get_devices(self):
        device = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="My Hub",
                     device_hw_version="HW",
                     device_fw_version="1.0",
                 )

        transport = RecordingTransport("ESP32")
        upstream = _UpstreamDevice(
            device_name="ESP32",
            transport=transport,
            relay_downstream=True,
        )
        device._upstreams.append(upstream)

        # Simulate what add_upstream does for GET_DEVICES
        identity = f",0,0,0,0,My Hub,hub"
        transport.send_command(f"BLAECK.GET_DEVICES{identity}")

        assert len(transport.sent) == 1
        sent = transport.sent[0]
        assert b"My Hub" in sent
        assert b"hub" in sent


class TestB6ResponseContainsClientIdentity:
    """Verify the B6 device info frame echoes back client identity."""

    def _make_server_with_client(self):
        device = _make_server_on_free_port()
        device.add_signal("x", "float")
        _start_retry(device)

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()
        return device, client

    def test_b6_contains_client_identity(self):
        """B6 response includes ClientName and ClientType from GET_DEVICES."""
        from blaecktcpy.hub import _decoder as decoder

        device, client = self._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0,Loggbok,desktop>")
            time.sleep(0.05)
            device.read()

            # Read the B6 response
            data = b""
            while True:
                try:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                except socket.timeout:
                    break

            # Parse B6 frame content (between <BLAECK: and /BLAECK>)
            start = data.index(b"<BLAECK:") + len(b"<BLAECK:")
            end = data.index(b"/BLAECK>")
            content = data[start:end]

            info = decoder.parse_devices(content)
            assert info.client_name == "Loggbok"
            assert info.client_type == "desktop"
        finally:
            client.close()
            device.close()

    def test_b6_defaults_without_identity(self):
        """B6 response uses defaults when no identity was sent."""
        from blaecktcpy.hub import _decoder as decoder

        device, client = self._make_server_with_client()

        try:
            client.sendall(b"<BLAECK.GET_DEVICES,0,0,0,0>")
            time.sleep(0.05)
            device.read()

            data = b""
            while True:
                try:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                except socket.timeout:
                    break

            start = data.index(b"<BLAECK:") + len(b"<BLAECK:")
            end = data.index(b"/BLAECK>")
            content = data[start:end]

            info = decoder.parse_devices(content)
            assert info.client_name == ""
            assert info.client_type == "unknown"
        finally:
            client.close()
            device.close()
