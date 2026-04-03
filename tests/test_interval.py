"""Tests for BlaeckTCPy.local_interval_ms property locked/unlocked behaviour."""

import socket
import time

import pytest

from blaecktcpy import IntervalMode, BlaeckTCPy
from conftest import _make_server_on_free_port, _start_retry


class TestServerIntervalProperty:
    """Verify BlaeckTCPy.local_interval_ms property locked/unlocked behaviour."""

    def _make_server_with_client(self):
        """Return (device, client_socket) with one connected client."""
        device = _make_server_on_free_port()
        device.add_signal("temp", "float", 0.0)
        _start_retry(device)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(("127.0.0.1", device._port))
        device._accept_new_clients()
        return device, client

    def test_interval_ms_activates_timed_data(self):
        """interval_ms = >0 should activate timed data immediately."""
        server, client = self._make_server_with_client()
        try:
            assert not server._timed_activated
            server.local_interval_ms = 500
            assert server._timed_activated
            assert server.local_interval_ms == 500
        finally:
            client.close()
            server.close()

    def test_interval_ms_zero_locks_at_zero(self):
        """interval_ms = 0 should lock at 0ms (fastest possible)."""
        server, client = self._make_server_with_client()
        try:
            server.local_interval_ms = 0
            assert server.local_interval_ms == 0
            assert server._timed_activated
        finally:
            client.close()
            server.close()

    def test_interval_client_releases_lock(self):
        """interval_ms = IntervalMode.CLIENT should release the lock."""
        server, client = self._make_server_with_client()
        try:
            server.local_interval_ms = 500
            assert server.local_interval_ms == 500
            server.local_interval_ms = IntervalMode.CLIENT
            assert server.local_interval_ms == IntervalMode.CLIENT
        finally:
            client.close()
            server.close()

    def test_interval_off_deactivates(self):
        """interval_ms = IntervalMode.OFF should deactivate timed data."""
        server, client = self._make_server_with_client()
        try:
            server.local_interval_ms = 500
            assert server._timed_activated
            server.local_interval_ms = IntervalMode.OFF
            assert not server._timed_activated
            assert server.local_interval_ms == IntervalMode.OFF
        finally:
            client.close()
            server.close()

    def test_locked_ignores_client_activate(self):
        """When locked, client ACTIVATE command should be ignored."""
        server, client = self._make_server_with_client()
        try:
            server.local_interval_ms = 500
            # Send ACTIVATE command from client with different interval
            activate_cmd = "<BLAECK.ACTIVATE,208,7,0,0>"
            client.sendall(activate_cmd.encode())
            time.sleep(0.05)
            server.read()
            # Lock still active, interval unchanged
            assert server.local_interval_ms == 500
            assert server._timed_activated
        finally:
            client.close()
            server.close()

    def test_locked_ignores_client_deactivate(self):
        """When locked, client DEACTIVATE should be ignored."""
        server, client = self._make_server_with_client()
        try:
            server.local_interval_ms = 500
            assert server._timed_activated
            # Send DEACTIVATE from client
            deactivate_cmd = "<BLAECK.DEACTIVATE>"
            client.sendall(deactivate_cmd.encode())
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
            time.sleep(0.05)
            server.read()
            assert server._timed_activated
        finally:
            client.close()
            server.close()

    def test_locked_timed_write_works_after_reconnect(self):
        """After client disconnect+reconnect, locked interval keeps working."""
        server, client = self._make_server_with_client()
        try:
            server.local_interval_ms = 50
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
