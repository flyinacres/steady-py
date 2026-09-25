"""Small helpers used by every layer: package-name normalization, IPython detection, stderr
silencing, and per-run memoization."""
import contextlib
import functools
import os
import re
from typing import Any, Callable, Dict, Tuple


def canonicalize_pkg_name(name: str) -> str:
    """PEP 503 normalization: lowercase and replace runs of [-_.] with a single hyphen."""
    return re.sub(r"[-_.]+", "-", name).strip("-").lower()


def is_running_in_ipython() -> bool:
    """Checks whether execution is occurring inside an active IPython/Jupyter kernel."""
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False


@contextlib.contextmanager
def silence_fd2_stderr():
    """
    Temporarily redirects OS-level file descriptor 2 (stderr) to os.devnull.
    Prevents low-level C++ drivers (e.g. CUDA cuInit 303) from polluting output.
    Ensures safe, generator-compliant exception propagation without crashing.
    """
    old_stderr_fd = None
    devnull_fd = None
    try:
        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            old_stderr_fd = os.dup(2)
            os.dup2(devnull_fd, 2)
        except Exception:
            pass

        yield

    finally:
        if old_stderr_fd is not None:
            try:
                os.dup2(old_stderr_fd, 2)
                os.close(old_stderr_fd)
            except Exception:
                pass
        if devnull_fd is not None:
            try:
                os.close(devnull_fd)
            except Exception:
                pass


def _memoize_for_run(func: Callable) -> Callable:
    """Memoizes functions scoped to a single run, handling Set, List, and Dict arguments."""
    cache: Dict[Tuple[Any, ...], Any] = {}

    def _cache_key_part(value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(sorted((k, _cache_key_part(v)) for k, v in value.items()))
        if isinstance(value, set):
            return frozenset(_cache_key_part(item) for item in value)
        if isinstance(value, list):
            return tuple(_cache_key_part(item) for item in value)
        if isinstance(value, tuple):
            return tuple(_cache_key_part(item) for item in value)
        return value

    def _defensive_copy(value: Any) -> Any:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, set):
            return set(value)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, tuple):
            return tuple(_defensive_copy(item) for item in value)
        return value

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = (
            tuple(_cache_key_part(a) for a in args),
            tuple(sorted((k, _cache_key_part(v)) for k, v in kwargs.items()))
        )
        if key not in cache:
            cache[key] = func(*args, **kwargs)
        return _defensive_copy(cache[key])

    wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
    return wrapper
