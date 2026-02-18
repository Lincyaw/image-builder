"""Batch build Docker images.

Usage:
    # Build all base images (no dataset needed)
    python -m r2e_docker.batch build_all_bases

    # Build base + commit images from HuggingFace dataset (streaming, no full download)
    HF_ENDPOINT=https://hf-mirror.com python -m r2e_docker.batch build_from_dataset --limit 1

    # Custom registry
    python -m r2e_docker.batch build_from_dataset --registry ghcr.io/myorg/ --limit 5
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import fire

from r2e_docker.config import DockerBuildConfig, RepoName, REPO_TEST_COMMANDS
from r2e_docker.builder import (
    build_base_image,
    build_commit_image,
    generate_commit_dockerfile,
)

# Default reference commits per repo (used for base image builds).
# Any valid commit works — the base image just needs one to install deps.
# These can be overridden via --reference_commits JSON file.
DEFAULT_REFERENCE_COMMITS: dict[str, str] = {
    "sympy": "98f087276dc2",
    "pandas": "2a6539c890c8",
    "numpy": "5cac4b7bfb93",
    "scrapy": "b1065b5d4062",
    "tornado": "d4db9c1a7798",
    "pillow": "5bff4a3253c8",
    "pyramid": "a1a1e8c36580",
    "datalad": "a1b6f2f2e2c2",
    "aiohttp": "4c72e78e19af",
    "coveragepy": "a781b7fe79d6",
    "orange3": "38520e8fb2b0",
    "bokeh": "2024f0e6693e",
}


def _parse_docker_image(docker_image: str) -> tuple[str, str]:
    """Extract (repo_name, commit_hash) from docker_image string.

    e.g. 'namanjain12/sympy_final:abc123' -> ('sympy', 'abc123')
    """
    name_tag = docker_image.rsplit("/", 1)[-1]
    name, tag = name_tag.rsplit(":", 1)
    repo = name.removesuffix("_final")
    return repo, tag


def _prepare_build_context(
    config: DockerBuildConfig,
    commit_hash: str,
    test_file_codes: list[str],
    test_file_names: list[str],
    dest_dir: Path,
) -> None:
    """Create a build context directory for a commit image."""
    dockerfile_content = generate_commit_dockerfile(config)
    (dest_dir / "Dockerfile").write_text(dockerfile_content)

    shutil.copy(config.install_script, dest_dir / "install.sh")

    tests_cmd = REPO_TEST_COMMANDS.get(config.repo_name)
    if tests_cmd is None:
        tests_cmd = (
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
            ".venv/bin/python -W ignore -m pytest -rA r2e_tests"
        )
    (dest_dir / "run_tests.sh").write_text(tests_cmd)

    tests_dir = dest_dir / "r2e_tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("")
    for code, name in zip(test_file_codes, test_file_names):
        (tests_dir / name).write_text(code)


def _build_one_commit(args: tuple) -> tuple[str, str | None]:
    """Build a single commit image. Returns (key, built_name | None)."""
    (
        repo_str,
        commit_hash,
        test_file_codes,
        test_file_names,
        registry,
        rebuild,
        do_push,
    ) = args

    try:
        config = DockerBuildConfig(
            repo_name=RepoName(repo_str),
            registry=registry,
            rebuild_commits=rebuild,
            push=do_push,
        )
    except ValueError:
        print(f"Unknown repo: {repo_str}, skipping")
        return (f"{repo_str}:{commit_hash}", None)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        _prepare_build_context(
            config, commit_hash, test_file_codes, test_file_names, tmpdir
        )
        result = build_commit_image(config, commit_hash, tmpdir)

    return (f"{repo_str}:{commit_hash}", result)


# ── CLI commands ─────────────────────────────────────────────────────────


def build_all_bases(
    reference_commits: str | None = None,
    repos: str | None = None,
    registry: str | None = None,
    rebuild: bool = False,
) -> None:
    """Build base images for all (or selected) repos. No dataset needed.

    Args:
        reference_commits: Path to JSON file {repo: commit_hash}.
                           If omitted, uses built-in defaults.
        repos: Comma-separated repo names to build. If omitted, builds all.
        registry: Docker registry prefix.
        rebuild: Force rebuild even if image exists.
    """
    reg = registry or os.environ.get("R2E_DOCKER_REGISTRY", "namanjain12/")

    if reference_commits:
        with open(reference_commits) as f:
            commits: dict[str, str] = json.load(f)
    else:
        commits = dict(DEFAULT_REFERENCE_COMMITS)

    if repos:
        selected = {r.strip() for r in repos.split(",")}
        commits = {k: v for k, v in commits.items() if k in selected}

    print(f"Building base images for {len(commits)} repos")
    for repo_str, commit_hash in commits.items():
        try:
            config = DockerBuildConfig(
                repo_name=RepoName(repo_str),
                registry=reg,
                rebuild_base=rebuild,
            )
            build_base_image(config, commit_hash)
        except Exception as e:
            print(f"Failed to build base for {repo_str}: {e}")


def build_from_dataset(
    dataset: str = "R2E-Gym/R2E-Gym-Lite",
    split: str = "train",
    registry: str | None = None,
    max_workers: int = 4,
    rebuild: bool = False,
    push: bool = False,
    base_only: bool = False,
    limit: int | None = None,
) -> None:
    """Build Docker images from a HuggingFace dataset (streaming, no full download).

    Args:
        dataset: HuggingFace dataset name.
        split: Dataset split.
        registry: Docker registry prefix.
        max_workers: Parallel workers for commit image builds.
        rebuild: Force rebuild even if images exist.
        push: Push images after building.
        base_only: Only build base images, skip commit images.
        limit: Max number of commit images to build (None = all).
    """
    from datasets import load_dataset as _load

    reg = registry or os.environ.get("R2E_DOCKER_REGISTRY", "namanjain12/")

    print(f"Streaming dataset {dataset} split={split} ...")
    ds = _load(dataset, split=split, streaming=True)

    # Scan entries
    repo_first_commit: dict[str, str] = {}
    all_tasks: list[tuple] = []
    count = 0

    for entry in ds:
        docker_image = entry.get("docker_image") or entry.get("image_name", "")
        if not docker_image:
            continue

        repo_str, commit_hash = _parse_docker_image(docker_image)

        if repo_str not in repo_first_commit:
            repo_first_commit[repo_str] = commit_hash

        if not base_only:
            exec_content = entry.get("execution_result_content", "")
            test_file_codes, test_file_names = [], []
            if exec_content:
                try:
                    exec_data = json.loads(exec_content)
                    test_file_codes = exec_data.get("test_file_codes", [])
                    test_file_names = exec_data.get("test_file_names", [])
                except (json.JSONDecodeError, TypeError):
                    pass

            all_tasks.append((
                repo_str, commit_hash,
                test_file_codes, test_file_names,
                reg, rebuild, push,
            ))

        count += 1
        if limit is not None and count >= limit:
            break

    # Step 1: Build base images
    print(f"\n=== Building base images for {len(repo_first_commit)} repos ===")
    for repo_str, commit_hash in repo_first_commit.items():
        try:
            config = DockerBuildConfig(
                repo_name=RepoName(repo_str),
                registry=reg,
                rebuild_base=rebuild,
            )
            build_base_image(config, commit_hash)
        except Exception as e:
            print(f"Failed to build base for {repo_str}: {e}")

    if base_only:
        print("--base_only set, done.")
        return

    # Step 2: Build commit images
    print(f"\n=== Building {len(all_tasks)} commit images (workers={max_workers}) ===")
    success, fail = 0, 0
    with Pool(max_workers) as pool:
        for key, result in pool.imap_unordered(_build_one_commit, all_tasks):
            if result:
                success += 1
            else:
                fail += 1
            total = success + fail
            if total % 10 == 0 or total == len(all_tasks):
                print(f"  progress: {total}/{len(all_tasks)}  ok={success} fail={fail}")

    print(f"\nDone. success={success} fail={fail} total={success + fail}")


if __name__ == "__main__":
    fire.Fire({
        "build_all_bases": build_all_bases,
        "build_from_dataset": build_from_dataset,
    })
