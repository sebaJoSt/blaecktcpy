"""Tests for the HTTP status page module."""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from blaecktcpy import BlaeckTCPy, IntervalMode, TimestampMode
from blaecktcpy._http import (
    _esc,
    _format_uptime,
    _get_state,
    _interval_str,
    _render_html,
    _upstream_interval_str,
)


# ---------------------------------------------------------------------------
# _format_uptime
# ---------------------------------------------------------------------------

class TestFormatUptime:
    def test_seconds_only(self):
        assert _format_uptime(45) == "45s"

    def test_zero(self):
        assert _format_uptime(0) == "0s"

    def test_minutes_and_seconds(self):
        assert _format_uptime(125) == "2m 05s"

    def test_exactly_one_minute(self):
        assert _format_uptime(60) == "1m 00s"

    def test_hours(self):
        assert _format_uptime(3661) == "1h 01m 01s"

    def test_days(self):
        assert _format_uptime(90061) == "1d 01h 01m"

    def test_large_value(self):
        result = _format_uptime(100 * 86400)
        assert result.startswith("100d")


# ---------------------------------------------------------------------------
# _interval_str
# ---------------------------------------------------------------------------

class TestIntervalStr:
    def _make_server(self):
        return BlaeckTCPy("127.0.0.1", 0, "Test", "HW", "1.0", http_port=None)

    def test_fixed_interval(self):
        server = self._make_server()
        server._fixed_interval_ms = 500
        assert _interval_str(server) == "500 ms (local)"

    def test_off(self):
        server = self._make_server()
        server._fixed_interval_ms = IntervalMode.OFF
        assert _interval_str(server) == "OFF"

    def test_client_inactive(self):
        server = self._make_server()
        server._fixed_interval_ms = IntervalMode.CLIENT
        server._timed_activated = False
        assert _interval_str(server) == "CLIENT (inactive)"

    def test_client_active(self):
        server = self._make_server()
        server._fixed_interval_ms = IntervalMode.CLIENT
        server._timed_activated = True
        server._timer._interval_ms = 200
        assert _interval_str(server) == "200 ms (client)"


# ---------------------------------------------------------------------------
# _upstream_interval_str
# ---------------------------------------------------------------------------

class TestUpstreamIntervalStr:
    def test_fixed(self):
        assert _upstream_interval_str(300) == "300 ms"

    def test_zero(self):
        assert _upstream_interval_str(0) == "0 ms"

    def test_off(self):
        assert _upstream_interval_str(IntervalMode.OFF) == "OFF"

    def test_client(self):
        assert _upstream_interval_str(IntervalMode.CLIENT) == "CLIENT"


# ---------------------------------------------------------------------------
# _esc
# ---------------------------------------------------------------------------

class TestEsc:
    def test_escapes_html_entities(self):
        assert _esc('a & b < c > d "e"') == 'a &amp; b &lt; c &gt; d &quot;e&quot;'

    def test_passthrough(self):
        assert _esc("plain text") == "plain text"

    def test_empty(self):
        assert _esc("") == ""


# ---------------------------------------------------------------------------
# _get_state / _render_html (require a started server)
# ---------------------------------------------------------------------------

class TestGetState:
    def setup_method(self):
        self.server = BlaeckTCPy("127.0.0.1", 0, "TestDev", "HW1", "FW1",
                                 http_port=None)
        self.server.add_signal("temp", "float", 22.5)
        self.server.add_signal("led", "bool", True)
        self.server.start()
        self.server._port = self.server._server_socket.getsockname()[1]

    def teardown_method(self):
        self.server.close()

    def test_state_has_required_keys(self):
        state = _get_state(self.server)
        assert state["device_name"] == "TestDev"
        assert state["hw_version"] == "HW1"
        assert state["fw_version"] == "FW1"
        assert state["local_signal_count"] == 2
        assert "tcp_address" in state
        assert "uptime" in state
        assert "interval" in state
        assert "timestamp_mode" in state

    def test_local_signals_in_state(self):
        state = _get_state(self.server)
        sigs = state["local_signals"]
        assert len(sigs) == 2
        assert sigs[0]["name"] == "temp"
        assert sigs[0]["type"] == "float"
        assert sigs[0]["value"] == 22.5
        assert sigs[1]["name"] == "led"
        assert sigs[1]["value"] is True

    def test_state_no_upstreams(self):
        state = _get_state(self.server)
        assert "upstreams" not in state

    def test_timestamp_mode_in_state(self):
        state = _get_state(self.server)
        assert state["timestamp_mode"] == "NONE"
        self.server._timestamp_mode = TimestampMode.UNIX
        state = _get_state(self.server)
        assert state["timestamp_mode"] == "UNIX"


class TestRenderHtml:
    def setup_method(self):
        self.server = BlaeckTCPy("127.0.0.1", 0, "HtmlDev", "HW", "FW",
                                 http_port=None)
        self.server.add_signal("voltage", "double", 3.3)
        self.server.start()
        self.server._port = self.server._server_socket.getsockname()[1]

    def teardown_method(self):
        self.server.close()

    def test_html_contains_device_name(self):
        html = _render_html(self.server)
        assert "HtmlDev" in html

    def test_html_is_valid_structure(self):
        html = _render_html(self.server)
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html


# ---------------------------------------------------------------------------
# Live HTTP server (GET / and /api)
# ---------------------------------------------------------------------------

class TestHttpServer:
    def setup_method(self):
        self.server = BlaeckTCPy("127.0.0.1", 0, "HttpTest", "HW", "FW",
                                 http_port=0)
        self.server.add_signal("x", "float", 1.0)
        self.server.start()
        self.server._port = self.server._server_socket.getsockname()[1]
        self.http_port = self.server._httpd.server_address[1]

    def teardown_method(self):
        self.server.close()

    def test_api_returns_json(self):
        url = f"http://127.0.0.1:{self.http_port}/api"
        with urllib.request.urlopen(url, timeout=2) as resp:
            assert resp.status == 200
            assert "application/json" in resp.headers["Content-Type"]
            data = json.loads(resp.read())
            assert data["device_name"] == "HttpTest"
            assert data["local_signal_count"] == 1

    def test_root_returns_html(self):
        url = f"http://127.0.0.1:{self.http_port}/"
        with urllib.request.urlopen(url, timeout=2) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers["Content-Type"]
            body = resp.read().decode()
            assert "<!DOCTYPE html>" in body

    def test_404_for_unknown_path(self):
        url = f"http://127.0.0.1:{self.http_port}/nonexistent"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=2)
        assert exc_info.value.code == 404
