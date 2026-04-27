"""Preflight ``caretaker doctor`` checks.

``caretaker doctor`` catches configuration gaps *before* any agent boots,
so we never again spend a whole run silently swallowing 403s for half
the product surface. The scope-gap tracker in
:mod:`caretaker.github_client.scope_gap` is the after-the-fact safety
net; this module is the pre-flight equivalent.

The three classes of check today:

* **Secrets present** — every ``*_env`` name the loaded
  :class:`~caretaker.config.MaintainerConfig` references is looked up in
  ``os.environ``. The severity is ``FAIL`` when the owning config block
  is ``enabled=True`` and ``WARN`` otherwise (forward-compat: an
  operator may be staging env vars before flipping the switch).

* **GitHub token scopes** — we ask GitHub directly. First we inspect
  ``GET /user`` for an ``X-OAuth-Scopes`` response header (PATs have
  it; workflow ``GITHUB_TOKEN``s don't). When the header is absent we
  probe the handful of endpoint templates each enabled agent relies
  on and treat a ``403 Resource not accessible by integration`` as a
  missing scope. The required-scope map piggybacks on
  :data:`caretaker.github_client.scope_gap._ENDPOINT_SCOPE_MAP` so the
  after-the-fact tracker and the preflight agree on the vocabulary.

* **External services reachable** — for every ``*.enabled = true``
  block that points at a network dependency (Mongo, Redis, Neo4j,
  fleet registry, OIDC issuer) we resolve the host and, where it's
  cheap, open a TCP connection on a short timeout. Unreachable is
  reported as ``WARN`` by default, upgraded to ``FAIL`` in strict
  mode.

The checks deliberately return structured :class:`CheckResult`
objects so the CLI can render a human table *and* a machine-readable
JSON summary from a single source of truth.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from caretaker.github_client.scope_gap import is_scope_gap_message

if TYPE_CHECKING:
    from caretaker.config import MaintainerConfig
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)


# ── Result primitives ──────────────────────────────────────────────────


class Severity(StrEnum):
    """Check severities rendered in the doctor report."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    """Outcome of a single preflight check.

    ``category`` groups checks in the rendered table; ``name`` is the
    human-readable row title; ``detail`` is the short "why" that ships
    next to the severity so operators can act without re-reading the
    config.
    """

    category: str
    name: str
    severity: Severity
    detail: str
    # Optional hint surfaced only in the JSON payload (e.g. the scope
    # string GitHub expects, or the host:port we couldn't connect to).
    hint: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data


@dataclass
class DoctorReport:
    """Aggregated preflight report.

    Callers inspect :attr:`has_failures` to decide the process exit
    code. :meth:`to_dict` produces the JSON payload written to stdout
    when ``--json`` is passed.
    """

    results: list[CheckResult] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(r.severity is Severity.FAIL for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.severity is Severity.WARN for r in self.results)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    def summary_counts(self) -> dict[str, int]:
        counts = {sev.value: 0 for sev in Severity}
        for r in self.results:
            counts[r.severity.value] += 1
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "fail" if self.has_failures else ("warn" if self.has_warnings else "ok"),
            "counts": self.summary_counts(),
            "checks": [r.to_dict() for r in self.results],
        }


# ── Config → env-var references ────────────────────────────────────────


@dataclass
class EnvReference:
    """One env-var reference extracted from the effective config.

    ``owner_enabled`` tells the secrets check whether to escalate a
    missing value to ``FAIL`` (enabled block) or ``WARN`` (disabled
    block; future-proofing for operators staging rollouts).
    """

    env_name: str
    config_path: str
    owner_enabled: bool
    # Free-form description surfaced in the detail column.
    purpose: str


def collect_env_references(config: MaintainerConfig) -> list[EnvReference]:
    """Return every env-var reference the config transitively makes.

    The list is ordered by the order in which blocks are declared on
    :class:`~caretaker.config.MaintainerConfig` so the rendered table
    groups related checks together.
    """
    refs: list[EnvReference] = []

    # MongoDB durable state (memory store + audit log + evolution).
    refs.append(
        EnvReference(
            env_name=config.mongo.mongodb_url_env,
            config_path="mongo.mongodb_url_env",
            owner_enabled=config.mongo.enabled,
            purpose="MongoDB / Cosmos DB connection string",
        )
    )

    # Redis cache / dedupe.
    refs.append(
        EnvReference(
            env_name=config.redis.redis_url_env,
            config_path="redis.redis_url_env",
            owner_enabled=config.redis.enabled,
            purpose="Redis connection string",
        )
    )

    # Fleet registry OAuth2 client creds. The legacy HMAC ``secret_env``
    # check was removed in v0.20.1 — v0.20.0 dropped HMAC heartbeat auth
    # entirely (emitter no longer signs, receiver no longer verifies), so
    # demanding ``CARETAKER_FLEET_SECRET`` here would FAIL bootstrap-check
    # for every consumer that correctly migrated to OAuth2.
    fleet_oauth = config.fleet_registry.oauth2
    fleet_oauth_enabled = config.fleet_registry.enabled and fleet_oauth.enabled
    for env_name, sub in (
        (fleet_oauth.client_id_env, "client_id_env"),
        (fleet_oauth.client_secret_env, "client_secret_env"),
        (fleet_oauth.token_url_env, "token_url_env"),
    ):
        refs.append(
            EnvReference(
                env_name=env_name,
                config_path=f"fleet_registry.oauth2.{sub}",
                owner_enabled=fleet_oauth_enabled,
                purpose="Fleet registry OAuth2 client credential",
            )
        )

    # GitHub App — private key, webhook secret, optional OAuth client.
    gha = config.github_app
    refs.append(
        EnvReference(
            env_name=gha.private_key_env,
            config_path="github_app.private_key_env",
            owner_enabled=gha.enabled,
            purpose="GitHub App PEM-encoded private key",
        )
    )
    refs.append(
        EnvReference(
            env_name=gha.webhook_secret_env,
            config_path="github_app.webhook_secret_env",
            owner_enabled=gha.enabled,
            purpose="GitHub App webhook shared secret",
        )
    )
    for env_name, sub in (
        (gha.oauth_client_id_env, "oauth_client_id_env"),
        (gha.oauth_client_secret_env, "oauth_client_secret_env"),
    ):
        refs.append(
            EnvReference(
                env_name=env_name,
                config_path=f"github_app.{sub}",
                owner_enabled=gha.enabled,
                purpose="GitHub App OAuth client credential",
            )
        )

    # Admin dashboard — OIDC + session secret.
    admin = config.admin_dashboard
    for env_name, sub, purpose in (
        (admin.oidc_client_id_env, "oidc_client_id_env", "Admin OIDC client id"),
        (
            admin.oidc_client_secret_env,
            "oidc_client_secret_env",
            "Admin OIDC client secret",
        ),
        (admin.session_secret_env, "session_secret_env", "Admin session signing key"),
    ):
        refs.append(
            EnvReference(
                env_name=env_name,
                config_path=f"admin_dashboard.{sub}",
                owner_enabled=admin.enabled,
                purpose=purpose,
            )
        )

    # Graph store — Neo4j URL + auth.
    graph = config.graph_store
    for env_name, sub, purpose in (
        (graph.neo4j_url_env, "neo4j_url_env", "Neo4j Bolt URL"),
        (graph.neo4j_auth_env, "neo4j_auth_env", "Neo4j auth pair"),
    ):
        refs.append(
            EnvReference(
                env_name=env_name,
                config_path=f"graph_store.{sub}",
                owner_enabled=graph.enabled,
                purpose=purpose,
            )
        )

    # Telemetry — App Insights connection string (only referenced when enabled).
    refs.append(
        EnvReference(
            env_name=config.telemetry.application_insights_connection_string_env,
            config_path="telemetry.application_insights_connection_string_env",
            owner_enabled=config.telemetry.enabled,
            purpose="Application Insights connection string",
        )
    )

    return refs


# ── LLM model-string → env-var inference ──────────────────────────────
#
# Ordered tuple so "most specific prefix wins" (longest-prefix match) is
# easy to enforce — ``vertex_ai/claude-sonnet-4`` must resolve to the
# Vertex env vars, not Anthropic, even though the suffix looks Anthropic.
# Each entry is ``(prefix, (env_names,))`` — the prefix is matched with
# ``str.startswith`` after lowercasing. The empty-string "prefix" entries
# at the end handle bare model ids (``claude-*``, ``gpt-*``, ``o1-*``,
# ``o3-*``, ``chatgpt-*``) via a predicate check inline in
# :func:`_env_vars_for_model`.
_MODEL_PREFIX_ENV_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Longest / most specific first.
    ("ollama_chat/", ("OLLAMA_API_BASE",)),
    ("vertex_ai/", ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEX_PROJECT")),
    ("azure_ai/", ("AZURE_AI_API_KEY", "AZURE_AI_API_BASE")),
    ("anthropic/", ("ANTHROPIC_API_KEY",)),
    ("bedrock/", ("AWS_ACCESS_KEY_ID",)),
    ("openai/", ("OPENAI_API_KEY",)),
    ("ollama/", ("OLLAMA_API_BASE",)),
    ("mistral/", ("MISTRAL_API_KEY",)),
    ("cohere/", ("COHERE_API_KEY",)),
    ("gemini/", ("GEMINI_API_KEY",)),
    ("groq/", ("GROQ_API_KEY",)),
    ("azure/", ("AZURE_API_KEY", "AZURE_API_BASE")),
)

# Providers whose env vars are local-dev only — a missing value should
# downgrade to WARN even when the model is the primary (default_model).
_LOCAL_DEV_PROVIDERS: frozenset[str] = frozenset({"ollama/", "ollama_chat/"})

# Bare-model-id fallback mapping — applied only when no explicit prefix
# matches. Kept as ``(predicate, env_vars)`` so the rules read top-down.
_BARE_MODEL_PREFIXES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("claude-",), ("ANTHROPIC_API_KEY",)),
    (("gpt-", "o1-", "o3-", "chatgpt-"), ("OPENAI_API_KEY",)),
)


def _env_vars_for_model(model: str) -> tuple[tuple[str, ...], str | None]:
    """Return ``(env_vars, matched_prefix)`` for ``model``.

    ``matched_prefix`` is the literal prefix from
    :data:`_MODEL_PREFIX_ENV_MAP` we matched (for local-dev WARN
    classification and diagnostic detail), or ``None`` when we fell
    through to the bare-model-id fallback or couldn't classify at all.

    When no known prefix or bare pattern matches, returns
    ``((), None)`` and the caller renders the UNKNOWN WARN row.
    """
    lowered = model.lower()
    for prefix, envs in _MODEL_PREFIX_ENV_MAP:
        if lowered.startswith(prefix):
            return envs, prefix
    for patterns, envs in _BARE_MODEL_PREFIXES:
        if any(lowered.startswith(p) for p in patterns):
            return envs, None
    return (), None


def _distinct_models(config: MaintainerConfig) -> tuple[set[str], set[str]]:
    """Return ``(primary_models, fallback_only_models)``.

    ``primary_models`` is the union of ``default_model`` and every
    ``feature_models[*].model`` override — these are the strings a real
    request will pin, so a missing env → FAIL. ``fallback_only_models``
    are entries that appear ONLY in ``fallback_models`` (the LiteLLM
    best-effort chain), where a missing env → WARN.
    """
    primary: set[str] = set()
    if config.llm.default_model:
        primary.add(config.llm.default_model)
    for feature in config.llm.feature_models.values():
        if feature.model:
            primary.add(feature.model)
    fallback_only = {m for m in config.llm.fallback_models if m and m not in primary}
    return primary, fallback_only


def _collect_llm_env_references(config: MaintainerConfig) -> list[EnvReference]:
    """Return the LLM env-var references implied by the config.

    Walks the distinct model strings in ``{default_model} ∪ feature
    overrides ∪ fallback_models`` and maps each to its LiteLLM-required
    env var(s) by prefix. Deduplicates so a single ``env_name`` appears
    once regardless of how many models share it.

    Severity classification (read by :func:`check_env_secrets`):

    * Env required by a *primary* model (``default_model`` or any
      ``feature_models[*].model``) → ``owner_enabled=True`` (missing → FAIL).
    * Env required ONLY by a fallback-chain entry → ``owner_enabled=False``
      (missing → WARN; the chain is best-effort).
    * Local-dev providers (``ollama``/``ollama_chat``) → ``owner_enabled=False``
      (missing → WARN even when primary).

    Short-circuit: when ``provider == "anthropic"`` the direct SDK path
    only ever reads ``ANTHROPIC_API_KEY``, so we emit that single ref
    and skip the model-walk entirely (the direct client cannot consume
    prefixed strings like ``openai/gpt-4o`` anyway).

    Unknown prefixes yield a single UNKNOWN WARN row with an
    informational ``purpose`` — we never hard-fail on an unclassifiable
    model because LiteLLM resolves env vars per-request and the
    operator may be using a custom/experimental model.
    """
    # Legacy direct-SDK fast path. A direct-Anthropic deployment is
    # defined by ``provider == "anthropic"``, not by the model-string
    # shape — so we don't require ``default_model`` to literally start
    # with ``claude-`` here (though it usually does).
    if config.llm.provider == "anthropic":
        return [
            EnvReference(
                env_name="ANTHROPIC_API_KEY",
                config_path="llm.provider=anthropic",
                owner_enabled=True,
                purpose="Anthropic SDK API key",
            )
        ]

    primary, fallback_only = _distinct_models(config)
    # Walk primary first so when an env is shared with a fallback model
    # the stronger (primary → FAIL) classification wins.
    refs: list[EnvReference] = []
    seen: dict[str, EnvReference] = {}
    unknown_rendered = False

    def _purpose(env: str, model: str, prefix: str | None) -> str:
        suffix = f"prefix {prefix!r}" if prefix else "bare model id"
        return f"LiteLLM {env} (model {model!r}, {suffix})"

    def _consider(model: str, *, is_primary: bool) -> None:
        nonlocal unknown_rendered
        envs, prefix = _env_vars_for_model(model)
        if not envs:
            if unknown_rendered:
                return
            unknown_rendered = True
            refs.append(
                EnvReference(
                    env_name="UNKNOWN",
                    config_path=f"llm model {model!r}",
                    owner_enabled=False,  # never FAIL
                    purpose=(
                        f"Cannot determine LLM env var for model {model!r}; "
                        "LiteLLM resolves per-request"
                    ),
                )
            )
            return
        local_dev = prefix in _LOCAL_DEV_PROVIDERS
        owner_enabled = is_primary and not local_dev
        for env_name in envs:
            existing = seen.get(env_name)
            if existing is None:
                ref = EnvReference(
                    env_name=env_name,
                    config_path="llm.default_model / feature_models / fallback_models",
                    owner_enabled=owner_enabled,
                    purpose=_purpose(env_name, model, prefix),
                )
                seen[env_name] = ref
                refs.append(ref)
            elif owner_enabled and not existing.owner_enabled:
                # A later (primary) model requires what an earlier
                # (fallback-only) model required; upgrade the row so
                # missing → FAIL rather than WARN.
                seen[env_name] = EnvReference(
                    env_name=existing.env_name,
                    config_path=existing.config_path,
                    owner_enabled=True,
                    purpose=existing.purpose,
                )
                # Replace in the ordered list too.
                for i, r in enumerate(refs):
                    if r.env_name == env_name:
                        refs[i] = seen[env_name]
                        break

    for model in sorted(primary):
        _consider(model, is_primary=True)
    for model in sorted(fallback_only):
        _consider(model, is_primary=False)

    return refs


# ── Check runners ──────────────────────────────────────────────────────


def check_env_secrets(config: MaintainerConfig, env: dict[str, str]) -> list[CheckResult]:
    """Return one :class:`CheckResult` per env-var reference in ``config``.

    ``env`` is injected so tests can pass a fixture without touching
    the real process environment.
    """
    refs = collect_env_references(config)
    llm_refs = _collect_llm_env_references(config)

    # GITHUB_TOKEN / COPILOT_PAT are not declared in the config model
    # but the orchestrator refuses to start without at least one of
    # them (see ``EnvCredentialsProvider.__init__``). Check them here
    # so ``caretaker doctor`` surfaces the same failure mode.
    results: list[CheckResult] = []
    if not env.get("GITHUB_TOKEN") and not env.get("COPILOT_PAT"):
        results.append(
            CheckResult(
                category="secrets",
                name="GITHUB_TOKEN",
                severity=Severity.FAIL,
                detail=(
                    "Neither GITHUB_TOKEN nor COPILOT_PAT is set; the GitHub "
                    "client will refuse to start."
                ),
                hint="GITHUB_TOKEN or COPILOT_PAT",
            )
        )
    else:
        results.append(
            CheckResult(
                category="secrets",
                name="GITHUB_TOKEN",
                severity=Severity.OK,
                detail="GitHub token present",
            )
        )

    for ref in refs:
        present = bool(env.get(ref.env_name))
        if present:
            severity = Severity.OK
            detail = f"{ref.purpose} present"
        elif ref.owner_enabled:
            severity = Severity.FAIL
            detail = f"{ref.purpose} missing ({ref.config_path} is enabled)"
        else:
            severity = Severity.WARN
            detail = f"{ref.purpose} missing ({ref.config_path} is disabled; forward-compat)"
        results.append(
            CheckResult(
                category="secrets",
                name=ref.env_name,
                severity=severity,
                detail=detail,
                hint=ref.config_path,
            )
        )

    for ref in llm_refs:
        results.append(_render_model_env_row(ref, env))
    return results


def _render_model_env_row(ref: EnvReference, env: dict[str, str]) -> CheckResult:
    """Render an LLM env-var reference into a ``category="llm"`` row.

    Keeping the category distinct from ``secrets`` lets operators (and
    the JSON consumer) tell at a glance which rows are driven by the
    config-declared ``*_env`` names vs inferred from the model string.

    The UNKNOWN sentinel (from
    :func:`_collect_llm_env_references` when no prefix matches) is
    always a WARN regardless of env contents — the row is informational.
    """
    if ref.env_name == "UNKNOWN":
        return CheckResult(
            category="llm",
            name=ref.env_name,
            severity=Severity.WARN,
            detail=ref.purpose,
            hint=ref.config_path,
        )
    present = bool(env.get(ref.env_name))
    if present:
        return CheckResult(
            category="llm",
            name=ref.env_name,
            severity=Severity.OK,
            detail=f"{ref.purpose} present",
            hint=ref.config_path,
        )
    if ref.owner_enabled:
        severity = Severity.FAIL
        detail = f"{ref.purpose} missing (required by primary model)"
    else:
        severity = Severity.WARN
        detail = f"{ref.purpose} missing (fallback-chain or local-dev; best-effort)"
    return CheckResult(
        category="llm",
        name=ref.env_name,
        severity=severity,
        detail=detail,
        hint=ref.config_path,
    )


# ── GitHub token probes ────────────────────────────────────────────────


# (endpoint template, HTTP method, required scope hint, enabled?-predicate hint)
# The predicate is a dotted path whose truthiness we read off the config.
# Kept as a string so the map is pure data and can be introspected by tests.
@dataclass(frozen=True)
class _ScopeProbe:
    method: str
    path: str
    scope: str
    needed_when: str  # human description of which agents need it


def _required_probes(config: MaintainerConfig, owner: str, repo: str) -> list[_ScopeProbe]:
    """Build the list of probe endpoints the enabled agents require.

    We only probe surface that's actually enabled so the preflight
    report doesn't drown operators in warnings for features they
    haven't switched on.
    """
    probes: list[_ScopeProbe] = []

    # Always: the bare ``GET /repos/{owner}/{repo}`` works under any
    # read scope and proves the token can see the repo at all.
    probes.append(
        _ScopeProbe(
            method="GET",
            path=f"/repos/{owner}/{repo}",
            scope="metadata: read",
            needed_when="baseline repo access",
        )
    )

    if config.issue_agent.enabled or config.charlie_agent.enabled:
        probes.append(
            _ScopeProbe(
                method="GET",
                path=f"/repos/{owner}/{repo}/issues",
                scope="issues: read",
                needed_when="issue_agent / charlie_agent",
            )
        )

    if config.pr_agent.enabled or config.pr_reviewer.enabled:
        probes.append(
            _ScopeProbe(
                method="GET",
                path=f"/repos/{owner}/{repo}/pulls",
                scope="pull_requests: read",
                needed_when="pr_agent / pr_reviewer",
            )
        )

    if config.devops_agent.enabled or config.self_heal_agent.enabled:
        probes.append(
            _ScopeProbe(
                method="GET",
                path=f"/repos/{owner}/{repo}/actions/runs",
                scope="actions: read",
                needed_when="devops_agent / self_heal_agent",
            )
        )

    if config.security_agent.enabled:
        if config.security_agent.include_dependabot:
            probes.append(
                _ScopeProbe(
                    method="GET",
                    path=f"/repos/{owner}/{repo}/dependabot/alerts",
                    scope="security_events: read",
                    needed_when="security_agent (dependabot)",
                )
            )
        if config.security_agent.include_code_scanning:
            probes.append(
                _ScopeProbe(
                    method="GET",
                    path=f"/repos/{owner}/{repo}/code-scanning/alerts",
                    scope="security_events: read",
                    needed_when="security_agent (code scanning)",
                )
            )
        if config.security_agent.include_secret_scanning:
            probes.append(
                _ScopeProbe(
                    method="GET",
                    path=f"/repos/{owner}/{repo}/secret-scanning/alerts",
                    scope="security_events: read",
                    needed_when="security_agent (secret scanning)",
                )
            )

    return probes


def _parse_repo_slug(slug: str) -> tuple[str, str] | None:
    """Parse ``owner/repo`` out of a ``GITHUB_REPOSITORY`` style string."""
    if not slug or "/" not in slug:
        return None
    owner, _, repo = slug.partition("/")
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo:
        return None
    return owner, repo


async def check_github_scopes(
    config: MaintainerConfig,
    github: GitHubClient,
    env: dict[str, str],
) -> list[CheckResult]:
    """Probe the token for each scope the enabled agents need.

    The check sequence:

    1. ``GET /user`` — parses ``X-OAuth-Scopes`` when present (PATs).
    2. ``GET /repos/{owner}/{repo}`` plus one probe per enabled agent
       surface. A 403 with a scope-gap body → ``FAIL`` row. A
       non-403 error (network, auth, 404 on a missing repo) is
       reported as ``WARN`` so the preflight doesn't hard-fail on
       transient issues.

    We intentionally make no *write* calls during preflight; the
    scope-gap tracker still catches write-scope gaps at runtime and
    emits its own issue.
    """
    results: list[CheckResult] = []

    slug = env.get("GITHUB_REPOSITORY", "")
    parsed = _parse_repo_slug(slug)
    if parsed is None:
        results.append(
            CheckResult(
                category="github",
                name="GITHUB_REPOSITORY",
                severity=Severity.WARN,
                detail=(
                    "GITHUB_REPOSITORY not set; preflight cannot probe repo-scoped "
                    "endpoints. Set it to 'owner/repo' in the workflow env."
                ),
            )
        )
        return results
    owner, repo = parsed

    # 1) Read declared scopes when the token advertises them (PATs do;
    #    installation tokens do not — GitHub issues them with
    #    permissions instead of OAuth scopes).
    try:
        resp = await _raw_get_with_headers(github, "/user")
    except Exception as exc:  # noqa: BLE001 — preflight is tolerant
        exc_str = str(exc)
        # GitHub App installation tokens cannot call /user — they are
        # scoped to a repository, not a user identity. A 403 here is
        # expected and normal; treat it as WARN and continue probing
        # repo-scoped endpoints. Only hard-fail for non-403 errors
        # (network failure, bad token prefix, etc.) that indicate the
        # token itself is unusable for any API call.
        if "403" in exc_str:
            results.append(
                CheckResult(
                    category="github",
                    name="GET /user",
                    severity=Severity.WARN,
                    detail=(
                        "GET /user returned 403 — likely a GitHub App installation "
                        "token (expected; installation tokens are repo-scoped and "
                        "cannot call /user). Skipping scope-header check; "
                        "repo-scoped probes will still run."
                    ),
                )
            )
            # Fall through to the repo-scoped probes below.
        else:
            results.append(
                CheckResult(
                    category="github",
                    name="GET /user",
                    severity=Severity.FAIL,
                    detail=f"token rejected by GitHub: {exc}",
                )
            )
            # Non-403 means the token is entirely unusable — no point
            # probing repo endpoints.
            return results
    else:
        declared = resp.get("x-oauth-scopes", "")
        if declared:
            results.append(
                CheckResult(
                    category="github",
                    name="declared scopes",
                    severity=Severity.OK,
                    detail=f"OAuth scopes: {declared}",
                    hint=declared,
                )
            )
        else:
            results.append(
                CheckResult(
                    category="github",
                    name="declared scopes",
                    severity=Severity.OK,
                    detail=(
                        "no X-OAuth-Scopes header (installation or fine-grained "
                        "token); falling back to endpoint probes."
                    ),
                )
            )

    # 2) Probe each enabled-surface endpoint.
    for probe in _required_probes(config, owner, repo):
        probe_result = await _probe_endpoint(github, probe)
        results.append(probe_result)

    return results


async def _raw_get_with_headers(github: GitHubClient, path: str) -> dict[str, str]:
    """Perform ``GET path`` and return response headers (lowercased).

    Reaches into :class:`GitHubClient`'s httpx client directly so we
    can observe the ``X-OAuth-Scopes`` response header, which the
    normal ``_request`` helper discards. Raises on non-2xx so the
    caller can flag the token.
    """
    # Deferred import keeps the circular boundary clean at module
    # import time: the CLI imports doctor; doctor references the
    # github_client type only lazily.
    creds = github._creds
    token = await creds.default_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = await github._client.request("GET", path, headers=headers)
    if resp.status_code >= 400:
        try:
            body = resp.json()
            message = body.get("message", resp.text)
        except Exception:
            message = resp.text
        raise RuntimeError(f"{resp.status_code}: {message}")
    return {k.lower(): v for k, v in resp.headers.items()}


async def _probe_endpoint(github: GitHubClient, probe: _ScopeProbe) -> CheckResult:
    """Issue a single HEAD/GET probe and map the outcome to a row."""
    creds = github._creds
    try:
        token = await creds.default_token()
    except Exception as exc:
        return CheckResult(
            category="github",
            name=f"{probe.method} {probe.path}",
            severity=Severity.FAIL,
            detail=f"no GitHub token available: {exc}",
            hint=probe.scope,
        )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = await github._client.request(probe.method, probe.path, headers=headers)
    except Exception as exc:  # noqa: BLE001 — network errors → WARN
        return CheckResult(
            category="github",
            name=f"{probe.method} {probe.path}",
            severity=Severity.WARN,
            detail=f"request failed: {exc}",
            hint=probe.scope,
        )
    status = resp.status_code
    if status < 400:
        return CheckResult(
            category="github",
            name=f"{probe.method} {probe.path}",
            severity=Severity.OK,
            detail=f"ok ({probe.needed_when})",
            hint=probe.scope,
        )
    # 403 + scope-gap body → FAIL with the exact scope needed.
    if status == 403:
        try:
            body = resp.json()
            message = body.get("message", resp.text)
        except Exception:
            message = resp.text
        if is_scope_gap_message(message):
            return CheckResult(
                category="github",
                name=f"{probe.method} {probe.path}",
                severity=Severity.FAIL,
                detail=f"403 scope-gap: needs {probe.scope} ({probe.needed_when})",
                hint=probe.scope,
            )
        # Rate-limit 403s are transient — treat as WARN so a momentary
        # burst in the installation's quota doesn't hard-block the run.
        lowered_message = message.casefold()
        if "rate limit" in lowered_message:
            return CheckResult(
                category="github",
                name=f"{probe.method} {probe.path}",
                severity=Severity.WARN,
                detail=f"403 rate-limit (transient; retry later): {message}",
                hint=probe.scope,
            )
        return CheckResult(
            category="github",
            name=f"{probe.method} {probe.path}",
            severity=Severity.FAIL,
            detail=f"403: {message}",
            hint=probe.scope,
        )
    # 404 is not a scope failure — the resource just doesn't exist.
    # Report as WARN so operators see it but we don't block the run.
    if status == 404:
        return CheckResult(
            category="github",
            name=f"{probe.method} {probe.path}",
            severity=Severity.WARN,
            detail=f"404 (endpoint absent; {probe.needed_when})",
            hint=probe.scope,
        )
    return CheckResult(
        category="github",
        name=f"{probe.method} {probe.path}",
        severity=Severity.WARN,
        detail=f"HTTP {status} ({probe.needed_when})",
        hint=probe.scope,
    )


# ── External-service reachability ──────────────────────────────────────


@dataclass(frozen=True)
class _ServiceProbe:
    name: str
    config_path: str
    url: str


def _collect_service_probes(config: MaintainerConfig, env: dict[str, str]) -> list[_ServiceProbe]:
    probes: list[_ServiceProbe] = []
    if config.mongo.enabled:
        url = env.get(config.mongo.mongodb_url_env, "")
        if url:
            probes.append(_ServiceProbe(name="mongo", config_path="mongo.mongodb_url_env", url=url))
    if config.redis.enabled:
        url = env.get(config.redis.redis_url_env, "")
        if url:
            probes.append(_ServiceProbe(name="redis", config_path="redis.redis_url_env", url=url))
    if config.graph_store.enabled:
        url = env.get(config.graph_store.neo4j_url_env, "")
        if url:
            probes.append(
                _ServiceProbe(
                    name="neo4j",
                    config_path="graph_store.neo4j_url_env",
                    url=url,
                )
            )
    if config.fleet_registry.enabled and config.fleet_registry.endpoint:
        probes.append(
            _ServiceProbe(
                name="fleet_registry",
                config_path="fleet_registry.endpoint",
                url=config.fleet_registry.endpoint,
            )
        )
    if config.admin_dashboard.enabled and config.admin_dashboard.oidc_issuer_url:
        probes.append(
            _ServiceProbe(
                name="oidc_issuer",
                config_path="admin_dashboard.oidc_issuer_url",
                url=config.admin_dashboard.oidc_issuer_url,
            )
        )
    return probes


def _extract_host_port(url: str) -> tuple[str, int] | None:
    """Best-effort ``(host, port)`` extraction for probing.

    Handles ``mongodb+srv://``, ``rediss://``, ``bolt://``, ``https://``
    etc. without taking a dependency on each provider's driver.
    Returns ``None`` when the URL is unparseable or carries no host.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = parsed.hostname
    if not host:
        return None
    if parsed.port:
        return host, parsed.port
    # Default ports for known schemes — deliberately a tiny table, we
    # only need it to produce *some* signal.
    default_ports = {
        "http": 80,
        "https": 443,
        "redis": 6379,
        "rediss": 6379,
        "mongodb": 27017,
        "mongodb+srv": 27017,
        "bolt": 7687,
        "neo4j": 7687,
        "neo4j+s": 7687,
    }
    port = default_ports.get(parsed.scheme, 0)
    if port == 0:
        return None
    return host, port


