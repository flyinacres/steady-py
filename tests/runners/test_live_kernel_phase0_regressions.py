#!/usr/bin/env python3
"""Phase 0: Live-Kernel Regressions Test.

Simulates an interactive session: executes regular user cells, then calls steady_py.snapshot()
directly inside the active kernel (the correct live-kernel entry point -- the CLI's argument
parser is never invoked from inside a kernel, so there is no sys.argv to contaminate and no
console log handler to duplicate; both of those only exist on the cli.main() path). Verifies
live-session history introspection: a prior cell's import is picked up with no target given.
"""

from __future__ import annotations

from pathlib import Path
import sys

from e2e_harness import (
    fail_test,
    interactive_kernel,
)


def main() -> None:
    run_tool = (
        "import steady_py\n"
        "result = steady_py.snapshot()\n"
        "print([d.name for d in result.notebooks[0].report.dependencies])"
    )

    print("Starting interactive kernel...")
    with interactive_kernel("python3") as kernel:
        print("1. Populating kernel history with dummy imports...")
        res1 = kernel.execute("import ipykernel\nprint('Dummy cell executed')")
        if not res1.ok:
            fail_test("Setup Dummy Imports", "Execution failed", stderr="\n".join(res1.errors))

        print("\n2. Running steady_py.snapshot() in the kernel...")
        res2 = kernel.execute(run_tool)
        if not res2.ok:
            fail_test(
                "Execute Tool in Kernel",
                "steady_py.snapshot() raised an error when called with no target inside a live kernel",
                stderr="\n".join(res2.errors),
                stdout=res2.stdout,
            )
        print("   PASS: steady_py.snapshot() ran cleanly inside the live kernel.")

        print("\n3. Verifying History Introspection...")
        if "ipykernel" not in res2.stdout.lower():
            preview = res2.stdout[-1000:] if len(res2.stdout) > 1000 else res2.stdout
            fail_test(
                "Inspect Session History",
                "Tool failed to extract prior cell history (ipykernel not found in output).",
                details={"stdout_preview": preview},
            )
        print("   PASS: History introspection correctly captured prior cells.")


if __name__ == "__main__":
    main()
