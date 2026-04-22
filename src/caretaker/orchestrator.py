"""Orchestrator — wires all agents together and runs the main loop."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker import __version__
from caretaker.agent_protocol import AgentContext
from caretaker.agents import EVENT_AGENT_MAP, build_registry
from caretaker.config import MaintainerConfig
from caretaker.evolution.backends.factory import build_evolution_store
from caretaker.evolution.crystallizer import SkillCrystallizer
from caretaker.evolution.mutator import StrategyMutator
from caretaker.evolution.planner import PlanMode
from caretaker.evolution.reflection import ReflectionEngine
from caretaker.foundry.dispatcher import ExecutorDispatcher
from caretaker.foundry.executor import FoundryExecutor
from caretaker.github_app import AppJWTSigner, GitHubAppCredentialsProvider, InstallationTokenMinter
from caretaker.github_client.api import GitHubClient
from caretaker.github_client.credentials import ChainCredentialsProvider, EnvCredentialsProvider
from caretaker.goals.definitions import build_goals
from caretaker.goals.engine import GoalContext, GoalEngine
from caretaker.goals.models import GoalStatus
from caretaker.llm.copilot import CopilotProtocol
from caretaker.llm.provider import LiteLLMProvider
from caretaker.llm.router import LLMRouter
from caretaker.mcp import MCPClient, TelemetryClient
from caretaker.observability import record_orchestrator_soft_fail
from caretaker.state.audit_log import AuditLogWriter
from caretaker.state.backends.factory import build_memory_backend
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
)
from caretaker.state.tracker import StateTracker

if TYPE_CHECKING:
    from caretaker.evolution.insight_store import InsightStore
    from caretaker.goals.models import GoalEvaluation
    from caretaker.registry import AgentRegistry

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """Return a UTC-aware datetime, attaching UTC if the datetime is naive.

    All new producers in ``src/caretaker/`` now emit tz-aware UTC datetimes
    (see the datetime.utcnow() sweep PR). This helper is retained purely to
    normalize datetimes round-tripped from older serialized state that may
    still contain naive ISO strings without a ``+00:00`` offset.  Once the
    persisted state corpus has rolled over, the remaining call sites can be
    dropped.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ── Transient-error classification ────────────────────────────────────
#
# The post-run exit gate buckets every entry in ``RunSummary.errors`` as
# either transient or non-transient. If the whole run yielded only
# transient errors and some real work landed, we emit a Prometheus
# soft-fail counter and exit 0 — those categories represent "the run
# did its job; one integration was briefly unhappy" and should not flip
# the workflow to failure (which in turn triggers self-heal and the
# "Unknown caretaker failure: Process completed with exit code 1." issue
# storm observed on Example-React / flashcards).
_TRANSIENT_SUBSTRINGS: tuple[str, ...] = (
    # GitHub sub-systems that return 403 on workflow-token scopes we can't
    # widen from a consumer repo (dependabot alerts, code-scanning,
    # secret-scanning, assignee permission checks).
    "dependabot alerts unavailable",
    "code scanning alerts unavailable",
    "code-scanning",
    "secret scanning",
    "secret-scanning",
    "resource not accessible by integration",
    "assignees could not be set",
    # Network / upstream flapping.
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporary failure",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    # Empty memory snapshot produced by upload-artifact when the file is
    # not yet written (first-run path on a fresh runner).
    "memory snapshot",
    "empty artifact",
    "no files were found",
    # Rate-limit backoffs — already handled by the cooldown gate but the
    # string may surface in a bucketed agent error before the gate kicks.
    "rate limit",
    "secondary rate limit",
    "429",
)

_TRANSIENT_STATUS_CODES: tuple[str, ...] = ("500", "502", "503", "504")