def _check_tcp_reachable(host: str, port: int, *, timeout: float = 2.0) -> tuple[bool, str]:
    """Return ``(reachable, detail)`` for a short-timeout TCP probe."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} reachable"
    except OSError as exc:
        return False, f"{host}:{port} unreachable: {exc}"


def check_external_services(
    config: MaintainerConfig,
    env: dict[str, str],
    *,
    strict: bool,
) -> list[CheckResult]:
    """Return reachability checks for every enabled external service."""
    results: list[CheckResult] = []
    probes = _collect_service_probes(config, env)
    for probe in probes:
        host_port = _extract_host_port(probe.url)
        if host_port is None:
            results.append(
                CheckResult(
                    category="services",
                    name=probe.name,
                    severity=Severity.WARN,
                    detail=f"cannot parse host/port from {probe.config_path}",
                    hint=probe.config_path,
                )
            )
            continue
        host, port = host_port
        ok, detail = _check_tcp_reachable(host, port)
        if ok:
            results.append(
                CheckResult(
                    category="services",
                    name=probe.name,
                    severity=Severity.OK,
                    detail=detail,
                    hint=probe.config_path,
                )
            )
        else:
            sev = Severity.FAIL if strict else Severity.WARN
            results.append(
                CheckResult(
                    category="services",
                    name=probe.name,
                    severity=sev,
                    detail=detail,
                    hint=probe.config_path,
                )
            )
    return results


# ── Orchestration ──────────────────────────────────────────────────────


def check_coding_agent_config(config: MaintainerConfig) -> list[CheckResult]:
    """Validate ``executor.provider`` resolves to a registered coding agent.

    The dispatcher tolerates an unknown provider at runtime (logs and falls
    back to Copilot) so a typo doesn't crash the orchestrator. ``doctor``
    surfaces the misconfiguration as a row so operators can spot it before
    they wonder why every task is going to Copilot.
    """
    rows: list[CheckResult] = []
    cfg = config.executor

    # Names always known to the registry-construction code in
    # ``Orchestrator._build_executor_dispatcher``. ``copilot`` and
    # ``auto`` are reserved router-only values that don't need a
    # corresponding enabled feature; ``foundry`` is gated on its own
    # ``enabled`` flag so a typo'd ``provider: foundry`` without
    # ``foundry.enabled = true`` surfaces here as a WARN row.
    known: set[str] = {"copilot", "auto"}
    if cfg.foundry.enabled:
        known.add("foundry")
    if cfg.claude_code.enabled:
        known.add("claude_code")
    if cfg.opencode.enabled:
        known.add("opencode")
    for name, agent_cfg in cfg.agents.items():
        if agent_cfg.enabled:
            known.add(name)

    if cfg.provider not in known:
        # Distinguish between "named agent isn't enabled" and "provider
        # doesn't exist at all" — the first is a common misconfig where
        # the operator pasted ``provider: opencode`` but forgot
        # ``opencode.enabled: true``.
        is_typed_disabled = (
            (cfg.provider == "claude_code" and not cfg.claude_code.enabled)
            or (cfg.provider == "opencode" and not cfg.opencode.enabled)
            or (cfg.provider == "foundry" and not cfg.foundry.enabled)
        )
        detail = (
            f"executor.provider={cfg.provider!r} is set but the "
            f"corresponding agent block has enabled=False — caretaker will "
            "route to Copilot instead. Set the matching ``.enabled = true``."
            if is_typed_disabled
            else (
                f"executor.provider={cfg.provider!r} does not match any "
                f"registered coding agent (known: {', '.join(sorted(known))}). "
                "Tasks will silently route to Copilot."
            )
        )
        rows.append(
            CheckResult(
                category="executor",
                name="provider",
                severity=Severity.WARN,
                detail=detail,
                hint="executor.provider",
            )
        )

    # Validate per-PR ``complex_reviewer`` matches a known reviewer
    # backend. Pull the hand-off backend list from the canonical source
    # (``handoff_reviewer.known_backends``) so adding a new backend
    # there doesn't silently desync this check.
    from caretaker.pr_reviewer.handoff_reviewer import known_backends

    pr_cfg = config.pr_reviewer
    valid_reviewers = {"inline", *known_backends()}
    if pr_cfg.complex_reviewer not in valid_reviewers:
        rows.append(
            CheckResult(
                category="executor",
                name="pr-reviewer backend",
                severity=Severity.WARN,
                detail=(
                    f"pr_reviewer.complex_reviewer={pr_cfg.complex_reviewer!r} "
                    f"is not a recognized hand-off backend "
                    f"(known: {', '.join(sorted(valid_reviewers))}). "
                    "Caretaker falls back to claude_code."
                ),
                hint="pr_reviewer.complex_reviewer",
            )
        )

    if not rows:
        rows.append(
            CheckResult(
                category="executor",
                name="provider",
                severity=Severity.OK,
                detail=(
                    f"executor.provider={cfg.provider!r}, "
                    f"pr_reviewer.complex_reviewer={pr_cfg.complex_reviewer!r}"
                ),
            )
        )
    return rows


async def run_doctor(
    config: MaintainerConfig,
    *,
    env: dict[str, str] | None = None,
    github: GitHubClient | None = None,
    strict: bool = False,
    skip_github: bool = False,
) -> DoctorReport:
    """Run every preflight check and return the aggregated report.

    ``github`` is accepted so tests can inject a mocked client; when
    omitted we construct one lazily from the environment (only if
    ``skip_github`` is False).
    """
    env = env if env is not None else dict(os.environ)
    report = DoctorReport()

    for result in check_env_secrets(config, env):
        report.add(result)

    if not skip_github:
        close_after = False
        if github is None:
            try:
                from caretaker.github_client.api import GitHubClient as _GitHubClient

                github = _GitHubClient()
                close_after = True
            except Exception as exc:  # noqa: BLE001 — token absent → FAIL row
                report.add(
                    CheckResult(
                        category="github",
                        name="client",
                        severity=Severity.FAIL,
                        detail=f"cannot construct GitHubClient: {exc}",
                    )
                )
                github = None
        if github is not None:
            try:
                for result in await check_github_scopes(config, github, env):
                    report.add(result)
            finally:
                if close_after:
                    await github.close()

    for result in check_external_services(config, env, strict=strict):
        report.add(result)

    for result in check_coding_agent_config(config):
        report.add(result)

    return report


def run_doctor_sync(
    config: MaintainerConfig,
    *,
    env: dict[str, str] | None = None,
    strict: bool = False,
    skip_github: bool = False,
) -> DoctorReport:
    """Blocking convenience wrapper for the CLI entrypoint."""
    return asyncio.run(run_doctor(config, env=env, strict=strict, skip_github=skip_github))


# ── LLM-probe preflight ───────────────────────────────────────────────
#
# ``--llm-probe`` resolves every distinct model string the config pins
# (``default_model`` + ``feature_models[*].model`` + ``fallback_models``)
# against the LiteLLM registry and reports which endpoints are live.
# Today a typo like ``azure_ai/claude-sonnet-4-typo`` is silently
# invisible until the first feature fires at runtime, swallowing the
# 404 behind a fallback-chain retry. The probe exchanges one paid token
# per model for deterministic onboarding / post-rotation confidence.
#
# Category conventions mirror the rest of the doctor:
#
# * ``category="llm-probe"`` — every row emitted by this mode.
# * ``name=<model-string>`` — the row maps 1:1 to a model entry. The
#   ``litellm`` row name is reserved for the package-missing bail.
# * Severity:
#       OK   — env vars present AND endpoint returned 2xx.
#       WARN — env vars present but probe failed (network / 5xx / timeout),
#              or a fallback-chain model failed (not critical path).
#       FAIL — env vars missing (primary model) OR 401/403/404 from endpoint.


def _probe_prompt_messages() -> list[dict[str, str]]:
    """Return the minimal prompt for the cheap LLM probe.

    ``"1+1="`` is the smallest input that still exercises auth +
    endpoint resolution end-to-end. ``max_tokens=1`` is set at the call
    site. Kept as a standalone helper so tests can assert the exact
    shape passed to ``litellm.acompletion``.
    """
    return [{"role": "user", "content": "1+1="}]


@dataclass(frozen=True)
class _ModelProbeSpec:
    """One model + its provider classification for probing.

    ``is_primary`` tracks whether the model is referenced from
    ``default_model`` / ``feature_models`` (primary-path, failures →
    FAIL) or only from ``fallback_models`` (fallback-chain, failures →
    WARN per the contract).
    """

    model: str
    is_primary: bool
    env_vars: tuple[str, ...]
    matched_prefix: str | None


def _collect_probe_specs(config: MaintainerConfig) -> list[_ModelProbeSpec]:
    """Return one :class:`_ModelProbeSpec` per distinct configured model.

    Reuses :func:`_distinct_models` and :func:`_env_vars_for_model` so
    the prefix → env-var mapping stays a single source of truth with
    :func:`_collect_llm_env_references`. Primary models come first so
    the rendered report groups the critical-path rows on top.
    """
    primary, fallback_only = _distinct_models(config)
    specs: list[_ModelProbeSpec] = []
    for model in sorted(primary):
        envs, prefix = _env_vars_for_model(model)
        specs.append(
            _ModelProbeSpec(
                model=model,
                is_primary=True,
                env_vars=envs,
                matched_prefix=prefix,
            )
        )
    for model in sorted(fallback_only):
        envs, prefix = _env_vars_for_model(model)
        specs.append(
            _ModelProbeSpec(
                model=model,
                is_primary=False,
                env_vars=envs,
                matched_prefix=prefix,
            )
        )
    return specs


def _missing_env_vars(spec: _ModelProbeSpec, env: dict[str, str]) -> list[str]:
    """Return env var names required by ``spec`` that are missing from ``env``."""
    return [name for name in spec.env_vars if not env.get(name)]


def _extract_http_status(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from a LiteLLM exception.

    LiteLLM raises a zoo of exception classes (``AuthenticationError``,
    ``NotFoundError``, plain ``httpx.HTTPStatusError`` rewraps) that
    all expose the upstream status differently. We check the common
    attributes (``status_code``, ``code``, ``http_status``) and fall
    back to a regex over the stringified exception — good enough to
    classify 401/403/404 as FAIL vs 5xx as WARN without taking a hard
    dependency on litellm's exception tree.
    """
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
        if isinstance(value, str) and value.isdigit():
            candidate = int(value)
            if 100 <= candidate < 600:
                return candidate
    # Fallback: search the string form. LiteLLM typically embeds the
    # upstream status in the error body (``"Error code: 401 - {...}"``
    # or ``"AuthenticationError: 401 Unauthorized"``).
    text = str(exc)
    import re

    match = re.search(r"\b(4\d{2}|5\d{2})\b", text)
    if match:
        return int(match.group(1))
    return None


