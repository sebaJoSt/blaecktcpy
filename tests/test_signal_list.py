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

    def test_append_invalidates_cache(self):
        _ = self.collection["temperature"]  # populate cache
        new_sig = Signal("wind", "float", 5.0)
        self.collection.append(new_sig)
        assert self.collection["wind"].value == 5.0

    def test_del_invalidates_cache(self):
        _ = self.collection["humidity"]  # populate cache
        del self.collection[1]
        with pytest.raises(KeyError):
            _ = self.collection["humidity"]
        assert self.collection["pressure"].value == 1013.25

    def test_slice_delete_invalidates_cache(self):
        _ = self.collection["temperature"]
        del self.collection[:2]
        assert len(self.collection) == 1
        assert self.collection["pressure"].value == 1013.25
        with pytest.raises(KeyError):
            _ = self.collection["temperature"]

    def test_clear_invalidates_cache(self):
        _ = self.collection["temperature"]
        self.collection.clear()
        with pytest.raises(KeyError):
            _ = self.collection["temperature"]

    def test_iadd_invalidates_cache(self):
        _ = self.collection["temperature"]
        self.collection += [Signal("wind", "float", 3.0)]
        assert self.collection["wind"].value == 3.0

    def test_extend_invalidates_cache(self):
        _ = self.collection["temperature"]
        self.collection.extend([Signal("wind", "float", 3.0)])
        assert len(self.collection) == 4
        assert self.collection["wind"].value == 3.0

    def test_remove_invalidates_cache(self):
        target = self.collection[1]  # humidity
        _ = self.collection["humidity"]
        self.collection.remove(target)
        assert len(self.collection) == 2
        with pytest.raises(KeyError):
            _ = self.collection["humidity"]

    def test_pop_returns_signal_and_invalidates_cache(self):
        _ = self.collection["pressure"]
        popped = self.collection.pop()
        assert popped.signal_name == "pressure"
        assert len(self.collection) == 2
        with pytest.raises(KeyError):
            _ = self.collection["pressure"]

    def test_pop_by_index(self):
        popped = self.collection.pop(0)
        assert popped.signal_name == "temperature"
        assert len(self.collection) == 2

    def test_setitem_invalidates_cache(self):
        _ = self.collection["temperature"]
        replacement = Signal("altitude", "float", 500.0)
        self.collection[0] = replacement
        assert self.collection["altitude"].value == 500.0
        with pytest.raises(KeyError):
            _ = self.collection["temperature"]

    def test_insert_invalidates_cache(self):
        _ = self.collection["temperature"]
        new_sig = Signal("wind", "float", 5.0)
        self.collection.insert(1, new_sig)
        assert len(self.collection) == 4
        assert self.collection["wind"].value == 5.0
        assert self.collection[1].signal_name == "wind"

    def test_index_of_existing(self):
        assert self.collection.index_of("temperature") == 0
        assert self.collection.index_of("humidity") == 1
        assert self.collection.index_of("pressure") == 2

    def test_index_of_missing(self):
        assert self.collection.index_of("nonexistent") is None
