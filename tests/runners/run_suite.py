#!/usr/bin/env python3
"""E2E Test Suite Runner for steady-py.

Executes Dockerized end-to-end integration tests across target runtime tiers
(python3.11, kaggle, colab, local_pkg) and common tier-independent tests.

Examples:
    python tests/runners/run_suite.py --tier common
    python tests/runners/run_suite.py --tier python3.11
    python tests/runners/run_suite.py --all
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence

# Repository root (two levels up from tests/runners/)
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_DIR = REPO_ROOT / ".test_artifacts"
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


@dataclass(frozen=True)
class PositiveFixture:
    path: str
    verify_patterns: tuple[str, ...] = ()
    setup_cell_patterns: tuple[str, ...] = ()  # in the setup cell snapshot wrote, not the outputs


@dataclass(frozen=True)
class NegativeFixture:
    path: str
    expected_ename: str
    expected_evalue_substring: str


@dataclass(frozen=True)
class TierConfig:
    image: str
    positive_fixtures: tuple[PositiveFixture, ...] = ()
    negative_fixtures: tuple[NegativeFixture, ...] = ()


# google.colab shares the top-level name `google` with packages the images install, so only its
# full name gets it classified as platform-provided; the fixture alone would pass without that.
PLATFORM_FIXTURE = PositiveFixture(
    path="tests/fixtures/e2e/platform_pseudo_module.ipynb",
    setup_cell_patterns=("# google.colab (provided automatically by platform",),
)

TIER_CONFIGS: dict[str, TierConfig] = {
    "python3.11": TierConfig(
        image="python:3.11-slim",
        positive_fixtures=(
            PositiveFixture(
                path="tests/fixtures/e2e/test_partial_install_recovery.ipynb",
                verify_patterns=(
                    "Partial install succeeded for valid packages: humanize==4.16.0, tabulate==0.9.0",
                    "tabulate==0.0.0.nonexistent failed to install",
                ),
            ),
            PositiveFixture(
                path="tests/fixtures/e2e/test_numpy_old_pin_preserves_api.ipynb",
                verify_patterns=(
                    "numpy==1.23.5: numpy.bool alias still works as expected",
                ),
            ),
        ),
        negative_fixtures=(
            NegativeFixture(
                path="tests/fixtures/e2e/test_e2e_failed_repin_surfaces_downstream.ipynb",
                expected_ename="AssertionError",
                expected_evalue_substring="failed silently",
            ),
        ),
    ),
    "kaggle": TierConfig(
        image="gcr.io/kaggle-images/python:latest",
        positive_fixtures=(
            PositiveFixture(path="tests/fixtures/e2e/test_pip_satisfied.ipynb"),
            PositiveFixture(path="tests/fixtures/e2e/pinned_install.ipynb"),
            PLATFORM_FIXTURE,
        ),
    ),
    "colab": TierConfig(
        image="us-docker.pkg.dev/colab-images/public/cpu-runtime:latest",
        positive_fixtures=(
            PositiveFixture(path="tests/fixtures/e2e/test_pip_satisfied.ipynb"),
            PositiveFixture(path="tests/fixtures/e2e/pinned_install.ipynb"),
            PLATFORM_FIXTURE,
        ),
    ),
    "local_pkg": TierConfig(
        image="python:3.11-slim",
        positive_fixtures=(
            PositiveFixture(
                path="tests/fixtures/e2e/test_local_pkg_timeout.ipynb",
                verify_patterns=("Installation timed out after 2s.",),
            ),
            PositiveFixture(
                path="tests/fixtures/e2e/test_local_pkg_build_failure.ipynb",
                verify_patterns=(
                    "RuntimeError: deliberate build failure for local_test_pkg",
                ),
            ),
        ),
    ),
}


def build_docker_cmd(tier: str, notebook_path: str, merged_path: str, output_path: str) -> str:
    """Build the internal bash shell commands for each tier environment."""
    if tier == "python3.11":
        return (
            "pip install -e /workspace -q && "
            "pip install --no-cache-dir ipykernel nbconvert==7.17.1 humanize==4.16.0 "
            "tabulate==0.9.0 numpy==1.23.5 && "
            "python -m ipykernel install --user --name python3 && "
            f'python -m steady_py snapshot "{notebook_path}" --output && '
            f'jupyter nbconvert --to notebook --execute "{merged_path}" --output "{output_path}" '
            "--ExecutePreprocessor.timeout=300 --ExecutePreprocessor.kernel_name=python3"
        )
    if tier == "kaggle":
        return (
            "pip install -e /workspace -q && "
            "python3 -m venv --system-site-packages --without-pip --clear /tmp/run_env && "
            f'/tmp/run_env/bin/python -m steady_py snapshot "{notebook_path}" --output && '
            f'/tmp/run_env/bin/python -m jupyter nbconvert --to notebook --execute "{merged_path}" '
            f'--output "{output_path}" --ExecutePreprocessor.timeout=300'
        )
    if tier == "colab":
        return (
            "pip install -e /workspace -q && "
            f'python3 -m steady_py snapshot "{notebook_path}" --output && '
            f'jupyter nbconvert --to notebook --execute "{merged_path}" --output "{output_path}" '
            "--ExecutePreprocessor.timeout=300 --ExecutePreprocessor.kernel_name=python3"
        )
    if tier == "local_pkg":
        return (
            "pip install -e /workspace -q && "
            "pip install --no-cache-dir ipykernel nbconvert==7.17.1 -q && "
            "python -m ipykernel install --user --name python3 && "
            "PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/bootstrap "
            "pip install --no-cache-dir setuptools wheel && "
            f'SEED_VER=$(grep -oE "local_test_pkg==[0-9.]+" "{notebook_path}" | head -1 | cut -d= -f3) && '
            "PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/seed_dist "
            'pip install --no-cache-dir local_test_pkg=="$SEED_VER" && '
            f'python -m steady_py snapshot "{notebook_path}" --output --timeout 2 && '
            "pip uninstall -y local_test_pkg && "
            "PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/dist "
            "PIP_NO_BUILD_ISOLATION=1 PIP_NO_CACHE_DIR=1 "
            f'jupyter nbconvert --to notebook --execute "{merged_path}" --output "{output_path}" '
            "--ExecutePreprocessor.timeout=300 --ExecutePreprocessor.kernel_name=python3"
        )
    raise ValueError(f"No Docker command mapping configured for tier: {tier}")


def run_docker(
    image: str,
    bash_script: str,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Execute a bash command inside a container, streaming output and capturing text."""
    cmd = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "missing",
        "-v",
        f"{REPO_ROOT}:/workspace",
        "-w",
        "/workspace",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "PYDEVD_DISABLE_FILE_VALIDATION=1",
        "-e",
        "PIP_ROOT_USER_ACTION=ignore",
        "-e",
        "PIP_DISABLE_PIP_VERSION_CHECK=1",
    ]

    if env:
        for k, v in env.items():
            cmd.extend(["-e", f"{k}={v}"])

    cmd.extend([
        "--entrypoint",
        "/bin/bash",
        image,
        "-c",
        f"{bash_script} 2>&1",
    ])

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    lines: list[str] = []
    if process.stdout:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)

    process.wait()
    return process.returncode, "".join(lines)


