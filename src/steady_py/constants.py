"""Version numbers, fixed names and label constants, and the static lookup tables shared by every layer."""
import importlib.metadata
import sys
from typing import Dict, Set


TOOL_VERSION: str
try:
    TOOL_VERSION = importlib.metadata.version("steady-py")
except importlib.metadata.PackageNotFoundError:
    TOOL_VERSION = "0.0.0+unknown"
SCHEMA_VERSION: str = "1.0"
MANIFEST_SCHEMA_VERSION: str = "1.0"


DEFAULT_UNIVERSAL_MANIFEST_NAME: str = "requirements-all.txt"

HELP_URL: str = "https://github.com/flyinacres/notebook_env/blob/main/HELP.md"

# Fixed first line of the generated Cell 1. Shared by the generator and by is_prior_setup_cell,
# so what the tool writes and what it later recognizes as its own cannot drift apart.
SETUP_MARKDOWN_HEADING: str = "### 🛠️ Environment Setup & Dependency Verification"

DEFAULT_IGNORED_DIRS: Set[str] = {
    ".git", ".venv", "venv", "env", "__pycache__", ".ipynb_checkpoints", "build", "dist"
}

SUPPORTED_GPU_FRAMEWORKS: Set[str] = {"torch", "tensorflow", "jax"}

CANONICAL_TO_FRAMEWORK_DISPLAY: Dict[str, str] = {
    "torch": "PyTorch",
    "tensorflow": "TensorFlow",
    "jax": "JAX"
}


class StatusLabel:
    """Standardized metadata and language classification status labels."""
    PYTHON = "python"
    CORRUPTED = "corrupted"
    ERROR = "error"
    UNKNOWN = "unknown"


# Names shared by the drift/validation code and the JSON it emits. Plain string constants (like
# StatusLabel above), not Enum members, so values embed in the manifest literal, serialize to JSON and
# format into messages as ordinary strings on every supported Python version. Using the constant
# instead of a bare literal turns a typo into an immediate AttributeError instead of a finding that
# silently never matches.

class Signal:
    """DriftFinding.signal values."""
    CONFLICT = "conflict"
    YANKED = "yanked"
    REMOVED = "removed"
    NOT_FOUND_ON_PYPI = "not_found_on_pypi"
    STALE = "stale"
    MAJOR_BUMP = "major_bump"
    UNSUPPORTED_PYTHON = "unsupported_python"
    UNVERIFIABLE_CUSTOM_INDEX = "unverifiable_custom_index"
    CHECK_ERROR = "check_error"
    TAMPERED = "tampered"
    LOCAL_MODULE_MISSING = "local_module_missing"
    LOCAL_MODULE_UNVERIFIABLE = "local_module_unverifiable"


class Severity:
    """DriftFinding.severity values. NOTICE is a known custom source demoted at check time."""
    CONFIRMED = "confirmed"
    HEURISTIC = "heuristic"
    ERROR = "error"
    NOTICE = "notice"


class BaselineStatus:
    """DriftFinding.baseline_status values (check-drift against a generation-time baseline)."""
    NEW = "new"
    KNOWN = "known"
    NOT_CHECKED_AT_GENERATION = "not_checked_at_generation"


class DependencyStatus:
    """DependencyEntry.status values."""
    PINNED = "pinned"
    GUARDED = "guarded"
    PLATFORM_PSEUDO_MODULE = "platform_pseudo_module"
    BUILD_TOOL = "build_tool"
    LOCAL_MODULE = "local_module"
    AUXILIARY_TOOL = "auxiliary_tool"
    WRITEFILE_SCRIPT = "writefile_script"
    DIRECT_REFERENCE = "direct_reference"
    SYSTEM_PATH = "system_path"


class FetchStatus:
    """Outcome of a PyPI metadata fetch."""
    FOUND = "found"
    NOT_FOUND = "not_found"
    NETWORK_ERROR = "network_error"


class ReportKind:
    """DriftCheckReport.kind values."""
    CHECK = "check"
    VALIDATION = "validation"


IMPORT_TO_PYPI_MAP: Dict[str, str] = {
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "attr": "attrs",
    "serial": "pyserial",
    "dotenv": "python-dotenv",
    "mpl_toolkits": "matplotlib",
    "skimage": "scikit-image"
}

# Standard build and packaging bootstrap tools; excluded from requirement lockfiles
BUILD_AND_PACKAGING_TOOLS: Set[str] = {
    "pip",
    "setuptools",
    "wheel"
}

PLATFORM_PSEUDO_MODULES: Set[str] = {
    "dbutils",
    "kaggle_secrets",
    "google.colab",
    "pyspark.dbutils",
    "__main__",
    "steady_py",
    "databricks"
}

TRANSITIVE_FRAMEWORK_MAP: Dict[str, str] = {
    "fastai": "torch",
    "torchvision": "torch",
    "torchaudio": "torch",
    "timm": "torch",
    "keras": "tensorflow",
    "flax": "jax",
}

STD_LIB: Set[str] = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else {
    "os", "sys", "re", "json", "ast", "subprocess", "datetime", "math", "random", 
    "time", "pathlib", "typing", "collections", "itertools", "functools", "shutil"
}

SHELL_CELL_MAGICS: Set[str] = {
    "%%bash", "%%sh", "%%zsh", "%%script", "%%cmd", "%%powershell"
}