async def _probe_model_endpoint(
    spec: _ModelProbeSpec,
    *,
    acompletion: Any,
    timeout: float,
) -> CheckResult:
    """Fire the cheap ``acompletion`` ping and map the outcome to a row.

    Contract (matches the task spec):

    * 2xx → OK.
    * 401/403/404 → FAIL (auth or endpoint resolution broken).
    * Other HTTP error (5xx) → WARN (transient, operator can retry).
    * :class:`asyncio.TimeoutError` → WARN.
    * Any other exception → WARN with the error class name.

    A *fallback-only* model NEVER escalates to FAIL at the endpoint
    layer — its probe failure is reported at WARN with the rationale
    in the detail text (a broken fallback link doesn't block the
    primary request path).
    """
    kwargs: dict[str, Any] = {
        "model": spec.model,
        "messages": _probe_prompt_messages(),
        "max_tokens": 1,
        "temperature": 0.0,
    }

    logger.info("LLM probe: %s via %s", spec.model, spec.matched_prefix or "bare")
    start = time.monotonic()
    try:
        await asyncio.wait_for(acompletion(**kwargs), timeout=timeout)
    except TimeoutError:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.warning("LLM probe: %s TIMEOUT (%.1fms)", spec.model, elapsed_ms)
        detail = (
            f"probe timed out after {timeout:.1f}s "
            f"(model={spec.model!r}, provider={spec.matched_prefix or 'bare'})"
        )
        return CheckResult(
            category="llm-probe",
            name=spec.model,
            severity=Severity.WARN,
            detail=detail,
            hint="TimeoutError",
        )
    except Exception as exc:  # noqa: BLE001 — classify every failure class
        elapsed_ms = (time.monotonic() - start) * 1000
        status = _extract_http_status(exc)
        logger.warning(
            "LLM probe: %s FAIL (%.1fms) status=%s err=%s",
            spec.model,
            elapsed_ms,
            status,
            type(exc).__name__,
        )
        # Auth / not-found errors on a primary model are FAIL — they
        # mean the typo-in-model or wrong-key case the probe exists to
        # catch. On a fallback-only model they downgrade to WARN since
        # the fallback chain is best-effort.
        hard_fail_status = {401, 403, 404}
        if status in hard_fail_status and spec.is_primary:
            detail = f"HTTP {status} ({type(exc).__name__}): {exc}"
            return CheckResult(
                category="llm-probe",
                name=spec.model,
                severity=Severity.FAIL,
                detail=detail,
                hint=str(status),
            )
        if status in hard_fail_status:
            # Fallback-only: explicitly annotate why we aren't FAILing.
            detail = (
                f"HTTP {status} on fallback-only model ({type(exc).__name__}): {exc} — "
                "fallback-chain entries are best-effort, not escalated to FAIL"
            )
            return CheckResult(
                category="llm-probe",
                name=spec.model,
                severity=Severity.WARN,
                detail=detail,
                hint=str(status),
            )
        # Transient / unknown: WARN in both cases so operators notice
        # without the CI gate flipping red on a flaky 502.
        status_tag = f"HTTP {status}" if status is not None else "no HTTP status"
        detail = f"probe failed ({status_tag}, {type(exc).__name__}): {exc}"
        return CheckResult(
            category="llm-probe",
            name=spec.model,
            severity=Severity.WARN,
            detail=detail,
            hint=type(exc).__name__,
        )
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("LLM probe: %s OK (%.1fms)", spec.model, elapsed_ms)
    role = "primary" if spec.is_primary else "fallback"
    return CheckResult(
        category="llm-probe",
        name=spec.model,
        severity=Severity.OK,
        detail=f"endpoint ok ({role}; {elapsed_ms:.0f}ms)",
        hint=spec.matched_prefix,
    )