# What each image runs, printed at the start of every tier so a stale image shows (task 30).
IMAGE_VERSIONS_CMD = (
    "python3 -c 'import sys, importlib.metadata as m; "
    "v = lambda n: next((d.version for d in m.distributions(name=n)), \"not installed\"); "
    "print(\"Image runs Python\", sys.version.split()[0] + \", numpy\", v(\"numpy\") + \", pandas\", v(\"pandas\"))'"
)
_REFRESHED: set[str] = set()


def refresh_image(image: str) -> None:
    """Pull `image` and print its creation date and its Python, numpy and pandas versions, once per
    run. run_docker's `--pull missing` never updates a cached image: on 2026-11-20 a cached Kaggle
    image ran Python 3.12 while live Kaggle ran 3.13.18."""
    if image in _REFRESHED:
        return
    print(f"Pulling {image}...")
    if subprocess.run(["docker", "pull", "-q", image]).returncode != 0:
        raise RuntimeError(f"Could not pull {image}")
    created = subprocess.run(["docker", "image", "inspect", "-f", "{{.Created}}", image],
                             capture_output=True, text=True, check=True).stdout.strip()
    print(f"Image {image} was created {created}")
    if run_docker(image, IMAGE_VERSIONS_CMD)[0] != 0:
        raise RuntimeError(f"Could not read the versions in {image}")
    _REFRESHED.add(image)


