from __future__ import annotations

import asyncio
import json

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
    from app.core.config import settings
    from app.detection import DetectionPipeline
    from app.ingestion.generator import SyntheticIngester, build_scenario

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
            from app.agent.orchestrator import OrchestratorAgent
            from app.core.db import close_pool, get_pool, init_db

            await init_db()
            pool = await get_pool()

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
    """Show latest risk scores for all monitored services."""
    from app.core.db import close_pool, get_pool, init_db

    async def _run() -> None:
        await init_db()
        pool = await get_pool()

        async with pool.acquire() as db:
            rows = await db.fetch(
                """
                SELECT DISTINCT ON (service)
                    service, score, top_signal, time_to_incident_min, ts
                FROM risk_scores
                ORDER BY service, ts DESC
                """
            )

        await close_pool()

        if not rows:
            click.echo("No risk scores found. Run `detect <scenario>` to generate data.")
            return

        click.echo(f"\n{'Service':<25} {'Score':>6}  {'Level':<8}  {'ETA':>8}  Top Signal")
        click.echo("─" * 95)

        for r in sorted(rows, key=lambda x: x["score"], reverse=True):
            score = float(r["score"])
            if score > 70:
                fg = "red"
                level = "HIGH"
            elif score > 50:
                fg = "yellow"
                level = "MED"
            else:
                fg = "green"
                level = "OK"

            score_str = click.style(f"{score:6.1f}", fg=fg, bold=True)
            ttm = r["time_to_incident_min"]
            ttm_str = f"{ttm:.0f}m" if ttm is not None else "  —"
            top = (r["top_signal"] or "—")[:45]
            click.echo(
                f"  {r['service']:<23} {score_str}  {level:<8}  {ttm_str:>8}  {top}"
            )

    asyncio.run(_run())


@cli.command()
def incidents() -> None:
    """Show last 10 incidents with RCA summaries."""
    from app.core.db import close_pool, get_pool, init_db

    async def _run() -> None:
        await init_db()
        pool = await get_pool()

        async with pool.acquire() as db:
            rows = await db.fetch(
                """
                SELECT id, service, severity, summary, created_at
                FROM incidents
                ORDER BY created_at DESC
                LIMIT 10
                """
            )

        await close_pool()

        if not rows:
            click.echo("No incidents found.")
            return

        _SEVERITY_FG = {"CRITICAL": "red", "HIGH": "red", "MEDIUM": "yellow", "LOW": "green"}

        click.echo(
            f"\n{'ID':>5}  {'Service':<20}  {'Severity':<10}  {'Created At':<22}  Summary"
        )
        click.echo("─" * 100)
        for r in rows:
            sev_str = click.style(
                f"{r['severity']:<10}", fg=_SEVERITY_FG.get(r["severity"], "white"), bold=True
            )
            summary_short = (r["summary"][:50] + "…") if len(r["summary"]) > 50 else r["summary"]
            click.echo(
                f"  {r['id']:>4}  {r['service']:<20}  {sev_str}  "
                f"{str(r['created_at'])[:22]:<22}  {summary_short}"
            )

    asyncio.run(_run())


@cli.command()
@click.argument("service")
def explain(service: str) -> None:
    """Ask the agent to explain current risk elevation for a service."""
    from app.core.db import close_pool, get_pool, init_db

    async def _run() -> None:
        await init_db()
        pool = await get_pool()

        async with pool.acquire() as db:
            row = await db.fetchrow(
                """
                SELECT score, trend_score, pattern_score, llm_score,
                       top_signal, time_to_incident_min
                FROM risk_scores
                WHERE service = $1
                ORDER BY ts DESC
                LIMIT 1
                """,
                service,
            )

        if row is None:
            click.echo(f"No risk score for '{service}'. Run `detect <scenario>` first.")
            await close_pool()
            return

        from app.agent.orchestrator import OrchestratorAgent
        from app.detection.scorer import RiskScore

        rs = RiskScore(
            service=service,
            score=float(row["score"]),
            anomaly_score=float(row["llm_score"] or 0.0),
            trend_score=float(row["trend_score"] or 0.0),
            pattern_score=float(row["pattern_score"] or 0.0),
            time_to_incident_min=row["time_to_incident_min"],
            top_signals=[row["top_signal"]] if row["top_signal"] else [],
        )

        click.echo(f"\nInvestigating '{service}' (score: {rs.score:.1f}/100) …")
        agent = OrchestratorAgent(db_pool=pool, langfuse=None)
        result = await agent.investigate(rs)

        click.echo(f"\nSeverity  : {result.get('severity', '—')}")
        click.echo(f"Summary   : {result.get('summary', '—')}")
        click.echo(f"\nRoot cause:\n  {result.get('root_cause', '—')}")

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

        if result.get("degraded_mode"):
            click.echo("\n[DEGRADED MODE]")

        await close_pool()

    asyncio.run(_run())


