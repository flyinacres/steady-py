"""steady_py.install(), the installer generated Cell 2 calls at notebook run time. pip and the
installed-package lookup are faked, so these run offline and install nothing."""
import importlib.metadata
import subprocess
import sys
import types

import pytest

from steady_py import constants, runtime

CURRENT = (sys.version_info.major, sys.version_info.minor)


def _manifest(deps=(), raw=(), custom=(), python=None):
    """deps: (name, version, flags) triples."""
    return {
        "dependencies": [{"name": n, "version": v, "flags": list(f)} for n, v, f in deps],
        "raw_installs": list(raw), "custom_sourced": list(custom),
        "python_version": python or {}, "generated_at": "",
    }


class FakeEnvironment:
    """Stands in for subprocess.run (pip) and importlib.metadata.version. `versions` is what is
    installed; `outcomes` maps a substring of the pip command to a return code or an exception to
    raise (default 0); `effects` maps a substring to the versions a successful install leaves."""

    def __init__(self, monkeypatch, versions=None, outcomes=None, effects=None, output=""):
        self.versions = dict(versions or {})
        self.outcomes = outcomes or {}
        self.effects = effects or {}
        self.output = output
        self.commands = []
        self.timeouts = []
        monkeypatch.setattr(subprocess, "run", self.run)
        monkeypatch.setattr(importlib.metadata, "version", self.version)

    def version(self, name):
        if name not in self.versions:
            raise importlib.metadata.PackageNotFoundError(name)
        return self.versions[name]

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        self.timeouts.append(kwargs.get("timeout"))
        joined = " ".join(cmd)
        outcome = next((o for key, o in self.outcomes.items() if key in joined), 0)
        if isinstance(outcome, BaseException):
            raise outcome
        if self.output:
            kwargs["stdout"].write(self.output)
        if outcome == 0:
            for key, changes in self.effects.items():
                if key in joined:
                    self.versions.update(changes)
        return types.SimpleNamespace(returncode=outcome)


class TestPythonVersion:
    """The installer warns when the notebook runs on a different Python than it was made with, and
    then carries on: pip gives its own clear error for a pin that cannot be installed, so stopping
    early would only hide the other problems."""

    def test_a_matching_python_prints_no_warning(self, capsys):
        runtime.install(_manifest(python={"major": CURRENT[0], "minor": CURRENT[1]}))
        assert "created with Python" not in capsys.readouterr().out

    @pytest.mark.parametrize("required", [(CURRENT[0], CURRENT[1] + 1), (2, 7), (4, CURRENT[1]), (2, CURRENT[1])])
    def test_any_other_python_warns_and_carries_on(self, required, capsys):
        runtime.install(_manifest(python={"major": required[0], "minor": required[1]}))
        out = capsys.readouterr().out
        label = f"{required[0]}.{required[1]}"
        assert f"This code was created with Python {label}. You are trying to run it with {CURRENT[0]}.{CURRENT[1]}." in out
        assert f"consider changing your runtime Python version back to {label}" in out
        assert "Setup complete!" in out   # it went on (there were no dependencies to install)


class TestAlreadyInstalled:
    def test_a_matching_pin_skips_pip(self, monkeypatch, capsys):
        env = FakeEnvironment(monkeypatch, versions={"numpy": "1.26.0"})
        result = runtime.install(_manifest([("numpy", "1.26.0", [])]))
        assert env.commands == []
        assert result.installed == 1 and result.ok
        assert "numpy (1.26.0) already satisfied" in capsys.readouterr().out

    def test_an_unpinned_package_is_satisfied_by_any_version(self, monkeypatch):
        env = FakeEnvironment(monkeypatch, versions={"requests": "2.0.0"})
        assert runtime.install(_manifest([("requests", "", [])])).installed == 1
        assert env.commands == []

    def test_a_different_installed_version_is_reinstalled_at_the_pin(self, monkeypatch):
        env = FakeEnvironment(monkeypatch, versions={"numpy": "2.0.0"})
        runtime.install(_manifest([("numpy", "1.26.0", [])]))
        assert [cmd[-1] for cmd in env.commands] == ["numpy==1.26.0"]