def cleanup_artifacts(*paths: Path) -> None:
    for path in paths:
        if path.exists():
            try:
                path.unlink()
            except OSError as err:
                print(f"Warning: Failed to remove artifact {path}: {err}", file=sys.stderr)


def _collect_notebook_outputs(notebook_path: Path) -> tuple[list[str], list[dict]]:
    """Walks a notebook's cell outputs once. Returns (stream/result text per
    cell, error outputs with their cell index) so callers can check either or
    both without re-parsing the notebook.
    """
    if not notebook_path.exists():
        raise RuntimeError(f"Expected output notebook was not generated: {notebook_path}")

    with notebook_path.open("r", encoding="utf-8") as f:
        nb_data = json.load(f)

    collected_text: list[str] = []
    error_outputs: list[dict] = []
    for cell_idx, cell in enumerate(nb_data.get("cells", [])):
        for output in cell.get("outputs", []):
            output_type = output.get("output_type")
            if output_type == "error":
                error_outputs.append({"cell": cell_idx, "output": output})
            elif output_type == "stream":
                text = output.get("text", "")
                collected_text.append("".join(text) if isinstance(text, list) else text)
            elif output_type in {"execute_result", "display_data"}:
                data_text = output.get("data", {}).get("text/plain", "")
                collected_text.append("".join(data_text) if isinstance(data_text, list) else data_text)
    return collected_text, error_outputs


def verify_positive_notebook(notebook_path: Path, patterns: Sequence[str]) -> None:
    collected_text, error_outputs = _collect_notebook_outputs(notebook_path)

    if error_outputs:
        first = error_outputs[0]
        ename = first["output"].get("ename", "Error")
        evalue = first["output"].get("evalue", "")
        raise AssertionError(
            f"Unexpected cell execution error in {notebook_path.name} (cell {first['cell']}): {ename}: {evalue}"
        )

    if not patterns:
        return

    all_output = strip_ansi("\n".join(collected_text))
    for pattern in patterns:
        if pattern not in all_output:
            raise AssertionError(
                f"Verification failed for {notebook_path.name}.\n"
                f"Missing expected pattern:\n  '{pattern}'\n"
                f"Searched outputs:\n{all_output}"
            )

def verify_setup_cell(notebook_path: Path, patterns: Sequence[str]) -> None:
    """Each pattern appears in the setup cell (the code cell holding STEADY_PY_MANIFEST)."""
    if not patterns:
        return
    with notebook_path.open("r", encoding="utf-8") as f:
        cells = json.load(f).get("cells", [])
    sources = ["".join(c["source"]) if isinstance(c["source"], list) else c["source"] for c in cells
               if c.get("cell_type") == "code"]
    setup = next((s for s in sources if "STEADY_PY_MANIFEST" in s), None)
    if setup is None:
        raise AssertionError(f"No setup cell in {notebook_path.name}")
    for pattern in patterns:
        if pattern not in setup:
            raise AssertionError(f"Setup cell of {notebook_path.name} lacks:\n  '{pattern}'\nSetup cell:\n{setup}")


def verify_negative_notebook(
    notebook_path: Path,
    expected_ename: str,
    expected_evalue_substring: str,
) -> None:
    _, error_outputs = _collect_notebook_outputs(notebook_path)

    if len(error_outputs) != 1:
        raise AssertionError(
            f"Expected exactly one cell error in {notebook_path.name}, but found {len(error_outputs)}."
        )

    err = error_outputs[0]["output"]
    ename = err.get("ename", "")
    evalue = err.get("evalue", "")

    if ename != expected_ename:
        raise AssertionError(
            f"Cell error ename mismatch: expected '{expected_ename}', found '{ename}'"
        )
    if expected_evalue_substring not in evalue:
        raise AssertionError(
            f"Cell error evalue mismatch: expected substring '{expected_evalue_substring}', found '{evalue}'"
        )


