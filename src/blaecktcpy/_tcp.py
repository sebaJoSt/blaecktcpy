"""TCP client connection management for BlaeckTCPy.

Handles socket lifecycle, client accept/disconnect, non-blocking I/O,
and message broadcasting.  Used internally by :class:`~blaecktcpy.BlaeckTCPy`.
"""

from __future__ import annotations

import logging
import selectors
import socket
import sys
from typing import TYPE_CHECKING

from ._signal import IntervalMode

if TYPE_CHECKING:
    from ._protocols import TCPHost

_MAX_RECV_BUFFER = 65536
_CLIENT_RECV_CHUNK = 4096


class ClientManager:
    """Manages TCP server socket and downstream client connections."""

    def __init__(self, server: TCPHost, logger: logging.Logger) -> None:
        self._server: TCPHost = server
        self._logger: logging.Logger = logger
        self._server_socket: socket.socket | None = None
        self._clients: dict[int, socket.socket] = {}
        self._next_client_id: int = 0
        self._commanding_client: socket.socket | None = None
        self._sel: selectors.DefaultSelector | None = None
        self._recv_buffers: dict[socket.socket, str] = {}
        self.data_clients: set[int] = set()
        self._client_meta: dict[int, dict[str, str]] = {}
        self._client_addrs: dict[int, str] = {}

    # ── Socket lifecycle ─────────────────────────────────────────────

    def init_socket(self) -> None:
        """Create TCP server socket with platform-specific options."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            self._server_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        else:
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def bind(self, ip: str, port: int) -> None:
        """Bind server socket to *ip*:*port*."""
        assert self._server_socket is not None
        self._server_socket.bind((ip, port))

    def start_listening(self) -> None:
        """Set non-blocking mode and start listening for connections."""
        assert self._server_socket is not None
        self._server_socket.setblocking(False)
        self._server_socket.listen()
        self._clients = {}
        self._next_client_id = 0
        self._commanding_client = None
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._server_socket, selectors.EVENT_READ)

    # ── Client connections ───────────────────────────────────────────

    def accept(self) -> None:
        """Accept all pending new connections in non-blocking loop."""
        assert self._server_socket is not None
        while True:
            try:
                conn, addr = self._server_socket.accept()
                conn.setblocking(False)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                assert self._sel is not None
                self._sel.register(conn, selectors.EVENT_READ)
                client_id = self._next_client_id
                self._next_client_id += 1
                self._clients[client_id] = conn
                self._recv_buffers[conn] = ""
                self.data_clients.add(client_id)
                self._client_meta[client_id] = {"name": "", "type": "unknown"}
                self._client_addrs[client_id] = f"{addr[0]}:{addr[1]}"
                self._logger.info(f"Client #{client_id} connected: {addr[0]}:{addr[1]}")
                if self._server._connect_callback is not None:
                    self._server._connect_callback(client_id)
            except (BlockingIOError, OSError):
                break

    def client_id_for(self, conn: socket.socket) -> int:
        """Find the client ID for a given socket, or ``-1`` if not found."""
        for cid, c in self._clients.items():
            if c is conn:
                return cid
        return -1

    def disconnect(self, conn: socket.socket) -> None:
        """Remove and close a client connection."""
        client_id = self.client_id_for(conn)
        try:
            assert self._sel is not None
            self._sel.unregister(conn)
        except Exception:
            pass
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass
        if client_id >= 0:
            self._clients.pop(client_id, None)
            self.data_clients.discard(client_id)
            meta = self._client_meta.pop(client_id, {})
            self._client_addrs.pop(client_id, None)
        else:
            meta = {}
        self._recv_buffers.pop(conn, None)
        if self._commanding_client is conn:
            self._commanding_client = None
        name = meta.get("name", "")
        rtype = meta.get("type", "unknown")
        cid = client_id if client_id >= 0 else "?"
        if name:
            self._logger.info(f"Client #{cid} disconnected ({rtype}: {name})")
        else:
            self._logger.info(f"Client #{cid} disconnected")
        if client_id >= 0 and self._server._disconnect_callback is not None:
            self._server._disconnect_callback(client_id)
        if not self._clients and self._server._fixed_interval_ms == IntervalMode.CLIENT:
            self._server._timed_activated = False

    # ── I/O ──────────────────────────────────────────────────────────

    def read_commands(self) -> list[tuple[str, list[str], socket.socket]]:
        """Non-blocking read from all clients; parse ``<cmd,p1,p2>`` messages."""
        if self._sel is None:
            raise AttributeError(
                "ClientManager not started — call start_listening() first"
            )
        messages: list[tuple[str, list[str], socket.socket]] = []

        events = self._sel.select(timeout=0)
        for key, _ in events:
            if key.fileobj is self._server_socket:
                self.accept()
            else:
                conn = key.fileobj
                assert isinstance(conn, socket.socket)
                try:
                    chunk = conn.recv(_CLIENT_RECV_CHUNK)
                    if not chunk:
                        self.disconnect(conn)
                        continue

                    self._recv_buffers[conn] = self._recv_buffers.get(
                        conn, ""
                    ) + chunk.decode("utf-8", errors="ignore")

                    self._logger.debug(f"_tcp_read raw chunk: {chunk!r}")

                    if len(self._recv_buffers[conn]) > _MAX_RECV_BUFFER:
                        self._logger.warning(
                            "Receive buffer overflow — dropping client"
                        )
                        self.disconnect(conn)
                        continue

                    buf = self._recv_buffers[conn]
                    while True:
                        start = buf.find("<")
                        if start == -1:
                            buf = ""
                            break
                        end = buf.find(">", start)
                        if end == -1:
                            buf = buf[start:]
                            break
                        content = buf[start + 1 : end]
                        buf = buf[end + 1 :]

                        parts = content.split(",")
                        command = parts[0].strip()
                        params = (
                            [p.strip() for p in parts[1:]] if len(parts) > 1 else []
                        )
                        messages.append((command, params, conn))

                    self._recv_buffers[conn] = buf

                except BlockingIOError:
                    pass
                except OSError as e:
                    self._logger.debug(f"Read error: {e}")
                    self.disconnect(conn)

        return messages

    def send_all(self, data: bytes) -> bool:
        """Broadcast *data* to all connected clients."""
        if not self._clients:
            return False

        sent = False
        for conn in list(self._clients.values()):
            try:
                conn.sendall(data)
                sent = True
            except OSError as e:
                self._logger.debug(f"Send error: {e}")
                self.disconnect(conn)

        return sent

    def send_data(self, data: bytes) -> bool:
        """Send *data* only to clients in :attr:`data_clients`."""
        if not self._clients:
            return False

        sent = False
        for client_id, conn in list(self._clients.items()):
            if client_id not in self.data_clients:
                continue
            try:
                conn.sendall(data)
                sent = True
            except OSError as e:
                self._logger.debug(f"Send error: {e}")
                self.disconnect(conn)

        return sent

    # ── Cleanup ──────────────────────────────────────────────────────

    def close(self) -> None:
        """Close all client sockets, the selector, and the server socket."""
        for conn in list(self._clients.values()):
            try:
                if self._sel is not None:
                    self._sel.unregister(conn)
            except Exception:
                pass
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self._clients.clear()

        if self._sel is not None and self._server_socket is not None:
            try:
                self._sel.unregister(self._server_socket)
            except Exception:
                pass
            self._sel.close()

        if self._server_socket is not None:
            self._server_socket.close()