async def run_llm_probe(
    config: MaintainerConfig,
    *,
    env: dict[str, str] | None = None,
    acompletion: Any = None,
) -> DoctorReport:
    """Resolve every configured model and probe its endpoint.

    See :func:`_probe_model_endpoint` for the per-model severity
    contract. Zero other checks run in this mode — the flag is a
    different blast-radius preflight from the default doctor path.

    ``acompletion`` is injected for tests; in production we import
    ``litellm.acompletion`` lazily so an operator who doesn't use
    LiteLLM never pays the import cost. When the import fails we
    emit a single FAIL row (``name="litellm"``) instead of crashing —
    the only remediation is an install, and the operator needs to see
    that as a row, not a traceback.
    """
    env = env if env is not None else dict(os.environ)
    report = DoctorReport()

    if acompletion is None:
        try:
            litellm = importlib.import_module("litellm")
        except ImportError as exc:
            report.add(
                CheckResult(
                    category="llm-probe",
                    name="litellm",
                    severity=Severity.FAIL,
                    detail=(
                        f"litellm package not installed ({exc}); install "
                        "caretaker-github[litellm] or pip install litellm"
                    ),
                    hint="ImportError",
                )
            )
            return report
        acompletion = litellm.acompletion

    specs = _collect_probe_specs(config)
    if not specs:
        # Defensive: an empty ``default_model`` + empty feature_models +
        # empty fallback_models is possible only via a malformed config
        # load, but we'd rather render a single row than silently exit 0.
        report.add(
            CheckResult(
                category="llm-probe",
                name="(no models configured)",
                severity=Severity.WARN,
                detail=(
                    "no distinct models resolved from "
                    "default_model / feature_models / fallback_models"
                ),
            )
        )
        return report

    timeout = float(config.llm.timeout_seconds)

    for spec in specs:
        # Env-var check is authoritative and runs *before* any network
        # call — an obviously-missing key must not spend a real token.
        missing = _missing_env_vars(spec, env)
        if missing:
            role = "primary" if spec.is_primary else "fallback"
            # Only primary models escalate a missing env var to FAIL.
            # Fallback-only models stay at WARN to match the "fallback
            # chain is best-effort" contract used elsewhere in doctor.
            severity = Severity.FAIL if spec.is_primary else Severity.WARN
            report.add(
                CheckResult(
                    category="llm-probe",
                    name=spec.model,
                    severity=severity,
                    detail=(
                        f"required env var(s) missing: {', '.join(missing)} "
                        f"(model={spec.model!r}, {role}; probe skipped)"
                    ),
                    hint=",".join(missing),
                )
            )
            continue
        if not spec.env_vars and spec.matched_prefix is None:
            # UNKNOWN prefix path: we can't check env vars, but the
            # probe itself will exercise whatever LiteLLM resolves.
            # Emit an info row then still probe, so the operator sees
            # both the classification gap and the live outcome.
            logger.info("LLM probe: %s has unclassified prefix; probing anyway", spec.model)
        report.add(
            await _probe_model_endpoint(
                spec,
                acompletion=acompletion,
                timeout=timeout,
            )
        )

    return report


