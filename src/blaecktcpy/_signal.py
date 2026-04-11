"""Signal dataclass for BlaeckTCP typed data."""

import struct
from enum import IntEnum
from dataclasses import dataclass, field


class IntervalMode(IntEnum):
    """Timed data interval modes for the :attr:`interval_ms` property.

    * **OFF** (-1) — Timed data disabled; client ACTIVATE ignored.
    * **CLIENT** (-2) — Client controlled (default); the client's
      ACTIVATE / DEACTIVATE commands determine the rate.
    """

    OFF = -1
    CLIENT = -2


class TimestampMode(IntEnum):
    """Timestamp modes for data frames.

    * **NONE** (0) — No timestamp in data frames (default).
    * **MICROS** (1) — Microseconds since start (protocol-level only;
      used by Arduino upstream devices, not available for blaecktcpy servers).
    * **UNIX** (2) — Microseconds since Unix epoch (1970-01-01 UTC).
    """

    NONE = 0
    MICROS = 1
    UNIX = 2


@dataclass(init=False)
class Signal:
    """Represents a BlaeckTCP signal with typed data"""

    signal_name: str
    datatype: str
    updated: bool = False
    _value: int | float | bool = field(init=False, repr=False)

    # Class-level mappings
    DATATYPE_TO_CODE = {
        "bool": 0,
        "byte": 1,
        "short": 2,
        "unsigned short": 3,
        "int": 6,
        "unsigned int": 7,
        "long": 6,
        "unsigned long": 7,
        "float": 8,
        "double": 9,
    }

    DATATYPE_SIZES = {
        "bool": 1,
        "byte": 1,
        "short": 2,
        "unsigned short": 2,
        "int": 4,
        "unsigned int": 4,
        "long": 4,
        "unsigned long": 4,
        "float": 4,
        "double": 8,
    }

    SIGNED_TYPES = {"short", "int", "long"}
    FLOAT_TYPES = {"float", "double"}

    def __init__(
        self,
        signal_name: str,
        datatype: str,
        value: int | float = 0,
        updated: bool = False,
    ):
        self.signal_name = signal_name
        self.datatype = datatype
        self.updated = updated
        self._validate_datatype(datatype)
        self.value = value

    @classmethod
    def _validate_datatype(cls, datatype: str) -> None:
        if datatype not in cls.DATATYPE_TO_CODE:
            raise ValueError(f"Invalid datatype: {datatype}")

    @classmethod
    def _integer_range(cls, datatype: str) -> tuple[int, int]:
        if datatype == "bool":
            return 0, 1

        bits = cls.DATATYPE_SIZES[datatype] * 8
        if datatype in cls.SIGNED_TYPES:
            return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        return 0, (1 << bits) - 1

    def _normalize_value(self, value: int | float) -> int | float | bool:
        if self.datatype in self.FLOAT_TYPES:
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid value for {self.datatype} signal '{self.signal_name}': {value!r}"
                ) from exc

        if isinstance(value, bool):
            normalized = int(value)
        elif isinstance(value, int):
            normalized = value
        elif isinstance(value, float):
            if not value.is_integer():
                raise ValueError(
                    f"Invalid value for {self.datatype} signal '{self.signal_name}': {value!r}"
                )
            normalized = int(value)
        else:
            raise ValueError(
                f"Invalid value for {self.datatype} signal '{self.signal_name}': {value!r}"
            )

        min_value, max_value = self._integer_range(self.datatype)
        if not min_value <= normalized <= max_value:
            raise ValueError(
                f"Value {normalized} out of range for {self.datatype} "
                f"signal '{self.signal_name}' [{min_value}, {max_value}]"
            )

        if self.datatype == "bool":
            return bool(normalized)
        return normalized

    @property
    def value(self) -> int | float | bool:
        return self._value

    @value.setter
    def value(self, value: int | float) -> None:
        self._value = self._normalize_value(value)

    def to_bytes(self) -> bytes:
        """Convert signal value to bytes based on datatype"""
        if self.datatype in self.FLOAT_TYPES:
            fmt = "<f" if self.datatype == "float" else "<d"
            return struct.pack(fmt, self.value)
        else:
            signed = self.datatype in self.SIGNED_TYPES
            return int(self.value).to_bytes(
                self.DATATYPE_SIZES[self.datatype], "little", signed=signed
            )

    def get_dtype_byte(self) -> bytes:
        """Get the datatype code as a single byte"""
        return self.DATATYPE_TO_CODE[self.datatype].to_bytes(1, "little")

    def __repr__(self):
        return f"{self.signal_name}: {self.datatype} = {self.value}"


class SignalList(list):
    """A list of signals with name-based access.

    Supports indexing by integer or signal name::

        signals[0].value
        signals["temperature"].value

    Name-based lookups use an internal dict cache (O(1) amortised).
    The cache is lazily rebuilt after any list mutation.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._name_cache: dict[str, Signal] | None = None
        self._index_cache: dict[str, int] | None = None

    def _invalidate_cache(self):
        self._name_cache = None
        self._index_cache = None

    def _ensure_cache(self):
        if self._name_cache is None:
            self._name_cache = {sig.signal_name: sig for sig in self}
            self._index_cache = {sig.signal_name: i for i, sig in enumerate(self)}

    def index_of(self, name: str) -> int | None:
        """Return the index of a signal by name, or None if not found. O(1)."""
        self._ensure_cache()
        return self._index_cache.get(name)

    def __getitem__(self, key):
        if isinstance(key, str):
            self._ensure_cache()
            try:
                return self._name_cache[key]
            except KeyError:
                raise KeyError(f"No signal named {key!r}")
        return super().__getitem__(key)

    # Override mutating methods to invalidate the cache
    def append(self, item):
        super().append(item)
        self._invalidate_cache()

    def extend(self, items):
        super().extend(items)
        self._invalidate_cache()

    def insert(self, index, item):
        super().insert(index, item)
        self._invalidate_cache()

    def remove(self, item):
        super().remove(item)
        self._invalidate_cache()

    def pop(self, index=-1):
        result = super().pop(index)
        self._invalidate_cache()
        return result

    def clear(self):
        super().clear()
        self._invalidate_cache()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._invalidate_cache()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._invalidate_cache()

    def __iadd__(self, other):
        result = super().__iadd__(other)
        self._invalidate_cache()
        return result