class TestFailures:
    def test_failure_diagnostics_contain_verified_version(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When a package install fails, install() prints the author-verified version and captures stderr."""
        manifest = {
            "dependencies": [{"name": "broken_pkg", "version": "1.0.0", "flags": []}],
            "raw_installs": [], "custom_sourced": [], "python_version": {}, "generated_at": "",
        }

        def fake_run(*args, **kwargs):
            kwargs["stdout"].write("Mocked pip error: Could not find wheel")
            return types.SimpleNamespace(returncode=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        runtime.install(manifest)

        captured = capsys.readouterr().out
        assert "❌" in captured
        assert "broken_pkg==1.0.0" in captured
        assert "Mocked pip error" in captured

    def test_best_effort_execution_continues_on_failure(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure on package 1 does not abort execution for package 2."""
        manifest = {
            "dependencies": [
                {"name": "fail_pkg", "version": "1.0.0", "flags": []},
                {"name": "pass_pkg", "version": "2.0.0", "flags": []},
            ],
            "raw_installs": [], "custom_sourced": [], "python_version": {}, "generated_at": "",
        }

        def mock_run(cmd, *args, **kwargs):
            if "fail_pkg" in " ".join(cmd):
                return types.SimpleNamespace(returncode=1, stderr="Failed", stdout="")
            return types.SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(subprocess, "run", mock_run)
        runtime.install(manifest)

        captured = capsys.readouterr().out
        assert "❌" in captured and "fail_pkg" in captured
        assert "✅" in captured and "pass_pkg" in captured
        assert "[1/2]" in captured
        assert "[2/2]" in captured

    def test_a_timeout_fails_that_package_and_the_rest_are_still_tried(self, monkeypatch, capsys):
        env = FakeEnvironment(monkeypatch, outcomes={"slow_pkg": subprocess.TimeoutExpired(cmd="pip", timeout=5)})
        result = runtime.install(_manifest([("slow_pkg", "1.0", []), ("fast_pkg", "2.0", [])]), timeout=5)
        assert "Installation timed out after 5s" in capsys.readouterr().out
        assert result.failed == ["slow_pkg==1.0"] and result.installed == 1
        assert env.timeouts == [5, 5]

    def test_pip_that_cannot_start_is_a_failure_not_a_crash(self, monkeypatch, capsys):
        FakeEnvironment(monkeypatch, outcomes={"pkg": OSError("no such file")})
        result = runtime.install(_manifest([("pkg", "1.0", [])]))
        assert "Execution failed: no such file" in capsys.readouterr().out
        assert result.failed == ["pkg==1.0"]

    def test_scoped_flags_reach_pip_and_are_shown_on_failure(self, monkeypatch, capsys):
        flags = ["--index-url", "https://download.pytorch.org/whl/cu121"]
        env = FakeEnvironment(monkeypatch, outcomes={"torch": 1})
        runtime.install(_manifest([("torch", "2.3.0", flags)]))
        assert env.commands[0][-3:] == ["torch==2.3.0", *flags]
        assert f"Scoped Flags: {' '.join(flags)}" in capsys.readouterr().out

    def test_only_a_custom_sourced_failure_points_to_the_author(self, monkeypatch, capsys):
        FakeEnvironment(monkeypatch, outcomes={"==": 1})
        runtime.install(_manifest([("inhouse", "1.0", []), ("public", "1.0", [])], custom=["inhouse"]))
        out = capsys.readouterr().out
        assert out.count("custom-specified by the notebook's author") == 1
        assert out.index("custom-specified by the notebook's author") < out.index("public==1.0 failed")


class TestResult:
    def test_install_returns_a_result_a_caller_can_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """install() returns something a caller can check, instead of always None."""
        manifest = {
            "dependencies": [
                {"name": "fail_pkg", "version": "1.0.0", "flags": []},
                {"name": "pass_pkg", "version": "2.0.0", "flags": []},
            ],
            "raw_installs": [], "custom_sourced": [], "python_version": {}, "generated_at": "",
        }

        def mock_run(cmd, *args, **kwargs):
            if "fail_pkg" in " ".join(cmd):
                return types.SimpleNamespace(returncode=1, stderr="Failed", stdout="")
            return types.SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = runtime.install(manifest)

        assert result.total == 2
        assert result.installed == 1
        assert result.failed == ["fail_pkg==1.0.0"]
        assert result.ok is False

    def test_install_result_ok_when_everything_installs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = {
            "dependencies": [{"name": "pass_pkg", "version": "2.0.0", "flags": []}],
            "raw_installs": [], "custom_sourced": [], "python_version": {}, "generated_at": "",
        }
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stderr="", stdout=""))
        result = runtime.install(manifest)
        assert result.total == 1 and result.installed == 1 and result.failed == [] and result.ok is True


