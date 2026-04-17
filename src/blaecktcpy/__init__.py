"""blaecktcpy — BlaeckTCP Protocol Implementation."""

import logging
from importlib.metadata import version
from typing import override

__version__ = version("blaecktcpy")

# -- Logging setup (colour-coded console handler) ---------------------------
logger = logging.getLogger("blaecktcpy")
if not logger.handlers:

    class _ColorFormatter(logging.Formatter):
        _LEVEL_COLORS: dict[int, str] = {
            logging.DEBUG: "\033[36m",  # cyan
            logging.INFO: "\033[32m",  # green
            logging.WARNING: "\033[33m",  # yellow
            logging.ERROR: "\033[31m",  # red
            logging.CRITICAL: "\033[1;31m",  # bold red
        }
        _RESET: str = "\033[0m"

        @override
        def format(self, record: logging.LogRecord) -> str:
            color = self._LEVEL_COLORS.get(record.levelno, "")
            return (
                f"{color}{record.getMessage()}{self._RESET}"
                if color
                else record.getMessage()
            )

    _handler = logging.StreamHandler()
    _handler.setFormatter(_ColorFormatter())
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# -- Eager re-exports -------------------------------------------------------
from ._signal import Signal, SignalList, IntervalMode, TimestampMode  # noqa: E402
from ._server import (
    BlaeckTCPy,
    LIB_VERSION as LIB_VERSION,
    LIB_NAME as LIB_NAME,
    STATUS_OK as STATUS_OK,
    STATUS_UPSTREAM_LOST as STATUS_UPSTREAM_LOST,
    STATUS_UPSTREAM_RECONNECTED as STATUS_UPSTREAM_RECONNECTED,
)  # noqa: E402
from .hub._manager import UpstreamDevice  # noqa: E402

__all__ = [
    "Signal",
    "SignalList",
    "IntervalMode",
    "TimestampMode",
    "BlaeckTCPy",
    "UpstreamDevice",
]
