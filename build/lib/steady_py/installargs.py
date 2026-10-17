"""Reading one install command as the shell and the installer read it: shell words and operators,
which tool a command runs, and the targets and options of its arguments (fix_plan.md, step 1.4).

Pure functions over text; magics.py decides where commands come from and what they mean for a
notebook."""
import os
import re
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from steady_py.constants import (
    OTHER_ENVIRONMENT_PIP_OPTIONS, PIP_OPTIONS, UV_PIP_VALUE_OPTIONS, Readability, TargetKind, Tool,
)
from steady_py.models import InstallOption, InstallTarget

SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;", "\n"}
JOINS = {"&&", "||"}
_OPERATORS = ("&&", "||", ";;", "|&", ">>", ">&", "&>", "<<", "<&", "<>", ";", "|", "&", "(", ")", "<", ">")
_REDIRECTS = {">>", ">&", "&>", "<<", "<&", "<>", "<", ">"}
_SHELL_OPENERS = {"if", "case", "for", "while", "until", "select"}
_SHELL_CLOSERS = {"fi", "esac", "done"}
_SHELL_LEADS = {"then", "else", "elif", "do", "!", "{", "time"} | _SHELL_OPENERS
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PIP = re.compile(r"^pip(3(\.\d+)?)?$")
_PYTHON = re.compile(r"^(python(3(\.\d+)?)?|\{sys\.executable\})$")
_CONDA_FRONTS = {"conda", "mamba", "micromamba"}
SYSTEM_TOOLS = {"apt-get", "brew", "yum"}
_CONDA_VALUE_OPTIONS = {"-c", "--channel", "-n", "--name", "-p", "--prefix", "--file"}
_CONDA_RUN_VALUE_OPTIONS = {"-n", "--name", "-p", "--prefix", "--cwd"}
_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".zip")
_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_LEGACY_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?((?:===?|!=|~=|<=?|>=?)[^;\s@]+)$")
COMPUTED_MARK = "{?}"  # stands in for a non-literal Python call argument


class UnreadableCommand(ValueError):
    """The shell couldn't split the command either (an unclosed quote)."""


def tokenize(command: str) -> List[str]:
    """Shell words and operators, quotes removed. `#` starts a comment only at the start of a word,
    as in the shell (G18), so `url#egg=x` keeps its fragment. Redirections and their targets are
    dropped. Raises UnreadableCommand for an unclosed quote."""
    tokens: List[str] = []
    word: List[str] = []
    in_word = False
    skip_next = False
    i, n = 0, len(command)

    def finish() -> None:
        nonlocal in_word, skip_next
        if in_word:
            if skip_next:
                skip_next = False
            else:
                tokens.append("".join(word))
        word.clear()
        in_word = False

    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                word.append(command[i + 1])
                in_word = True
            i += 2
            continue
        if ch in "'\"":
            end = command.find(ch, i + 1)
            if ch == '"':
                end = i + 1
                while end < n and command[end] != '"':
                    end += 2 if command[end] == "\\" else 1
            if end >= n or end < 0:
                raise UnreadableCommand(f"no closing {ch}")
            body = command[i + 1:end]
            word.append(body if ch == "'" else re.sub(r'\\([\\"$`])', r"\1", body))
            in_word = True
            i = end + 1
            continue
        if ch == "#" and not in_word:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == "\n":
            finish()
            tokens.append("\n")
            i += 1
            continue
        if ch.isspace():
            finish()
            i += 1
            continue
        op = next((o for o in _OPERATORS if command.startswith(o, i)), None)
        if op:
            if op in _REDIRECTS and in_word and "".join(word).isdigit():
                word.clear()
                in_word = False  # `2>&1`: the digits are the descriptor
            finish()
            if op in _REDIRECTS:
                skip_next = True
            else:
                tokens.append(op)
            i += len(op)
            continue
        word.append(ch)
        in_word = True
        i += 1
    finish()
    return tokens


@dataclass
class ShellSegment:
    """One simple command of a shell line, after leading shell keywords."""
    words: List[str]
    opens: Optional[str] = None  # if, case, for, ... when this segment starts a block
    closes: bool = False


def segments(tokens: List[str]) -> Iterator[ShellSegment]:
    """Simple commands between separators. A leading keyword (`then pip install x`) is stripped and
    reported, so callers can track `if`/`case` blocks."""
    current: List[str] = []
    for tok in [*tokens, ";"]:
        if tok not in SEPARATORS:
            current.append(tok)
            continue
        words, opens, closes = current, None, False
        while words and words[0] in _SHELL_LEADS:
            opens = words[0] if words[0] in _SHELL_OPENERS else opens
            words = words[1:]
        if words and words[0] in _SHELL_CLOSERS:
            closes, words = True, words[1:]
        if words or opens or closes:
            yield ShellSegment(words, opens, closes)
        current = []