def run_llm_probe_sync(
    config: MaintainerConfig,
    *,
    env: dict[str, str] | None = None,
) -> DoctorReport:
    """Blocking convenience wrapper for the CLI entrypoint."""
    return asyncio.run(run_llm_probe(config, env=env))


# ── Bootstrap-only preflight ───────────────────────────────────────────
#
# The full ``doctor`` preflight opens a GitHubClient, probes every scope
# the enabled agents need, and TCP-pokes each external dependency. That
# is the right thing to do once we know the *process itself* can start,
# but the audio_engineer outage (2026-04-22) was a reminder that failures
# which happen *before* caretaker imports — a bad workflow YAML, a
# missing pin file, a config that no longer parses on the pinned tag,
# or an enabled agent whose env var was never provisioned — are silent:
# the full doctor never gets a chance to run and the workflow dies with
# a bare "workflow file issue". ``--bootstrap-check`` is the tight
# subset that validates exactly those four things with zero outbound
# network calls so it is cheap to wire in as the *first* step in every
# consumer's caretaker workflow.


def check_import_ok() -> CheckResult:
    """Confirm the caretaker package actually imports in this interpreter.

    A FAIL here means ``pip install`` from the pinned tag produced a
    broken install (missing transitive dep, Python version mismatch,
    etc.). Since we're already inside a caretaker module this check is
    effectively the tautology "did the import that loaded us succeed",
    but we still report it so operators see the green row and know
    bootstrap-check itself ran — and if the import ever grows a runtime
    side effect (eg. eager loading of an optional SDK) this check
    becomes the natural place to trap it.
    """
    try:
        import caretaker  # noqa: F401 — reimport is the check
    except Exception as exc:  # noqa: BLE001 — we want every failure class
        return CheckResult(
            category="bootstrap",
            name="import caretaker",
            severity=Severity.FAIL,
            detail=f"caretaker failed to import: {exc}",
        )
    return CheckResult(
        category="bootstrap",
        name="import caretaker",
        severity=Severity.OK,
        detail="caretaker package imports cleanly",
    )


