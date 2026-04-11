"""Tests for the HTTP status page module."""

import json
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

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
    _transport_str,
    _upstream_interval_str,
)
from blaecktcpy.hub._upstream import UpstreamTCP


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
        return BlaeckTCPy(
                   ip="127.0.0.1",
                   port=0,
                   device_name="Test",
                   device_hw_version="HW",
                   device_fw_version="1.0",
                   http_port=None,
               )

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
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="TestDev",
                          device_hw_version="HW1",
                          device_fw_version="FW1",
                          http_port=None,
                      )
        self.server.add_signal("temp", "float", 22.5)
        self.server.add_signal("led", "bool", True)
        self.server.start()
        self.server._port = self.server._tcp._server_socket.getsockname()[1]

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
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="HtmlDev",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=None,
                      )
        self.server.add_signal("voltage", "double", 3.3)
        self.server.start()
        self.server._port = self.server._tcp._server_socket.getsockname()[1]

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
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="HttpTest",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=0,
                      )
        self.server.add_signal("x", "float", 1.0)
        self.server.start()
        self.server._port = self.server._tcp._server_socket.getsockname()[1]
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


# ---------------------------------------------------------------------------
# _interval_str / _upstream_interval_str — fallback branches
# ---------------------------------------------------------------------------

class TestIntervalStrFallback:
    def test_unknown_negative_value(self):
        """Line 249: fallback for unrecognised negative interval."""
        server = BlaeckTCPy(
                     ip="127.0.0.1",
                     port=0,
                     device_name="T",
                     device_hw_version="H",
                     device_fw_version="F",
                     http_port=None,
                 )
        server._fixed_interval_ms = -999
        assert _interval_str(server) == "-999"
        server.close()


class TestUpstreamIntervalStrFallback:
    def test_unknown_negative_value(self):
        """Line 260: fallback for unrecognised negative interval."""
        assert _upstream_interval_str(-999) == "-999"


# ---------------------------------------------------------------------------
# _transport_str
# ---------------------------------------------------------------------------

class TestTransportStr:
    def test_tcp_transport(self):
        tcp = UpstreamTCP("dev1", "192.168.1.10", 9325)
        upstream = SimpleNamespace(transport=tcp)
        assert _transport_str(upstream) == "TCP 192.168.1.10:9325"

    def test_serial_transport(self):
        serial_t = SimpleNamespace(port="COM3", baudrate=115200)
        upstream = SimpleNamespace(transport=serial_t)
        assert _transport_str(upstream) == "Serial COM3 (115200)"

    def test_serial_transport_no_baud(self):
        serial_t = SimpleNamespace(port="/dev/ttyUSB0")
        upstream = SimpleNamespace(transport=serial_t)
        assert _transport_str(upstream) == "Serial /dev/ttyUSB0"


# ---------------------------------------------------------------------------
# _get_state with clients
# ---------------------------------------------------------------------------

class TestGetStateWithClients:
    def setup_method(self):
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="TestDev",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=None,
                      )
        self.server.start()
        self.server._port = self.server._tcp._server_socket.getsockname()[1]

    def teardown_method(self):
        self.server._tcp._clients.clear()
        self.server.close()

    def test_clients_appear_in_state(self):
        self.server._tcp._clients[5] = None
        self.server._tcp._client_addrs[5] = "192.168.1.50:12345"
        self.server._tcp._client_meta[5] = {"name": "MyApp", "type": "logger"}
        state = _get_state(self.server)
        assert state["client_count"] == 1
        c = state["clients"][0]
        assert c["id"] == 5
        assert c["name"] == "MyApp (logger)"
        assert c["address"] == "192.168.1.50:12345"
        assert c["data"] is False

    def test_client_name_with_unknown_type(self):
        self.server._tcp._clients[1] = None
        self.server._tcp._client_addrs[1] = "10.0.0.1:9000"
        self.server._tcp._client_meta[1] = {"name": "Probe", "type": "unknown"}
        state = _get_state(self.server)
        assert state["clients"][0]["name"] == "Probe"

    def test_data_client_flag(self):
        self.server._tcp._clients[7] = None
        self.server._tcp._client_addrs[7] = "10.0.0.2:8000"
        self.server._tcp._client_meta[7] = {}
        self.server.data_clients.add(7)
        state = _get_state(self.server)
        assert state["clients"][0]["data"] is True


# ---------------------------------------------------------------------------
# _get_state with upstreams
# ---------------------------------------------------------------------------