@dataclass
class ParsedCommand:
    """What one simple command installs, if it's an install command."""
    tool: str
    readability: str = Readability.LITERAL
    targets: List[InstallTarget] = field(default_factory=list)
    options: List[InstallOption] = field(default_factory=list)
    elsewhere: str = ""


def _skip_assignments(words: List[str]) -> List[str]:
    i = 0
    while i < len(words) and _ASSIGNMENT.match(words[i]):
        i += 1
    return words[i:]


def parse_command(words: List[str]) -> Optional[ParsedCommand]:
    """The install command `words` run, or None if they don't install packages."""
    words = _skip_assignments(words)
    if not words:
        return None
    front = os.path.basename(words[0]) if "/" in words[0] else words[0]
    rest = words[1:]
    if _PIP.match(front):
        return _pip_install(rest, Tool.PIP)
    if _PYTHON.match(front):
        while rest and rest[0].startswith("-") and rest[0] != "-m":
            rest = rest[1:]
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] == "pip":
            return _pip_install(rest[2:], Tool.PIP)
        return None
    if front == "uv" and rest[:1] == ["pip"]:
        return _pip_install(rest[1:], Tool.UV)
    if front in _CONDA_FRONTS:
        return _conda(rest)
    if front in SYSTEM_TOOLS:
        names = [w for w in rest if not w.startswith("-")]
        if names[:1] == ["install"]:
            return ParsedCommand(Tool.SYSTEM, targets=[InstallTarget(TargetKind.NAME, w, name=w) for w in names[1:]])
    return None


def _conda(words: List[str]) -> Optional[ParsedCommand]:
    if words[:1] == ["run"]:
        env = ""
        i = 1
        while i < len(words) and words[i].startswith("-"):
            opt, _, inline = words[i].partition("=")
            if opt in _CONDA_RUN_VALUE_OPTIONS:
                value = inline or (words[i + 1] if i + 1 < len(words) else "")
                if opt in {"-n", "--name", "-p", "--prefix"} and not (opt in {"-n", "--name"} and value == "base"):
                    env = f"conda run {opt} {value}"
                i += 1 if inline else 2
            else:
                i += 1
        inner = parse_command(words[i:])
        if inner is not None and env and not inner.elsewhere:
            inner.elsewhere = env
        return inner
    sub = words[:2] if words[:1] == ["env"] else words[:1]
    if sub not in (["install"], ["create"], ["env", "create"], ["env", "update"]):
        return None
    file_options = {"--file", "-f"} if sub[0] == "env" else {"--file"}
    parsed = ParsedCommand(Tool.CONDA)
    args = words[len(sub):]
    i = 0
    while i < len(args):
        word = args[i]
        opt, eq, inline = word.partition("=")
        if word.startswith("-") and (opt in _CONDA_VALUE_OPTIONS or opt in file_options):
            value = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
            i += 1 if eq else 2
            if opt in file_options:
                parsed.targets.append(InstallTarget(TargetKind.REQUIREMENTS_FILE, value))
            elif opt in {"-n", "--name", "-p", "--prefix"} and not (opt in {"-n", "--name"} and value == "base"):
                parsed.elsewhere = f"{opt} {value}"
            parsed.options.append(InstallOption(opt, value, known=True))
            continue
        if word.startswith("-"):
            parsed.options.append(InstallOption(word, None, known=False))
        else:
            parsed.targets.append(InstallTarget(TargetKind.NAME, word, name=word))
        i += 1
    if any(_is_computed(t.text) for t in parsed.targets):
        parsed.readability = Readability.COMPUTED
    return parsed


def _long_option(name: str, uv: bool) -> Optional[Tuple[str, bool]]:
    """(canonical name, takes a value) for a long option, accepting a unique prefix as optparse does."""
    if uv and name in UV_PIP_VALUE_OPTIONS:
        return UV_PIP_VALUE_OPTIONS[name], True
    if name in PIP_OPTIONS:
        return PIP_OPTIONS[name]
    if uv:
        return None
    matches = {PIP_OPTIONS[s] for s in PIP_OPTIONS if s.startswith("--") and s.startswith(name)}
    return matches.pop() if len(matches) == 1 else None


def _short_option(letter: str, uv: bool) -> Optional[Tuple[str, bool]]:
    spelling = f"-{letter}"
    if uv and spelling in UV_PIP_VALUE_OPTIONS:
        return UV_PIP_VALUE_OPTIONS[spelling], True
    return PIP_OPTIONS.get(spelling)


