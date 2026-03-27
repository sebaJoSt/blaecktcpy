"""Upstream transport connections for BlaeckHub.

Provides UpstreamTCP and UpstreamSerial classes that connect to
upstream BlaeckTCP(y)/BlaeckSerial devices as a client, reading
binary frames from their data stream.
"""

import logging
import socket
import time

logger = logging.getLogger("blaecktcpy")

_START_MARKER = b"<BLAECK:"
_END_MARKER = b"/BLAECK>"
_MAX_BUFFER = 1_048_576  # 1 MB buffer limit


class _UpstreamBase:
    """Base class with shared frame extraction and polling logic."""

    def __init__(self, name: str):
        self.name = name
        self._buffer = b""
        self._connected = False
        self._last_seen = 0.0
        self.last_error: str = ""

    # -- Subclass must implement --

    def connect(self, timeout: float = 5.0) -> bool:
        raise NotImplementedError

    def read_available(self) -> bytes:
        raise NotImplementedError

    def send(self, data: bytes) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

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
            logger.warning(f"Upstream '{self.name}' buffer overflow, clearing")
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

    def _handle_disconnect(self):
        logger.warning(f"Upstream '{self.name}' disconnected")
        self._cleanup()

    def _cleanup(self):
        self._connected = False


class UpstreamTCP(_UpstreamBase):
    """Non-blocking TCP client connection to an upstream BlaeckTCP(y) device."""

    def __init__(self, name: str, ip: str, port: int):
        super().__init__(name)
        self.ip = ip
        self.port = port
        self._socket: socket.socket | None = None

    def connect(self, timeout: float = 5.0) -> bool:
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._socket.settimeout(timeout)
            self._socket.connect((self.ip, self.port))
            self._socket.setblocking(False)
            self._connected = True
            self._last_seen = time.time()
            logger.info(f"Upstream '{self.name}' connected: {self.ip}:{self.port}")
            return True
        except OSError as e:
            self.last_error = str(e)
            logger.error(f"Upstream '{self.name}' connection failed: {e}")
            self._cleanup()
            return False

    def send(self, data: bytes) -> bool:
        if not self._connected or not self._socket:
            return False
        try:
            self._socket.sendall(data)
            return True
        except OSError as e:
            logger.debug(f"Upstream '{self.name}' send error: {e}")
            self._handle_disconnect()
            return False

    def read_available(self) -> bytes:
        if not self._connected or not self._socket:
            return b""
        try:
            data = self._socket.recv(65536)
            if not data:
                self._handle_disconnect()
                return b""
            self._last_seen = time.time()
            return data
        except BlockingIOError:
            return b""
        except OSError as e:
            logger.debug(f"Upstream '{self.name}' read error: {e}")
            self._handle_disconnect()
            return b""

    def _cleanup(self):
        super()._cleanup()
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def close(self):
        if self._connected:
            logger.info(f"Upstream '{self.name}' closing")
        self._cleanup()
        self._buffer = b""


class UpstreamSerial(_UpstreamBase):
    """Serial connection to an upstream BlaeckSerial device.

    Requires pyserial: ``pip install blaecktcpy[serial]``
    """

    def __init__(self, name: str, port: str, baudrate: int = 115200, dtr: bool = True):
        super().__init__(name)
        try:
            import serial as _serial  # noqa: F401
        except ImportError:
            raise ImportError(
                "pyserial is required for serial upstreams. "
                "Install with: pip install blaecktcpy[serial]"
            )
        self._serial_module = _serial
        self.port = port
        self.baudrate = baudrate
        self.dtr = dtr
        self._serial = None

    def connect(self, timeout: float = 5.0) -> bool:
        try:
            ser = self._serial_module.Serial()
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
            logger.info(
                f"Upstream '{self.name}' connected: {self.port} @ {self.baudrate}"
            )
            return True
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Upstream '{self.name}' serial connection failed: {e}")
            self._cleanup()
            return False

    def send(self, data: bytes) -> bool:
        if not self._connected or not self._serial:
            return False
        try:
            self._serial.write(data)
            self._serial.flush()
            logger.debug(f"Upstream '{self.name}' sent: {data}")
            return True
        except Exception as e:
            logger.debug(f"Upstream '{self.name}' send error: {e}")
            self._handle_disconnect()
            return False

    def read_available(self) -> bytes:
        if not self._connected or not self._serial:
            return b""
        try:
            waiting = self._serial.in_waiting
            if waiting > 0:
                data = self._serial.read(waiting)
                if data:
                    logger.debug(
                        f"Upstream '{self.name}' raw recv: {len(data)} bytes: {data[:80]}"
                    )
                    self._last_seen = time.time()
                    return data
            return b""
        except Exception as e:
            logger.debug(f"Upstream '{self.name}' read error: {e}")
            self._handle_disconnect()
            return b""

    def _cleanup(self):
        super()._cleanup()
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def close(self):
        if self._connected:
            logger.info(f"Upstream '{self.name}' closing")
        self._cleanup()
        self._buffer = b""
