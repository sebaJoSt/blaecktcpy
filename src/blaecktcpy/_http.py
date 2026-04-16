"""Lightweight HTTP status page for BlaeckTCPy.

Serves a single-page status page on an opt-in port using only the
Python standard library.  Styled with Pico CSS (loaded from CDN).

Two routes:
    /       — HTML status page (loaded once by the browser)
    /api    — JSON snapshot of live server state (polled every 1 s)
"""

from __future__ import annotations

import json
import math
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from ._server import BlaeckTCPy
    from .hub._manager import UpstreamDevice

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = string.Template(r"""<!DOCTYPE html>
<html data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>$device_name — blaecktcpy</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css" media="print" onload="this.media='all'">
  <noscript><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css"></noscript>
  <style>
    :root { --pico-font-size: 87.5%; }
    .status-dot { display:inline-block; width:.7em; height:.7em; border-radius:50%; }
    .status-dot.up { background:#22c55e; }
    .status-dot.down { background:#ef4444; }
    nav a { cursor:pointer; text-decoration:none; }
    #theme-toggle { color:inherit; position:relative; right:15px; user-select:none; }
    #theme-toggle svg { width:1.5rem; height:1.5rem; vertical-align:middle; }
    [data-theme="dark"] #theme-toggle { color:#f0c040; }
    [data-theme="dark"] #theme-toggle svg { width:1.8rem; height:1.8rem; }
    [data-theme="light"] #theme-toggle svg { position:relative; top:-2px; }
    #server-stopped { text-align:center; padding:.6rem; font-weight:bold; position:sticky; top:0; z-index:999; }
    [data-theme="light"] #server-stopped { background:#fef2f2; color:#991b1b; border-bottom:1px solid #fecaca; }
    [data-theme="dark"] #server-stopped { background:#450a0a; color:#fca5a5; border-bottom:1px solid #7f1d1d; }
    footer { text-align:center; font-size:1.2rem; opacity:.65; padding-top:1rem; }
    article { margin-bottom:1rem; padding:1.5rem; }
    article h3 { margin-bottom:1.2rem; }
    #device-info-table td:first-child { width:30%; }
    #device-info-table td:last-child { width:70%; }
    .signal-table th:nth-child(1), .signal-table td:nth-child(1) { width:4%; }
    .signal-table th:nth-child(2), .signal-table td:nth-child(2) { width:18%; }
    .signal-table th:nth-child(3), .signal-table td:nth-child(3) { width:13%; }
    .signal-table th:nth-child(4), .signal-table td:nth-child(4) { width:65%; }
    article table { margin-bottom:0; }
  </style>
</head>
<body>
  <div id="server-stopped" hidden>Server stopped — this page is no longer updating</div>
  <header class="container" style="padding-top:4.5rem; margin-bottom:0;">
    <nav>
      <ul>
        <li>
          <hgroup style="padding-left:10px;">
            <h1 style="margin:0;display:inline;">blaecktcpy</h1>
            <a id="theme-toggle" aria-label="Toggle color scheme" style="margin-left:18px;vertical-align:middle;position:relative;top:-3px;"></a>
            <p style="margin:0;">v$lib_version</p>
          </hgroup>
        </li>
      </ul>
    </nav>
  </header>

  <main class="container">
    <!-- Device Info -->
    <article>
      <h3>Device Info</h3>
      <table id="device-info-table">
        <tbody>
          <tr><td><strong>Device</strong></td><td id="d-name">$device_name</td></tr>
          <tr><td><strong>HW Version</strong></td><td id="d-hw">$hw_version</td></tr>
          <tr><td><strong>FW Version</strong></td><td id="d-fw">$fw_version</td></tr>
          <tr><td><strong>TCP</strong></td><td id="d-tcp">$tcp_address</td></tr>
          <tr><td><strong>Uptime</strong></td><td id="d-uptime">$uptime</td></tr>
          <tr><td><strong>Interval</strong></td><td id="d-interval">$interval</td></tr>
          $timestamp_row
        </tbody>
      </table>
    </article>

    <!-- Downstream Clients -->
    <article>
      <h3>Downstream Clients (<span id="c-count">$client_count</span>)</h3>
      <table id="client-table">
        <thead><tr><th>Client ID</th><th>Name</th><th>Address</th><th>Data</th></tr></thead>
        <tbody>$client_rows</tbody>
      </table>
    </article>

    <!-- Local Signals -->
    <article>
      <h3>Local Signals (<span id="ls-count">$local_signal_count</span>)</h3>
      <table id="local-signal-table" class="signal-table striped">
        <thead><tr><th>#</th><th>Name</th><th>Type</th><th>Value</th></tr></thead>
        <tbody>$local_signal_rows</tbody>
      </table>
    </article>

    $upstream_html
  </main>

  <footer class="container">
    Made with ☕ by Sebastian Strobl
    <br>
    <a href="https://github.com/sebaJoSt/blaecktcpy" target="_blank" rel="noopener">
      github.com/sebaJoSt/blaecktcpy
    </a>
    &middot; Auto-refresh: 1 s
  </footer>

  <script>
  // -- Theme toggle --
  (function() {
    const html = document.documentElement;
    const stored = localStorage.getItem('blaecktcpy-theme');
    html.dataset.theme = stored || (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');
    const btn = document.getElementById('theme-toggle');
    const sunSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M12 19a1 1 0 0 1 .993 .883l.007 .117v1a1 1 0 0 1 -1.993 .117l-.007 -.117v-1a1 1 0 0 1 1 -1z" stroke-width="0" fill="currentColor"/><path d="M18.313 16.91l.094 .083l.7 .7a1 1 0 0 1 -1.32 1.497l-.094 -.083l-.7 -.7a1 1 0 0 1 1.218 -1.567l.102 .07z" stroke-width="0" fill="currentColor"/><path d="M7.007 16.993a1 1 0 0 1 .083 1.32l-.083 .094l-.7 .7a1 1 0 0 1 -1.497 -1.32l.083 -.094l.7 -.7a1 1 0 0 1 1.414 0z" stroke-width="0" fill="currentColor"/><path d="M4 11a1 1 0 0 1 .117 1.993l-.117 .007h-1a1 1 0 0 1 -.117 -1.993l.117 -.007h1z" stroke-width="0" fill="currentColor"/><path d="M21 11a1 1 0 0 1 .117 1.993l-.117 .007h-1a1 1 0 0 1 -.117 -1.993l.117 -.007h1z" stroke-width="0" fill="currentColor"/><path d="M6.213 4.81l.094 .083l.7 .7a1 1 0 0 1 -1.32 1.497l-.094 -.083l-.7 -.7a1 1 0 0 1 1.217 -1.567l.102 .07z" stroke-width="0" fill="currentColor"/><path d="M19.107 4.893a1 1 0 0 1 .083 1.32l-.083 .094l-.7 .7a1 1 0 0 1 -1.497 -1.32l.083 -.094l.7 -.7a1 1 0 0 1 1.414 0z" stroke-width="0" fill="currentColor"/><path d="M12 2a1 1 0 0 1 .993 .883l.007 .117v1a1 1 0 0 1 -1.993 .117l-.007 -.117v-1a1 1 0 0 1 1 -1z" stroke-width="0" fill="currentColor"/><path d="M12 7a5 5 0 1 1 -4.995 5.217l-.005 -.217l.005 -.217a5 5 0 0 1 4.995 -4.783z" stroke-width="0" fill="currentColor"/></svg>';
    const moonSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M12 1.992a10 10 0 1 0 9.236 13.838c.341 -.82 -.476 -1.644 -1.298 -1.31a6.5 6.5 0 0 1 -6.864 -10.787l.077 -.08c.551 -.63 .113 -1.653 -.758 -1.653h-.266l-.068 -.006l-.06 -.002z" stroke-width="0" fill="currentColor"/></svg>';
    function updateIcon() { btn.innerHTML = html.dataset.theme === 'dark' ? sunSvg : moonSvg; }
    updateIcon();
    btn.onclick = function(e) {
      e.preventDefault();
      html.dataset.theme = html.dataset.theme === 'dark' ? 'light' : 'dark';
      localStorage.setItem('blaecktcpy-theme', html.dataset.theme);
      updateIcon();
    };
  })();

  // -- Auto-refresh via /api --
  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; }
  function setHtml(id, v) { var el = document.getElementById(id); if (el) el.innerHTML = v; }
  var _pollTimer = null;
  var _stopped = false;

  function refresh() {
    fetch('/api').then(r => r.json()).then(function(d) {
      if (_stopped) {
        location.reload();
        return;
      }
      setText('d-uptime', d.uptime);
      setText('d-interval', d.interval);
      // Timestamp row: show only when not NONE
      var tsEl = document.getElementById('d-timestamp');
      if (tsEl) {
        tsEl.parentElement.style.display = d.timestamp_mode === 'NONE' ? 'none' : '';
        tsEl.textContent = d.timestamp_mode;
      }
      setText('c-count', d.clients.length);
      setText('ls-count', d.local_signals.length);

      // Clients
      var ct = '';
      if (d.clients.length === 0) {
        ct = '<tr><td colspan="4"><em>No clients connected</em></td></tr>';
      } else {
        d.clients.forEach(function(c) {
          ct += '<tr><td>'+c.id+'</td><td>'+esc(c.name)+'</td><td>'+esc(c.address)+'</td><td>'+(c.data ? '<span style="color:green">\u2713</span>' : '<span style="color:red">\u2717</span>')+'</td></tr>';
        });
      }
      setHtml('client-table-body', ct);

      // Local signals
      var ls = '';
      d.local_signals.forEach(function(s, i) {
        ls += '<tr><td>'+(i+1)+'</td><td>'+esc(s.name)+'</td><td>'+esc(s.type)+'</td><td>'+esc(String(s.value))+'</td></tr>';
      });
      setHtml('local-signal-body', ls);

      // Upstreams
      if (d.upstreams) {
        // Summary table
        var ut = '';
        d.upstreams.forEach(function(u) {
          var dot = '<span class="status-dot '+(u.connected?'up':'down')+'" title="'+(u.connected?'Connected':'Disconnected')+'"></span>';
          ut += '<tr><td>'+esc(u.name)+'</td><td>'+dot+'</td><td>'+esc(u.transport)+'</td><td>'+u.signal_count+'</td><td>'+esc(u.interval)+'</td><td>'+(u.relay?'yes':'no')+'</td><td>'+(u.auto_reconnect?'yes':'no')+'</td></tr>';
        });
        setHtml('upstream-summary-body', ut);

        // Per-upstream signal tables
        d.upstreams.forEach(function(u, idx) {
          var tbody = document.getElementById('upstream-signals-'+idx);
          var countEl = document.getElementById('upstream-count-'+idx);
          var statusEl = document.getElementById('upstream-status-'+idx);
          if (countEl) countEl.textContent = u.signals.length;
          if (statusEl) statusEl.innerHTML = '<span class="status-dot '+(u.connected?'up':'down')+'" title="'+(u.connected?'Connected':'Disconnected')+'"></span>';
          if (tbody) {
            var rs = '';
            u.signals.forEach(function(s, i) {
              rs += '<tr><td>'+(i+1)+'</td><td>'+esc(s.name)+'</td><td>'+esc(s.type)+'</td><td>'+esc(String(s.value))+'</td></tr>';
            });
            tbody.innerHTML = rs;
          }
        });
      }
    }, function(err) {
      if (!_stopped) {
        _stopped = true;
        var banner = document.getElementById('server-stopped');
        if (banner) {
          banner.hidden = false;
          var msg = (err && err.message) ? err.message : '';
          var isNetworkError = !msg || msg === 'Failed to fetch' || msg === 'NetworkError when attempting to reach resource.';
          banner.textContent = isNetworkError
            ? 'Server stopped \u2014 this page is no longer updating'
            : 'Connection lost: ' + msg;
        }
        document.title = '(stopped) ' + document.title;
        // Slow down to 5 s retries
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = setInterval(refresh, 5000);
      }
    });
  }

  function esc(s) { var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

  // Give tbody elements proper IDs for updates
  (function() {
    var ct = document.querySelector('#client-table tbody');
    if (ct) ct.id = 'client-table-body';
    var ls = document.querySelector('#local-signal-table tbody');
    if (ls) ls.id = 'local-signal-body';
    var us = document.querySelector('#upstream-summary tbody');
    if (us) us.id = 'upstream-summary-body';
  })();

  _pollTimer = setInterval(refresh, 1000);
  </script>
</body>
</html>""")