def check_config_parse(config_path: str | Path) -> tuple[CheckResult, MaintainerConfig | None]:
    """Parse the config YAML and return a row plus the loaded object (or None).

    Parsing failures are ``FAIL`` — an unparseable config is the exact
    case ``bootstrap-check`` exists to catch. We return the loaded
    ``MaintainerConfig`` (or ``None``) so subsequent checks can reuse it
    without re-reading the file.
    """
    # Deferred import keeps this module cheap to import in minimal
    # environments (the CLI already loads MaintainerConfig at top of
    # module, but tests that monkey-patch ``doctor`` shouldn't have to
    # pay for the pydantic model graph).
    from caretaker.config import MaintainerConfig as _MaintainerConfig

    path = Path(config_path)
    if not path.is_file():
        return (
            CheckResult(
                category="bootstrap",
                name="config file",
                severity=Severity.FAIL,
                detail=f"config file not found: {path}",
                hint=str(path),
            ),
            None,
        )
    try:
        loaded = _MaintainerConfig.from_yaml(path)
    except Exception as exc:  # noqa: BLE001 — any parse failure is FAIL
        return (
            CheckResult(
                category="bootstrap",
                name="config file",
                severity=Severity.FAIL,
                detail=f"config parse failed: {exc}",
                hint=str(path),
            ),
            None,
        )
    return (
        CheckResult(
            category="bootstrap",
            name="config file",
            severity=Severity.OK,
            detail=f"parsed {path} (version={loaded.version})",
            hint=str(path),
        ),
        loaded,
    )


