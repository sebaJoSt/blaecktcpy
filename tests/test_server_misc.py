"""Misc tests for BlaeckTCPy server — easy-win coverage gaps."""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from blaecktcpy import BlaeckTCPy, IntervalMode, Signal, TimestampMode
from blaecktcpy._server import _IntervalTimer


# ---------------------------------------------------------------------------
# _IntervalTimer
# ---------------------------------------------------------------------------

class TestIntervalTimer:
    def test_interval_ms_property(self):
        t = _IntervalTimer()
        assert t.interval_ms == 0

    def test_elapsed_returns_true_when_zero_interval(self):
        t = _IntervalTimer()
        assert t.elapsed() is True

    def test_elapsed_first_tick(self):
        t = _IntervalTimer()
        t.activate(100)
        assert t.elapsed() is True  # first tick always True

    def test_elapsed_catches_up_after_sleep(self):
        t = _IntervalTimer()
        t.activate(1)  # 1 ms interval
        t.elapsed()  # consume first tick
        time.sleep(0.01)  # wait 10 ms >> 1 ms interval
        assert t.elapsed() is True  # lines 88-90: setpoint catch-up


# ---------------------------------------------------------------------------
# __repr__
# ---------------------------------------------------------------------------

class TestRepr:
    def test_repr_before_start(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     http_port=None,
                 )
        r = repr(server)
        assert "blaecktcpy" in r
        assert "0 clients" in r
        assert "inactive" in r
        assert "0 signals" in r
        server.close()

    def test_repr_after_start_with_signals(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     http_port=None,
                 )
        server.add_signal("x", "float", 0.0)
        server.start()
        r = repr(server)
        assert "1 signal" in r
        assert "0 clients" in r
        server.close()

    def test_repr_with_upstream(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     http_port=None,
                 )
        server._hub._upstreams.append(SimpleNamespace())
        r = repr(server)
        assert "1 upstreams" in r
        server._hub._upstreams.clear()
        server.close()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class TestContextManager:
    def test_with_statement(self):
        with BlaeckTCPy(
                 ip="127.0.0.1",
                 port=0,
                 device_name="Dev",
                 device_hw_version="HW",
                 device_fw_version="FW",
                 http_port=None,
             ) as server:
            server.start()
            assert server._started
        # After exiting, close() should have been called (server socket closed)
        assert server._tcp._sel is None or server._tcp._server_socket.fileno() == -1


# ---------------------------------------------------------------------------
# _find_free_port / _stdin_is_interactive
# ---------------------------------------------------------------------------

class TestFindFreePort:
    def test_finds_port(self):
        port = BlaeckTCPy._find_free_port("127.0.0.1", 49000)
        assert isinstance(port, int)
        assert port > 49000

    def test_stdin_is_interactive(self):
        result = BlaeckTCPy._stdin_is_interactive()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# add_signal edge cases
# ---------------------------------------------------------------------------

class TestAddSignalEdgeCases:
    def test_add_signal_bad_type(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     http_port=None,
                 )
        with pytest.raises(TypeError, match="Expected Signal or str"):
            server.add_signal(42)
        server.close()


# ---------------------------------------------------------------------------
# delete_signals before start
# ---------------------------------------------------------------------------

class TestDeleteSignalsBeforeStart:
    def test_clears_signal_list(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     http_port=None,
                 )
        server.add_signal("x", "float", 0.0)
        assert len(server.signals) == 1
        server.delete_signals()
        assert len(server.signals) == 0
        server.close()


# ---------------------------------------------------------------------------
# add_tcp / add_serial after start
# ---------------------------------------------------------------------------

class TestAddUpstreamAfterStart:
    def setup_method(self):
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="Dev",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=None,
                      )
        self.server.start()

    def teardown_method(self):
        self.server.close()

    def test_add_tcp_after_start_raises(self):
        with pytest.raises(RuntimeError, match="Cannot add upstreams after start"):
            self.server.add_tcp("10.0.0.1", 9325)

    def test_add_serial_after_start_raises(self):
        with pytest.raises(RuntimeError, match="Cannot add upstreams after start"):
            self.server.add_serial("COM3")


# ---------------------------------------------------------------------------
# start() twice
# ---------------------------------------------------------------------------

class TestStartTwice:
    def test_start_twice_raises(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     http_port=None,
                 )
        server.start()
        with pytest.raises(RuntimeError, match="Already started"):
            server.start()
        server.close()


# ---------------------------------------------------------------------------
# _decode_four_byte
# ---------------------------------------------------------------------------

class TestDecodeFourByte:
    def test_normal_decode(self):
        assert BlaeckTCPy._decode_four_byte(["232", "3", "0", "0"]) == 1000

    def test_with_invalid_param(self):
        result = BlaeckTCPy._decode_four_byte(["10", "abc", "0", "0"])
        assert result == 10  # only first byte counts, "abc" skipped

    def test_empty(self):
        assert BlaeckTCPy._decode_four_byte([]) == 0


# ---------------------------------------------------------------------------
# _resolve_timestamp edge cases
# ---------------------------------------------------------------------------

class TestResolveTimestamp:
    def setup_method(self):
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="Dev",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=None,
                      )
        self.server.add_signal("x", "float")
        self.server.start()

    def teardown_method(self):
        self.server.close()

    def test_unix_timestamp_type_error_bool(self):
        self.server._timestamp_mode = TimestampMode.UNIX
        with pytest.raises(TypeError, match="unix_timestamp must be float"):
            self.server._resolve_timestamp(True)

    def test_unix_timestamp_wrong_mode(self):
        self.server._timestamp_mode = TimestampMode.NONE
        with pytest.raises(ValueError, match="unix_timestamp can only be used"):
            self.server._resolve_timestamp(1234567890.0)


# ---------------------------------------------------------------------------
# on_before_write callback registration
# ---------------------------------------------------------------------------

class TestOnBeforeWrite:
    def test_registers_callback(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     http_port=None,
                 )

        @server.on_before_write()
        def refresh():
            pass

        assert server._before_write_callback is refresh
        server.close()


# ---------------------------------------------------------------------------
# Logger disabled when log_level=None
# ---------------------------------------------------------------------------

class TestLoggerDisabled:
    def test_logger_disabled_when_none(self):
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="Dev",
                     device_hw_version="HW",
                     device_fw_version="FW",
                     log_level=None,
                     http_port=None,
                 )
        assert server._logger.disabled is True
        server.close()
