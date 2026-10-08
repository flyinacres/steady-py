"""Version numbers, fixed names and label constants, and the static lookup tables shared by every layer."""
import importlib.metadata
import sys
from typing import Dict, List, Set, Tuple


TOOL_VERSION: str
try:
    TOOL_VERSION = importlib.metadata.version("steady-py")
except importlib.metadata.PackageNotFoundError:
    TOOL_VERSION = "0.0.0+unknown"
SCHEMA_VERSION: str = "1.0"
MANIFEST_SCHEMA_VERSION: str = "1.0"


DEFAULT_UNIVERSAL_MANIFEST_NAME: str = "requirements-all.txt"

HELP_URL: str = "https://github.com/flyinacres/steady-py/blob/main/HELP.md"

# Fixed first line of the generated Cell 1. Shared by the generator and by is_prior_setup_cell,
# so what the tool writes and what it later recognizes as its own cannot drift apart.
SETUP_MARKDOWN_HEADING: str = "### 🛠️ Environment Setup & Dependency Verification"

DEFAULT_IGNORED_DIRS: Set[str] = {  # a fast path by name; the markers below catch any other name
    ".git", ".venv", "venv", "env", "__pycache__", ".ipynb_checkpoints", "build", "dist",
    "site-packages", "dist-packages",  # installed packages (wheels ship notebooks)
}
ENVIRONMENT_DIR_MARKERS: Tuple[str, ...] = ("pyvenv.cfg", "conda-meta")  # a venv or a conda env

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


class GuardKind:
    """Guard.kind values."""
    IF = "if"
    TRY = "try"
    EXCEPT = "except"
    FUNCTION = "function"
    SHELL_JOINED = "shell_joined"
    SHELL_CONDITIONAL = "shell_conditional"


class Invocation:
    """InstallLine.invocation values."""
    LINE_MAGIC = "line_magic"
    SHELL_ESCAPE = "shell_escape"
    SHELL_CELL = "shell_cell"
    PYTHON_CALL = "python_call"


class Tool:
    """InstallLine.tool values."""
    PIP = "pip"
    UV = "uv"
    CONDA = "conda"
    SYSTEM = "system"


class Readability:
    """InstallLine.readability values: whether a static scan can know what the line installs."""
    LITERAL = "literal"
    COMPUTED = "computed"  # `$pkg`, `{var}` or a non-literal call argument, known only when it runs
    UNREADABLE = "unreadable"  # the shell couldn't split it either (an unclosed quote)


class TargetKind:
    """InstallTarget.kind values (fix_plan.md, section 3.1, item 8)."""
    REQUIREMENT = "requirement"
    DIRECT_REFERENCE = "direct_reference"  # `name @ url`, stored as written (CH3)
    URL = "url"
    PATH = "path"
    EDITABLE = "editable"
    REQUIREMENTS_FILE = "requirements_file"
    CONSTRAINTS_FILE = "constraints_file"
    NAME = "name"  # a conda or system package, parsed no further than its name
    COMPUTED = "computed"
    INVALID = "invalid"  # pip rejects the whole command


# Options that change what a pip line installs; each pin and raw install carries its line's (G14).
CARRIED_PIP_OPTIONS: Set[str] = {
    "--index-url", "--extra-index-url", "--find-links", "--trusted-host", "--no-index", "--no-deps", "--pre",
}

# Options naming somewhere other than the kernel's environment (fix_plan.md, section 3.1, item 5).
OTHER_ENVIRONMENT_PIP_OPTIONS: Set[str] = {"--target", "--prefix", "--root"}

# pip install's own options, as optparse reads them: each spelling maps to (canonical long name,
# takes a value). Taken from pip's install parser; a unit test keeps it in step with pip.
_PIP_OPTION_GROUPS: List[Tuple[Tuple[str, ...], bool]] = [
    (("--requirement", "-r"), True), (("--constraint", "-c"), True), (("--editable", "-e"), True),
    (("--target", "-t"), True), (("--platform",), True), (("--python-version",), True),
    (("--implementation",), True), (("--abi",), True), (("--root",), True), (("--prefix",), True),
    (("--src", "--source", "--source-dir", "--source-directory"), True), (("--upgrade-strategy",), True),
    (("--config-settings", "-C"), True), (("--global-option",), True), (("--no-binary",), True),
    (("--only-binary",), True), (("--progress-bar",), True), (("--root-user-action",), True),
    (("--report",), True), (("--index-url", "-i", "--pypi-url"), True), (("--extra-index-url",), True),
    (("--find-links", "-f"), True), (("--python",), True), (("--log", "--log-file", "--local-log"), True),
    (("--keyring-provider",), True), (("--proxy",), True), (("--retries",), True),
    (("--timeout", "--default-timeout"), True), (("--exists-action",), True), (("--trusted-host",), True),
    (("--cert",), True), (("--client-cert",), True), (("--cache-dir",), True), (("--use-feature",), True),
    (("--use-deprecated",), True), (("--group",), True),
    (("--no-deps", "--no-dependencies"), False), (("--pre",), False), (("--dry-run",), False),
    (("--user",), False), (("--no-user",), False), (("--upgrade", "-U"), False), (("--force-reinstall",), False),
    (("--ignore-installed", "-I"), False), (("--ignore-requires-python",), False),
    (("--no-build-isolation",), False), (("--use-pep517",), False), (("--no-use-pep517",), False),
    (("--check-build-dependencies",), False), (("--break-system-packages",), False), (("--compile",), False),
    (("--no-compile",), False), (("--no-warn-script-location",), False), (("--no-warn-conflicts",), False),
    (("--prefer-binary",), False), (("--require-hashes",), False), (("--no-clean",), False),
    (("--no-index",), False), (("--help", "-h"), False), (("--debug",), False), (("--isolated",), False),
    (("--require-virtualenv", "--require-venv"), False), (("--verbose", "-v"), False),
    (("--version", "-V"), False), (("--quiet", "-q"), False), (("--no-input",), False),
    (("--no-cache-dir",), False), (("--disable-pip-version-check",), False), (("--no-color",), False),
    (("--no-python-version-warning",), False),
]
PIP_OPTIONS: Dict[str, Tuple[str, bool]] = {
    spelling: (names[0], takes_value) for names, takes_value in _PIP_OPTION_GROUPS for spelling in names
}

# `uv pip install` options pip doesn't have that take a value, so their value isn't read as a package.
UV_PIP_VALUE_OPTIONS: Dict[str, str] = {
    spelling: names[0] for names in (
        ("--python", "-p"), ("--upgrade-package", "-P"), ("--reinstall-package",), ("--index-strategy",),
        ("--index",), ("--default-index",), ("--torch-backend",), ("--prerelease",), ("--resolution",),
        ("--exclude-newer",), ("--link-mode",), ("--extra",), ("--overrides",), ("--build-constraints", "-b"),
        ("--python-platform",), ("--color",), ("--directory",), ("--project",), ("--config-file",),
        ("--no-build-isolation-package",), ("--config-setting",),
    ) for spelling in names
}


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

TOOL_IMPORT_NAME = "steady_py"  # never a dependency: Cell 2 installs it

PLATFORM_PSEUDO_MODULES: Set[str] = {
    "dbutils",
    "kaggle_secrets",
    "google.colab",
    "pyspark.dbutils",
    "__main__",
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

# Cell magics whose body IPython runs as Python; black's list (black.handle_ipynb_magics). Bodies of
# other cell magics (%%html, %%sql) aren't Python and aren't analyzed.
PYTHON_CELL_MAGICS: Set[str] = {"capture", "prun", "pypy", "python", "python3", "time", "timeit"}