def check_version_pin(pin_path: str | Path) -> CheckResult:
    """Confirm the ``.github/maintainer/.version`` pin file is present and non-empty.

    Consumer workflows run ``pip install git+…@v$(cat .version)`` to
    install the pinned caretaker tag. A missing or empty pin file means
    the install step will either fail outright or silently install a
    stale ``HEAD``, both of which have bitten us in the past.

    Content validation is intentionally tiny — we only check it parses
    as ``MAJOR.MINOR.PATCH`` with optional ``vX`` prefix — because the
    authoritative "does this tag exist" check requires network access
    and ``bootstrap-check`` is deliberately offline.
    """
    path = Path(pin_path)
    if not path.is_file():
        return CheckResult(
            category="bootstrap",
            name="version pin",
            severity=Severity.FAIL,
            detail=f"pin file missing: {path}",
            hint=str(path),
        )
    raw = path.read_text().strip()
    if not raw:
        return CheckResult(
            category="bootstrap",
            name="version pin",
            severity=Severity.FAIL,
            detail=f"pin file is empty: {path}",
            hint=str(path),
        )
    # Very tolerant: strip an optional leading 'v' and require the rest
    # looks like MAJOR.MINOR.PATCH with optional pre-release suffix.
    candidate = raw.removeprefix("v")
    parts = candidate.split(".")
    looks_numeric = len(parts) >= 3 and all(p and p[0].isdigit() for p in parts[:3])
    if not looks_numeric:
        return CheckResult(
            category="bootstrap",
            name="version pin",
            severity=Severity.FAIL,
            detail=f"pin does not look like a version: {raw!r}",
            hint=str(path),
        )
    return CheckResult(
        category="bootstrap",
        name="version pin",
        severity=Severity.OK,
        detail=f"pinned to v{candidate}",
        hint=str(path),
    )


