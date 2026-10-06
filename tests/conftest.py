import sys

import numpy as _real_numpy
import pytest


@pytest.fixture
def real_numpy(monkeypatch):
    """Several test modules replace sys.modules["numpy"] with a stub at import
    time. Tests that need real array math or pickling restore the real module."""
    monkeypatch.setitem(sys.modules, "numpy", _real_numpy)
    return _real_numpy
