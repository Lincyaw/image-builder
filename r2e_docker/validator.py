"""Post-build F2P/P2P validation for Docker images.

Two-step validation:

Step 1 — Pre-patch (old commit, current image state):
  Runs tests and classifies results against expected_output_json:
  - F2P (fail-to-pass): expected PASSED on new commit, actually fails on old commit
  - P2P (pass-to-pass): expected PASSED, actually passes (stable)
  - F2F (fail-to-fail): expected not PASSED, actually not passes (fine)
  - P2F (pass-to-fail): expected not PASSED, actually passes (bad — must be 0)

Step 2 — Post-patch (checkout new commit):
  Checks out the new (fixed) commit, re-runs tests, verifies:
  - All expected-PASSED tests actually PASS
  - F2P tests now PASS (the fix works)

An image passes validation when:
  Step 1: same test set, F2P >= 1, P2F == 0
  Step 2: all expected-PASSED tests pass (F2P resolved, P2P still pass)
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
    post_patch_output: str = ""
    post_patch_failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One-line summary."""
        status = "PASS" if self.passed else "FAIL"
        post = ""
        if self.post_patch_failures:
            post = f" post_fail={len(self.post_patch_failures)}"
        return (
            f"[{status}] F2P={len(self.f2p_tests)} P2P={len(self.p2p_tests)} "
            f"F2F={len(self.f2f_tests)} P2F={len(self.p2f_tests)} "
            f"missing={len(self.missing_tests)} extra={len(self.extra_tests)}"
            f"{post} | {self.reason}"
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

        if self.post_patch_failures:
            _section("Post-patch failures (should all PASS)", self.post_patch_failures)

        lines.append("--- Raw test output (pre-patch) ---")
        lines.append(self.raw_output)

        if self.post_patch_output:
            lines.append("")
            lines.append("--- Raw test output (post-patch) ---")
            lines.append(self.post_patch_output)

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
    new_commit: str | None = None,
) -> ValidationResult:
    """Run tests in a Docker image and validate against expected results.

    Two-step validation when ``new_commit`` is provided:
      Step 1: Run tests on old commit (current image state).
      Step 2: Checkout new_commit, reinstall, run tests again.

    Args:
        image: Docker image name (e.g. "arl/sympy_final:abc123").
        expected_output_json: {test_name: expected_status} from dataset.
        timeout: Timeout in seconds for each docker run.
        new_commit: The NEW (fixed) commit hash. If provided, enables step 2
            validation (checkout new commit, verify F2P tests now pass).
            The R2E dataset's commit_hash field is the new commit.

    Returns:
        ValidationResult with classification of all tests.
    """
    # ---- Step 1: Pre-patch (old commit) ----
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

    if not result.passed:
        return result

    # ---- Step 2: Post-patch (new commit) ----
    if new_commit is None:
        # Step 1 only — keep existing behavior
        return result

    # Run tests on the new (fixed) commit inside a container
    try:
        # Start container (not --rm, so we can exec commands)
        container_id = subprocess.run(
            ["docker", "create", image, "tail", "-f", "/dev/null"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

        subprocess.run(
            ["docker", "start", container_id],
            capture_output=True,
            timeout=30,
        )

        # Checkout the new (fixed) commit and reinstall
        checkout_cmd = (
            f"cd /testbed && "
            f"git reset --hard && git checkout -f {new_commit} && "
            f"bash install.sh --quick"
        )
        subprocess.run(
            ["docker", "exec", container_id, "bash", "-c", checkout_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # Run tests on the new commit
        post_res = subprocess.run(
            ["docker", "exec", container_id, "bash", "run_tests.sh"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        post_output = post_res.stdout + post_res.stderr

    except subprocess.TimeoutExpired:
        result.passed = False
        result.reason = f"post-patch docker exec timed out after {timeout}s"
        result.post_patch_output = f"TIMEOUT after {timeout}s"
        return result
    except Exception as e:
        result.passed = False
        result.reason = f"post-patch execution error: {e}"
        return result
    finally:
        # Cleanup container
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass

    result.post_patch_output = post_output

    post_actual = parse_test_output(post_output)
    if post_actual is None:
        result.passed = False
        result.reason = "could not parse post-patch test output"
        return result

    # All expected-PASSED tests should now PASS on the new commit
    post_failures = []
    for name, exp_status in expected_output_json.items():
        if exp_status == "PASSED":
            actual_status = post_actual.get(name)
            if actual_status != "PASSED":
                post_failures.append(f"{name} (got {actual_status})")

    if post_failures:
        result.passed = False
        result.post_patch_failures = post_failures
        result.reason = (
            f"step 2 (post-patch) failed: {len(post_failures)} expected-PASSED "
            f"tests did not pass on new commit"
        )

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
