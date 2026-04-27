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
@click.option("--no-agent", is_flag=True, help="Skip agent RCA, detection only.")
def detect(scenario: str, no_agent: bool) -> None:
    """Run detection pipeline + agent RCA against a named failure scenario."""
    import asyncio
    import json
    from app.core.config import settings
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
        rs = None
        async for rs in pipeline.run():
            step += 1
            top = rs.top_signals[0][:55] if rs.top_signals else "—"
            click.echo(
                f"{step:>4}/{total:<3} {rs.score:>6.1f} {rs.anomaly_score:>8.1f}"
                f" {rs.trend_score:>7.1f} {rs.pattern_score:>8.1f}  {top}"
            )

        click.echo("─" * 95)
        if rs is None:
            click.echo("No signals processed.")
            return

        click.echo(f"\nFinal risk score: {rs.score:.1f}/100")
        if rs.top_signals:
            click.echo("Top signals:")
            for s in rs.top_signals:
                click.echo(f"  • {s}")
        click.echo(f"\nGround truth root cause:\n  {fixture.ground_truth.root_cause}")
        click.echo(f"Pre-emptive action:\n  {fixture.ground_truth.pre_emptive_action}")

        if no_agent or rs.score < 70:
            if rs.score < 70:
                click.echo(f"\nAgent not triggered (score {rs.score:.1f} < 70).")
            return

        # --- Agent RCA ---
        click.echo("\n" + "═" * 95)
        click.echo("AGENT RCA — connecting to database and starting investigation …")
        click.echo("═" * 95)

        try:
            from app.core.db import get_pool, init_db, close_pool
            from app.agent.orchestrator import OrchestratorAgent

            await init_db()
            pool = await get_pool()

            # Persist all metric signals so tools have data to query
            async with pool.acquire() as db:
                for sig in signals:
                    if sig.signal_type == "metric" and sig.metrics:
                        await db.execute(
                            """
                            INSERT INTO signals
                                (ts, service, signal_type, metrics, scenario)
                            VALUES ($1, $2, 'metric', $3::jsonb, $4)
                            """,
                            sig.ts,
                            sig.service,
                            json.dumps(sig.metrics),
                            sig.scenario,
                        )
                    elif sig.signal_type == "log" and sig.message:
                        await db.execute(
                            """
                            INSERT INTO signals
                                (ts, service, signal_type, level, message,
                                 trace_id, error_code, scenario)
                            VALUES ($1, $2, 'log', $3, $4, $5, $6, $7)
                            """,
                            sig.ts,
                            sig.service,
                            sig.level,
                            sig.message,
                            sig.trace_id,
                            sig.error_code,
                            sig.scenario,
                        )

            agent = OrchestratorAgent(db_pool=pool, langfuse=None)
            click.echo("Calling Claude claude-sonnet-4-5 …\n")
            result = await agent.investigate(rs)

            click.echo(f"Severity   : {result.get('severity', '—')}")
            click.echo(f"Summary    : {result.get('summary', '—')}")
            click.echo(f"\nRoot cause : {result.get('root_cause', '—')}")
            hyps = result.get("hypotheses", [])
            if hyps:
                click.echo("\nHypotheses:")
                for i, h in enumerate(hyps, 1):
                    click.echo(f"  {i}. {h}")
            steps = result.get("fix_steps", [])
            if steps:
                click.echo("\nFix steps:")
                for i, s in enumerate(steps, 1):
                    click.echo(f"  {i}. {s}")
            inc_id = result.get("incident_id")
            if inc_id:
                click.echo(f"\nIncident #{inc_id} persisted to database.")
            if result.get("degraded_mode"):
                click.echo("\n[DEGRADED MODE — LLM circuit breaker was open]")
            lf = result.get("langfuse_url")
            if lf:
                click.echo(f"\nLangfuse trace: {lf}")

            await close_pool()

        except Exception as exc:
            click.echo(f"\nAgent layer error: {exc}")
            click.echo("Detection output above is still valid.")

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
    import asyncio
    import json
    from pathlib import Path
    from app.core.config import settings
    from app.core.db import get_pool, init_db
    from app.agent.runbook_ingester import RunbookIngester

    async def _run() -> None:
        from app.agent.embedder import LocalEmbedder
        await init_db()
        pool = await get_pool()
        embedder = LocalEmbedder()
        runbooks_dir = Path(__file__).resolve().parent.parent / "runbooks"
        async with pool.acquire() as db:
            ingester = RunbookIngester(db=db, embedder=embedder)
            results = await ingester.ingest_all(runbooks_dir)
        total = sum(results.values())
        click.echo(f"Ingested {len(results)} runbooks, {total} new chunks inserted.")
        for name, count in results.items():
            click.echo(f"  {name}: {count} chunks")

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
