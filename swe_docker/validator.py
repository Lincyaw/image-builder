"""Two-step F2P/P2P validation for SWE-bench Docker images.

Validation requires BOTH steps to pass:

Step 1 — Pre-patch (old commit, no gold patch):
  - F2P tests must FAIL (they reveal the bug)
  - P2P tests must PASS (they are stable)

Step 2 — Post-patch (gold patch applied):
  - F2P tests must PASS (the fix resolves them)
  - P2P tests must PASS (they remain stable)

An image passes validation only when both steps succeed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import docker
import docker.errors

from swe_docker.constants import START_TEST_OUTPUT, END_TEST_OUTPUT


def _ansi_escape(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ---------------------------------------------------------------------------
# Test output parsing  (SWE-bench format)
# ---------------------------------------------------------------------------


def parse_log(raw: str) -> dict[str, str]:
    """Parse SWE-bench eval output into {test_id: status}.

    Looks for the region between START_TEST_OUTPUT / END_TEST_OUTPUT markers,
    then extracts pytest-style result lines.
    """
    cleaned = _ansi_escape(raw)
    lines = cleaned.splitlines()

    # Find test output region
    start_idx = end_idx = None
    for i, line in enumerate(lines):
        if START_TEST_OUTPUT in line:
            start_idx = i + 1
        if END_TEST_OUTPUT in line:
            end_idx = i
            break

    if start_idx is None:
        # Fallback: try to find "short test summary info" section
        for i, line in enumerate(lines):
            if "short test summary info" in line:
                start_idx = i + 1
                break

    if start_idx is None:
        return {}

    region = lines[start_idx:end_idx] if end_idx else lines[start_idx:]

    # Parse PASSED/FAILED/ERROR lines
    results: dict[str, str] = {}
    status_pat = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)")

    for line in region:
        stripped = line.strip()
        if not stripped:
            continue
        m = status_pat.match(stripped)
        if m:
            status, test_id = m.group(1), m.group(2)
            if status == "ERROR":
                status = "FAILED"
            results[test_id] = status

    return results


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Combined result of the two-step validation."""

    passed: bool
    reason: str

    # Step 1 (pre-patch) classification
    pre_f2p_correct: int = 0   # F2P tests that correctly FAIL
    pre_f2p_wrong: int = 0     # F2P tests that wrongly PASS
    pre_p2p_correct: int = 0   # P2P tests that correctly PASS
    pre_p2p_wrong: int = 0     # P2P tests that wrongly FAIL

    # Step 2 (post-patch) classification
    post_f2p_correct: int = 0  # F2P tests that correctly PASS
    post_f2p_wrong: int = 0    # F2P tests that wrongly FAIL
    post_p2p_correct: int = 0  # P2P tests that correctly PASS
    post_p2p_wrong: int = 0    # P2P tests that wrongly FAIL

    pre_raw: str = ""
    post_raw: str = ""
    details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] "
            f"pre(F2P_fail={self.pre_f2p_correct} F2P_pass={self.pre_f2p_wrong} "
            f"P2P_pass={self.pre_p2p_correct} P2P_fail={self.pre_p2p_wrong}) "
            f"post(F2P_pass={self.post_f2p_correct} F2P_fail={self.post_f2p_wrong} "
            f"P2P_pass={self.post_p2p_correct} P2P_fail={self.post_p2p_wrong}) "
            f"| {self.reason}"
        )

    def detailed_log(self) -> str:
        lines = [self.summary(), ""]
        for d in self.details:
            lines.append(f"  {d}")
        lines.append("")
        lines.append("--- Pre-patch raw output ---")
        lines.append(self.pre_raw[-2000:] if len(self.pre_raw) > 2000 else self.pre_raw)
        lines.append("")
        lines.append("--- Post-patch raw output ---")
        lines.append(self.post_raw[-2000:] if len(self.post_raw) > 2000 else self.post_raw)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Container-based execution
# ---------------------------------------------------------------------------

HEREDOC_DELIMITER = "EOF_SWE_DOCKER_1399519320"


def _exec_in_container(
    container,
    cmd: str,
    timeout: int = 300,
) -> tuple[str, bool]:
    """Execute a command in a running container.

    Returns (output, timed_out).
    """
    import threading
    import time

    exec_result = b""
    exec_id = None
    exception = None
    timed_out = False

    def run():
        nonlocal exec_result, exec_id, exception
        try:
            exec_id = container.client.api.exec_create(container.id, cmd, workdir="/testbed")["Id"]
            stream = container.client.api.exec_start(exec_id, stream=True)
            for chunk in stream:
                exec_result += chunk
        except Exception as e:
            exception = e

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout)

    if exception:
        raise exception

    if thread.is_alive():
        if exec_id is not None:
            try:
                pid = container.client.api.exec_inspect(exec_id)["Pid"]
                container.exec_run(f"kill -TERM {pid}", detach=True)
            except Exception:
                pass
        timed_out = True

    return exec_result.decode("utf-8", errors="replace"), timed_out


