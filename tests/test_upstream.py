"""Tests for hub/_upstream.py — Transport Protocol, base class, and UpstreamTCP."""

import socket
import threading
import time

import pytest

from blaecktcpy.hub._upstream import (
    Transport,
    UpstreamTCP,
    _MAX_BUFFER,
    _UpstreamBase,
)


# ── In-memory transport for testing base class logic ────────────────


class MemoryTransport(_UpstreamBase):
    """Controllable transport for testing _UpstreamBase logic."""

    def __init__(self, name: str = "mem"):
        super().__init__(name)
        self._inbox = b""
        self._outbox = b""
        self._fail_connect = False
        self._fail_send = False

    def connect(self, timeout: float = 5.0) -> bool:
        if self._fail_connect:
            self.last_error = "refused"
            return False
        self._connected = True
        self._last_seen = time.time()
        return True

    def read_available(self) -> bytes:
        data = self._inbox
        self._inbox = b""
        return data

    def send(self, data: bytes) -> bool:
        if not self._connected:
            return False
        if self._fail_send:
            self._handle_disconnect()
            return False
        self._outbox += data
        return True

    def inject(self, data: bytes) -> None:
        """Inject data to be 'received' on the next read_available()."""
        self._inbox += data


# ── Helper: loopback TCP server ─────────────────────────────────────


