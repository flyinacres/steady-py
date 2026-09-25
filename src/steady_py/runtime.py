"""The runtime installer the generated setup cell calls: installs the manifest's pins one package at a
time, so one bad pin never blocks the rest, and reports what could not be installed."""
import importlib.metadata
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from steady_py.constants import HELP_URL


@dataclass
class InstallResult:
    """What steady_py.install() actually did: how many of the manifest's packages installed
    successfully and which failed, so a caller can check success without scraping printed
    output. `failed` holds each failed specifier, e.g. "broken_pkg==1.0.0"."""
    total: int
    installed: int
    failed: List[str]

    @property
    def ok(self) -> bool:
        return not self.failed


def install(manifest: Dict[str, Any], timeout: int = 120) -> InstallResult:
    """The runtime installer: the `install` verb, and the only endpoint that runs at
    notebook run time. Reads a STEADY_PY_MANIFEST dict and installs each pinned
    dependency sequentially via pip (to avoid index conflicts), printing progress and a
    final summary. Called from generated Cell 2 as `steady_py.install(STEADY_PY_MANIFEST)`.
    Needs internet; internet-off runs are unsupported.
    """
    py = manifest.get("python_version") or {}
    required = (py.get("major"), py.get("minor"))
    current = (sys.version_info.major, sys.version_info.minor)
    if None not in required and current != required:
        req_ver = f"{required[0]}.{required[1]}"
        curr_ver = f"{current[0]}.{current[1]}"
        print(f"⚠️ This code was created with Python {req_ver}. You are trying to run it with {curr_ver}.")
        print(f"If installation fails, consider changing your runtime Python version back to {req_ver}.\n")

    print(f"Applying verified environment dependencies [{manifest.get('generated_at', '')}]...")
    print("💡 Note: Dependencies are installed sequentially to prevent index conflicts.\n")

    passed_count = 0
    failed_packages: List[Tuple[str, str, List[str], str]] = []
    any_install_performed = False
    dependencies = manifest.get("dependencies", [])
    raw_installs = manifest.get("raw_installs", [])
    total_deps = len(dependencies) + len(raw_installs)
    installed_baseline: Dict[str, str] = {}

    def _run_pip_subprocess(cmd: List[str], to: int) -> Tuple[int, List[str]]:
        captured: List[str] = []
        returncode = 0
        try:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as tmp_out:
                proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=tmp_out, stderr=subprocess.STDOUT, timeout=to)
                returncode = proc.returncode
                tmp_out.seek(0)
                for line in tmp_out.read().splitlines():
                    if line.strip():
                        captured.append(line)
                        print(f"    {line}")
                sys.stdout.flush()
        except subprocess.TimeoutExpired:
            returncode = -1
            captured.append(f"Error: installation exceeded per-package timeout limit ({to}s).")
            print(f"    ❌ Installation timed out after {to}s.")
        except Exception as exc:
            returncode = -1
            captured.append(f"Execution failed: {exc}")
            print(f"    ❌ Execution failed: {exc}")
        return returncode, captured

    idx = 0
    for item in dependencies:
        idx += 1
        name = item["name"]
        ver = item.get("version", "")
        flags = item.get("flags", [])
        specifier = f"{name}=={ver}" if ver else name

        already_satisfied = False
        try:
            current_ver = importlib.metadata.version(name)
            if not ver or current_ver == ver:
                already_satisfied = True
                passed_count += 1
                installed_baseline[name] = current_ver
                print(f"[{idx}/{total_deps}] ⚡ {name} ({current_ver}) already satisfied in environment")
        except Exception:
            pass

        if already_satisfied:
            continue

        cmd = [
            sys.executable, "-m", "pip", "install",
            "--no-input", "--disable-pip-version-check", "--no-warn-script-location",
            specifier,
        ] + flags

        print(f"[{idx}/{total_deps}] 📦 Installing {specifier}...")
        sys.stdout.flush()

        returncode, captured_output = _run_pip_subprocess(cmd, timeout)

        if returncode == 0:
            passed_count += 1
            any_install_performed = True
            print(f"    ✅ {specifier} installed successfully")
            try:
                current_ver = importlib.metadata.version(name)
                installed_baseline[name] = current_ver
            except Exception:
                pass
            for prev_pkg, prev_ver in list(installed_baseline.items()):
                if prev_pkg == name:
                    continue
                try:
                    active_now = importlib.metadata.version(prev_pkg)
                    if active_now != prev_ver:
                        print(f"   ⚠️ Dependency Drift: Installing '{specifier}' caused '{prev_pkg}' to drift from {prev_ver} ➔ {active_now}")
                        installed_baseline[prev_pkg] = active_now
                except Exception:
                    pass
        else:
            err_snippet = captured_output[-1] if captured_output else "Unknown pip error"
            failed_packages.append((specifier, ver, flags, "\n".join(captured_output)))
            print(f"    ❌ {specifier} failed to install (exit code {returncode})")
            if name in manifest.get("custom_sourced", []):
                print("       ⚠️ This package is custom-specified by the notebook's author (not on public PyPI).")
                print("          If it's unavailable, contact the author for its current location.")
            print(f"       ├─ Author Verified Version: {ver or 'unspecified'}")
            if flags:
                print(f"       ├─ Scoped Flags: {' '.join(flags)}")
            print(f"       └─ Error: {err_snippet}\n")

    if raw_installs:
        print("\n📎 Installing non-standard sources (git/URL/local file)...")
        print("   These are installed exactly as specified but can't be verified against PyPI.")
        print("   You are responsible for ensuring anyone running this notebook has access to the same resource.\n")
        for raw_idx, raw_spec in enumerate(raw_installs, start=idx + 1):
            print(f"[{raw_idx}/{total_deps}] 📦 Installing (raw): {raw_spec}")
            sys.stdout.flush()
            raw_cmd = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--no-warn-script-location", raw_spec]
            raw_returncode, raw_captured = _run_pip_subprocess(raw_cmd, timeout)
            if raw_returncode == 0:
                passed_count += 1
                print(f"    ✅ {raw_spec} installed successfully")
            else:
                failed_packages.append((raw_spec, "", [], "\n".join(raw_captured)))
                print(f"    ❌ {raw_spec} failed to install (exit code {raw_returncode})")
                print("       ⚠️ This is a custom-specified source (git/URL/local file), not a standard PyPI package.")
                print("          If it's unreachable, contact the notebook's author for its current location.")

    print("\n" + "=" * 60)
    if not failed_packages:
        print(f"✅ Setup complete! All {passed_count}/{total_deps} dependencies verified.")
    else:
        print(f"⚠️ Setup completed with issues: {passed_count}/{total_deps} packages installed.")
        print("Troubleshooting Steps:")
        print("1. Internet Access: Ensure your notebook environment has active internet access.")
        print("2. Unpinned Installs: Test installing failed libraries manually: '!pip install <pkg>'")
        print(f"3. Troubleshooting Steps: For a detailed guide on resolving setup errors, see: {HELP_URL}")

    if any_install_performed:
        print("\n⚠️ Note: You may need to restart the kernel to use updated packages.")
    print("=" * 60)

    return InstallResult(total=total_deps, installed=passed_count, failed=[spec for spec, *_ in failed_packages])
