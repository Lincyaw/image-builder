"""CLI entry point for r2e_docker — build and push Docker images.

Usage:
    python -m r2e_docker.cli build_base --repo sympy --reference_commit abc123
    python -m r2e_docker.cli build_base --repo sympy --reference_commit abc123 --registry ghcr.io/myorg/
    python -m r2e_docker.cli build_commit --repo sympy --commit abc123 --context_dir ./dir
    python -m r2e_docker.cli build_commit --repo sympy --commit abc123 --context_dir ./dir --push
    python -m r2e_docker.cli build_all_bases --reference_commits refs.json

Environment variables:
    R2E_DOCKER_REGISTRY  — default registry prefix (fallback: "namanjain12/")
"""

from __future__ import annotations

import json

import fire

from r2e_docker.config import DockerBuildConfig, RepoName
from r2e_docker.builder import build_base_image, build_commit_image, push_image


def build_base(
    repo: str,
    reference_commit: str,
    registry: str | None = None,
    rebuild: bool = False,
    timeout: int = 2400,
) -> str:
    """Build the base Docker image for a repository.

    Args:
        repo: Repository name (e.g. 'sympy').
        reference_commit: Commit hash to use for initial dependency installation.
        registry: Docker registry prefix. Defaults to R2E_DOCKER_REGISTRY env var or 'namanjain12/'.
        rebuild: Force rebuild even if image exists.
        timeout: Build timeout in seconds.

    Returns:
        The base image name.
    """
    kwargs: dict = {
        "repo_name": RepoName(repo),
        "rebuild_base": rebuild,
        "base_build_timeout": timeout,
    }
    if registry is not None:
        kwargs["registry"] = registry
    config = DockerBuildConfig(**kwargs)
    return build_base_image(config, reference_commit)


def build_all_bases(
    reference_commits: str,
    registry: str | None = None,
    rebuild: bool = False,
) -> dict[str, str]:
    """Build base images for multiple repos.

    Args:
        reference_commits: Path to a JSON file mapping repo names to commit hashes,
            e.g. {"sympy": "abc123", "pandas": "def456"}.
        registry: Docker registry prefix.
        rebuild: Force rebuild.

    Returns:
        Dict mapping repo name to built image name.
    """
    with open(reference_commits) as f:
        commits: dict[str, str] = json.load(f)

    results: dict[str, str] = {}
    for repo_name, commit_hash in commits.items():
        try:
            img = build_base(
                repo=repo_name,
                reference_commit=commit_hash,
                registry=registry,
                rebuild=rebuild,
            )
            results[repo_name] = img
        except Exception as e:
            print(f"Failed to build base for {repo_name}: {e}")
            results[repo_name] = f"FAILED: {e}"
    return results


def build_commit(
    repo: str,
    commit: str,
    context_dir: str,
    registry: str | None = None,
    push: bool = False,
    rebuild: bool = False,
    memory_limit: str = "1g",
    timeout: int = 600,
) -> str | None:
    """Build a thin per-commit Docker image.

    Args:
        repo: Repository name.
        commit: The old commit hash to checkout.
        context_dir: Directory with install.sh, run_tests.sh, r2e_tests/.
        registry: Docker registry prefix.
        push: Push after building.
        rebuild: Force rebuild.
        memory_limit: Docker build memory limit.
        timeout: Build timeout in seconds.

    Returns:
        The image name on success, None on failure.
    """
    kwargs: dict = {
        "repo_name": RepoName(repo),
        "rebuild_commits": rebuild,
        "push": push,
        "memory_limit": memory_limit,
        "commit_build_timeout": timeout,
    }
    if registry is not None:
        kwargs["registry"] = registry
    config = DockerBuildConfig(**kwargs)
    return build_commit_image(config, commit, context_dir)


def main():
    from r2e_docker.batch import build_from_dataset

    fire.Fire(
        {
            "build_base": build_base,
            "build_all_bases": build_all_bases,
            "build_commit": build_commit,
            "build_from_dataset": build_from_dataset,
            "push": push_image,
        }
    )


if __name__ == "__main__":
    main()
