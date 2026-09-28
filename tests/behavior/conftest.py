"""Every behavior test runs against the strict fake PyPI, so no test reaches the real one."""
import pytest


@pytest.fixture(autouse=True)
def _offline(pypi):
    pass
