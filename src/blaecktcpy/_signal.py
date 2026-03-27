"""Signal dataclass for BlaeckTCP typed data."""

import struct
from typing import Union
from dataclasses import dataclass


@dataclass
class Signal:
    """Represents a BlaeckTCP signal with typed data"""

    signal_name: str
    datatype: str
    value: Union[int, float] = 0
    updated: bool = False

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

    def __post_init__(self):
        """Validate datatype on initialization"""
        if self.datatype not in self.DATATYPE_TO_CODE:
            raise ValueError(f"Invalid datatype: {self.datatype}")

        # Auto-convert to proper type
        if self.datatype not in self.FLOAT_TYPES and not isinstance(self.value, int):
            self.value = int(self.value)

    def to_bytes(self) -> bytes:
        """Convert signal value to bytes based on datatype"""
        if self.datatype in self.FLOAT_TYPES:
            fmt = "f" if self.datatype == "float" else "d"
            return struct.pack(fmt, self.value)
        else:
            signed = self.datatype in self.SIGNED_TYPES
            return self.value.to_bytes(
                self.DATATYPE_SIZES[self.datatype], "little", signed=signed
            )

    def get_dtype_byte(self) -> bytes:
        """Get the datatype code as a single byte"""
        return self.DATATYPE_TO_CODE[self.datatype].to_bytes(1, "little")

    def __repr__(self):
        return f"{self.signal_name}: {self.datatype} = {self.value}"
