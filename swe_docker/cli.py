"""CLI entry point for swe_docker — build SWE-bench Verified Docker images."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from swe_docker.constants import DEFAULT_REGISTRY

app = typer.Typer(help="Build and validate SWE-bench Verified Docker images.")
console = Console()


@app.command("build")
def build(
    dataset: str = typer.Option("R2E-Gym/SWE-Bench-Verified", help="HuggingFace dataset name."),
    split: str = typer.Option("test", help="Dataset split."),
    registry: str = typer.Option(DEFAULT_REGISTRY, help="Docker registry prefix."),
    max_workers: int = typer.Option(4, help="Parallel workers for builds."),
    force_rebuild: bool = typer.Option(False, help="Force rebuild even if images exist."),
    limit: int | None = typer.Option(None, help="Max number of instances to build."),
    instance_ids: str | None = typer.Option(None, help="Comma-separated instance IDs to build."),
    validate: bool = typer.Option(False, help="Run two-step validation after building."),
    validation_timeout: int = typer.Option(600, help="Timeout per validation step (seconds)."),
    verbose_logs: bool = typer.Option(False, help="Save build logs/scripts/Dockerfile for ALL builds (not just failures)."),
) -> None:
    """Build Docker images from a HuggingFace dataset.

    Images are built in 3 tiers for maximum layer reuse:
    base -> env -> instance.
    """
    from swe_docker.builder import build_from_dataset, InstanceSpec, FAILED_LOG_DIR

    ids = None
    if instance_ids:
        ids = [x.strip() for x in instance_ids.split(",")]

    successful, failed = build_from_dataset(
        dataset_name=dataset,
        split=split,
        registry=registry,
        max_workers=max_workers,
        force_rebuild=force_rebuild,
        limit=limit,
        instance_ids=ids,
        verbose_logs=verbose_logs,
    )

    # Summary
    total = len(successful) + len(failed)
    table = Table(title="Build Summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Total", str(total))
    table.add_row("Build Success", str(len(successful)))
    table.add_row("Build Failed", str(len(failed)))
    console.print(table)

    if failed:
        console.print("[yellow]Failed instances:[/yellow]")
        for spec in failed[:20]:
            console.print(f"  - {spec.instance_id}")

    # Validation
    if validate and successful:
        console.print(f"\n=== Validating {len(successful)} images ===")
        import docker
        from swe_docker.validator import validate_image, delete_image

        client = docker.from_env()
        val_pass = 0
        val_fail: list[tuple[str, str]] = []

        from rich.progress import Progress, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Validating", total=len(successful))
            for spec in successful:
                try:
                    result = validate_image(spec, client, timeout=validation_timeout)
                    if result.passed:
                        val_pass += 1
                    else:
                        val_fail.append((spec.instance_id, result.reason))
                        delete_image(client, spec.instance_image_key)
                        # Save validation failure log
                        safe_id = spec.instance_id.replace("/", "_")
                        log_file = FAILED_LOG_DIR / f"validation_{safe_id}.log"
                        log_file.parent.mkdir(parents=True, exist_ok=True)
                        log_file.write_text(result.detailed_log())
                except Exception as e:
                    val_fail.append((spec.instance_id, str(e)))
                progress.advance(task)

        val_table = Table(title="Validation Summary")
        val_table.add_column("Metric")
        val_table.add_column("Value", justify="right")
        val_table.add_row("Validated", str(len(successful)))
        val_table.add_row("Passed", str(val_pass))
        val_table.add_row("Failed", str(len(val_fail)))
        console.print(val_table)

        if val_fail:
            console.print("[yellow]Validation failures:[/yellow]")
            for iid, reason in val_fail[:20]:
                console.print(f"  - {iid}: {reason}")

    console.print("\n[bold]Done.[/bold]")


@app.command("validate")
def validate_cmd(
    image: str = typer.Argument(help="Docker image name to validate."),
    instance_id: str = typer.Option(..., help="Instance ID for looking up test expectations."),
    dataset: str = typer.Option("R2E-Gym/SWE-Bench-Verified", help="HuggingFace dataset."),
    split: str = typer.Option("test", help="Dataset split."),
    registry: str = typer.Option(DEFAULT_REGISTRY, help="Docker registry prefix."),
    timeout: int = typer.Option(600, help="Timeout per validation step (seconds)."),
) -> None:
    """Validate a single already-built image with two-step F2P/P2P checks."""
    from datasets import load_dataset
    from swe_docker.builder import InstanceSpec
    from swe_docker.validator import validate_image
    from swe_docker.constants import MAP_REPO_VERSION_TO_SPECS

    ds = load_dataset(dataset, split=split)
    found = None
    for entry in ds:
        if entry.get("instance_id") == instance_id:
            found = entry
            break

    if found is None:
        console.print(f"[red]Instance {instance_id} not found in dataset.[/red]")
        raise typer.Exit(code=2)

    spec = InstanceSpec(found, registry=registry)

    import docker
    client = docker.from_env()
    result = validate_image(spec, client, timeout=timeout)
    console.print(result.detailed_log())
    if not result.passed:
        raise typer.Exit(code=1)
    console.print("[green]Validation PASSED[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