class TestDependencyDrift:
    def test_an_install_that_moves_an_earlier_package_is_reported(self, monkeypatch, capsys):
        FakeEnvironment(monkeypatch, versions={"numpy": "1.26.0"},
                        effects={"scipy": {"scipy": "1.11.0", "numpy": "2.0.0"}})
        runtime.install(_manifest([("numpy", "1.26.0", []), ("scipy", "1.11.0", [])]))
        assert "Installing 'scipy==1.11.0' caused 'numpy' to drift from 1.26.0 ➔ 2.0.0" in capsys.readouterr().out

    def test_no_drift_warning_when_nothing_moves(self, monkeypatch, capsys):
        FakeEnvironment(monkeypatch, versions={"numpy": "1.26.0"}, effects={"scipy": {"scipy": "1.11.0"}})
        runtime.install(_manifest([("numpy", "1.26.0", []), ("scipy", "1.11.0", [])]))
        assert "Dependency Drift" not in capsys.readouterr().out


class TestRawInstalls:
    """git, URL and local-file installs are passed to pip exactly as written, after the pins."""

    def test_raw_installs_follow_the_pins_and_can_fail_independently(self, monkeypatch, capsys):
        env = FakeEnvironment(monkeypatch, outcomes={"./local.whl": 1})
        result = runtime.install(_manifest([("numpy", "1.26.0", [])], raw=["git+https://example.com/a.git", "./local.whl"]))
        out = capsys.readouterr().out
        assert [cmd[-1] for cmd in env.commands] == ["numpy==1.26.0", "git+https://example.com/a.git", "./local.whl"]
        assert "[2/3] 📦 Installing (raw): git+https://example.com/a.git" in out
        assert "[3/3] 📦 Installing (raw): ./local.whl" in out
        assert "custom-specified source (git/URL/local file)" in out
        assert result.total == 3 and result.installed == 2 and result.failed == ["./local.whl"]


class TestSummary:
    def test_all_success_reports_the_counts(self, monkeypatch, capsys):
        FakeEnvironment(monkeypatch)
        runtime.install(_manifest([("a", "1.0", []), ("b", "1.0", [])]))
        assert "Setup complete! All 2/2 dependencies verified." in capsys.readouterr().out

    def test_any_failure_prints_the_troubleshooting_steps(self, monkeypatch, capsys):
        FakeEnvironment(monkeypatch, outcomes={"b==": 1})
        runtime.install(_manifest([("a", "1.0", []), ("b", "1.0", [])]))
        out = capsys.readouterr().out
        assert "Setup completed with issues: 1/2 packages installed." in out
        assert constants.HELP_URL in out

    @pytest.mark.parametrize("versions, raw, expect_note", [
        ({"a": "1.0"}, [], False),   # everything already satisfied: nothing changed
        ({}, [], True),              # a pinned package was installed
        ({"a": "1.0"}, ["git+https://example.com/b.git"], True),   # only a raw install ran
    ])
    def test_the_restart_note_appears_only_when_something_was_installed(self, monkeypatch, capsys, versions, raw, expect_note):
        FakeEnvironment(monkeypatch, versions=versions)
        runtime.install(_manifest([("a", "1.0", [])], raw=raw))
        assert ("restart the kernel" in capsys.readouterr().out) is expect_note
