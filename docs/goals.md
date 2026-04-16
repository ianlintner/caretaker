# Goal Engine

The goal engine is an experimental feature that adds quantitative, goal-based agent prioritization to caretaker's orchestration.

## Overview

Instead of running agents in a fixed order, the goal engine:

1. Evaluates measurable repository health goals
2. Scores each goal from 0.0 (unmet) to 1.0 (satisfied)
3. Detects divergence and critical states
4. Prioritizes agents that can improve the worst-scoring goals
5. Tracks goal history for trend analysis

**Status:** Experimental (disabled by default)

## Configuration

Enable the goal engine in your config:

```yaml
goal_engine:
  enabled: true # Enable goal evaluation
  goal_driven_dispatch: true # Reorder agents by goal impact
  divergence_threshold: 3 # Runs before triggering divergence alert
  stale_threshold: 5 # Runs before marking goal data as stale
  max_history: 20 # Maximum history snapshots per goal
```

## Defined Goals

### CI Health Goal

**ID:** `ci_health`

**Measures:** Whether CI pipelines are green on the default branch and open PRs.

**Score calculation:**

- Counts PRs with passing CI vs. total open PRs
- Checks default-branch CI status
- Weight: 2.0 (high priority)

**Contributing agents:** `pr`, `devops`

**Thresholds:**

- Satisfied: ≥ 0.95 (95%+ PRs passing)
- Critical: ≤ 0.3 (30%+ PRs failing)

### PR Lifecycle Goal

**ID:** `pr_lifecycle`

**Measures:** How efficiently PRs move from open to merged.

**Score calculation:**

- Tracks PR state progression (discovered → CI passing → reviewed → merged)
- Assigns progress weights to each state
- Higher scores for PRs closer to merge

**Contributing agents:** `pr`, `dependency`

**Thresholds:**

- Satisfied: ≥ 0.95
- Critical: ≤ 0.3

### Security Posture Goal

**ID:** `security_posture`

**Measures:** How many unresolved security findings exist.

**Score calculation:**

- Counts open security alerts (Dependabot, code scanning, secret scanning)
- Weights by severity (critical > high > medium)
- Lower alert count = higher score

**Contributing agents:** `security`, `dependency`

**Thresholds:**

- Satisfied: ≥ 0.95 (minimal open alerts)
- Critical: ≤ 0.3 (many unresolved alerts)

### Self-Health Goal

**ID:** `self_health`

**Measures:** Whether caretaker's own workflows are succeeding.

**Score calculation:**

- Tracks recent caretaker workflow run success rate
- Recent failures reduce score significantly
- Weight: 1.5 (important but not highest)

**Contributing agents:** `self_heal`

**Thresholds:**

- Satisfied: ≥ 0.95
- Critical: ≤ 0.3

## Goal Evaluation

The goal engine evaluates goals during each orchestrator run:

```python
# Pseudocode flow
for goal in registered_goals:
    snapshot = await goal.evaluate(state, context)
    history.append(snapshot)

    if snapshot.score <= goal.critical_threshold:
        status = CRITICAL
    elif snapshot.score >= goal.satisfaction_threshold:
        status = SATISFIED
    else:
        status = DIVERGING if trend_is_worsening else IN_PROGRESS
```

## Goal-Driven Dispatch

When `goal_driven_dispatch: true`, the orchestrator:

1. Evaluates all goals before running agents
2. Identifies the worst-scoring goals
3. Determines which agents contribute to those goals
4. Reorders agent execution to prioritize high-impact work

Example:

```
Normal order: [pr, issue, devops, security, ...]
Goal-driven:  [devops, pr, security, ...] # CI health critical
```

## Divergence Detection

The engine detects when goals are diverging (getting worse):

- Compares recent scores against history
- Triggers after `divergence_threshold` consecutive declines
- Marks goal as `DIVERGING` in state
- Can trigger escalation to humans

## Benefits

**Adaptive priorities:** Agents run when they matter most, not on a fixed schedule.

**Observable health:** Quantitative scores make repo health visible at a glance.

**Trend detection:** Historical tracking catches problems before they become critical.

**Smart escalation:** Diverging goals trigger human intervention automatically.

## Limitations

**Experimental:** API may change as we learn from real-world usage.

**Score tuning:** Goal scoring functions may need adjustment per repo.

**Overhead:** Goal evaluation adds computation to each orchestrator run.

**New concept:** Most users should start with the default agent ordering.

## When to Enable

Consider enabling the goal engine if:

- Your repo has complex, competing priorities
- You want data-driven agent dispatch
- You need visible health metrics
- You're comfortable with experimental features

Start with `enabled: true` but `goal_driven_dispatch: false` to observe goal scores without changing agent order.

## Future Directions

Potential enhancements:

- Custom goal definitions per repo
- Goal-based escalation rules
- Historical trend visualization
- Machine-learning score prediction
- Integration with external metrics (deploys, incidents, etc.)

## See Also

- [Agent overview](agents.md) — what each agent does
- [Architecture](architecture.md) — how the goal engine fits in
- [Configuration](configuration.md) — full config reference