# ---------------------------------------------------------------------------
# State → dict / HTML helpers
# ---------------------------------------------------------------------------

def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m {s:02d}s"
    d, h = divmod(h, 24)
    return f"{d}d {h:02d}h {m:02d}m"


def _interval_str(server: BlaeckTCPy) -> str:
    from ._signal import IntervalMode
    v = server._fixed_interval_ms
    if v >= 0:
        return f"{v} ms (local)"
    if v == IntervalMode.OFF:
        return "OFF"
    if v == IntervalMode.CLIENT:
        if server._timed_activated:
            return f"{server._timer._interval_ms} ms (client)"
        return "CLIENT (inactive)"
    return str(v)


def _upstream_interval_str(interval_ms: int) -> str:
    from ._signal import IntervalMode
    if interval_ms >= 0:
        return f"{interval_ms} ms"
    if interval_ms == IntervalMode.OFF:
        return "OFF"
    if interval_ms == IntervalMode.CLIENT:
        return "CLIENT"
    return str(interval_ms)


def _transport_str(upstream: UpstreamDevice) -> str:
    from .hub._upstream import UpstreamTCP
    t = upstream.transport
    if isinstance(t, UpstreamTCP):
        return f"TCP {t.ip}:{t.port}"
    # Serial
    port = getattr(t, "port", "?")
    baud = getattr(t, "baudrate", "")
    return f"Serial {port}" + (f" ({baud})" if baud else "")