async def check_version_pin_published(
    pin_path: str | Path,
    *,
    package: str | None = None,
    client: Any = None,
    timeout: float = 5.0,
) -> CheckResult:
    """Confirm the pinned caretaker version is live + installable on PyPI.

    :func:`check_version_pin` only validates *shape* — it cannot catch
    the space-tycoon PR #15 class of failure where the pin was a
    syntactically-valid version that PyPI had yanked. This check
    closes that gap by querying PyPI JSON for the pinned version.

    Severity mapping:

    * ``OK``    — version published and at least one artefact is live.
    * ``FAIL``  — version missing or yanked on PyPI; ``pip install`` will fail.
    * ``WARN``  — pin file missing/empty or PyPI unreachable (we don't want
      to flip preflight red for a transient outage).

    Network-dependent by design — bootstrap/offline paths keep using
    :func:`check_version_pin` instead.
    """
    # Local import avoids pulling httpx/fleet into the offline
    # ``check_version_pin`` code path used by bootstrap.
    from caretaker.fleet.version_drift import DEFAULT_CARETAKER_PACKAGE, check_pypi_version

    path = Path(pin_path)
    package_name = package or DEFAULT_CARETAKER_PACKAGE
    if not path.is_file():
        return CheckResult(
            category="bootstrap",
            name="version pin published",
            severity=Severity.WARN,
            detail=f"pin file missing: {path}",
            hint=str(path),
        )
    raw = path.read_text().strip()
    if not raw:
        return CheckResult(
            category="bootstrap",
            name="version pin published",
            severity=Severity.WARN,
            detail=f"pin file is empty: {path}",
            hint=str(path),
        )
    status = await check_pypi_version(package_name, raw, client=client, timeout=timeout)
    if status.exists:
        return CheckResult(
            category="bootstrap",
            name="version pin published",
            severity=Severity.OK,
            detail=f"{package_name}=={status.version} live on PyPI",
            hint=str(path),
        )
    if status.yanked:
        return CheckResult(
            category="bootstrap",
            name="version pin published",
            severity=Severity.FAIL,
            detail=(
                f"{package_name}=={status.version} yanked on PyPI — "
                f"pip install will warn/fail. Bump pin."
            ),
            hint=str(path),
        )
    # "unreachable" vs "version missing" — we only FAIL on the latter.
    if "unreachable" in status.reason:
        return CheckResult(
            category="bootstrap",
            name="version pin published",
            severity=Severity.WARN,
            detail=f"PyPI unreachable while checking {package_name}=={status.version}",
            hint=str(path),
        )
    return CheckResult(
        category="bootstrap",
        name="version pin published",
        severity=Severity.FAIL,
        detail=(
            f"{package_name}=={status.version} not found on PyPI: {status.reason}. "
            f"Check package rename or typo in pin."
        ),
        hint=str(path),
    )


def check_bootstrap_env_secrets(config: MaintainerConfig, env: dict[str, str]) -> list[CheckResult]:
    """Return a ``FAIL`` row for every env var an *enabled* block needs that isn't set.

    This is a deliberately stricter, quieter subset of
    :func:`check_env_secrets`:

    * Only enabled blocks produce rows — disabled forward-compat
      warnings are not actionable at bootstrap and just add noise.
    * We still always emit the GITHUB_TOKEN / COPILOT_PAT row because
      the orchestrator refuses to start without one of them.
    * We still always emit the LLM provider key row when provider is
      Anthropic, for the same reason.
    """
    results: list[CheckResult] = []

    if not env.get("GITHUB_TOKEN") and not env.get("COPILOT_PAT"):
        results.append(
            CheckResult(
                category="bootstrap",
                name="GITHUB_TOKEN",
                severity=Severity.FAIL,
                detail=(
                    "neither GITHUB_TOKEN nor COPILOT_PAT is set; the GitHub "
                    "client will refuse to start"
                ),
                hint="GITHUB_TOKEN or COPILOT_PAT",
            )
        )
    else:
        results.append(
            CheckResult(
                category="bootstrap",
                name="GITHUB_TOKEN",
                severity=Severity.OK,
                detail="GitHub token present",
            )
        )

    # LLM API key — mirrors the full-doctor behaviour. Now that
    # :func:`_collect_llm_env_references` infers env vars from the
    # model string (not just ``provider == "anthropic"``), bootstrap
    # emits one row per distinct LLM env var the config requires.
    # Only *primary* (``default_model`` / ``feature_models``) refs
    # escalate to FAIL here — fallback-only and UNKNOWN rows stay
    # quiet to match bootstrap's "only surface what blocks startup"
    # posture.
    for llm_ref in _collect_llm_env_references(config):
        if llm_ref.env_name == "UNKNOWN":
            # Informational only; bootstrap-check stays quiet on rows
            # that can't be acted on without the LiteLLM request path.
            continue
        if not llm_ref.owner_enabled:
            # Fallback-only or local-dev → bootstrap-check skips.
            continue
        if env.get(llm_ref.env_name):
            results.append(
                CheckResult(
                    category="bootstrap",
                    name=llm_ref.env_name,
                    severity=Severity.OK,
                    detail=f"{llm_ref.purpose} present",
                )
            )
        else:
            results.append(
                CheckResult(
                    category="bootstrap",
                    name=llm_ref.env_name,
                    severity=Severity.FAIL,
                    detail=f"{llm_ref.purpose} missing",
                    hint=llm_ref.config_path,
                )
            )

    for ref in collect_env_references(config):
        # Bootstrap mode skips disabled-block env vars entirely — the
        # full doctor covers that ground as a WARN, bootstrap-check
        # stays focused on what actually blocks startup.
        if not ref.owner_enabled:
            continue
        if env.get(ref.env_name):
            results.append(
                CheckResult(
                    category="bootstrap",
                    name=ref.env_name,
                    severity=Severity.OK,
                    detail=f"{ref.purpose} present",
                    hint=ref.config_path,
                )
            )
        else:
            results.append(
                CheckResult(
                    category="bootstrap",
                    name=ref.env_name,
                    severity=Severity.FAIL,
                    detail=f"{ref.purpose} missing ({ref.config_path} is enabled)",
                    hint=ref.config_path,
                )
            )

    return results


DEFAULT_VERSION_PIN_PATH = ".github/maintainer/.version"


def run_bootstrap_check(
    config_path: str | Path,
    *,
    env: dict[str, str] | None = None,
    pin_path: str | Path | None = None,
) -> DoctorReport:
    """Offline, pre-orchestrator sanity check.

    Runs four checks and stops on the first fatal parse failure:

    1. ``caretaker`` imports,
    2. the config YAML parses on *this* caretaker version,
    3. the version-pin file is present and looks like a semver,
    4. every env var declared by an enabled config block is set.

    No GitHub calls, no external-service probes. Designed to be wired
    in as a workflow step that runs *before* the full ``caretaker
    doctor`` call so operators see a clear actionable row instead of a
    bare "workflow file issue" or a swallowed ImportError.
    """
    env = env if env is not None else dict(os.environ)
    report = DoctorReport()

    report.add(check_import_ok())

    config_row, loaded = check_config_parse(config_path)
    report.add(config_row)

    report.add(check_version_pin(pin_path if pin_path is not None else DEFAULT_VERSION_PIN_PATH))

    # Skip env-var checks if the config didn't load — the references
    # collector walks the model, so without a model the rows would be
    # meaningless. The config-file FAIL row already tells the operator
    # what to fix first.
    if loaded is not None:
        for result in check_bootstrap_env_secrets(loaded, env):
            report.add(result)

    return report


# ── Rendering ──────────────────────────────────────────────────────────


def render_table(report: DoctorReport) -> str:
    """Render the report as a plain-text table suitable for stderr.

    We deliberately avoid external dependencies (rich, tabulate) so
    the preflight runs in the smallest possible environment.
    """
    if not report.results:
        return "(no checks ran)"
    header = ("CATEGORY", "NAME", "SEVERITY", "DETAIL")
    rows = [header]
    for r in report.results:
        rows.append((r.category, r.name, r.severity.value, r.detail))
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    sep = "  "
    lines = []
    for i, row in enumerate(rows):
        lines.append(sep.join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            lines.append(sep.join("-" * widths[j] for j in range(len(header))))
    counts = report.summary_counts()
    lines.append("")
    lines.append(
        "summary: OK={ok} WARN={warn} FAIL={fail}".format(
            ok=counts["OK"], warn=counts["WARN"], fail=counts["FAIL"]
        )
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_VERSION_PIN_PATH",
    "CheckResult",
    "DoctorReport",
    "EnvReference",
    "Severity",
    "check_bootstrap_env_secrets",
    "check_config_parse",
    "check_env_secrets",
    "check_external_services",
    "check_github_scopes",
    "check_import_ok",
    "check_version_pin",
    "check_version_pin_published",
    "collect_env_references",
    "render_table",
    "run_bootstrap_check",
    "run_doctor",
    "run_doctor_sync",
    "run_llm_probe",
    "run_llm_probe_sync",
]