def is_transient(error: BaseException | str) -> bool:
    """Return True if ``error`` looks like a transient / recoverable failure.

    The gate is intentionally conservative — we only bucket as transient
    when the text matches one of a small, fixed list of known-flappy
    conditions. Anything else (AttributeError, TypeError, unexpected
    GitHubAPIError, classification misses) stays non-transient so the
    run still fails loudly.
    """
    if isinstance(error, BaseException):
        # Timeouts and connection errors from stdlib / httpx are
        # transient regardless of message content.
        import asyncio
        import socket

        try:
            import httpx  # noqa: PLC0415

            transient_exc_types: tuple[type[BaseException], ...] = (
                asyncio.TimeoutError,
                TimeoutError,
                ConnectionError,
                socket.timeout,
                socket.gaierror,
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
            )
        except Exception:  # pragma: no cover - httpx optional at analysis time
            transient_exc_types = (
                asyncio.TimeoutError,
                TimeoutError,
                ConnectionError,
                socket.timeout,
                socket.gaierror,
            )

        if isinstance(error, transient_exc_types):
            return True
        text = f"{type(error).__name__}: {error}"
    else:
        text = error

    lower = text.lower()
    if any(sub in lower for sub in _TRANSIENT_SUBSTRINGS):
        return True
    # Upstream 5xx surfaces as ``GitHub API error 503: …``.
    return any(f"error {code}" in lower or f" {code} " in lower for code in _TRANSIENT_STATUS_CODES)


def _bucket_errors(errors: list[str]) -> tuple[list[str], list[str]]:
    """Split ``errors`` into ``(transient, non_transient)`` buckets."""
    transient: list[str] = []
    non_transient: list[str] = []
    for err in errors:
        (transient if is_transient(err) else non_transient).append(err)
    return transient, non_transient


def _extract_pr_number(event_type: str, payload: dict[str, Any]) -> int | None:
    """Extract a PR number from a GitHub event payload.

    Returns the PR number when the payload reliably identifies a single PR,
    or ``None`` when no PR can be determined (so the agent falls back to a
    full scan).
    """
    try:
        if event_type in ("pull_request", "pull_request_review"):
            return int(payload["pull_request"]["number"])
        if event_type == "check_run":
            prs = payload.get("check_run", {}).get("pull_requests", [])
            if prs:
                return int(prs[0]["number"])
        if event_type == "check_suite":
            prs = payload.get("check_suite", {}).get("pull_requests", [])
            if prs:
                return int(prs[0]["number"])
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _build_credentials_provider(
    config: MaintainerConfig,
) -> EnvCredentialsProvider | ChainCredentialsProvider | GitHubAppCredentialsProvider:
    """Build the appropriate credentials provider based on config and environment.

    When ``config.github_app.enabled`` is ``True`` and the required env vars
    are present, returns a :class:`GitHubAppCredentialsProvider` that mints
    short-lived installation tokens on demand.  A :class:`ChainCredentialsProvider`
    wraps the App provider with an :class:`EnvCredentialsProvider` fallback so
    existing ``GITHUB_TOKEN`` / ``COPILOT_PAT`` workflows continue to work
    during roll-out.

    ``COPILOT_PAT`` is forwarded to the App provider as a ``user_token_supplier``
    so that Copilot assignment (which still requires a user identity) keeps
    working when the PAT is configured.
    """
    app_cfg = config.github_app
    if not app_cfg.enabled:
        return EnvCredentialsProvider()

    # Resolve App ID: prefer env var override, fall back to config value.
    app_id_str = os.environ.get("CARETAKER_GITHUB_APP_ID", "")
    app_id: int | None = int(app_id_str) if app_id_str.isdigit() else app_cfg.app_id
    private_key = os.environ.get(app_cfg.private_key_env, "")
    installation_id_str = os.environ.get("CARETAKER_GITHUB_APP_INSTALLATION_ID", "")

    if not app_id or not private_key or not installation_id_str.isdigit():
        logger.warning(
            "github_app.enabled=true but required env vars are missing "
            "(CARETAKER_GITHUB_APP_ID / %s / CARETAKER_GITHUB_APP_INSTALLATION_ID). "
            "Falling back to GITHUB_TOKEN / COPILOT_PAT.",
            app_cfg.private_key_env,
        )
        return EnvCredentialsProvider()

    installation_id = int(installation_id_str)
    signer = AppJWTSigner(app_id=app_id, private_key_pem=private_key)
    minter = InstallationTokenMinter(
        signer=signer,
        refresh_skew_seconds=app_cfg.installation_token_refresh_skew_seconds,
    )

    # Use COPILOT_PAT as a user-identity token for Copilot assignment when set.
    async def _copilot_pat_supplier(_installation_id: int) -> str:
        return os.environ.get("COPILOT_PAT", "")

    app_provider = GitHubAppCredentialsProvider(
        minter=minter,
        default_installation_id=installation_id,
        user_token_supplier=_copilot_pat_supplier,
    )
    logger.info(
        "Using GitHub App credentials (app_id=%s, installation_id=%s)",
        app_id,
        installation_id,
    )
    # ChainCredentialsProvider falls back to env PAT if App minting fails.
    # EnvCredentialsProvider construction is guarded — it's optional when App is the sole source.
    try:
        env_fallback = EnvCredentialsProvider()
        return ChainCredentialsProvider([app_provider, env_fallback])
    except ValueError:
        # Neither GITHUB_TOKEN nor COPILOT_PAT is set; use App-only mode.
        return app_provider


