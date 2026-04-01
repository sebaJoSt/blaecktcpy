"""Shared test helpers and fixtures for blaecktcpy tests."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from blaecktcpy import BlaeckTCPy  # noqa: E402
from blaecktcpy._server import _UpstreamDevice  # noqa: E402
from blaecktcpy.hub._upstream import _UpstreamBase  # noqa: E402


def _make_server_on_free_port():
    """Create an unstarted BlaeckTCPy that will bind to a free port on start()."""
    return BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0")


def _start_retry(device, attempts=1):
    """Start device; after start, update _port from the actual bound address."""
    device.start()
    # Port 0 means OS picks a free port — read the actual port after bind
    device._port = device._server_socket.getsockname()[1]


class FakeTransport(_UpstreamBase):
    """Minimal transport for testing without real connections."""

    def __init__(self, name="fake"):
        super().__init__(name)
        self._connected = True

    def connect(self, timeout=5.0):
        self._connected = True
        return True

    def read_available(self):
        return b""

    def send(self, data):
        return True

    def close(self):
        self._connected = False


class RecordingTransport(_UpstreamBase):
    """Transport that records all sent data for verification."""

    def __init__(self, name="recording"):
        super().__init__(name)
        self._connected = True
        self.sent: list[bytes] = []

    def connect(self, timeout=5.0):
        self._connected = True
        return True

    def read_available(self):
        return b""

    def send(self, data):
        self.sent.append(data)
        return True

    def close(self):
        self._connected = False