def _safe_value(v: object) -> float | str | object:
    """Convert NaN/Inf to strings so json.dumps produces valid JSON."""
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Inf" if v > 0 else "-Inf"
    return v


def _get_state(server: BlaeckTCPy) -> dict[str, Any]:
    """Build a JSON-serialisable dict of the full server state."""
    from ._signal import TimestampMode

    uptime = time.time() - server._start_time if server._start_time else 0

    # Clients
    clients = []
    for cid in sorted(server._tcp._clients):
        addr = server._tcp._client_addrs.get(cid, "")
        meta = server._tcp._client_meta.get(cid, {})
        name = meta.get("name", "")
        ctype = meta.get("type", "")
        label = f"{name} ({ctype})" if name and ctype and ctype != "unknown" else name
        clients.append({
            "id": cid,
            "name": label,
            "address": addr,
            "data": cid in server.data_clients,
        })

    # Local signals
    local_count = server._local_signal_count
    local_signals = []
    for i in range(min(local_count, len(server.signals))):
        sig = server.signals[i]
        local_signals.append({
            "name": sig.signal_name,
            "type": sig.datatype,
            "value": _safe_value(sig.value),
        })

    state = {
        "device_name": server._device_name.decode(),
        "hw_version": server._device_hw_version.decode(),
        "fw_version": server._device_fw_version.decode(),
        "lib_version": _get_lib_version(),
        "tcp_address": f"{server._ip}:{server._port}",
        "uptime": _format_uptime(uptime),
        "interval": _interval_str(server),
        "timestamp_mode": TimestampMode(server._timestamp_mode).name,
        "client_count": len(clients),
        "clients": clients,
        "local_signal_count": local_count,
        "local_signals": local_signals,
    }

    # Upstreams (hub mode)
    if server._hub._upstreams:
        upstreams = []
        for u in server._hub._upstreams:
            signals = []
            for sig in (u._signals or []):
                signals.append({
                    "name": sig.signal_name,
                    "type": sig.datatype,
                    "value": _safe_value(sig.value),
                })
            upstreams.append({
                "name": u.device_name,
                "connected": u.transport.connected,
                "transport": _transport_str(u),
                "signal_count": len(u.symbol_table),
                "interval": _upstream_interval_str(u.interval_ms),
                "relay": u.relay_downstream,
                "auto_reconnect": u.auto_reconnect,
                "signals": signals,
            })
        state["upstreams"] = upstreams

    return state