class Orchestrator:
    """Central orchestrator that coordinates all agents."""

    def __init__(
        self,
        config: MaintainerConfig,
        github: GitHubClient,
        owner: str,
        repo: str,
    ) -> None:
        self._config = config
        self._github = github
        self._owner = owner
        self._repo = repo
        self._llm = LLMRouter(config.llm)
        self._state_tracker = StateTracker(github, owner, repo)

        # Memory backend — configured persistence backend (MongoDB when enabled)
        self._memory = build_memory_backend(config)
        if self._memory is not None:
            backend_type = getattr(config.memory_store, "backend", "sqlite")
            logger.info("MemoryBackend enabled: backend=%s", backend_type)

        # Audit log (writes to MongoDB audit_log collection + structured log)
        self._audit_log = AuditLogWriter.from_config(config)

        # Optional Telemetry & MCP clients
        self._telemetry: TelemetryClient | None = None
        self._mcp_client: MCPClient | None = None
        if config.telemetry.enabled:
            self._telemetry = TelemetryClient(config.telemetry)
        if config.mcp.enabled:
            self._mcp_client = MCPClient(config.mcp)

        # Per-orchestrator cache for the fleet heartbeat's OAuth2 client.
        # Owned here so multi-tenant hosts (admin backend, tests) don't
        # share a single cross-config client via a module global.
        from caretaker.fleet import FleetOAuthClientCache

        self._fleet_oauth_cache = FleetOAuthClientCache()

        # Evolution layer (InsightStore, ReflectionEngine, StrategyMutator, PlanMode)
        self._insight_store: InsightStore | None = None
        self._skill_crystallizer: SkillCrystallizer | None = None
        self._reflection_engine: ReflectionEngine | None = None
        self._strategy_mutator: StrategyMutator | None = None
        self._plan_mode: PlanMode | None = None
        if config.evolution.enabled:
            store = build_evolution_store(config)
            assert store is not None, "Evolution store cannot be None when evolution is enabled"
            self._insight_store = store
            self._skill_crystallizer = SkillCrystallizer(store)
            self._reflection_engine = ReflectionEngine()
            self._strategy_mutator = StrategyMutator(store)
            if config.evolution.plan_mode_enabled:
                self._plan_mode = PlanMode(
                    github=github,
                    owner=owner,
                    repo=repo,
                    claude_client=self._llm.claude,
                )
            logger.info("Evolution layer enabled: backend=%s", config.evolution.backend)

        # Apply any pending strategy mutations to runtime config before building agents
        if self._strategy_mutator is not None:
            config = self._strategy_mutator.apply_pending(config, OrchestratorState())

        # ── Executor dispatcher (Copilot / Foundry routing) ────────────
        self._executor_dispatcher: ExecutorDispatcher | None = None
        try:
            self._executor_dispatcher = self._build_executor_dispatcher(config, github, owner, repo)
        except Exception as exc:  # never block agent boot on Foundry config issues
            logger.warning("Failed to build ExecutorDispatcher; falling back to Copilot: %s", exc)
            self._executor_dispatcher = None

        ctx = AgentContext(
            github=github,
            owner=owner,
            repo=repo,
            config=config,
            llm_router=self._llm,
            dry_run=config.orchestrator.dry_run,
            memory=self._memory,
            mcp_client=self._mcp_client,
            telemetry=self._telemetry,
            executor_dispatcher=self._executor_dispatcher,
        )
        self._registry: AgentRegistry = build_registry(ctx)

        # Goal-seeking engine
        self._goal_engine: GoalEngine | None = None
        if config.goal_engine.enabled:
            self._goal_engine = GoalEngine(build_goals(), config.goal_engine)
            issues = self._goal_engine.validate(self._registry)
            for issue in issues:
                logger.warning(issue)

    def _build_executor_dispatcher(
        self,
        config: MaintainerConfig,
        github: GitHubClient,
        owner: str,
        repo: str,
    ) -> ExecutorDispatcher | None:
        """Build the Foundry executor + dispatcher, or return None when disabled.

        The dispatcher is *always* returned when any non-default executor
        config is present so the Copilot fallback path stays active.  When
        the config is the factory default (provider=copilot) we return None,
        which preserves the byte-identical legacy code path for agents.
        """
        executor_cfg = config.executor
        # Zero-config: exit preserves the legacy path (no dispatcher).
        if (
            executor_cfg.provider == "copilot"
            and not executor_cfg.foundry.enabled
            and not executor_cfg.claude_code.enabled
        ):
            return None

        copilot_protocol = CopilotProtocol(github, owner, repo)

        foundry_executor: FoundryExecutor | None = None
        if executor_cfg.foundry.enabled:
            provider = LiteLLMProvider(
                fallback_models=list(executor_cfg.foundry.fallback_models),
                timeout=executor_cfg.foundry.request_timeout_seconds,
            )
            if not provider.available:
                logger.warning(
                    "executor.foundry.enabled=True but LiteLLM provider is unavailable "
                    "(missing credentials or package). Routing stays on Copilot."
                )
            else:
                # Foundry's ``git push`` needs a write-capable token. Route
                # through GitHubClient so the App-installation token path (or
                # the env ``GITHUB_TOKEN`` fallback) is always respected.
                async def _push_token() -> str:
                    return await github.get_default_token()

                foundry_executor = FoundryExecutor(
                    provider=provider,
                    github=github,
                    owner=owner,
                    repo=repo,
                    config=executor_cfg.foundry,
                    token_supplier=_push_token,
                )
                logger.info(
                    "FoundryExecutor ready: model=%s allowed_task_types=%s",
                    executor_cfg.foundry.model,
                    executor_cfg.foundry.allowed_task_types,
                )

        claude_code_executor = None
        if executor_cfg.claude_code.enabled:
            from caretaker.claude_code_executor import ClaudeCodeExecutor

            claude_code_executor = ClaudeCodeExecutor(
                github=github,
                owner=owner,
                repo=repo,
                config=executor_cfg.claude_code,
            )
            logger.info(
                "ClaudeCodeExecutor ready: trigger_label=%s mention=%s",
                executor_cfg.claude_code.trigger_label,
                executor_cfg.claude_code.mention,
            )

        return ExecutorDispatcher(
            config=executor_cfg,
            foundry_executor=foundry_executor,
            copilot_protocol=copilot_protocol,
            claude_code_executor=claude_code_executor,
        )

    @classmethod
    def from_config_path(cls, path: str) -> Orchestrator:
        """Create an orchestrator from a YAML config file path."""
        config = MaintainerConfig.from_yaml(path)

        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
        repo_name = os.environ.get("GITHUB_REPOSITORY_NAME", "")

        # Fall back to GITHUB_REPOSITORY (owner/repo format)
        if not owner or not repo_name:
            full = os.environ.get("GITHUB_REPOSITORY", "")
            if "/" in full:
                owner, repo_name = full.split("/", 1)
            else:
                raise RuntimeError(
                    "GITHUB_REPOSITORY or GITHUB_REPOSITORY_OWNER + "
                    "GITHUB_REPOSITORY_NAME environment variables are required"
                )

        github = GitHubClient(credentials_provider=_build_credentials_provider(config))
        return cls(config=config, github=github, owner=owner, repo=repo_name)

    async def run(
        self,
        mode: str = "full",
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        report_path: str | None = None,
    ) -> int:
        """Run the orchestrator. Returns 0 on success, 1 on errors."""
        logger.info(
            "Orchestrator starting — mode=%s, version=%s, repo=%s/%s",
            mode,
            __version__,
            self._owner,
            self._repo,
        )

        # Honour any in-process rate-limit cooldown from a previous run
        # (same runner, same python process is rare in GHA but possible
        # for local loops or the K8s agent-worker that reuses the pod).
        # For the common GHA case, the cooldown state starts fresh on
        # every workflow invocation, so this is a no-op most of the
        # time and a safety net the rest.
        from caretaker.github_client.rate_limit import get_cooldown

        _cooldown = get_cooldown()
        if _cooldown.is_blocked():
            remaining = _cooldown.seconds_remaining()
            cooldown_snap = _cooldown.snapshot()
            logger.warning(
                "Orchestrator deferring: GitHub rate-limit cooldown still active "
                "(%.0fs remaining, reason=%s). Exiting cleanly so the next "
                "scheduled run picks up after the window closes.",
                remaining,
                cooldown_snap.get("reason"),
            )
            return 0

        # Load persisted state
        state = OrchestratorState()
        summary = RunSummary(mode=mode, run_at=datetime.now(UTC))
        has_errors = False

        try:
            state = await self._state_tracker.load()

            # Pending strategy mutations were applied in __init__ (before agents
            # were constructed) so AgentContext.config already carries them. We
            # intentionally do NOT re-apply here: rebuilding the registry mid-run
            # would invalidate in-flight agent state.

            # Snapshot PR states before agents run (for crystallization comparison)
            _pre_agent_prs = {n: pr.model_copy() for n, pr in state.tracked_prs.items()}

            # Initialize optional remote dependencies
            if self._mcp_client and self._mcp_client.config.enabled:
                await self._mcp_client.connect()

            # ── Goal pre-evaluation ───────────────────────────
            pre_eval: GoalEvaluation | None = None
            if (
                self._goal_engine
                and mode != "event"
                and self._config.goal_engine.goal_driven_dispatch
            ):
                goal_ctx = GoalContext(
                    github=self._github,
                    owner=self._owner,
                    repo=self._repo,
                    config=self._config,
                )
                pre_eval = await self._goal_engine.evaluate_all(state, goal_ctx)
                logger.info(
                    "Goal pre-evaluation: health=%.2f, escalations=%d, plan=%s",
                    pre_eval.overall_health,
                    len(pre_eval.escalations),
                    pre_eval.dispatch_plan,
                )
                for esc in pre_eval.escalations:
                    logger.warning(
                        "Goal escalation: %s — %s (action: %s)",
                        esc.goal_id,
                        esc.reason,
                        esc.recommended_action,
                    )

            # ── Agent dispatch ────────────────────────────────
            # Event-driven mode — route to specific agent
            if mode == "event" and event_type:
                await self._handle_event(event_type, event_payload or {}, state, summary)
            elif (
                self._goal_engine
                and self._config.goal_engine.goal_driven_dispatch
                and pre_eval is not None
            ):
                dispatch_mode = "full" if mode == "dry-run" else mode
                await self._run_goal_driven(
                    pre_eval, state, summary, dispatch_mode, event_payload or {}
                )
            else:
                # Dry-run evaluates full mode with read-only behavior controlled by context.
                dispatch_mode = "full" if mode == "dry-run" else mode
                # Scheduled mode — run every agent registered for this mode
                await self._registry.run_all(
                    state,
                    summary,
                    mode=dispatch_mode,
                    event_payload=event_payload or {},
                )

            # Cross-agent state reconciliation
            self._reconcile_state(state, summary)

            # ── Skill crystallization (Phase 1) ───────────────
            if self._skill_crystallizer is not None:
                recorded = self._skill_crystallizer.crystallize_transitions(
                    _pre_agent_prs, state.tracked_prs
                )
                if recorded:
                    logger.info("Evolution: crystallized %d skill outcomes", recorded)

            # ── Goal post-evaluation ──────────────────────────
            post_eval = None
            if self._goal_engine:
                goal_ctx = GoalContext(
                    github=self._github,
                    owner=self._owner,
                    repo=self._repo,
                    config=self._config,
                    current_summary=summary,
                )
                post_eval = await self._goal_engine.evaluate_all(state, goal_ctx)
                self._goal_engine.record_evaluation(state, post_eval)
                summary.goal_health = post_eval.overall_health
                summary.goal_escalation_count = len(post_eval.escalations)
                logger.info(
                    "Goal post-evaluation: health=%.2f (escalations=%d)",
                    post_eval.overall_health,
                    len(post_eval.escalations),
                )

                # ── Evolution post-eval hooks ─────────────────
                if post_eval is not None and self._insight_store is not None:
                    # Plan Mode: activate for CRITICAL goals (Phase 6)
                    if self._plan_mode is not None:
                        for goal_id, snap in post_eval.snapshots.items():
                            if (
                                snap.status == GoalStatus.CRITICAL
                                and goal_id not in state.active_plan_ids
                            ):
                                goal_obj = self._goal_engine.goals.get(goal_id)
                                if goal_obj:
                                    with contextlib.suppress(Exception):
                                        await self._plan_mode.activate(
                                            goal=goal_obj,
                                            evaluation=post_eval,
                                            state=state,
                                            insight_store=self._insight_store,
                                        )
                        await self._plan_mode.monitor_plans(state, post_eval)

                    # Strategy mutation evaluation (Phase 4)
                    if self._strategy_mutator is not None:
                        outcomes = self._strategy_mutator.evaluate_pending(state, post_eval)
                        for outcome in outcomes:
                            logger.info(
                                "Mutation %s: %s.%s %s (Δ=%.3f)",
                                outcome.outcome,
                                outcome.agent_name,
                                outcome.parameter,
                                outcome.new_value,
                                outcome.score_delta,
                            )

                    # Reflection engine (Phase 3)
                    if (
                        self._reflection_engine is not None
                        and self._config.evolution.reflection_enabled
                        and self._reflection_engine.should_reflect(post_eval, state)
                    ):
                        with contextlib.suppress(Exception):
                            reflection = await self._reflection_engine.reflect(
                                evaluation=post_eval,
                                state=state,
                                run_history=state.run_history[-10:],
                                insight_store=self._insight_store,
                                claude_client=self._llm.claude,
                            )
                            await self._state_tracker.post_reflection(reflection)
                            # Propose a mutation from the reflection if mutations enabled
                            if (
                                self._strategy_mutator is not None
                                and self._config.evolution.mutation_enabled
                            ):
                                self._strategy_mutator.propose_mutation(
                                    reflection, state, self._config
                                )

        except Exception as e:
            logger.error("Orchestrator error: %s", e, exc_info=True)
            summary.errors.append(str(e))
            has_errors = True
        finally:
            # Clean up optional remote dependencies
            if self._mcp_client is not None and self._mcp_client.config.enabled:
                try:
                    await self._mcp_client.disconnect()
                except Exception as e:
                    logger.warning("Failed to disconnect MCP client: %s", e)

        # Persist state (save also appends summary to history)
        await self._state_tracker.save(summary)

        # Opt-in fleet-registry heartbeat. Fail-open: the emitter logs
        # and swallows any error so a missing / misconfigured endpoint
        # can never fail the orchestrator run.
        if self._config.fleet_registry.enabled:
            try:
                from caretaker.fleet import emit_heartbeat

                await emit_heartbeat(
                    self._config,
                    summary,
                    oauth_cache=self._fleet_oauth_cache,
                )
            except Exception as e:
                logger.warning("Fleet heartbeat failed: %s", e)

        # Save memory store snapshot (for artifact upload / rollback)
        if self._memory is not None:
            self._memory.prune_expired()
            snapshot_path = self._config.memory_store.snapshot_path
            if snapshot_path:
                try:
                    with open(snapshot_path, "w", encoding="utf-8") as fh:
                        fh.write(self._memory.snapshot_json())
                    logger.info("Memory store snapshot written to %s", snapshot_path)
                except Exception as e:
                    logger.warning("Failed to write memory store snapshot: %s", e)

        # Post summary if configured
        if self._config.orchestrator.summary_issue and mode != "dry-run":
            try:
                await self._state_tracker.post_run_summary(summary)
            except Exception as e:
                logger.warning("Failed to post summary: %s", e)

        # ── Transient-error exit gate ─────────────────────────────
        #
        # Classify each entry in ``summary.errors`` into transient vs
        # non-transient. If every error is transient AND the run made
        # some measurable progress (PRs monitored, issues triaged, or a
        # bucketed agent error that proves work was attempted), we emit
        # a Prometheus soft-fail counter and exit 0. This keeps the
        # signal visible without the workflow itself flipping red and
        # triggering the self-heal-on-failure ladder that produced the
        # "Unknown caretaker failure: Process completed with exit code 1."
        # issue storm on Example-React and flashcards.
        soft_failed = False
        if summary.errors:
            transient_errors, non_transient_errors = _bucket_errors(summary.errors)
            work_landed = (
                summary.prs_monitored > 0
                or summary.issues_triaged > 0
                or summary.prs_merged > 0
                or summary.issues_assigned > 0
                or summary.issues_closed > 0
                or summary.build_failures_detected > 0
                or summary.self_heal_failures_analyzed > 0
            )
            # ``len(summary.errors) - len(transient_errors)`` is just the
            # non-transient count but using the list makes the branch
            # direct to read.
            if non_transient_errors or not work_landed:
                has_errors = True
                logger.warning(
                    "Run completed with %d error(s): transient=%d, non_transient=%d",
                    len(summary.errors),
                    len(transient_errors),
                    len(non_transient_errors),
                )
            else:
                soft_failed = True
                record_orchestrator_soft_fail(category="transient")
                logger.warning(
                    "Run completed with %d transient error(s) only — soft-fail, exit 0. "
                    "transient=%d, non_transient=0. Samples: %s",
                    len(summary.errors),
                    len(transient_errors),
                    transient_errors[:3],
                )
        else:
            logger.info("Run completed successfully")

        # Audit-log the run outcome
        try:
            import uuid as _uuid

            audit_outcome = "soft_fail" if soft_failed else ("error" if has_errors else "success")
            await self._audit_log.record(
                run_id=str(_uuid.uuid4()),
                agent_id="orchestrator",
                outcome=audit_outcome,
                tool=None,
                extra={
                    "mode": mode,
                    "prs_monitored": summary.prs_monitored,
                    "prs_merged": summary.prs_merged,
                    "issues_triaged": summary.issues_triaged,
                    "errors": len(summary.errors),
                    "soft_fail": soft_failed,
                },
            )
        except Exception as _audit_err:
            logger.debug("Failed to write orchestrator audit record: %s", _audit_err)

        # Close audit log and memory backend connections
        with contextlib.suppress(Exception):
            await self._audit_log.close()
        with contextlib.suppress(Exception):
            if self._memory is not None:
                self._memory.close()
        with contextlib.suppress(Exception):
            if self._insight_store is not None:
                self._insight_store.close()

        # Write JSON run report if a path was provided
        if report_path:
            try:
                report_data = summary.model_dump(mode="json")
                with open(report_path, "w", encoding="utf-8") as fh:
                    json.dump(report_data, fh, indent=2, default=str)
                logger.info("Run report written to %s", report_path)
            except Exception as e:
                logger.warning("Failed to write run report: %s", e)

        return 1 if has_errors else 0

    def _reconcile_state(self, state: OrchestratorState, summary: RunSummary) -> None:
        """Reconcile cross-agent tracked PR/issue state and derive reconciliation metrics."""
        now = datetime.now(UTC)

        issue_to_pr: dict[int, int] = {
            issue_number: tracked_issue.assigned_pr
            for issue_number, tracked_issue in state.tracked_issues.items()
            if tracked_issue.assigned_pr is not None
        }

        linked_pr_numbers = set(issue_to_pr.values())
        _terminal_pr_states = {
            PRTrackingState.MERGED,
            PRTrackingState.CLOSED,
            PRTrackingState.ESCALATED,
        }
        orphaned_prs = 0
        for pr_number, tracked_pr in state.tracked_prs.items():
            if tracked_pr.state in _terminal_pr_states:
                continue
            if pr_number not in linked_pr_numbers:
                orphaned_prs += 1
        summary.orphaned_prs = orphaned_prs

        stale_escalated = 0
        for tracked_issue in state.tracked_issues.values():
            if tracked_issue.assigned_pr is not None:
                pr = state.tracked_prs.get(tracked_issue.assigned_pr)
                if pr is not None:
                    if pr.state == PRTrackingState.MERGED:
                        tracked_issue.state = IssueTrackingState.COMPLETED
                    elif pr.state == PRTrackingState.CLOSED:
                        tracked_issue.state = IssueTrackingState.CLOSED
                    elif pr.state == PRTrackingState.ESCALATED:
                        tracked_issue.state = IssueTrackingState.ESCALATED

            if (
                tracked_issue.state
                in (
                    IssueTrackingState.ASSIGNED,
                    IssueTrackingState.IN_PROGRESS,
                )
                and tracked_issue.last_checked is not None
            ):
                age_days = (now - _as_utc(tracked_issue.last_checked)).days
                if age_days >= self._config.escalation.stale_days:
                    tracked_issue.state = IssueTrackingState.ESCALATED
                    tracked_issue.escalated = True
                    stale_escalated += 1

        summary.stale_assignments_escalated = stale_escalated

        total_work_items = summary.prs_monitored + summary.issues_triaged
        total_escalated = summary.prs_escalated + summary.issues_escalated
        summary.escalation_rate = (
            total_escalated / total_work_items if total_work_items > 0 else 0.0
        )

        merged_durations_hours: list[float] = []
        for tracked_pr in state.tracked_prs.values():
            if tracked_pr.merged_at and tracked_pr.first_seen_at:
                merged_durations_hours.append(
                    (
                        _as_utc(tracked_pr.merged_at) - _as_utc(tracked_pr.first_seen_at)
                    ).total_seconds()
                    / 3600.0
                )
        if merged_durations_hours:
            summary.avg_time_to_merge_hours = sum(merged_durations_hours) / len(
                merged_durations_hours
            )

        if summary.prs_monitored > 0:
            summary.copilot_success_rate = summary.prs_merged / summary.prs_monitored

    async def _run_goal_driven(
        self,
        evaluation: GoalEvaluation,
        state: OrchestratorState,
        summary: RunSummary,
        mode: str,
        event_payload: dict[str, Any],
    ) -> None:
        """Dispatch agents in goal-priority order, then remaining mode agents.

        All mode-eligible agents still run — goal evaluation only affects the
        order so that the most urgent work happens first.
        """
        mode_agents = self._registry.agents_for_mode(mode)
        mode_agent_names = {a.name for a in mode_agents}

        ran: set[str] = set()
        for agent_name in evaluation.dispatch_plan:
            if agent_name in mode_agent_names and agent_name not in ran:
                agent = self._registry.get(agent_name)
                if agent:
                    await self._registry.run_one(agent, state, summary, event_payload=event_payload)
                    ran.add(agent_name)

        for agent in mode_agents:
            if agent.name not in ran:
                await self._registry.run_one(agent, state, summary, event_payload=event_payload)

    async def _handle_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        state: OrchestratorState,
        summary: RunSummary,
    ) -> None:
        """Handle a single GitHub event by dispatching to specific agents."""
        logger.info("Handling event: %s", event_type)

        agent_names = EVENT_AGENT_MAP.get(event_type)
        if agent_names is None:
            # Unknown event — fall back to PR + Issue
            logger.info("Event type %s — running full cycle", event_type)
            for name in ("pr", "issue"):
                agent = self._registry.get(name)
                if agent:
                    await self._registry.run_one(agent, state, summary)
            return

        for name in agent_names:
            agent = self._registry.get(name)
            if not agent:
                continue

            if name == "pr" and event_type == "workflow_run":
                head_branch: str | None = payload.get("workflow_run", {}).get("head_branch")
                await self._registry.run_one(
                    agent,
                    state,
                    summary,
                    event_payload={"_head_branch": head_branch},
                )
            elif name == "pr" and event_type in (
                "pull_request",
                "pull_request_review",
                "check_run",
                "check_suite",
            ):
                pr_number = _extract_pr_number(event_type, payload)
                event_pr_payload: dict[str, Any] = {}
                if pr_number is not None:
                    event_pr_payload["_pr_number"] = pr_number
                    logger.info("Event %s — scoping PR agent to PR #%d", event_type, pr_number)
                await self._registry.run_one(
                    agent,
                    state,
                    summary,
                    event_payload=event_pr_payload,
                )
            elif name in ("devops", "self-heal"):
                await self._registry.run_one(
                    agent,
                    state,
                    summary,
                    event_payload=payload,
                )
            else:
                await self._registry.run_one(agent, state, summary)