def _write_script_to_container(container, script: str, path: str) -> None:
    """Write a script into a running container."""
    escaped = script.replace("'", "'\\''")
    cmd = f"bash -c 'cat > {path} << '\"'\"'{HEREDOC_DELIMITER}'\"'\"'\n{script}\n{HEREDOC_DELIMITER}'"
    # Simpler approach: use exec_run with stdin
    container.exec_run(f"bash -c \"cat <<'{HEREDOC_DELIMITER}' > {path}\n{script}\n{HEREDOC_DELIMITER}\"")
    container.exec_run(f"chmod +x {path}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_image(
    spec,  # InstanceSpec
    client: docker.DockerClient | None = None,
    timeout: int = 600,
) -> ValidationResult:
    """Run two-step validation on a built instance image.

    Step 1: Run eval script on old commit (no gold patch).
    Step 2: Apply gold patch, run eval script again.

    Args:
        spec: InstanceSpec with all instance info.
        client: Docker client (created if None).
        timeout: Per-step timeout in seconds.

    Returns:
        ValidationResult with pass/fail and details.
    """
    if client is None:
        client = docker.from_env()

    image_name = spec.instance_image_key
    f2p_tests = set(spec.fail_to_pass)
    p2p_tests = set(spec.pass_to_pass)
    eval_script = spec.eval_script()
    gold_patch = spec.patch

    if not f2p_tests:
        return ValidationResult(
            passed=False,
            reason="no FAIL_TO_PASS tests defined",
        )

    container = None
    try:
        # Create container
        container = client.containers.create(
            image=image_name,
            command="tail -f /dev/null",
            detach=True,
            platform=spec.platform_str,
        )
        container.start()

        # Write eval script into container
        _write_script_to_container(container, eval_script, "/root/eval.sh")

        # ---- Step 1: Pre-patch (old commit) ----
        pre_output, pre_timeout = _exec_in_container(
            container, "bash /root/eval.sh", timeout=timeout
        )

        if pre_timeout:
            return ValidationResult(
                passed=False,
                reason=f"pre-patch eval timed out after {timeout}s",
                pre_raw=f"TIMEOUT after {timeout}s",
            )

        pre_results = parse_log(pre_output)
        if not pre_results:
            return ValidationResult(
                passed=False,
                reason="could not parse pre-patch test output",
                pre_raw=pre_output,
            )

        # Classify pre-patch results
        pre_f2p_correct = pre_f2p_wrong = 0
        pre_p2p_correct = pre_p2p_wrong = 0
        details: list[str] = []

        for test_id in f2p_tests:
            status = pre_results.get(test_id)
            if status is None:
                details.append(f"PRE F2P missing: {test_id}")
                pre_f2p_wrong += 1
            elif status != "PASSED":
                pre_f2p_correct += 1  # correctly fails
            else:
                pre_f2p_wrong += 1
                details.append(f"PRE F2P unexpectedly PASSED: {test_id}")

        for test_id in p2p_tests:
            status = pre_results.get(test_id)
            if status is None:
                details.append(f"PRE P2P missing: {test_id}")
                pre_p2p_wrong += 1
            elif status == "PASSED":
                pre_p2p_correct += 1  # correctly passes
            else:
                pre_p2p_wrong += 1
                details.append(f"PRE P2P unexpectedly FAILED: {test_id}")

        # Check step 1 pass criteria
        step1_pass = pre_f2p_wrong == 0 and pre_p2p_wrong == 0

        if not step1_pass:
            reasons = []
            if pre_f2p_wrong:
                reasons.append(f"{pre_f2p_wrong} F2P tests did not fail pre-patch")
            if pre_p2p_wrong:
                reasons.append(f"{pre_p2p_wrong} P2P tests did not pass pre-patch")
            return ValidationResult(
                passed=False,
                reason="step 1 (pre-patch) failed: " + "; ".join(reasons),
                pre_f2p_correct=pre_f2p_correct,
                pre_f2p_wrong=pre_f2p_wrong,
                pre_p2p_correct=pre_p2p_correct,
                pre_p2p_wrong=pre_p2p_wrong,
                pre_raw=pre_output,
                details=details,
            )

        # ---- Step 2: Post-patch (apply gold patch) ----
        if not gold_patch:
            return ValidationResult(
                passed=False,
                reason="no gold patch available for step 2",
                pre_f2p_correct=pre_f2p_correct,
                pre_p2p_correct=pre_p2p_correct,
                pre_raw=pre_output,
                details=details,
            )

        # Apply gold patch
        _write_script_to_container(container, gold_patch, "/tmp/gold_patch.diff")
        apply_result = container.exec_run(
            "bash -c 'cd /testbed && git apply -v /tmp/gold_patch.diff'",
        )
        if apply_result.exit_code != 0:
            # Try with --reject
            apply_result = container.exec_run(
                "bash -c 'cd /testbed && git apply -v --reject /tmp/gold_patch.diff'",
            )
            if apply_result.exit_code != 0:
                # Try patch command
                apply_result = container.exec_run(
                    "bash -c 'cd /testbed && patch --batch --fuzz=5 -p1 -i /tmp/gold_patch.diff'",
                )
                if apply_result.exit_code != 0:
                    return ValidationResult(
                        passed=False,
                        reason=f"could not apply gold patch: {apply_result.output.decode('utf-8', errors='replace')[:500]}",
                        pre_f2p_correct=pre_f2p_correct,
                        pre_p2p_correct=pre_p2p_correct,
                        pre_raw=pre_output,
                        details=details,
                    )

        # Run eval again
        post_output, post_timeout = _exec_in_container(
            container, "bash /root/eval.sh", timeout=timeout
        )

        if post_timeout:
            return ValidationResult(
                passed=False,
                reason=f"post-patch eval timed out after {timeout}s",
                pre_f2p_correct=pre_f2p_correct,
                pre_p2p_correct=pre_p2p_correct,
                pre_raw=pre_output,
                post_raw=f"TIMEOUT after {timeout}s",
                details=details,
            )

        post_results = parse_log(post_output)
        if not post_results:
            return ValidationResult(
                passed=False,
                reason="could not parse post-patch test output",
                pre_f2p_correct=pre_f2p_correct,
                pre_p2p_correct=pre_p2p_correct,
                pre_raw=pre_output,
                post_raw=post_output,
                details=details,
            )

        # Classify post-patch results
        post_f2p_correct = post_f2p_wrong = 0
        post_p2p_correct = post_p2p_wrong = 0

        for test_id in f2p_tests:
            status = post_results.get(test_id)
            if status is None:
                details.append(f"POST F2P missing: {test_id}")
                post_f2p_wrong += 1
            elif status == "PASSED":
                post_f2p_correct += 1  # correctly passes after fix
            else:
                post_f2p_wrong += 1
                details.append(f"POST F2P still FAILED: {test_id}")

        for test_id in p2p_tests:
            status = post_results.get(test_id)
            if status is None:
                details.append(f"POST P2P missing: {test_id}")
                post_p2p_wrong += 1
            elif status == "PASSED":
                post_p2p_correct += 1
            else:
                post_p2p_wrong += 1
                details.append(f"POST P2P unexpectedly FAILED: {test_id}")

        step2_pass = post_f2p_wrong == 0 and post_p2p_wrong == 0

        if not step2_pass:
            reasons = []
            if post_f2p_wrong:
                reasons.append(f"{post_f2p_wrong} F2P tests did not pass post-patch")
            if post_p2p_wrong:
                reasons.append(f"{post_p2p_wrong} P2P tests did not pass post-patch")
            return ValidationResult(
                passed=False,
                reason="step 2 (post-patch) failed: " + "; ".join(reasons),
                pre_f2p_correct=pre_f2p_correct,
                pre_p2p_correct=pre_p2p_correct,
                post_f2p_correct=post_f2p_correct,
                post_f2p_wrong=post_f2p_wrong,
                post_p2p_correct=post_p2p_correct,
                post_p2p_wrong=post_p2p_wrong,
                pre_raw=pre_output,
                post_raw=post_output,
                details=details,
            )

        # Both steps passed!
        return ValidationResult(
            passed=True,
            reason="all checks passed (both pre-patch and post-patch)",
            pre_f2p_correct=pre_f2p_correct,
            pre_p2p_correct=pre_p2p_correct,
            post_f2p_correct=post_f2p_correct,
            post_p2p_correct=post_p2p_correct,
            pre_raw=pre_output,
            post_raw=post_output,
            details=details,
        )

    finally:
        if container is not None:
            try:
                container.stop(timeout=10)
            except Exception:
                pass
            try:
                container.remove(force=True)
            except Exception:
                pass


def delete_image(client: docker.DockerClient, image_name: str) -> bool:
    """Force-remove a Docker image. Returns True on success."""
    try:
        client.images.remove(image_name, force=True)
        return True
    except Exception:
        return False
