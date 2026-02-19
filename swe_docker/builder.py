"""3-tier Docker image build orchestration for SWE-bench Verified.

Build hierarchy:
  1. Base image   — Ubuntu + Miniconda (shared across ALL repos)
  2. Env image    — conda env + deps (shared per repo+version combo)
  3. Instance image — git clone + checkout + install (one per instance)

Uses the ``docker`` Python SDK for builds and ThreadPoolExecutor for
parallelism.
"""

from __future__ import annotations

import hashlib
import platform
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import docker
import docker.errors

from swe_docker.constants import (
    DEFAULT_DOCKER_SPECS,
    DEFAULT_REGISTRY,
    MAP_REPO_TO_SHORT_NAME,
    MAP_REPO_VERSION_TO_SPECS,
    USE_X86,
)
from swe_docker.dockerfiles import (
    get_dockerfile_base,
    get_dockerfile_env,
    get_dockerfile_instance,
)
from swe_docker.scripts import make_env_script, make_eval_script, make_repo_script

# ---------------------------------------------------------------------------
# Log paths
# ---------------------------------------------------------------------------

_LOG_DIR = Path("output/swe_docker")
FAILED_LOG_DIR = _LOG_DIR / "failed_logs"
_BUILD_LOG_DIR = _LOG_DIR / "build_logs"


