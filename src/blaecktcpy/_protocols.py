"""Structural typing protocols for composed-object back-references.

These protocols define the exact surface each component (ClientManager,
HubManager) requires from BlaeckTCPy.  pyright verifies that BlaeckTCPy
satisfies these protocols at the assignment site, catching renames or
removals of attributes that composed objects depend on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ._signal import SignalList


class TCPHost(Protocol):
    """What :class:`ClientManager` needs from the server."""

    _connect_callback: Callable[[int], Any] | None
    _disconnect_callback: Callable[[int], Any] | None
    _fixed_interval_ms: int
    _timed_activated: bool


class HubHost(Protocol):
    """What :class:`HubManager` needs from the server."""

    _started: bool
    signals: SignalList
    _local_signal_count: int
    _device_name: bytes
    _upstream_disconnect_callback: Callable[..., Any] | None

    @property
    def MSG_DATA(self) -> bytes: ...

    @property
    def connected(self) -> bool: ...

    def _fire_data_received(self, upstream: Any) -> None: ...

    def _build_data_msg(
        self,
        header: bytes,
        start: int = 0,
        end: int = -1,
        only_updated: bool = False,
        timestamp: int | None = None,
        timestamp_mode: int | None = None,
        status: int = ...,
        status_payload: bytes = ...,
    ) -> bytes: ...

    def _tcp_send_data(self, data: bytes) -> bool: ...

    def _update_schema_hash(self) -> None: ...
