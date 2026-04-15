"""Shared test helpers and fixtures for blaecktcpy tests."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from blaecktcpy import BlaeckTCPy  # noqa: E402
from blaecktcpy._server import UpstreamDevice  # noqa: E402
from blaecktcpy.hub._upstream import _UpstreamBase  # noqa: E402


def _make_server_on_free_port():
    """Create an unstarted BlaeckTCPy that will bind to a free port on start()."""
    return BlaeckTCPy(
               ip="127.0.0.1",
               port=0,
               device_name="Test",
               device_hw_version="HW",
               device_fw_version="1.0",
           )


def _start_retry(device, attempts=1):
    """Start device; after start, update _port from the actual bound address."""
    device.start()
    # Port 0 means OS picks a free port — read the actual port after bind
    device._port = device._tcp._server_socket.getsockname()[1]


class FakeTransport(_UpstreamBase):
    """Minimal transport for testing without real connections."""

    def __init__(self, name="fake"):
        super().__init__(name)
        self._connected = True
        self._pending_frames: list[bytes] = []
        self._connect_result: bool | None = None

    def connect(self, timeout=5.0):
        self._connected = True
        return True

    def start_connect(self, timeout=5.0):
        self._connect_pending = True
        self._connect_result = None

    def check_connect(self):
        if self._connected and not self._connect_pending:
            return True
        if not self._connect_pending:
            return False
        if self._connect_result is not None:
            self._connect_pending = False
            if self._connect_result:
                self._connected = True
                return True
            self._connect_result = None
            return False
        return None  # still pending

    def complete_connect(self, success=True):
        """Test helper: resolve a pending async connect on next check."""
        self._connect_result = success

    def inject_frame(self, content: bytes):
        """Test helper: queue a raw frame for read_frames() to return."""
        self._pending_frames.append(content)

    def read_available(self):
        if self._pending_frames:
            frame = self._pending_frames.pop(0)
            return b"<BLAECK:" + frame + b"/BLAECK>"
        return b""

    def send(self, data):
        return True

    def close(self):
        self._connected = False
        self._connect_pending = False
        self._connect_result = None


class RecordingTransport(_UpstreamBase):
    """Transport that records all sent data for verification."""

    def __init__(self, name="recording"):
        super().__init__(name)
        self._connected = True
        self.sent: list[bytes] = []
        self._pending_frames: list[bytes] = []
        self._connect_result: bool | None = None

    def connect(self, timeout=5.0):
        self._connected = True
        return True

    def start_connect(self, timeout=5.0):
        self._connect_pending = True
        self._connect_result = None

    def check_connect(self):
        if self._connected and not self._connect_pending:
            return True
        if not self._connect_pending:
            return False
        if self._connect_result is not None:
            self._connect_pending = False
            if self._connect_result:
                self._connected = True
                return True
            self._connect_result = None
            return False
        return None  # still pending

    def complete_connect(self, success=True):
        """Test helper: resolve a pending async connect on next check."""
        self._connect_result = success

    def inject_frame(self, content: bytes):
        """Test helper: queue a raw frame for read_frames() to return."""
        self._pending_frames.append(content)

    def read_available(self):
        if self._pending_frames:
            frame = self._pending_frames.pop(0)
            return b"<BLAECK:" + frame + b"/BLAECK>"
        return b""

    def send(self, data):
        self.sent.append(data)
        return True

    def close(self):
        self._connected = False
        self._connect_pending = False
        self._connect_result = None

