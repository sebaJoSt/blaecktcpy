import struct
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from blaecktcpy import Signal


INTEGER_CASES = [
    ("bool", 0, 1, 1, False),
    ("byte", 0, 255, 1, False),
    ("short", -32768, 32767, 2, True),
    ("unsigned short", 0, 65535, 2, False),
    ("int", -2147483648, 2147483647, 4, True),
    ("unsigned int", 0, 4294967295, 4, False),
    ("long", -2147483648, 2147483647, 4, True),
    ("unsigned long", 0, 4294967295, 4, False),
]

FLOAT_CASES = [
    ("float", -3.4028235e38, "<f"),
    ("float", 3.4028235e38, "<f"),
    ("double", -1.7976931348623157e308, "<d"),
    ("double", 1.7976931348623157e308, "<d"),
]


@pytest.mark.parametrize(
    ("datatype", "value", "byte_width", "signed"),
    [
        (datatype, min_value, byte_width, signed)
        for datatype, min_value, _, byte_width, signed in INTEGER_CASES
    ]
    + [
        (datatype, max_value, byte_width, signed)
        for datatype, _, max_value, byte_width, signed in INTEGER_CASES
    ],
)
def test_integer_datatypes_accept_boundary_values(datatype, value, byte_width, signed):
    signal = Signal("boundary", datatype, value)
    expected = bool(value) if datatype == "bool" else value
    assert signal.value == expected
    assert signal.to_bytes() == int(expected).to_bytes(
        byte_width, "little", signed=signed
    )


@pytest.mark.parametrize(
    ("datatype", "invalid_value"),
    [
        (datatype, min_value - 1)
        for datatype, min_value, _, _, _ in INTEGER_CASES
    ]
    + [
        (datatype, max_value + 1)
        for datatype, _, max_value, _, _ in INTEGER_CASES
    ],
)
def test_integer_datatypes_reject_values_out_of_range(datatype, invalid_value):
    with pytest.raises(ValueError):
        Signal("boundary", datatype, invalid_value)

    seed_value = 0 if datatype != "bool" else False
    signal = Signal("boundary", datatype, seed_value)
    with pytest.raises(ValueError):
        signal.value = invalid_value


@pytest.mark.parametrize("datatype", [datatype for datatype, *_ in INTEGER_CASES])
@pytest.mark.parametrize("invalid_value", [12.5, "42"])
def test_integer_datatypes_reject_lossy_values(datatype, invalid_value):
    signal = Signal("typed", datatype, 0 if datatype != "bool" else False)
    with pytest.raises(ValueError):
        signal.value = invalid_value


@pytest.mark.parametrize("datatype", [datatype for datatype, *_ in INTEGER_CASES])
def test_integer_datatypes_accept_integral_floats(datatype):
    signal = Signal("typed", datatype, 0 if datatype != "bool" else False)
    signal.value = 1.0 if datatype == "bool" else 12.0
    expected = True if datatype == "bool" else 12
    assert signal.value == expected


@pytest.mark.parametrize(("datatype", "value", "fmt"), FLOAT_CASES)
def test_float_and_double_boundary_values_serialize_little_endian(datatype, value, fmt):
    signal = Signal("float-boundary", datatype, value)
    assert signal.value == float(value)
    assert signal.to_bytes() == struct.pack(fmt, float(value))
