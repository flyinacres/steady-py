"""The same policy as tests/behavior: every characterization test runs against the strict fake PyPI."""
from tests.behavior.conftest import _offline  # noqa: F401  (autouse fixture, one owner)
