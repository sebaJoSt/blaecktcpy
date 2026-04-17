"""Upstream transport connections for BlaeckTCPy hub mode.

Provides UpstreamTCP and UpstreamSerial classes that connect to
upstream BlaeckTCP(y)/BlaeckSerial devices as a client, reading
binary frames from their data stream.
"""

import errno
import logging
import select
import socket
import time
from abc import ABC, abstractmethod
from typing import Any, Protocol, override, runtime_checkable

# Windows socket error codes
_WSAEWOULDBLOCK = 10035
_WSAECONNREFUSED = 10036

_UPSTREAM_RECV_BUFFER = 65536  # 64 KB per upstream read

try:
    import serial as _pyserial
except ImportError:
    _pyserial = None

_START_MARKER = b"<BLAECK:"
_END_MARKER = b"/BLAECK>"
_MAX_BUFFER = 1_048_576  # 1 MB buffer limit


@runtime_checkable
class Transport(Protocol):
    """Interface for upstream transport connections.

    Any object satisfying this protocol can be used as an upstream
    transport in :class:`~blaecktcpy.BlaeckTCPy` hub mode.  The
    built-in implementations are :class:`UpstreamTCP` and
    :class:`UpstreamSerial`; custom transports (e.g. for testing)
    need only implement these methods.
    """

    name: str
    last_error: str

    @property
    def connected(self) -> bool: ...

    @property
    def connect_pending(self) -> bool: ...

    @property
    def last_seen(self) -> float: ...

    def connect(self, timeout: float = 5.0) -> bool: ...
    def start_connect(self, timeout: float = 5.0) -> None: ...
    def check_connect(self) -> bool | None: ...
    def read_available(self) -> bytes: ...
    def send(self, data: bytes) -> bool: ...
    def send_command(self, command: str) -> bool: ...
    def read_frames(self) -> list[bytes]: ...
    def close(self) -> None: ...


class _UpstreamBase(ABC):
    """Base class with shared frame extraction and polling logic.

    Subclasses must implement :meth:`connect`, :meth:`read_available`,
    and :meth:`send`.  Everything else (frame extraction, command
    wrapping, connect lifecycle) is provided by this base class.
    """

    def __init__(self, name: str, logger: logging.Logger | None = None):
        self.name: str = name
        self._logger: logging.Logger = logger or logging.getLogger("blaecktcpy")
        self._buffer: bytes = b""
        self._connected: bool = False
        self._connect_pending: bool = False
        self._last_seen: float = 0.0
        self.last_error: str = ""

    # -- Subclass must implement --

    @abstractmethod
    def connect(self, timeout: float = 5.0) -> bool: ...

    @abstractmethod
    def read_available(self) -> bytes: ...

    @abstractmethod
    def send(self, data: bytes) -> bool: ...

    def close(self) -> None:
        if self._connected:
            self._logger.info(f"Upstream '{self.name}' closing")
        self._cleanup()

    # -- Non-blocking connect (override in subclass for true async) --

    def start_connect(self, timeout: float = 5.0) -> None:
        """Initiate a non-blocking connect.

        Default implementation falls back to blocking :meth:`connect`.
        Subclasses (e.g. UpstreamTCP) override for true async.
        """
        self.connect(timeout)
        self._connect_pending = False

    def check_connect(self) -> bool | None:
        """Check if a pending connect has completed.

        Returns:
            True if connected, False if failed, None if still pending.
        """
        if self._connected:
            return True
        return False

    @property
    def connect_pending(self) -> bool:
        return self._connect_pending

    # -- Shared API --

    def send_command(self, command: str) -> bool:
        """Send a text command (e.g. 'BLAECK.WRITE_SYMBOLS')."""
        return self.send(f"<{command}>".encode())

    def read_frames(self) -> list[bytes]:
        """Read available data and extract complete BlaeckTCP frames.

        Returns list of frame contents (bytes between markers).
        Incomplete frames are kept in buffer for the next call.
        """
        chunk = self.read_available()
        if chunk:
            self._buffer += chunk

        if len(self._buffer) > _MAX_BUFFER:
            self._logger.warning(f"Upstream '{self.name}' buffer overflow, clearing")
            self._buffer = b""
            return []

        if not self._buffer:
            return []

        frames = []
        while True:
            start = self._buffer.find(_START_MARKER)
            if start == -1:
                # Keep tail bytes that could be the start of a split marker
                # (e.g. b"<BLAECK" without the trailing ":")
                tail_keep = len(_START_MARKER) - 1
                if len(self._buffer) >= tail_keep:
                    self._buffer = self._buffer[-tail_keep:]
                break
            end = self._buffer.find(_END_MARKER, start + len(_START_MARKER))
            if end == -1:
                self._buffer = self._buffer[start:]
                break
            content = self._buffer[start + len(_START_MARKER) : end]
            frames.append(content)
            skip = end + len(_END_MARKER)
            if skip < len(self._buffer) and self._buffer[skip : skip + 1] == b"\r":
                skip += 1
            if skip < len(self._buffer) and self._buffer[skip : skip + 1] == b"\n":
                skip += 1
            self._buffer = self._buffer[skip:]
        return frames

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_seen(self) -> float:
        return self._last_seen

    def _handle_disconnect(self) -> None:
        self._logger.warning(f"Upstream '{self.name}' disconnected")
        self._cleanup()

    def _cleanup(self) -> None:
        self._connected = False
        self._connect_pending = False
        self._buffer = b""