def _ansi_escape(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _save_failure_log(category: str, name: str, content: str) -> Path:
    """Save a failure log to output/swe_docker/failed_logs/{category}_{name}.log."""
    safe = name.replace(":", "__").replace("/", "_")
    log_file = FAILED_LOG_DIR / f"{category}_{safe}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(content)
    return log_file


# ---------------------------------------------------------------------------
# Instance spec  (lightweight, no external dependency)
# ---------------------------------------------------------------------------


class InstanceSpec:
    """All the info needed to build + validate a single SWE-bench instance."""

    def __init__(self, instance: dict, registry: str = DEFAULT_REGISTRY):
        self.instance = instance
        self.registry = registry.rstrip("/") + "/" if registry else ""

        self.instance_id: str = instance["instance_id"]
        self.repo: str = instance["repo"]
        self.version: str = instance["version"]
        self.base_commit: str = instance["base_commit"]
        self.test_patch: str = instance["test_patch"]
        self.patch: str = instance.get("patch", "")

        self.specs = MAP_REPO_VERSION_TO_SPECS[self.repo][self.version]
        self.docker_specs = self.specs.get("docker_specs", {})
        self.short_name = MAP_REPO_TO_SHORT_NAME[self.repo]

        # Architecture
        if platform.machine() in {"aarch64", "arm64"}:
            self.arch = "arm64" if self.instance_id not in USE_X86 else "x86_64"
        else:
            self.arch = "x86_64"

        # FAIL_TO_PASS / PASS_TO_PASS  (may be JSON strings)
        self.fail_to_pass = self._load_json_or_list("FAIL_TO_PASS")
        self.pass_to_pass = self._load_json_or_list("PASS_TO_PASS")

    def _load_json_or_list(self, key: str) -> list[str]:
        import json
        val = self.instance.get(key, [])
        if isinstance(val, str):
            return json.loads(val)
        return val

    @property
    def platform_str(self) -> str:
        if self.arch == "x86_64":
            return "linux/x86_64"
        return "linux/arm64/v8"

    @property
    def base_image_key(self) -> str:
        return f"base.py.{self.arch}:latest"

    @property
    def env_image_key(self) -> str:
        """Hash-based key for repo-level image — same env spec = same image."""
        hash_input = str(self._env_script_list_key())
        if self.docker_specs:
            hash_input += str(self.docker_specs)
        hash_input += self.arch
        h = hashlib.sha256(hash_input.encode()).hexdigest()[:22]
        return f"{self.registry}{self.short_name}_base:{h}"

    def _env_script_list_key(self) -> list[str]:
        """Return the env script commands as a list (for hashing, matching SWE-bench)."""
        # We need the same hash as swebench SDK uses — it hashes the env_script_list
        # which is the list of commands, not the final script string.
        # Replicate make_env_script_list_py logic to produce the same list.
        from swe_docker.scripts import get_requirements, get_environment_yml
        HEREDOC_DELIMITER = "EOF_59812759871"
        env_name = "testbed"
        specs = self.specs
        cmds = ["source /opt/miniconda3/bin/activate"]

        pkgs = specs.get("packages", "")
        if pkgs == "requirements.txt":
            cmds.append(f"conda create -n {env_name} python={specs['python']} -y")
            reqs = get_requirements(self.instance)
            path_to_reqs = "$HOME/requirements.txt"
            cmds.append(f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}")
            cmds.append(f"conda activate {env_name} && python -m pip install -r {path_to_reqs}")
            cmds.append(f"rm {path_to_reqs}")
        elif pkgs == "environment.yml":
            reqs = get_environment_yml(self.instance, env_name)
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

        return cmds

    @property
    def instance_image_key(self) -> str:
        """Instance naming: {registry}{short_name}_final:{base_commit}"""
        return f"{self.registry}{self.short_name}_final:{self.base_commit}"

    # Script generation
    def setup_env_script(self) -> str:
        return make_env_script(self.instance, self.specs)

    def install_repo_script(self) -> str:
        return make_repo_script(
            self.specs, self.repo, "/testbed", self.base_commit
        )

    def eval_script(self) -> str:
        return make_eval_script(self.instance, self.specs)

    def base_dockerfile(self) -> str:
        merged = {**DEFAULT_DOCKER_SPECS, **self.docker_specs}
        return get_dockerfile_base(self.platform_str, self.arch, **merged)

    def env_dockerfile(self) -> str:
        merged = {**DEFAULT_DOCKER_SPECS, **self.docker_specs}
        return get_dockerfile_env(self.platform_str, self.base_image_key, **merged)

    def instance_dockerfile(self) -> str:
        return get_dockerfile_instance(self.platform_str, self.env_image_key)


# ---------------------------------------------------------------------------
# Build functions
# ---------------------------------------------------------------------------


def _build_image(
    client: docker.DockerClient,
    image_name: str,
    setup_scripts: dict[str, str],
    dockerfile: str,
    platform_str: str,
    build_dir: Path,
    nocache: bool = False,
) -> str:
    """Build a Docker image using *build_dir* as context.

    Writes *setup_scripts* and *dockerfile* into *build_dir*, runs
    ``docker build``, and returns the build log as a string.

    Raises ``docker.errors.BuildError`` on failure (with partial log
    attached as ``build_log`` attribute).
    """
    for name, content in setup_scripts.items():
        (build_dir / name).write_text(content)
    (build_dir / "Dockerfile").write_text(dockerfile)

    response = client.api.build(
        path=str(build_dir),
        tag=image_name,
        rm=True,
        forcerm=True,
        decode=True,
        platform=platform_str,
        nocache=nocache,
    )

    buildlog = ""
    for chunk in response:
        if "stream" in chunk:
            buildlog += _ansi_escape(chunk["stream"])
        elif "errorDetail" in chunk:
            raise docker.errors.BuildError(
                _ansi_escape(chunk["errorDetail"]["message"]), buildlog
            )
    return buildlog


def build_base_images(
    client: docker.DockerClient,
    specs: list[InstanceSpec],
    force_rebuild: bool = False,
    verbose_logs: bool = False,
) -> None:
    """Build base images (serial, typically just 1)."""
    base_images: dict[str, tuple[str, str]] = {}  # key -> (dockerfile, platform)
    for spec in specs:
        if spec.base_image_key not in base_images:
            base_images[spec.base_image_key] = (spec.base_dockerfile(), spec.platform_str)

    for image_name, (dockerfile, plat) in base_images.items():
        try:
            client.images.get(image_name)
            if not force_rebuild:
                print(f"Base image {image_name} already exists, skipping.")
                continue
            client.images.remove(image_name, force=True)
        except docker.errors.ImageNotFound:
            pass

        print(f"Building base image: {image_name}")
        if verbose_logs:
            build_dir = _BUILD_LOG_DIR / "base" / image_name.replace(":", "__")
            build_dir.mkdir(parents=True, exist_ok=True)
            try:
                log = _build_image(client, image_name, {}, dockerfile, plat, build_dir)
                (build_dir / "build.log").write_text(log)
            except docker.errors.BuildError as e:
                (build_dir / "build.log").write_text(
                    f"FAILED: {e}\n\n--- Build Output ---\n{getattr(e, 'build_log', '')}"
                )
                raise
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    _build_image(client, image_name, {}, dockerfile, plat, Path(tmpdir))
                except docker.errors.BuildError as e:
                    _save_failure_log(
                        "base", image_name,
                        f"Error: {e}\n\n--- Build Output ---\n{getattr(e, 'build_log', '')}"
                    )
                    raise
                except Exception as e:
                    _save_failure_log("base", image_name, str(e))
                    raise

    print("Base images built successfully.")


def build_env_images(
    client: docker.DockerClient,
    specs: list[InstanceSpec],
    force_rebuild: bool = False,
    max_workers: int = 4,
    verbose_logs: bool = False,
) -> set[str]:
    """Build env images. Returns set of failed env image keys."""
    build_base_images(client, specs, force_rebuild, verbose_logs)

    # Deduplicate env images
    env_configs: dict[str, tuple[str, str, str]] = {}  # key -> (script, dockerfile, platform)
    for spec in specs:
        key = spec.env_image_key
        if key in env_configs:
            continue
        try:
            client.images.get(key)
            if not force_rebuild:
                continue
            client.images.remove(key, force=True)
        except docker.errors.ImageNotFound:
            pass
        env_configs[key] = (spec.setup_env_script(), spec.env_dockerfile(), spec.platform_str)

    if not env_configs:
        print("No env images need to be built.")
        return set()

    print(f"Building {len(env_configs)} env images (workers={max_workers})")
    failed: set[str] = set()

    def _build_one_env(item: tuple[str, tuple[str, str, str]]) -> str | None:
        key, (script, dockerfile, plat) = item
        scripts = {"setup_env.sh": script}
        if verbose_logs:
            build_dir = _BUILD_LOG_DIR / "env" / key.replace(":", "__")
            build_dir.mkdir(parents=True, exist_ok=True)
            try:
                log = _build_image(client, key, scripts, dockerfile, plat, build_dir)
                (build_dir / "build.log").write_text(log)
                return None
            except docker.errors.BuildError as e:
                (build_dir / "build.log").write_text(
                    f"FAILED: {e}\n\n--- Build Output ---\n{getattr(e, 'build_log', '')}"
                )
                return f"{key}: {e}"
            except Exception as e:
                return f"{key}: {e}"
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    _build_image(client, key, scripts, dockerfile, plat, Path(tmpdir))
                    return None
                except docker.errors.BuildError as e:
                    _save_failure_log(
                        "env", key,
                        f"Error: {e}\n\n--- Build Output ---\n{getattr(e, 'build_log', '')}"
                    )
                    return f"{key}: {e}"
                except Exception as e:
                    _save_failure_log("env", key, str(e))
                    return f"{key}: {e}"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_build_one_env, item): item[0] for item in env_configs.items()}
        for future in as_completed(futures):
            error = future.result()
            if error:
                key = futures[future]
                failed.add(key)
                print(f"  FAILED env image: {error}")

    if not failed:
        print("All env images built successfully.")
    else:
        print(f"{len(failed)} env images failed.")

    return failed