@cli.command()
def briefing() -> None:
    """Generate on-call briefing and send to Slack if webhook is configured."""
    from app.alerts.slack import SlackAlerter
    from app.core.config import settings
    from app.core.db import close_pool, get_pool, init_db

    async def _run() -> None:
        await init_db()
        pool = await get_pool()

        async with pool.acquire() as db:
            rows = await db.fetch(
                """
                SELECT DISTINCT ON (service)
                    service, score, top_signal, time_to_incident_min
                FROM risk_scores
                ORDER BY service, ts DESC
                """
            )

        await close_pool()

        top_services = sorted(
            [
                {
                    "service": r["service"],
                    "score": float(r["score"]),
                    "top_signal": r["top_signal"] or "—",
                    "time_to_incident_min": r["time_to_incident_min"],
                }
                for r in rows
            ],
            key=lambda x: x["score"],
            reverse=True,
        )[:3]

        click.echo("\n📋 On-Call Briefing — Top Services at Risk")
        click.echo("─" * 55)

        if not top_services:
            click.echo("No risk scores found.")
            return

        for svc in top_services:
            score = svc["score"]
            fg = "red" if score > 70 else "yellow" if score > 50 else "green"
            score_str = click.style(f"{score:.1f}", fg=fg, bold=True)
            ttm = svc["time_to_incident_min"]
            ttm_str = f"{ttm:.0f} min" if ttm else "—"
            click.echo(f"  {svc['service']:<25} Score: {score_str}/100  ETA: {ttm_str}")
            click.echo(f"    {svc['top_signal'][:70]}")

        alerter = SlackAlerter(settings.slack_webhook_url)
        await alerter.send_on_call_briefing(top_services)

        if settings.slack_webhook_url:
            click.echo("\nBriefing sent to Slack ✓")
        else:
            click.echo("\n(Slack not configured — set SLACK_WEBHOOK_URL to enable)")

    asyncio.run(_run())


@cli.command(name="dlq-status")
def dlq_status() -> None:
    """Show DLQ length and oldest item."""
    from app.core.config import settings
    from app.core.redis_client import RedisClient

    async def _run() -> None:
        client = RedisClient(settings.redis_url)
        await client.connect()

        if not client.available:
            click.echo("Redis unavailable — cannot check DLQ.")
            return

        length = await client.dlq_length()
        click.echo(f"DLQ length: {length}")

        if length > 0:
            try:
                import redis.asyncio as aioredis

                r = aioredis.from_url(settings.redis_url, decode_responses=True)
                raw = await r.lindex("dlq:incidents", 0)
                await r.aclose()
                if raw:
                    item = json.loads(raw)
                    click.echo(
                        f"Oldest item: service={item.get('service', '?')}"
                        f"  retry_count={item.get('retry_count', 0)}"
                        f"  error={item.get('last_error', '—')[:60]}"
                    )
            except Exception as exc:
                click.echo(f"Could not peek at DLQ item: {exc}")

        await client.close()

    asyncio.run(_run())


@cli.command(name="ingest-runbooks")
def ingest_runbooks() -> None:
    """Ingest runbook markdown files into pgvector knowledge base."""
    from pathlib import Path

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