class UpstreamTCP(_UpstreamBase):
    """Non-blocking TCP client connection to an upstream BlaeckTCP(y) device."""

    def __init__(
        self, name: str, ip: str, port: int, logger: logging.Logger | None = None
    ):
        super().__init__(name, logger)
        self.ip: str = ip
        self.port: int = port
        self._socket: socket.socket | None = None
        self._connect_deadline: float = 0.0

    @override
    def connect(self, timeout: float = 5.0) -> bool:
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._socket.settimeout(timeout)
            self._socket.connect((self.ip, self.port))
            self._socket.setblocking(False)
            self._connected = True
            self._last_seen = time.time()
            self._logger.info(
                f"Upstream '{self.name}' connected: {self.ip}:{self.port}"
            )
            return True
        except OSError as e:
            self.last_error = str(e)
            self._logger.debug(f"Upstream '{self.name}' connection failed: {e}")
            self._cleanup()
            return False

    @override
    def start_connect(self, timeout: float = 5.0) -> None:
        """Initiate a non-blocking TCP connect."""
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._socket.setblocking(False)
            err = self._socket.connect_ex((self.ip, self.port))
            if err == 0:
                self._connected = True
                self._connect_pending = False
                self._last_seen = time.time()
                self._logger.info(
                    f"Upstream '{self.name}' connected: {self.ip}:{self.port}"
                )
            elif err in (
                errno.EINPROGRESS,
                errno.EWOULDBLOCK,
                _WSAEWOULDBLOCK,
                _WSAECONNREFUSED,
            ):
                self._connect_pending = True
                self._connect_deadline = time.time() + timeout
            else:
                raise OSError(err, f"connect_ex returned {err}")
        except OSError as e:
            self.last_error = str(e)
            self._logger.debug(f"Upstream '{self.name}' async connect failed: {e}")
            self._cleanup()

    @override
    def check_connect(self) -> bool | None:
        """Check if a pending non-blocking connect has completed."""
        if self._connected:
            return True
        if not self._connect_pending or not self._socket:
            return False
        # Timeout check
        if time.time() > self._connect_deadline:
            self.last_error = "connect timeout"
            self._logger.debug(f"Upstream '{self.name}' async connect timed out")
            self._cleanup()
            return False
        try:
            _, writable, errored = select.select([], [self._socket], [self._socket], 0)
            if errored:
                err = self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                self.last_error = f"SO_ERROR={err}"
                self._logger.debug(f"Upstream '{self.name}' async connect error: {err}")
                self._cleanup()
                return False
            if writable:
                err = self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err == 0:
                    self._connected = True
                    self._connect_pending = False
                    self._last_seen = time.time()
                    self._logger.info(
                        f"Upstream '{self.name}' connected: " f"{self.ip}:{self.port}"
                    )
                    return True
                self.last_error = f"SO_ERROR={err}"
                self._logger.debug(
                    f"Upstream '{self.name}' async connect failed: " f"SO_ERROR={err}"
                )
                self._cleanup()
                return False
            return None  # still pending
        except OSError as e:
            self.last_error = str(e)
            self._logger.debug(
                f"Upstream '{self.name}' async connect check failed: {e}"
            )
            self._cleanup()
            return False

    @override
    def send(self, data: bytes) -> bool:
        if not self._connected or not self._socket:
            return False
        try:
            self._socket.sendall(data)
            return True
        except OSError as e:
            self._logger.debug(f"Upstream '{self.name}' send error: {e}")
            self._handle_disconnect()
            return False

    @override
    def read_available(self) -> bytes:
        if not self._connected or not self._socket:
            return b""
        try:
            data = self._socket.recv(_UPSTREAM_RECV_BUFFER)
            if not data:
                self._handle_disconnect()
                return b""
            self._last_seen = time.time()
            return data
        except BlockingIOError:
            return b""
        except OSError as e:
            self._logger.debug(f"Upstream '{self.name}' read error: {e}")
            self._handle_disconnect()
            return b""

    @override
    def _cleanup(self) -> None:
        super()._cleanup()
        if self._socket:
            try:
                self._socket.close()
            except OSError as e:
                self._logger.debug(f"Upstream '{self.name}' socket close error: {e}")
            self._socket = None