def build_instance_images(
    client: docker.DockerClient,
    specs: list[InstanceSpec],
    force_rebuild: bool = False,
    max_workers: int = 4,
    verbose_logs: bool = False,
) -> tuple[list[InstanceSpec], list[InstanceSpec]]:
    """Build instance images. Returns (successful, failed) lists."""
    # Build env images first
    env_failed = build_env_images(client, specs, force_rebuild, max_workers, verbose_logs)
    if env_failed:
        skipped = [s for s in specs if s.env_image_key in env_failed]
        specs = [s for s in specs if s.env_image_key not in env_failed]
        print(f"Skipping {len(skipped)} instances due to failed env builds")

    print(f"Building {len(specs)} instance images (workers={max_workers})")
    successful: list[InstanceSpec] = []
    failed: list[InstanceSpec] = []

    def _build_one_instance(spec: InstanceSpec) -> tuple[InstanceSpec, str | None]:
        key = spec.instance_image_key
        # Skip if exists
        if not force_rebuild:
            try:
                client.images.get(key)
                return spec, None
            except docker.errors.ImageNotFound:
                pass

        scripts = {"setup_repo.sh": spec.install_repo_script()}
        dockerfile = spec.instance_dockerfile()
        plat = spec.platform_str

        if verbose_logs:
            build_dir = _BUILD_LOG_DIR / "instances" / key.replace(":", "__").replace("/", "_")
            build_dir.mkdir(parents=True, exist_ok=True)
            try:
                log = _build_image(client, key, scripts, dockerfile, plat, build_dir)
                (build_dir / "build.log").write_text(log)
                return spec, None
            except docker.errors.BuildError as e:
                (build_dir / "build.log").write_text(
                    f"FAILED: {e}\n\n--- Build Output ---\n{getattr(e, 'build_log', '')}"
                )
                return spec, str(e)
            except Exception as e:
                return spec, str(e)
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    _build_image(client, key, scripts, dockerfile, plat, Path(tmpdir))
                    return spec, None
                except docker.errors.BuildError as e:
                    _save_failure_log(
                        "instance", spec.instance_id,
                        f"Error: {e}\n\n--- Build Output ---\n{getattr(e, 'build_log', '')}"
                    )
                    return spec, str(e)
                except Exception as e:
                    _save_failure_log("instance", spec.instance_id, str(e))
                    return spec, str(e)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_build_one_instance, s): s for s in specs}
        for future in as_completed(futures):
            spec, error = future.result()
            if error:
                failed.append(spec)
                print(f"  FAILED instance: {spec.instance_id}: {error}")
            else:
                successful.append(spec)

    if not failed:
        print("All instance images built successfully.")
    else:
        print(f"{len(failed)} instance images failed to build.")

    return successful, failed