def verify_expected_failure_notebook(
    notebook_path: Path,
    stream_patterns: Sequence[str],
    expected_ename: str,
) -> None:
    """For a scenario where Cell 2 is expected to print failure guidance (a
    caught, non-raising install failure) and a downstream cell is expected to
    then raise as a real consequence (e.g. importing a package that never
    installed). Checks the guidance text appears in stream output, and that
    exactly one cell error occurred, of the expected type.
    """
    collected_text, error_outputs = _collect_notebook_outputs(notebook_path)

    if len(error_outputs) != 1:
        raise AssertionError(
            f"Expected exactly one cell error in {notebook_path.name}, but found {len(error_outputs)}."
        )

    ename = error_outputs[0]["output"].get("ename", "")
    if ename != expected_ename:
        raise AssertionError(
            f"Cell error ename mismatch: expected '{expected_ename}', found '{ename}'"
        )

    all_output = strip_ansi("\n".join(collected_text))
    for pattern in stream_patterns:
        if pattern not in all_output:
            raise AssertionError(
                f"Verification failed for {notebook_path.name}.\n"
                f"Missing expected pattern:\n  '{pattern}'\n"
                f"Searched outputs:\n{all_output}"
            )


def run_common_tests() -> None:
    print("=" * 60)
    print(" Running tier-independent tests")
    print("=" * 60)
    refresh_image("python:3.11-slim")

    print("\033[96mRunning Check-Drift E2E Test (generate + check-drift subprocesses)...\033[0m")
    exit_code, _ = run_docker(
        "python:3.11-slim",
        "pip install -e /workspace -q && python tests/runners/test_check_drift.py",
    )
    if exit_code != 0:
        raise RuntimeError("Check-drift e2e test failed.")
    print("\033[92mPASS: Check-drift e2e test\n\033[0m")

    print("\033[96mRunning raw_installs E2E (explicit path, inferred URL, unreachable source, inferred local path)...\033[0m")
    exit_code, _ = run_docker(
        "python:3.11-slim",
        "pip install -e /workspace -q && "
        "pip install --no-cache-dir jupyter_client ipykernel -q && "
        "python -m ipykernel install --user --name python3 && "
        "python tests/runners/test_raw_installs.py",
    )
    if exit_code != 0:
        raise RuntimeError("raw_installs e2e test failed.")
    print("\033[92mPASS: raw_installs e2e test\n\033[0m")

    print("\033[96mRunning Phase 5g: Live-Kernel Stale Module Test...\033[0m")
    exit_code, _ = run_docker(
        "python:3.11-slim",
        "pip install -e /workspace -q && "
        "pip install --no-cache-dir jupyter_client ipykernel numpy==1.26.4 -q && "
        "python -m ipykernel install --user --name python3 && "
        "python tests/runners/test_live_kernel_stale_repin.py",
    )
    if exit_code != 0:
        raise RuntimeError("Live-kernel stale module test failed.")
    print("\033[92mPASS: Live-kernel stale module test\n\033[0m")

    print("\033[96mRunning Phase 5g: Live-Kernel Phase 0 Regressions...\033[0m")
    exit_code, _ = run_docker(
        "python:3.11-slim",
        "pip install -e /workspace -q && "
        "pip install --no-cache-dir jupyter_client ipykernel -q && "
        "python -m ipykernel install --user --name python3 && "
        "python tests/runners/test_live_kernel_phase0_regressions.py",
    )
    if exit_code != 0:
        raise RuntimeError("Live-kernel Phase 0 regressions test failed.")
    print("\033[92mPASS: Live-kernel Phase 0 regressions test\n\033[0m")

    print("\033[96mRunning Phase 5f: Hardware Mocking (CUDA)...\033[0m")
    exit_code, _ = run_docker(
        "python:3.11-slim",
        "pip install -e /workspace -q && python tests/runners/test_hardware_mock.py",
        env={
            "PYTHONPATH": "/workspace/tests/fixtures/mock_pkgs",
            "TEST_HW_MODE": "cuda",
            "MOCK_CUDA_AVAILABLE": "1",
            "MOCK_MPS_AVAILABLE": "0",
        },
    )
    if exit_code != 0:
        raise RuntimeError("Hardware mocking test (CUDA) failed.")

    print("\033[96mRunning Phase 5f: Hardware Mocking (MPS)...\033[0m")
    exit_code, _ = run_docker(
        "python:3.11-slim",
        "pip install -e /workspace -q && python tests/runners/test_hardware_mock.py",
        env={
            "PYTHONPATH": "/workspace/tests/fixtures/mock_pkgs",
            "TEST_HW_MODE": "mps",
            "MOCK_CUDA_AVAILABLE": "0",
            "MOCK_MPS_AVAILABLE": "1",
        },
    )
    if exit_code != 0:
        raise RuntimeError("Hardware mocking test (MPS) failed.")
    print("\033[92mPASS: Hardware mocking tests\n\033[0m")

    print("\033[96mRunning Phase 5k: Local Package Pin-and-Verify (1.0.0 -> 2.0.0)...\033[0m")
    repin_nb = Path("tests/fixtures/e2e/test_local_pkg_pin_and_verify.ipynb")
    repin_merged = repin_nb.with_name(repin_nb.stem + "_merged.ipynb")
    out_v1 = SCRATCH_DIR / "out_v1.ipynb"
    out_v2 = SCRATCH_DIR / "out_v2.ipynb"
    out_v3 = SCRATCH_DIR / "out_v3.ipynb"
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    repin_cmd = (
        "pip install -e /workspace -q && "
        "pip install --no-cache-dir ipykernel nbconvert==7.17.1 -q && "
        "python -m ipykernel install --user --name python3 && "
        "PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/dist "
        "pip install --no-cache-dir local_test_pkg==1.0.0 && "
        f"PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/dist "
        f'python -m steady_py snapshot "{repin_nb.as_posix()}" --output && '
        "pip uninstall -y local_test_pkg && "
        "PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/dist "
        f'jupyter nbconvert --to notebook --execute "{repin_merged.as_posix()}" '
        f'--output "/workspace/{out_v1.relative_to(REPO_ROOT).as_posix()}" '
        "--ExecutePreprocessor.timeout=300 --ExecutePreprocessor.kernel_name=python3 && "
        "PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/dist "
        "pip install --no-cache-dir local_test_pkg==2.0.0 && "
        f"PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/dist "
        f'python -m steady_py snapshot "{repin_nb.as_posix()}" --output && '
        "pip uninstall -y local_test_pkg && "
        "PIP_NO_INDEX=1 PIP_FIND_LINKS=/workspace/tests/fixtures/local_test_pkg/dist "
        f'jupyter nbconvert --to notebook --execute "{repin_merged.as_posix()}" '
        f'--output "/workspace/{out_v2.relative_to(REPO_ROOT).as_posix()}" '
        "--ExecutePreprocessor.timeout=300 --ExecutePreprocessor.kernel_name=python3 && "
        "pip uninstall -y local_test_pkg && "
        "mkdir -p /tmp/empty_dist && "
        "PIP_NO_INDEX=1 PIP_FIND_LINKS=/tmp/empty_dist "
        f'jupyter nbconvert --to notebook --execute "{repin_merged.as_posix()}" '
        f'--output "/workspace/{out_v3.relative_to(REPO_ROOT).as_posix()}" '
        "--ExecutePreprocessor.timeout=300 --ExecutePreprocessor.kernel_name=python3 "
        "--ExecutePreprocessor.allow_errors=True"
    )

    try:
        exit_code, repin_output = run_docker("python:3.11-slim", repin_cmd)
        if exit_code != 0:
            raise RuntimeError("Local package pin-and-verify run failed.")

        clean_repin_output = strip_ansi(repin_output)
        for expected in (
            "Verified present in /workspace/tests/fixtures/local_test_pkg/dist: local_test_pkg-1.0.0",
            "Verified present in /workspace/tests/fixtures/local_test_pkg/dist: local_test_pkg-2.0.0",
        ):
            if expected not in clean_repin_output:
                raise AssertionError(
                    f"Missing expected local-artifact verification message: '{expected}'"
                )

        verify_positive_notebook(out_v1, ("Pinned install verification passed: local_test_pkg 1.0.0 active",))
        verify_positive_notebook(out_v2, ("Pinned install verification passed: local_test_pkg 2.0.0 active",))
        verify_expected_failure_notebook(
            out_v3,
            stream_patterns=(
                "This package is custom-specified by the notebook's author (not on public PyPI).",
                "If it's unavailable, contact the author for its current location.",
            ),
            expected_ename="ModuleNotFoundError",
        )
    finally:
        cleanup_artifacts(REPO_ROOT / repin_merged, out_v1, out_v2, out_v3)

    print("\033[92mPASS: Local package pin-and-verify test\n\033[0m")