class UpstreamSerial(_UpstreamBase):
    """Serial connection to an upstream BlaeckSerial device.

    Requires pyserial: ``pip install blaecktcpy[serial]``
    """

    def __init__(
        self,
        name: str,
        port: str,
        baudrate: int = 115200,
        dtr: bool = True,
        logger: logging.Logger | None = None,
    ):
        super().__init__(name, logger)
        if _pyserial is None:
            raise ImportError(
                "pyserial is required for serial upstreams. "
                "Install with: pip install blaecktcpy[serial]"
            )
        self.port: str = port
        self.baudrate: int = baudrate
        self.dtr: bool = dtr
        self._serial: Any = None

    @override
    def connect(self, timeout: float = 5.0) -> bool:
        try:
            assert _pyserial is not None
            ser = _pyserial.Serial()
            ser.port = self.port
            ser.baudrate = self.baudrate
            ser.timeout = 0
            ser.dtr = self.dtr
            ser.open()
            self._serial = ser
            if self.dtr:
                # Wait for device to boot (Arduino resets on DTR)
                time.sleep(2)
            else:
                # Short delay for serial line to stabilize
                time.sleep(0.1)
            # Drain any stale data from previous session
            self._serial.reset_input_buffer()
            self._connected = True
            self._last_seen = time.time()
            self._logger.info(
                f"Upstream '{self.name}' connected: {self.port} @ {self.baudrate}"
            )
            return True
        except Exception as e:
            self.last_error = str(e)
            self._logger.debug(f"Upstream '{self.name}' serial connection failed: {e}")
            self._cleanup()
            return False

    @override
    def send(self, data: bytes) -> bool:
        if not self._connected or not self._serial:
            return False
        try:
            self._serial.write(data)
            self._serial.flush()
            self._logger.debug(f"Upstream '{self.name}' sent: {data}")
            return True
        except Exception as e:
            self._logger.debug(f"Upstream '{self.name}' send error: {e}")
            self._handle_disconnect()
            return False

    @override
    def read_available(self) -> bytes:
        if not self._connected or not self._serial:
            return b""
        try:
            waiting = self._serial.in_waiting
            if waiting > 0:
                data = self._serial.read(waiting)
                if data:
                    self._logger.debug(
                        f"Upstream '{self.name}' raw recv: {len(data)} bytes: {data[:80]}"
                    )
                    self._last_seen = time.time()
                    return data
            return b""
        except Exception as e:
            self._logger.debug(f"Upstream '{self.name}' read error: {e}")
            self._handle_disconnect()
            return b""

    @override
    def _cleanup(self) -> None:
        super()._cleanup()
        if self._serial:
            try:
                self._serial.close()
            except Exception as e:
                self._logger.debug(f"Upstream '{self.name}' serial close error: {e}")
            self._serial = None
