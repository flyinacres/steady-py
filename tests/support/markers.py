"""Matrix bookkeeping for tests: `finding` tags a test with a triage-matrix ID, `known_bug` marks
a test that documents an unfixed finding. Tier markers and the hooks that read these live in
tests/conftest.py."""
import pytest

TIERS = ("venv", "kernel", "docker")


class _Marks(list):
    """A list of marks that also works as a decorator, so one helper serves `@known_bug(...)`,
    `pytest.param(..., marks=known_bug(...))` and `pytestmark = known_bug(...)`."""

    def __call__(self, func):
        for mark in self:
            func = mark(func)
        return func


def finding(finding_id: str) -> _Marks:
    return _Marks([pytest.mark.finding(finding_id)])


def known_bug(finding_id: str, why: str) -> _Marks:
    """Strict xfail that expects an AssertionError: a harness crash (TypeError, missing fixture)
    is a real failure, and a pass (XPASS) fails until the mark is removed with the fix."""
    xfail = pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{finding_id}: {why}")
    return _Marks([*finding(finding_id), xfail])