# Image drift on the hosted images: the notebook is snapshotted in a venv whose numpy and pandas
# differ from the image's (DRIFT_PINS), then run in the image's own kernel, which has no steady-py,
# so the first run also shows whether Cell 2's freshly installed helper imports without a restart
# (R5's question, on an image where pip installs as root). The second run starts a fresh kernel,
# standing in for the restart Cell 2 asks for.
DRIFT_NOTEBOOK = Path("tests/fixtures/e2e/image_drift.ipynb")
# Both have Python 3.13 wheels and work together (checked 2026-11-20), and differ from both images:
# Kaggle ships numpy 2.1.3 and pandas 2.3.3 (both pins are downgrades), Colab numpy 2.4.6 and
# pandas 2.2.3 (numpy a downgrade, pandas an upgrade). Recheck when refresh_image's versions move.
DRIFT_PINS = {"numpy": "2.1.0", "pandas": "2.3.0"}
RESTART_NOTE = "restart the kernel"


def build_drift_cmd(merged_path: str, first_run: str, second_run: str) -> str:
    pins = " ".join(f"{name}=={version}" for name, version in DRIFT_PINS.items())
    execute = (
        "python3 -m jupyter nbconvert --to notebook --execute --allow-errors "
        "--ExecutePreprocessor.timeout=600 --ExecutePreprocessor.kernel_name=python3"
    )
    return (
        "python3 -m venv --system-site-packages --without-pip --clear /tmp/snap_env && "
        f"/tmp/snap_env/bin/python -m pip install -q /workspace {pins} && "
        "/tmp/snap_env/bin/python -m pip wheel -q --no-deps /workspace -w /tmp/helper_wheels && "
        f'/tmp/snap_env/bin/python -m steady_py snapshot "{DRIFT_NOTEBOOK.as_posix()}" --output && '
        # Cell 2 pins the release steady-py==<version>; until it is on PyPI, the wheel stands in for it
        "export PIP_FIND_LINKS=/tmp/helper_wheels && "
        f'{execute} "{merged_path}" --output "{first_run}" && '
        f'{execute} "{first_run}" --output "{second_run}"'
    )


