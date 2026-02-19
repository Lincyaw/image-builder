"""Post-build F2P/P2P validation for Docker images.

Runs tests inside a built image and classifies results:
- F2P (fail-to-pass): expected PASSED on new commit, actually fails on old commit (bug-revealing)
- P2P (pass-to-pass): expected PASSED, actually passes (stable)
- F2F (fail-to-fail): expected not PASSED, actually not passes (fine)
- P2F (pass-to-fail): expected not PASSED, actually passes (bad — must be 0)

An image passes validation when:
  1. The test sets match (no missing/extra tests)
  2. At least one F2P test exists
  3. Zero P2F tests
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def parse_test_output(raw_output: str) -> dict[str, str] | None:
    """Parse pytest/unittest output into {test_name: status} dict.

    Looks for the "short test summary info" section and parses lines like:
        PASSED module::Class::method
        FAILED module::Class::method - reason

    Returns dict in dot notation matching expected_output_json format:
        {"Class.method": "PASSED"}

    Returns None if the summary section is not found.
    """
    cleaned = strip_ansi(raw_output)
    lines = cleaned.splitlines()

    # Find the "short test summary info" section
    start = None
    for i, line in enumerate(lines):
        if "short test summary info" in line:
            start = i + 1
            break

    if start is None:
        return None

    results: dict[str, str] = {}
    status_pattern = re.compile(
        r"^(PASSED|FAILED|ERROR)\s+(\S+)"
    )

    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        # Stop at the final summary line (e.g. "= 5 passed, 2 failed in 1.23s =")
        if stripped.startswith("=") and stripped.endswith("="):
            break

        m = status_pattern.match(stripped)
        if m:
            status = m.group(1)
            full_path = m.group(2)
            # Convert module::Class::method -> Class.method
            parts = full_path.split("::")
            if len(parts) >= 3:
                test_name = f"{parts[-2]}.{parts[-1]}"
            elif len(parts) == 2:
                test_name = parts[-1]
            else:
                test_name = full_path
            # Normalize ERROR to FAILED for classification
            if status == "ERROR":
                status = "FAILED"
            results[test_name] = status

    return results if results else None


@dataclass
class ValidationResult:
    """Result of validating an image against expected test outcomes."""

    passed: bool
    reason: str
    f2p_tests: list[str] = field(default_factory=list)
    p2p_tests: list[str] = field(default_factory=list)
    f2f_tests: list[str] = field(default_factory=list)
    p2f_tests: list[str] = field(default_factory=list)
    missing_tests: list[str] = field(default_factory=list)
    extra_tests: list[str] = field(default_factory=list)
    raw_output: str = ""

    def summary(self) -> str:
        """One-line summary."""
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] F2P={len(self.f2p_tests)} P2P={len(self.p2p_tests)} "
            f"F2F={len(self.f2f_tests)} P2F={len(self.p2f_tests)} "
            f"missing={len(self.missing_tests)} extra={len(self.extra_tests)} "
            f"| {self.reason}"
        )

    def detailed_log(self) -> str:
        """Multi-line log for file output."""
        lines = [self.summary(), ""]

        def _section(title: str, items: list[str]) -> None:
            lines.append(f"--- {title} ({len(items)}) ---")
            for item in sorted(items):
                lines.append(f"  {item}")
            lines.append("")

        _section("F2P (bug-revealing, expected PASS got FAIL)", self.f2p_tests)
        _section("P2P (stable, expected PASS got PASS)", self.p2p_tests)
        _section("F2F (expected FAIL got FAIL)", self.f2f_tests)
        _section("P2F (BAD, expected FAIL got PASS)", self.p2f_tests)
        _section("Missing tests (expected but not in output)", self.missing_tests)
        _section("Extra tests (in output but not expected)", self.extra_tests)

        lines.append("--- Raw test output ---")
        lines.append(self.raw_output)
        return "\n".join(lines)


def compare_results(
    actual: dict[str, str], expected: dict[str, str]
) -> ValidationResult:
    """Classify each test and determine if validation passes.

    Args:
        actual: {test_name: status} from running tests on the OLD commit.
        expected: {test_name: status} from the dataset (NEW commit results).

    Pass criteria:
        - Same test set (no missing/extra)
        - F2P > 0 (at least one bug-revealing test)
        - P2F == 0 (no tests that shouldn't pass but do)
    """
    expected_names = set(expected.keys())
    actual_names = set(actual.keys())

    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    common = expected_names & actual_names

    f2p, p2p, f2f, p2f = [], [], [], []

    for name in sorted(common):
        exp = expected[name]
        act = actual[name]
        if exp == "PASSED" and act != "PASSED":
            f2p.append(name)
        elif exp == "PASSED" and act == "PASSED":
            p2p.append(name)
        elif exp != "PASSED" and act != "PASSED":
            f2f.append(name)
        else:  # exp != "PASSED" and act == "PASSED"
            p2f.append(name)

    # Determine pass/fail
    reasons = []
    if missing:
        reasons.append(f"{len(missing)} missing tests")
    if extra:
        reasons.append(f"{len(extra)} extra tests")
    if not f2p:
        reasons.append("no F2P tests (need >=1 bug-revealing test)")
    if p2f:
        reasons.append(f"{len(p2f)} P2F tests (should be 0)")

    passed = not reasons
    reason = "all checks passed" if passed else "; ".join(reasons)

    return ValidationResult(
        passed=passed,
        reason=reason,
        f2p_tests=f2p,
        p2p_tests=p2p,
        f2f_tests=f2f,
        p2f_tests=p2f,
        missing_tests=missing,
        extra_tests=extra,
    )


def validate_image(
    image: str,
    expected_output_json: dict[str, str],
    timeout: int = 300,
) -> ValidationResult:
    """Run tests in a Docker image and validate against expected results.

    Args:
        image: Docker image name (e.g. "namanjain12/sympy_final:abc123").
        expected_output_json: {test_name: expected_status} from dataset.
        timeout: Timeout in seconds for docker run.

    Returns:
        ValidationResult with classification of all tests.
    """
    try:
        res = subprocess.run(
            ["docker", "run", "--rm", image, "bash", "run_tests.sh"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        raw_output = res.stdout + res.stderr
    except subprocess.TimeoutExpired:
        return ValidationResult(
            passed=False,
            reason=f"docker run timed out after {timeout}s",
            raw_output=f"TIMEOUT after {timeout}s",
        )

    actual = parse_test_output(raw_output)
    if actual is None:
        return ValidationResult(
            passed=False,
            reason="could not parse test output (no summary section found)",
            raw_output=raw_output,
        )

    result = compare_results(actual, expected_output_json)
    result.raw_output = raw_output
    return result


def delete_image(image: str) -> bool:
    """Force-remove a Docker image. Returns True on success."""
    try:
        subprocess.run(
            ["docker", "rmi", "-f", image],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except Exception:
        return False
