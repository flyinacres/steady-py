"""The endpoints: scan, snapshot and check.

Each one computes and returns a typed result (see results.py). None of them prints or exits;
that is the CLI's job. They reach the analysis code through the `core` module at call time, so
tests can patch a function on `core` and have it take effect here.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from steady_py import core
from steady_py.results import CheckOptions, CheckResult, NotebookCheck, TargetKind


def check(target: str, options: Optional[CheckOptions] = None) -> CheckResult:
    """Compares the manifest in a notebook or .py file with live PyPI: yanked or removed pins,
    conflicts, an unsupported Python, a hand-edited manifest, missing local modules.

    Read-only. A file with no manifest is not an error (a notebook from before manifests is a
    valid state): the result has `manifest_found` False and nothing to report.
    """
    options = options or CheckOptions()
    return CheckResult(target=target, kind=TargetKind.FILE, notebooks=[_check_file(target, options)])


def _check_file(path: str, options: CheckOptions) -> NotebookCheck:
    manifest, error = core.extract_manifest_from_file(path)
    if error:
        return NotebookCheck(path=path, error=error)
    if manifest is None:
        return NotebookCheck(path=path)

    findings: List[core.DriftFinding] = []

    # Verify the manifest hasn't been hand-edited since it was generated. Only meaningful here:
    # generation is writing dependency_hash for the first time, not verifying a prior one. The
    # stored hash stays on the manifest so the report shows what the file actually contains.
    stored_hash = manifest.dependency_hash
    recomputed_hash = manifest.verified_hash
    if recomputed_hash != stored_hash:
        findings.append(core.DriftFinding(
            package="", version="", signal=core.Signal.TAMPERED, severity=core.Severity.CONFIRMED,
            message=f"Manifest hash mismatch in {path} -- it may have been hand-edited since generation.",
            details={"stored_hash": stored_hash, "recomputed_hash": recomputed_hash},
        ))

    findings.extend(core.check_local_modules(manifest, notebook_dir=str(Path(path).parent), root_dir=options.root_dir))
    findings.extend(core.run_pin_checks(manifest.dependencies, manifest.python_version))

    report = core.build_drift_check_report(path, manifest, findings)
    return NotebookCheck(path=path, manifest_found=True, report=report)
