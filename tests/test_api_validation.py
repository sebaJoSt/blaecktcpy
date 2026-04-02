"""Tests for API parameter validation: bool coercion strictness."""

import pytest

from conftest import _make_server_on_free_port


class TestBoolCoercionStrictness:
    """Public APIs reject non-bool values for bool parameters."""

    def test_add_tcp_relay_downstream_rejects_int(self):
        device = _make_server_on_free_port()
        with pytest.raises(TypeError, match="relay_downstream must be True or False"):
            device.add_tcp("127.0.0.1", 9999, relay_downstream=1)

    def test_add_tcp_relay_downstream_rejects_string(self):
        device = _make_server_on_free_port()
        with pytest.raises(TypeError, match="relay_downstream must be True or False"):
            device.add_tcp("127.0.0.1", 9999, relay_downstream="yes")

    def test_add_tcp_forward_custom_commands_rejects_int(self):
        device = _make_server_on_free_port()
        with pytest.raises(TypeError, match="forward_custom_commands must be True, False, or a list"):
            device.add_tcp("127.0.0.1", 9999, forward_custom_commands=1)

    def test_add_serial_relay_downstream_rejects_int(self):
        device = _make_server_on_free_port()
        with pytest.raises(TypeError, match="relay_downstream must be True or False"):
            device.add_serial("COM99", relay_downstream=0)

    def test_add_serial_forward_custom_commands_rejects_int(self):
        device = _make_server_on_free_port()
        with pytest.raises(TypeError, match="forward_custom_commands must be True, False, or a list"):
            device.add_serial("COM99", forward_custom_commands=1)

    def test_add_tcp_accepts_true_false(self):
        """Sanity: actual booleans don't raise (connection will fail but that's OK)."""
        device = _make_server_on_free_port()
        # These should reach the transport layer (and fail there), not TypeError
        with pytest.raises(Exception, match="(?!relay_downstream|forward_custom_commands)"):
            device.add_tcp("127.0.0.1", 1, relay_downstream=True, forward_custom_commands=False, timeout=0.1)