def _make_fake_upstream(name="UpDev", connected=True, interval_ms=500,
                        relay=True, auto_reconnect=False, signals=None):
    """Build a fake _UpstreamDevice-like object for testing."""
    tcp = UpstreamTCP(name, "10.0.0.1", 9325)
    tcp._connected = connected
    return SimpleNamespace(
        device_name=name,
        transport=tcp,
        symbol_table=[SimpleNamespace()] * (len(signals) if signals else 0),
        interval_ms=interval_ms,
        relay_downstream=relay,
        auto_reconnect=auto_reconnect,
        _signals=signals,
    )


class TestGetStateWithUpstreams:
    def setup_method(self):
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="HubDev",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=None,
                      )
        self.server.start()
        self.server._port = self.server._tcp._server_socket.getsockname()[1]

    def teardown_method(self):
        self.server.close()

    def test_upstreams_in_state(self):
        from blaecktcpy._signal import Signal
        sig = Signal(signal_name="rpm", datatype="int", value=1200)
        upstream = _make_fake_upstream(signals=[sig])
        self.server._hub._upstreams.append(upstream)
        state = _get_state(self.server)
        assert "upstreams" in state
        assert len(state["upstreams"]) == 1
        u = state["upstreams"][0]
        assert u["name"] == "UpDev"
        assert u["connected"] is True
        assert u["signal_count"] == 1
        assert u["interval"] == "500 ms"
        assert u["relay"] is True
        assert u["auto_reconnect"] is False
        assert u["signals"][0]["name"] == "rpm"

    def test_upstream_disconnected(self):
        upstream = _make_fake_upstream(connected=False, signals=None)
        self.server._hub._upstreams.append(upstream)
        state = _get_state(self.server)
        u = state["upstreams"][0]
        assert u["connected"] is False
        assert u["signals"] == []


# ---------------------------------------------------------------------------
# _render_html with clients and upstreams
# ---------------------------------------------------------------------------

class TestRenderHtmlWithClients:
    def setup_method(self):
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="HtmlDev",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=None,
                      )
        self.server.start()
        self.server._port = self.server._tcp._server_socket.getsockname()[1]

    def teardown_method(self):
        self.server._tcp._clients.clear()
        self.server.close()

    def test_client_rows_rendered(self):
        self.server._tcp._clients[1] = None
        self.server._tcp._client_addrs[1] = "10.0.0.1:8000"
        self.server._tcp._client_meta[1] = {"name": "Browser", "type": "web"}
        self.server.data_clients.add(1)
        html = _render_html(self.server)
        assert "Browser (web)" in html
        assert "10.0.0.1:8000" in html
        assert "color:green" in html  # data client ✓

    def test_non_data_client_shows_red(self):
        self.server._tcp._clients[2] = None
        self.server._tcp._client_addrs[2] = "10.0.0.2:9000"
        self.server._tcp._client_meta[2] = {}
        html = _render_html(self.server)
        assert "color:red" in html  # non-data client ✗


class TestRenderHtmlWithUpstreams:
    def setup_method(self):
        self.server = BlaeckTCPy(
                          ip="127.0.0.1",
                          port=0,
                          device_name="HubHtml",
                          device_hw_version="HW",
                          device_fw_version="FW",
                          http_port=None,
                      )
        self.server.add_signal("x", "float", 0.0)
        self.server.start()
        self.server._port = self.server._tcp._server_socket.getsockname()[1]

    def teardown_method(self):
        self.server.close()

    def test_upstream_summary_table_rendered(self):
        from blaecktcpy._signal import Signal
        sig = Signal(signal_name="temp", datatype="float", value=22.5)
        upstream = _make_fake_upstream(name="Arduino1", signals=[sig])
        self.server._hub._upstreams.append(upstream)
        html = _render_html(self.server)
        assert "Upstreams (1)" in html
        assert "Arduino1" in html
        assert "status-dot up" in html  # connected

    def test_upstream_disconnected_dot(self):
        upstream = _make_fake_upstream(name="OffDev", connected=False,
                                       signals=[])
        self.server._hub._upstreams.append(upstream)
        html = _render_html(self.server)
        assert "status-dot down" in html

    def test_upstream_signal_rows_rendered(self):
        from blaecktcpy._signal import Signal
        sig = Signal(signal_name="pressure", datatype="double", value=101.3)
        upstream = _make_fake_upstream(name="Sensor", signals=[sig])
        self.server._hub._upstreams.append(upstream)
        html = _render_html(self.server)
        assert "pressure" in html
        assert "101.3" in html
