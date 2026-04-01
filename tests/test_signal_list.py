"""Tests for SignalList collection access patterns."""

import pytest

from blaecktcpy import Signal, SignalList


class TestSignalList:
    """Verify SignalList collection access patterns."""

    def setup_method(self):
        self.signals = [
            Signal("temperature", "float", 22.5),
            Signal("humidity", "float", 65.0),
            Signal("pressure", "float", 1013.25),
        ]
        self.collection = SignalList(self.signals)

    def test_access_by_index(self):
        assert self.collection[0].signal_name == "temperature"
        assert self.collection[1].signal_name == "humidity"
        assert self.collection[2].signal_name == "pressure"

    def test_access_by_name(self):
        assert self.collection["temperature"].value == 22.5
        assert self.collection["humidity"].value == 65.0

    def test_index_out_of_range(self):
        with pytest.raises(IndexError):
            _ = self.collection[5]

    def test_name_not_found(self):
        with pytest.raises(KeyError, match="wind_speed"):
            _ = self.collection["wind_speed"]

    def test_invalid_key_type(self):
        with pytest.raises(TypeError, match="float"):
            _ = self.collection[1.5]

    def test_len(self):
        assert len(self.collection) == 3

    def test_iter(self):
        names = [s.signal_name for s in self.collection]
        assert names == ["temperature", "humidity", "pressure"]

    def test_empty_collection(self):
        empty = SignalList([])
        assert len(empty) == 0
        assert list(empty) == []

    def test_value_updates_propagate(self):
        self.collection["temperature"].value = 30.0
        assert self.signals[0].value == 30.0
