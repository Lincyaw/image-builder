"""Script generation for SWE-bench Docker image builds.

Ported from swebench.harness.test_spec.python — generates shell scripts for
env setup, repo setup, and eval.
"""

from __future__ import annotations

import os
import posixpath
import re

import requests as _requests

from swe_docker.constants import (
    MAP_REPO_TO_ENV_YML_PATHS,
    MAP_REPO_TO_REQS_PATHS,
    MAP_REPO_VERSION_TO_SPECS,
    NON_TEST_EXTS,
    SWE_BENCH_URL_RAW,
    START_TEST_OUTPUT,
    END_TEST_OUTPUT,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36"
}


# ---------------------------------------------------------------------------
# Requirement / environment.yml fetching
# ---------------------------------------------------------------------------


def _get_requirements_by_commit(repo: str, commit: str) -> str:
    """Fetch requirements.txt from GitHub for a specific repo+commit."""
    for req_path in MAP_REPO_TO_REQS_PATHS[repo]:
        reqs_url = posixpath.join(SWE_BENCH_URL_RAW, repo, commit, req_path)
        resp = _requests.get(reqs_url, headers=_HEADERS)
        if resp.status_code == 200:
            break
    else:
        raise ValueError(
            f"Could not find requirements.txt at {MAP_REPO_TO_REQS_PATHS[repo]} "
            f"for {repo}@{commit}"
        )

    lines = resp.text
    original_req = []
    additional_reqs = []
    req_dir = "/".join(req_path.split("/")[:-1])
    exclude_line = lambda line: any(
        line.strip().startswith(x) for x in ["-e .", "#", ".[test"]
    )

    for line in lines.split("\n"):
        if line.strip().startswith("-r"):
            file_name = line[len("-r"):].strip()
            nested_url = os.path.join(SWE_BENCH_URL_RAW, repo, commit, req_dir, file_name)
            nested = _requests.get(nested_url, headers=_HEADERS)
            if nested.status_code == 200:
                for extra_line in nested.text.split("\n"):
                    if not exclude_line(extra_line):
                        additional_reqs.append(extra_line)
        else:
            if not exclude_line(line):
                original_req.append(line)

    additional_reqs.append("\n".join(original_req))
    return "\n".join(additional_reqs)


def get_requirements(instance: dict) -> str:
    commit = instance.get("environment_setup_commit") or instance["base_commit"]
    return _get_requirements_by_commit(instance["repo"], commit)


def _get_environment_yml_by_commit(repo: str, commit: str, env_name: str) -> str:
    """Fetch environment.yml from GitHub for a specific repo+commit."""
    for req_path in MAP_REPO_TO_ENV_YML_PATHS[repo]:
        reqs_url = posixpath.join(SWE_BENCH_URL_RAW, repo, commit, req_path)
        resp = _requests.get(reqs_url, headers=_HEADERS)
        if resp.status_code == 200:
            break
    else:
        raise ValueError(
            f"Could not find environment.yml at {MAP_REPO_TO_ENV_YML_PATHS[repo]} "
            f"for {repo}@{commit}"
        )

    cleaned = []
    for line in resp.text.split("\n"):
        if line.startswith("name:"):
            cleaned.append(f"name: {env_name}")
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def get_environment_yml(instance: dict, env_name: str) -> str:
    commit = instance.get("environment_setup_commit") or instance["base_commit"]
    return _get_environment_yml_by_commit(instance["repo"], commit, env_name)


# ---------------------------------------------------------------------------
# Test directives from test_patch
# ---------------------------------------------------------------------------


def _get_modified_files(patch: str) -> list[str]:
    """Extract modified file paths from a unified diff."""
    diff_pat = r"diff --git a/.* b/(.*)"
    files = re.findall(diff_pat, patch)
    return files


def get_test_directives(instance: dict) -> list[str]:
    """Get test directives from the test_patch of a task instance."""
    diff_pat = r"diff --git a/.* b/(.*)"
    test_patch = instance["test_patch"]
    directives = re.findall(diff_pat, test_patch)
    directives = [
        d for d in directives if not any(d.endswith(ext) for ext in NON_TEST_EXTS)
    ]

    # Django: convert paths to module notation
    if instance["repo"] == "django/django":
        transformed = []
        for d in directives:
            d = d[:-len(".py")] if d.endswith(".py") else d
            d = d[len("tests/"):] if d.startswith("tests/") else d
            d = d.replace("/", ".")
            transformed.append(d)
        directives = transformed

    return directives


# ---------------------------------------------------------------------------
# Script generators
# ---------------------------------------------------------------------------


