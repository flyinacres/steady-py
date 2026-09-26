"""Parsing pin strings: the extras tag on a manifest name, and PEP 440 local version labels."""
import pytest

from steady_py import installed


def test_split_pin_name_extracts_extra():
    name, extra = installed.split_pin_name("pandas[test]")
    assert name == "pandas"
    assert extra == "test"


def test_split_pin_name_no_extra():
    name, extra = installed.split_pin_name("pandas")
    assert name == "pandas"
    assert extra is None


def test_all_requested_extras_are_parsed():
    name, extras = installed.split_pin_extras("multi-extra-pkg[b,a]")
    assert name == "multi-extra-pkg"
    assert extras == frozenset({"a", "b"})


@pytest.mark.parametrize("version, expected", [
    ("2.3.1+cu121", True),    # a local label: PyPI can never host it
    ("2.3.1", False),
    ("not-a-version", False),
])
def test_has_local_version_identifier(version, expected):
    assert installed.has_local_version_identifier(version) is expected
