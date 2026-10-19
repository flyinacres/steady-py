"""Matrix bookkeeping for tests: `finding` tags a test with a triage-matrix ID, `known_bug` marks
a test that documents an unfixed finding. Tier markers and the hooks that read these live in
tests/conftest.py. `unavailable` is how a test whose tier can't run here bows out.

CI sets STEADY_PY_REQUIRE_TIERS, under which a missing prerequisite fails the test instead of
skipping it, so a tier that silently didn't run can't pass."""
import os
from typing import NoReturn

import pytest

TIERS = ("venv", "kernel", "docker", "corpus")


class _Marks(list):
    """A list of marks that also works as a decorator, so one helper serves `@known_bug(...)`,
    `pytest.param(..., marks=known_bug(...))` and `pytestmark = known_bug(...)`."""

    def __call__(self, func):
        for mark in self:
            func = mark(func)
        return func


def unavailable(reason: str) -> NoReturn:
    """Skips the test because its tier can't run here, or fails it when STEADY_PY_REQUIRE_TIERS is set."""
    if os.environ.get("STEADY_PY_REQUIRE_TIERS"):
        pytest.fail(f"a required tier can't run: {reason}", pytrace=False)
    pytest.skip(reason)


def finding(finding_id: str) -> _Marks:
    return _Marks([pytest.mark.finding(finding_id)])


def known_bug(finding_id: str, why: str) -> _Marks:
    """Strict xfail that expects an AssertionError: a harness crash (TypeError, missing fixture)
    is a real failure, and a pass (XPASS) fails until the mark is removed with the fix."""
    xfail = pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{finding_id}: {why}")
    return _Marks([*finding(finding_id), xfail])