def make_env_script(instance: dict, specs: dict, env_name: str = "testbed") -> str:
    """Generate the env setup script (conda env + dependencies)."""
    HEREDOC_DELIMITER = "EOF_59812759871"
    cmds = ["source /opt/miniconda3/bin/activate"]

    pkgs = specs.get("packages", "")
    if pkgs == "requirements.txt":
        cmds.append(f"conda create -n {env_name} python={specs['python']} -y")
        reqs = get_requirements(instance)
        path_to_reqs = "$HOME/requirements.txt"
        cmds.append(f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}")
        cmds.append(f"conda activate {env_name} && python -m pip install -r {path_to_reqs}")
        cmds.append(f"rm {path_to_reqs}")
    elif pkgs == "environment.yml":
        reqs = get_environment_yml(instance, env_name)
        path_to_reqs = "environment.yml"
        cmds.append(f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}")
        if specs.get("no_use_env"):
            cmds.append(f"conda create -c conda-forge -n {env_name} python={specs['python']} -y")
            cmds.append(f"conda env update -f {path_to_reqs}")
        else:
            cmds.append(f"conda env create --file {path_to_reqs}")
            cmds.append(f"conda activate {env_name} && conda install python={specs['python']} -y")
        cmds.append(f"rm {path_to_reqs}")
    else:
        cmds.append(f"conda create -n {env_name} python={specs['python']} {pkgs} -y")

    cmds.append(f"conda activate {env_name}")

    if "pip_packages" in specs:
        pip_packages = " ".join(specs["pip_packages"])
        cmds.append(f"python -m pip install {pip_packages}")

    return "\n".join(["#!/bin/bash", "set -euxo pipefail"] + cmds) + "\n"


def make_repo_script(
    specs: dict,
    repo: str,
    repo_directory: str,
    base_commit: str,
    env_name: str = "testbed",
) -> str:
    """Generate the repo setup script (clone + checkout + install)."""
    cmds = [
        f"git clone -o origin https://github.com/{repo} {repo_directory}",
        f"chmod -R 777 {repo_directory}",
        f"cd {repo_directory}",
        f"git reset --hard {base_commit}",
        "git remote remove origin",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f'echo "Current environment: $CONDA_DEFAULT_ENV"',
    ]

    if "pre_install" in specs:
        cmds.extend(specs["pre_install"])
    if "install" in specs:
        cmds.append(specs["install"])

    # Clean diff marker
    cmds.extend([
        "git config --global user.email setup@swebench.config",
        "git config --global user.name SWE-bench",
        "git commit --allow-empty -am SWE-bench",
    ])

    return "\n".join(["#!/bin/bash", "set -euxo pipefail"] + cmds) + "\n"


def make_eval_script(
    instance: dict,
    specs: dict,
    env_name: str = "testbed",
    repo_directory: str = "/testbed",
) -> str:
    """Generate the eval script (apply test patch + run tests).

    This script is used for BOTH pre-patch and post-patch validation.
    The gold patch (if any) is applied separately before running this script.
    """
    HEREDOC_DELIMITER = "EOF_114329324912"
    base_commit = instance["base_commit"]
    test_patch = instance["test_patch"]
    test_files = _get_modified_files(test_patch)

    reset_tests_command = f"git checkout {base_commit} {' '.join(test_files)}"
    apply_test_patch_command = (
        f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{test_patch}\n{HEREDOC_DELIMITER}"
    )
    test_command = " ".join(
        [
            MAP_REPO_VERSION_TO_SPECS[instance["repo"]][instance["version"]]["test_cmd"],
            *get_test_directives(instance),
        ]
    )

    cmds = [
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
        f"cd {repo_directory}",
    ]
    if "eval_commands" in specs:
        cmds.extend(specs["eval_commands"])
    cmds.extend([
        f"git config --global --add safe.directory {repo_directory}",
        f"cd {repo_directory}",
        "git status",
        "git show",
        f"git -c core.fileMode=false diff {base_commit}",
        "source /opt/miniconda3/bin/activate",
        f"conda activate {env_name}",
    ])
    if "install" in specs:
        cmds.append(specs["install"])
    cmds.extend([
        reset_tests_command,
        apply_test_patch_command,
        f": '{START_TEST_OUTPUT}'",
        test_command,
        f": '{END_TEST_OUTPUT}'",
        reset_tests_command,
    ])

    # Use set -uxo (no -e) — don't exit early; we need to revert tests
    return "\n".join(["#!/bin/bash", "set -uxo pipefail"] + cmds) + "\n"