# ---------------------------------------------------------------------------
# High-level entry: build from HuggingFace dataset
# ---------------------------------------------------------------------------


def build_from_dataset(
    dataset_name: str = "R2E-Gym/SWE-Bench-Verified",
    split: str = "test",
    registry: str = DEFAULT_REGISTRY,
    max_workers: int = 4,
    force_rebuild: bool = False,
    limit: int | None = None,
    instance_ids: list[str] | None = None,
    verbose_logs: bool = False,
) -> tuple[list[InstanceSpec], list[InstanceSpec]]:
    """Build images from a HuggingFace dataset.

    Returns (successful, failed) lists of InstanceSpec.
    """
    from datasets import load_dataset

    print(f"Loading dataset {dataset_name} split={split} ...")
    ds = load_dataset(dataset_name, split=split)

    specs: list[InstanceSpec] = []
    for entry in ds:
        iid = entry.get("instance_id", "")
        if instance_ids and iid not in instance_ids:
            continue
        repo = entry.get("repo", "")
        if repo not in MAP_REPO_VERSION_TO_SPECS:
            continue
        version = entry.get("version", "")
        if version not in MAP_REPO_VERSION_TO_SPECS.get(repo, {}):
            continue
        try:
            specs.append(InstanceSpec(entry, registry=registry))
        except Exception as e:
            print(f"  Skipping {iid}: {e}")
            continue
        if limit is not None and len(specs) >= limit:
            break

    print(f"Loaded {len(specs)} instances to build.")
    client = docker.from_env()
    return build_instance_images(client, specs, force_rebuild, max_workers, verbose_logs)