class LoopbackServer:
    """Single-connection TCP server for testing UpstreamTCP."""

    def __init__(self) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port: int = self.server.getsockname()[1]
        self.client: socket.socket | None = None

    def accept(self, timeout: float = 2.0) -> None:
        self.server.settimeout(timeout)
        self.client, _ = self.server.accept()

    def send(self, data: bytes) -> None:
        assert self.client is not None
        self.client.sendall(data)

    def recv(self, size: int = 4096) -> bytes:
        assert self.client is not None
        return self.client.recv(size)

    def close_client(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def close(self) -> None:
        self.close_client()
        self.server.close()


@pytest.fixture()
def loopback():
    srv = LoopbackServer()
    yield srv
    srv.close()


# ═══════════════════════════════════════════════════════════════════
#  Transport Protocol
# ═══════════════════════════════════════════════════════════════════


class TestTransportProtocol:
    """Verify the Transport Protocol structural typing."""

    def test_upstream_base_satisfies_protocol(self):
        mt = MemoryTransport()
        assert isinstance(mt, Transport)

    def test_upstream_tcp_satisfies_protocol(self):
        tcp = UpstreamTCP("t", "127.0.0.1", 9999)
        assert isinstance(tcp, Transport)


# ═══════════════════════════════════════════════════════════════════
#  _UpstreamBase via MemoryTransport
# ═══════════════════════════════════════════════════════════════════


class TestBaseInitialState:
    def test_name(self):
        assert MemoryTransport("foo").name == "foo"

    def test_disconnected(self):
        assert not MemoryTransport().connected

    def test_no_connect_pending(self):
        assert not MemoryTransport().connect_pending

    def test_last_seen_zero(self):
        assert MemoryTransport().last_seen == 0.0

    def test_last_error_empty(self):
        assert MemoryTransport().last_error == ""

    def test_cannot_instantiate_abc_directly(self):
        with pytest.raises(TypeError, match="abstract method"):
            _UpstreamBase("x")  # type: ignore[abstract]


class TestBaseConnect:
    def test_connect_success(self):
        mt = MemoryTransport()
        assert mt.connect()
        assert mt.connected
        assert mt.last_seen > 0

    def test_connect_failure(self):
        mt = MemoryTransport()
        mt._fail_connect = True
        assert not mt.connect()
        assert not mt.connected
        assert mt.last_error == "refused"


class TestBaseStartConnect:
    def test_default_falls_back_to_blocking(self):
        mt = MemoryTransport()
        mt.start_connect()
        assert mt.connected
        assert not mt.connect_pending


class TestBaseCheckConnect:
    def test_returns_true_when_connected(self):
        mt = MemoryTransport()
        mt.connect()
        assert mt.check_connect() is True

    def test_returns_false_when_disconnected(self):
        mt = MemoryTransport()
        assert mt.check_connect() is False


class TestBaseSend:
    def test_send_when_disconnected(self):
        mt = MemoryTransport()
        assert not mt.send(b"hello")

    def test_send_when_connected(self):
        mt = MemoryTransport()
        mt.connect()
        assert mt.send(b"hello")
        assert mt._outbox == b"hello"

    def test_send_failure_disconnects(self):
        mt = MemoryTransport()
        mt.connect()
        mt._fail_send = True
        assert not mt.send(b"x")
        assert not mt.connected


class TestBaseSendCommand:
    def test_wraps_command(self):
        mt = MemoryTransport()
        mt.connect()
        mt.send_command("BLAECK.WRITE_SYMBOLS")
        assert mt._outbox == b"<BLAECK.WRITE_SYMBOLS>"

    def test_returns_false_when_disconnected(self):
        mt = MemoryTransport()
        assert not mt.send_command("X")


class TestBaseClose:
    def test_close_resets_state(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(b"data")
        mt.read_frames()  # load buffer
        mt.close()
        assert not mt.connected
        assert not mt.connect_pending

    def test_close_when_already_disconnected(self):
        mt = MemoryTransport()
        mt.close()  # should not raise


class TestBaseHandleDisconnect:
    def test_resets_state(self):
        mt = MemoryTransport()
        mt.connect()
        mt._handle_disconnect()
        assert not mt.connected


# ═══════════════════════════════════════════════════════════════════
#  Frame extraction (read_frames)
# ═══════════════════════════════════════════════════════════════════


def _frame(content: bytes) -> bytes:
    """Build a complete BlaeckTCP frame."""
    return b"<BLAECK:" + content + b"/BLAECK>"


class TestReadFrames:
    def test_no_data_returns_empty(self):
        mt = MemoryTransport()
        mt.connect()
        assert mt.read_frames() == []

    def test_single_frame(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(_frame(b"\x01\x02"))
        assert mt.read_frames() == [b"\x01\x02"]

    def test_multiple_frames(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(_frame(b"A") + _frame(b"B") + _frame(b"C"))
        frames = mt.read_frames()
        assert frames == [b"A", b"B", b"C"]

    def test_partial_frame_kept_in_buffer(self):
        mt = MemoryTransport()
        mt.connect()
        # Inject start but no end
        mt.inject(b"<BLAECK:partial")
        assert mt.read_frames() == []
        # Complete the frame
        mt.inject(b"/BLAECK>")
        assert mt.read_frames() == [b"partial"]

    def test_split_start_marker(self):
        """Partial start marker at end of chunk is preserved."""
        mt = MemoryTransport()
        mt.connect()
        mt.inject(b"<BLAECK")
        assert mt.read_frames() == []
        mt.inject(b":" + b"data" + b"/BLAECK>")
        assert mt.read_frames() == [b"data"]

    def test_crlf_after_end_marker(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(_frame(b"X") + b"\r\n" + _frame(b"Y"))
        frames = mt.read_frames()
        assert frames == [b"X", b"Y"]

    def test_lf_only_after_end_marker(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(_frame(b"X") + b"\n" + _frame(b"Y"))
        frames = mt.read_frames()
        assert frames == [b"X", b"Y"]

    def test_cr_only_after_end_marker(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(_frame(b"X") + b"\r" + _frame(b"Y"))
        frames = mt.read_frames()
        assert frames == [b"X", b"Y"]

    def test_garbage_before_frame(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(b"junk" + _frame(b"ok"))
        assert mt.read_frames() == [b"ok"]

    def test_empty_frame(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(_frame(b""))
        assert mt.read_frames() == [b""]

    def test_buffer_overflow_clears(self):
        mt = MemoryTransport()
        mt.connect()
        mt.inject(b"X" * (_MAX_BUFFER + 1))
        assert mt.read_frames() == []
        # Buffer should be cleared
        mt.inject(_frame(b"after"))
        assert mt.read_frames() == [b"after"]

    def test_incremental_accumulation(self):
        """Multiple reads accumulate in buffer until frame complete."""
        mt = MemoryTransport()
        mt.connect()
        mt.inject(b"<BLAECK:")
        assert mt.read_frames() == []
        mt.inject(b"da")
        assert mt.read_frames() == []
        mt.inject(b"ta/BLAECK>")
        assert mt.read_frames() == [b"data"]

    def test_frame_with_binary_content(self):
        mt = MemoryTransport()
        mt.connect()
        payload = bytes(range(256))
        mt.inject(_frame(payload))
        assert mt.read_frames() == [payload]


# ═══════════════════════════════════════════════════════════════════
#  UpstreamTCP — real loopback
# ═══════════════════════════════════════════════════════════════════


class TestTCPConnect:
    def test_connect_success(self, loopback: LoopbackServer):
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        assert tcp.connect(timeout=2.0)
        assert tcp.connected
        assert tcp.last_seen > 0
        tcp.close()
        t.join()

    def test_connect_refused(self):
        tcp = UpstreamTCP("test", "127.0.0.1", 1)  # port 1 — should fail
        assert not tcp.connect(timeout=0.5)
        assert not tcp.connected
        assert tcp.last_error != ""

    def test_close_resets(self, loopback: LoopbackServer):
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.connect(timeout=2.0)
        tcp.close()
        assert not tcp.connected
        assert tcp._socket is None
        t.join()


class TestTCPSendRecv:
    def test_send_and_recv(self, loopback: LoopbackServer):
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.connect(timeout=2.0)
        t.join()

        # Send from client → server
        assert tcp.send(b"hello")
        got = loopback.recv()
        assert got == b"hello"

        # Send from server → client
        loopback.send(_frame(b"\x42"))
        time.sleep(0.05)
        frames = tcp.read_frames()
        assert frames == [b"\x42"]

        tcp.close()

    def test_send_when_disconnected(self):
        tcp = UpstreamTCP("test", "127.0.0.1", 9999)
        assert not tcp.send(b"x")

    def test_read_when_disconnected(self):
        tcp = UpstreamTCP("test", "127.0.0.1", 9999)
        assert tcp.read_available() == b""


class TestTCPDisconnectDetection:
    def test_server_close_detected(self, loopback: LoopbackServer):
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.connect(timeout=2.0)
        t.join()

        loopback.close_client()
        time.sleep(0.05)
        tcp.read_available()
        assert not tcp.connected

    def test_send_after_server_close(self, loopback: LoopbackServer):
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.connect(timeout=2.0)
        t.join()

        loopback.close_client()
        time.sleep(0.05)
        tcp.read_available()  # triggers disconnect
        assert not tcp.send(b"x")


class TestTCPNonBlockingConnect:
    def test_start_connect_immediate(self, loopback: LoopbackServer):
        """start_connect to a listening port may connect immediately."""
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.start_connect(timeout=2.0)
        # May already be connected, or may be pending
        for _ in range(20):
            result = tcp.check_connect()
            if result is True:
                break
            time.sleep(0.05)
        assert tcp.connected
        tcp.close()
        t.join()

    def test_check_connect_timeout(self):
        """Connect to a non-listening address should time out."""
        # Use an address that won't respond (RFC 5737 TEST-NET)
        tcp = UpstreamTCP("test", "192.0.2.1", 9999)
        tcp.start_connect(timeout=0.1)
        if tcp.connect_pending:
            time.sleep(0.2)
            result = tcp.check_connect()
            assert result is False
            assert not tcp.connected

    def test_check_connect_no_pending(self):
        tcp = UpstreamTCP("test", "127.0.0.1", 9999)
        assert tcp.check_connect() is False

    def test_check_connect_returns_true_if_already_connected(self, loopback: LoopbackServer):
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.connect()
        assert tcp.check_connect() is True
        tcp.close()
        t.join()


class TestTCPCleanup:
    def test_cleanup_closes_socket(self, loopback: LoopbackServer):
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.connect(timeout=2.0)
        t.join()

        sock = tcp._socket
        assert sock is not None
        tcp._cleanup()
        assert tcp._socket is None
        assert not tcp.connected

    def test_cleanup_when_no_socket(self):
        tcp = UpstreamTCP("test", "127.0.0.1", 9999)
        tcp._cleanup()  # should not raise


class TestTCPReadBlocking:
    def test_read_no_data_available(self, loopback: LoopbackServer):
        """Non-blocking recv with no data returns empty bytes."""
        tcp = UpstreamTCP("test", "127.0.0.1", loopback.port)
        t = threading.Thread(target=loopback.accept)
        t.start()
        tcp.connect(timeout=2.0)
        t.join()

        # Nothing sent, should get empty
        data = tcp.read_available()
        assert data == b""
        tcp.close()


# ═══════════════════════════════════════════════════════════════════
#  UpstreamSerial — constructor & import check
# ═══════════════════════════════════════════════════════════════════


class TestSerialImport:
    def test_constructor_with_pyserial(self):
        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", baudrate=9600, dtr=False)
        assert ser.name == "test"
        assert ser.port == "COM1"
        assert ser.baudrate == 9600
        assert ser.dtr is False
        assert not ser.connected

    def test_send_when_disconnected(self):
        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1")
        assert not ser.send(b"x")

    def test_read_when_disconnected(self):
        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1")
        assert ser.read_available() == b""

    def test_cleanup_no_serial(self):
        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1")
        ser._cleanup()  # should not raise


class TestSerialMocked:
    """Test serial connect/send/read with a mocked pyserial.Serial."""

    def test_connect_success(self):
        from unittest.mock import MagicMock, patch

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        with patch("blaecktcpy.hub._upstream._pyserial") as mock_mod:
            mock_mod.Serial.return_value = mock_serial
            assert ser.connect(timeout=1.0)
        assert ser.connected
        ser.close()

    def test_connect_dtr_true(self):
        from unittest.mock import MagicMock, patch

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=True)
        mock_serial = MagicMock()
        with patch("blaecktcpy.hub._upstream._pyserial") as mock_mod, \
             patch("blaecktcpy.hub._upstream.time.sleep"):
            mock_mod.Serial.return_value = mock_serial
            assert ser.connect(timeout=1.0)
        assert ser.connected
        ser.close()

    def test_connect_failure(self):
        from unittest.mock import patch

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        with patch("blaecktcpy.hub._upstream._pyserial") as mock_mod:
            mock_mod.Serial.side_effect = OSError("port busy")
            assert not ser.connect(timeout=1.0)
        assert not ser.connected
        assert "port busy" in ser.last_error

    def test_send_success(self):
        from unittest.mock import MagicMock

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        ser._serial = mock_serial
        ser._connected = True
        assert ser.send(b"hello")
        mock_serial.write.assert_called_once_with(b"hello")
        mock_serial.flush.assert_called_once()

    def test_send_error_disconnects(self):
        from unittest.mock import MagicMock

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        mock_serial.write.side_effect = OSError("write error")
        ser._serial = mock_serial
        ser._connected = True
        assert not ser.send(b"hello")
        assert not ser.connected

    def test_read_success(self):
        from unittest.mock import MagicMock

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        mock_serial.in_waiting = 5
        mock_serial.read.return_value = b"hello"
        ser._serial = mock_serial
        ser._connected = True
        assert ser.read_available() == b"hello"

    def test_read_nothing_waiting(self):
        from unittest.mock import MagicMock

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        mock_serial.in_waiting = 0
        ser._serial = mock_serial
        ser._connected = True
        assert ser.read_available() == b""

    def test_read_error_disconnects(self):
        from unittest.mock import MagicMock, PropertyMock

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        type(mock_serial).in_waiting = PropertyMock(side_effect=OSError("read error"))
        ser._serial = mock_serial
        ser._connected = True
        assert ser.read_available() == b""
        assert not ser.connected

    def test_cleanup_closes_serial(self):
        from unittest.mock import MagicMock

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        ser._serial = mock_serial
        ser._connected = True
        ser._cleanup()
        mock_serial.close.assert_called_once()
        assert ser._serial is None

    def test_cleanup_close_error(self):
        from unittest.mock import MagicMock

        from blaecktcpy.hub._upstream import UpstreamSerial

        ser = UpstreamSerial("test", "COM1", dtr=False)
        mock_serial = MagicMock()
        mock_serial.close.side_effect = OSError("close failed")
        ser._serial = mock_serial
        ser._connected = True
        ser._cleanup()  # should not raise
        assert ser._serial is None