def verify_drift_runs(first_run: Path, second_run: Path) -> None:
    first_text, first_errors = _collect_notebook_outputs(first_run)
    first = strip_ansi("\n".join(first_text))
    if f"Installing numpy=={DRIFT_PINS['numpy']}" not in first:
        raise RuntimeError(f"Drift scenario not reproduced: Cell 2 did not install numpy=={DRIFT_PINS['numpy']}.\n{first}")
    if "Could not install the pinned" in first:
        raise AssertionError(f"Cell 2 could not install its helper:\n{first}")
    helper_errors = [e for e in first_errors if "steady_py" in e["output"].get("evalue", "")]
    if helper_errors:
        raise AssertionError(f"Cell 2's freshly installed helper did not import (R5): {helper_errors[0]['output'].get('evalue')}")
    if RESTART_NOTE not in first:
        raise AssertionError(f"Cell 2 installed packages but did not say to restart the kernel:\n{first}")
    in_use = [line for line in first.splitlines() if line.startswith("versions in use:")]
    print(f"First run, before the restart: {in_use[0] if in_use else 'the check cell did not print'}"
          + "".join(f"; cell {e['cell']} raised {e['output'].get('ename')}" for e in first_errors))

    expected = [f"⚡ {name} ({version}) already satisfied" for name, version in DRIFT_PINS.items()]
    expected += ["Setup complete!", "versions in use: numpy {numpy}, pandas {pandas}, sum 3".format(**DRIFT_PINS)]
    verify_positive_notebook(second_run, expected)
    second = strip_ansi("\n".join(_collect_notebook_outputs(second_run)[0]))
    if RESTART_NOTE in second:
        raise AssertionError(f"After the restart Cell 2 installed nothing but still said to restart:\n{second}")