def _get_lib_version() -> str:
    from importlib.metadata import version
    return version("blaecktcpy")


def _render_html(server: BlaeckTCPy) -> str:
    """Render the full HTML page from current server state."""
    state = _get_state(server)

    # Client rows
    client_rows = ""
    if state["clients"]:
        for c in state["clients"]:
            data_icon = '<span style="color:green">✓</span>' if c["data"] else '<span style="color:red">✗</span>'
            client_rows += (
                f"<tr><td>{c['id']}</td><td>{_esc(c['name'])}</td>"
                f"<td>{_esc(c['address'])}</td><td>{data_icon}</td></tr>"
            )
    else:
        client_rows = '<tr><td colspan="4"><em>No clients connected</em></td></tr>'

    # Local signal rows
    local_signal_rows = ""
    for i, s in enumerate(state["local_signals"]):
        local_signal_rows += (
            f"<tr><td>{i + 1}</td><td>{_esc(s['name'])}</td>"
            f"<td>{_esc(s['type'])}</td><td>{_esc(str(s['value']))}</td></tr>"
        )

    # Upstream HTML
    upstream_html = ""
    if state.get("upstreams"):
        # Summary table
        summary_rows = ""
        for u in state["upstreams"]:
            dot_class = "up" if u["connected"] else "down"
            dot_label = "Connected" if u["connected"] else "Disconnected"
            summary_rows += (
                f"<tr><td>{_esc(u['name'])}</td>"
                f'<td><span class="status-dot {dot_class}" title="{dot_label}"></span></td>'
                f"<td>{_esc(u['transport'])}</td>"
                f"<td>{u['signal_count']}</td>"
                f"<td>{_esc(u['interval'])}</td>"
                f"<td>{'yes' if u['relay'] else 'no'}</td>"
                f"<td>{'yes' if u['auto_reconnect'] else 'no'}</td></tr>"
            )

        upstream_html = f"""
    <article>
      <h3>Upstreams ({len(state['upstreams'])})</h3>
      <table id="upstream-summary">
        <thead><tr><th>Name</th><th>Status</th><th>Transport</th><th>Signals</th><th>Interval</th><th>Relay</th><th>Reconnect</th></tr></thead>
        <tbody>{summary_rows}</tbody>
      </table>
    </article>"""

        # Per-upstream signal details
        for idx, u in enumerate(state["upstreams"]):
            dot_class = "up" if u["connected"] else "down"
            dot_label = "Connected" if u["connected"] else "Disconnected"
            sig_rows = ""
            for i, s in enumerate(u["signals"]):
                sig_rows += (
                    f"<tr><td>{i + 1}</td><td>{_esc(s['name'])}</td>"
                    f"<td>{_esc(s['type'])}</td><td>{_esc(str(s['value']))}</td></tr>"
                )
            upstream_html += f"""
    <article>
      <details>
        <summary>{_esc(u['name'])} \u2014 Signals (<span id="upstream-count-{idx}">{len(u['signals'])}</span>) &nbsp; <span id="upstream-status-{idx}"><span class="status-dot {dot_class}" title="{dot_label}"></span></span></summary>
        <table class="signal-table striped">
          <thead><tr><th>#</th><th>Name</th><th>Type</th><th>Value</th></tr></thead>
          <tbody id="upstream-signals-{idx}">{sig_rows}</tbody>
        </table>
      </details>
    </article>"""

    # Timestamp row — only shown when not NONE
    ts_mode = state["timestamp_mode"]
    timestamp_row = (
        f'<tr><td><strong>Timestamp</strong></td><td id="d-timestamp">{ts_mode}</td></tr>'
        if ts_mode != "NONE" else ""
    )

    return _HTML_TEMPLATE.substitute(
        device_name=_esc(state["device_name"]),
        hw_version=_esc(state["hw_version"]),
        fw_version=_esc(state["fw_version"]),
        lib_version=_esc(state["lib_version"]),
        tcp_address=state["tcp_address"],
        uptime=state["uptime"],
        interval=_esc(state["interval"]),
        timestamp_row=timestamp_row,
        client_count=state["client_count"],
        client_rows=client_rows,
        local_signal_count=state["local_signal_count"],
        local_signal_rows=local_signal_rows,
        upstream_html=upstream_html,
    )


def _esc(text: str) -> str:
    """Minimal HTML entity escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# HTTP handler & server
# ---------------------------------------------------------------------------

def _make_handler(server: BlaeckTCPy):
    """Create a request handler class bound to a BlaeckTCPy instance."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api":
                body = json.dumps(_get_state(server)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/" or self.path == "":
                body = _render_html(server).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        @override
        def log_message(self, format: str, *args: object) -> None:
            # Silence default stderr logging
            pass

        @override
        def address_string(self) -> str:
            # Skip reverse DNS lookup — avoids multi-second delays
            return self.client_address[0]

    return _Handler


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """HTTPServer that refuses to share a port (no SO_REUSEADDR)."""

    allow_reuse_address = False


def start_http_server(
    server: BlaeckTCPy, port: int, bind: str = ""
) -> ThreadingHTTPServer:
    """Start the HTTP status page in a daemon thread.

    Args:
        server: The BlaeckTCPy instance to expose.
        port: TCP port for the HTTP server.
        bind: Address to bind to (empty = same as BlaeckTCPy).

    Returns:
        The HTTPServer instance (already serving in a background thread).
    """
    handler = _make_handler(server)
    bind_addr = bind or server._ip
    httpd = _ExclusiveHTTPServer((bind_addr, port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
