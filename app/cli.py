import click


@click.group()
def cli() -> None:
    """Ops Intelligence Agent CLI."""


@cli.command()
@click.argument("scenario")
def simulate(scenario: str) -> None:
    """Inject a named failure scenario into the signal stream."""
    from app.ingestion.generator import build_scenario

    try:
        fixture = build_scenario(scenario)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="scenario") from e

    click.echo(f"Injecting scenario: {fixture.name} on service: {fixture.service}")
    click.echo(f"Signal shape: {len(fixture.signal_shape)} metric snapshots")
    click.echo(f"Log events: {len(fixture.log_events)} events")
    click.echo(f"Root cause: {fixture.ground_truth.root_cause}")


@cli.command()
@click.argument("scenario", default="kafka_lag_cascade")
def detect(scenario: str) -> None:
    """Run detection pipeline against a named failure scenario and print risk scores."""
    import asyncio
    from app.ingestion.generator import SyntheticIngester, build_scenario
    from app.detection import DetectionPipeline

    async def _run() -> None:
        try:
            fixture = build_scenario(scenario)
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint="scenario") from e

        total = len(fixture.signal_shape)
        click.echo(f"\nScenario : {fixture.name}")
        click.echo(f"Service  : {fixture.service}")
        click.echo(f"Steps    : {total} metric snapshots (30 s each ≈ {total // 2} min)")
        click.echo(f"\n{'Step':>6} {'Score':>6} {'Anomaly':>8} {'Trend':>7} {'Pattern':>8}  Signal")
        click.echo("─" * 95)

        signals = [s.to_signal() for s in fixture.signal_shape]
        pipeline = DetectionPipeline(SyntheticIngester(signals=signals))

        step = 0
        async for rs in pipeline.run():
            step += 1
            top = rs.top_signals[0][:55] if rs.top_signals else "—"
            click.echo(
                f"{step:>4}/{total:<3} {rs.score:>6.1f} {rs.anomaly_score:>8.1f}"
                f" {rs.trend_score:>7.1f} {rs.pattern_score:>8.1f}  {top}"
            )

        click.echo("─" * 95)
        click.echo(f"\nFinal risk score: {rs.score:.1f}/100")  # type: ignore[possibly-undefined]
        if rs.top_signals:
            click.echo("Top signals:")
            for s in rs.top_signals:
                click.echo(f"  • {s}")
        click.echo(f"\nGround truth root cause:\n  {fixture.ground_truth.root_cause}")
        click.echo(f"Pre-emptive action:\n  {fixture.ground_truth.pre_emptive_action}")

    asyncio.run(_run())


@cli.command()
def status() -> None:
    """Show live risk scores for all monitored services."""
    click.echo("Risk scores: (live streaming not yet wired — use `detect <scenario>` for now)")


@cli.command()
@click.argument("service")
def explain(service: str) -> None:
    """Ask the agent to explain current risk elevation for a service."""
    click.echo(f"Agent explanation for {service}: (agent layer not yet implemented — Day 3)")


@cli.command()
def incidents() -> None:
    """Show last 10 incidents with RCA summaries."""
    click.echo("Incidents: (agent layer not yet implemented — Day 3)")


@cli.command(name="ingest-runbooks")
def ingest_runbooks() -> None:
    """Ingest runbook markdown files into pgvector knowledge base."""
    click.echo("Runbook ingestion: (RAG layer not yet implemented — Day 4)")


if __name__ == "__main__":
    cli()