def run_drift_scenario(image: str) -> None:
    merged_path = DRIFT_NOTEBOOK.with_name(DRIFT_NOTEBOOK.stem + "_merged.ipynb")
    first_run = SCRATCH_DIR / "out_drift_first.ipynb"
    second_run = SCRATCH_DIR / "out_drift_second.ipynb"
    print(f"\n=== Image drift and helper install: {DRIFT_NOTEBOOK.as_posix()} ===")
    cmd = build_drift_cmd(
        merged_path.as_posix(),
        f"/workspace/{first_run.relative_to(REPO_ROOT).as_posix()}",
        f"/workspace/{second_run.relative_to(REPO_ROOT).as_posix()}",
    )
    try:
        exit_code, _ = run_docker(image, cmd)
        if exit_code != 0:
            raise RuntimeError(f"Image drift scenario failed to run (exit code {exit_code})")
        verify_drift_runs(first_run, second_run)
    finally:
        cleanup_artifacts(REPO_ROOT / merged_path, first_run, second_run)
    print(">>> PASS: image drift and helper install")


def run_tier_tests(tier: str) -> None:
    config = TIER_CONFIGS[tier]
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f" Running E2E Suite: {tier.upper()} TIER ({config.image})")
    print("=" * 60)
    refresh_image(config.image)

    for fixture in config.positive_fixtures:
        nb_path = Path(fixture.path)
        merged_path = nb_path.with_name(nb_path.stem + "_merged.ipynb")
        output_name = f"out_{tier}_{nb_path.stem}.ipynb"
        output_host_path = SCRATCH_DIR / output_name
        output_container_path = f"/workspace/{output_host_path.relative_to(REPO_ROOT).as_posix()}"

        print(f"\n=== [{tier.upper()}] Processing (Expect PASS): {nb_path.as_posix()} ===")
        cmd = build_docker_cmd(tier, nb_path.as_posix(), merged_path.as_posix(), output_container_path)

        try:
            exit_code, _ = run_docker(config.image, cmd)
            if exit_code != 0:
                raise RuntimeError(f"Tier execution failed on positive test {nb_path.as_posix()} (exit code {exit_code})")
            verify_positive_notebook(output_host_path, fixture.verify_patterns)
            verify_setup_cell(output_host_path, fixture.setup_cell_patterns)
        finally:
            cleanup_artifacts(REPO_ROOT / merged_path, output_host_path)

        print(f">>> PASS: {nb_path.as_posix()}")

    for fixture in config.negative_fixtures:
        nb_path = Path(fixture.path)
        merged_path = nb_path.with_name(nb_path.stem + "_merged.ipynb")
        output_name = f"out_{tier}_{nb_path.stem}.ipynb"
        output_host_path = SCRATCH_DIR / output_name
        output_container_path = f"/workspace/{output_host_path.relative_to(REPO_ROOT).as_posix()}"

        print(f"\n=== [{tier.upper()}] Processing (Expect FAIL): {nb_path.as_posix()} ===")
        base_cmd = build_docker_cmd(tier, nb_path.as_posix(), merged_path.as_posix(), output_container_path)
        cmd = f"{base_cmd} --allow-errors"

        try:
            exit_code, _ = run_docker(config.image, cmd)
            if exit_code != 0:
                raise RuntimeError(
                    f"Tier container crashed unexpectedly during negative test execution for {nb_path.as_posix()} (exit code {exit_code})"
                )
            verify_negative_notebook(
                output_host_path,
                expected_ename=fixture.expected_ename,
                expected_evalue_substring=fixture.expected_evalue_substring,
            )
        finally:
            cleanup_artifacts(REPO_ROOT / merged_path, output_host_path)

        print(f">>> PASS (Structural failure verified): {nb_path.as_posix()}")

    if tier in ("kaggle", "colab"):
        run_drift_scenario(config.image)

    print("\n" + "=" * 60)
    print(f" {tier.upper()} TIER: ALL TESTS PASSED")
    print("=" * 60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run E2E Dockerized integration suites across target environments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--tier",
        choices=["common", *TIER_CONFIGS.keys()],
        help="Execute a specific tier suite ('common' for live-kernel/hardware mock tests).",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Run common tests followed by all tier configurations.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.all:
            run_common_tests()
            for tier in TIER_CONFIGS:
                run_tier_tests(tier)
        elif args.tier == "common":
            run_common_tests()
        else:
            run_tier_tests(args.tier)

        print("\n" + "=" * 60)
        print(" ALL REQUESTED TESTS PASSED")
        print("=" * 60)
    except Exception as err:
        print(f"\n\033[91mFAILED: {err}\033[0m", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()