def _pip_install(words: List[str], tool: str) -> Optional[ParsedCommand]:
    """`words` after the pip front-end: global options, then `install` and its arguments."""
    uv = tool == Tool.UV
    while words and words[0].startswith("-"):
        known = _long_option(words[0].partition("=")[0], uv) if words[0].startswith("--") else _short_option(words[0][1:2], uv)
        takes_value = bool(known and known[1] and "=" not in words[0] and (words[0].startswith("--") or len(words[0]) == 2))
        words = words[2:] if takes_value else words[1:]
    if words[:1] != ["install"]:
        return None
    parsed = ParsedCommand(tool)
    raw: List[Tuple[str, str]] = []  # (canonical option or "", value or target)
    args = words[1:]
    i = 0
    while i < len(args):
        word = args[i]
        i += 1
        if word.startswith("--") and len(word) > 2:
            name, eq, inline = word.partition("=")
            known = _long_option(name, uv)
            if known is None:
                parsed.options.append(InstallOption(word, None, known=False))
                continue
            canonical, takes_value = known
            if not takes_value:
                parsed.options.append(InstallOption(canonical))
                continue
            if not eq:
                if i >= len(args):
                    raw.append(("", word))  # pip: the option requires an argument
                    continue
                inline = args[i]
                i += 1
            raw.append((canonical, inline))
        elif word.startswith("-") and len(word) > 1:
            j = 1
            while j < len(word):
                known = _short_option(word[j], uv)
                if known is None:
                    parsed.options.append(InstallOption(word if j == 1 else f"-{word[j:]}", None, known=False))
                    break
                canonical, takes_value = known
                if takes_value:
                    value = word[j + 1:]
                    if not value and i >= len(args):
                        raw.append(("", word))  # pip: the option requires an argument
                        break
                    if not value:
                        value = args[i]
                        i += 1
                    raw.append((canonical, value))
                    break
                parsed.options.append(InstallOption(canonical))
                j += 1
        else:
            raw.append(("", word))
    for canonical, value in raw:
        if canonical in {"--requirement", "--requirements-from-script"}:
            parsed.targets.append(InstallTarget(TargetKind.REQUIREMENTS_FILE, value))
        elif canonical == "--constraint":
            parsed.targets.append(InstallTarget(TargetKind.CONSTRAINTS_FILE, value))
        elif canonical == "--editable":
            parsed.targets.append(InstallTarget(TargetKind.EDITABLE, value))
        elif canonical:
            parsed.options.append(InstallOption(canonical, value))
            if canonical in OTHER_ENVIRONMENT_PIP_OPTIONS and not parsed.elsewhere:
                parsed.elsewhere = f"{canonical} {value}"
        else:
            parsed.targets.append(classify_target(value))
    if any(t.kind == TargetKind.COMPUTED or _is_computed(t.text) for t in parsed.targets) \
            or any(_is_computed(o.value or "") for o in parsed.options):
        parsed.readability = Readability.COMPUTED
    return parsed


def _is_computed(text: str) -> bool:
    """IPython expands `$name` and `{expr}` before the shell runs; a static scan can't know them (G4)."""
    return "$" in text or "{" in text or "}" in text


def _looks_like_path(token: str) -> bool:
    return (token.startswith((".", "/", "\\", "~")) or bool(_WINDOWS_PATH.match(token))
            or "/" in token or "\\" in token or token.lower().endswith(_ARCHIVE_SUFFIXES))


def classify_target(token: str) -> InstallTarget:
    """A positional pip argument as pip reads it."""
    if token.startswith("-"):
        return InstallTarget(TargetKind.INVALID, token)  # an option missing its value
    if _is_computed(token):
        return InstallTarget(TargetKind.COMPUTED, token)
    try:
        req = Requirement(token)
    except InvalidRequirement:
        req = None
    if req is not None and req.url:
        return _requirement(TargetKind.DIRECT_REFERENCE, token, req)
    if "://" in token or token.lower().startswith(_VCS_PREFIXES):
        return InstallTarget(TargetKind.URL, token)
    if _looks_like_path(token):
        # the shell expands an unquoted `*` or `?` when the notebook runs; Cell 2 runs no shell
        return InstallTarget(TargetKind.COMPUTED if "*" in token or "?" in token else TargetKind.PATH, token)
    if req is not None:
        return _requirement(TargetKind.REQUIREMENT, token, req)
    legacy = _LEGACY_REQUIREMENT.match(token)
    if legacy:  # pip's own packaging still reads a non-PEP 440 version (`x==0.0.0.nonexistent`); so does Cell 2
        name, extras, specifier = legacy.groups()
        return InstallTarget(TargetKind.REQUIREMENT, token, name=name, canonical=canonicalize_name(name),
                             extras=sorted(e.strip() for e in (extras or "").strip("[]").split(",") if e.strip()),
                             specifier=specifier)
    return InstallTarget(TargetKind.INVALID, token)


def _requirement(kind: str, token: str, req: Requirement) -> InstallTarget:
    return InstallTarget(kind, token, name=req.name, canonical=canonicalize_name(req.name),
                         extras=sorted(req.extras), specifier=str(req.specifier),
                         marker=str(req.marker) if req.marker else "")


def kaggle_dataset(path: str) -> Optional[str]:
    """The dataset a Kaggle input path comes from (decision 11): `/kaggle/input/<dataset>/...`, as a
    `file://` URL, or the older relative `../input/<dataset>/...`."""
    match = re.match(r"^(?:file://)?(?:/kaggle/input|\.\./input)/([^/]+)", path)
    return match.group(1) if match else None

