# Model routing

The skill splits work across three tiers so the strongest model is spent only on judgement. The split is by *kind of work*, not by product name, so it carries over to any agent stack.

## Tiers

| Tier | Kind of work | Signs a task belongs here |
|---|---|---|
| **Planner** | judgement, dialogue, synthesis | the answer depends on weighing evidence, on what the client wants, or on wording a conclusion |
| **Worker** | multi-step execution with tools | there is a command to run, a file to produce, sources to collect and cite, an error to fix |
| **Light** | one lookup or one mechanical check | a single fact with a source, a format check, a conversion, a one-page summary |

## Stage → tier

| Step | Tier | Notes |
|---|---|---|
| Locate toolkit, `check_env.py` | Light | report the route lists only |
| Interview, HS-code confirmation, checkpoints A/B/C | Planner | never delegated |
| HS description lookup, FX rate with citation, port/LOCODE lookup | Light | return value + source + date |
| `trade_data.py plan / fetch / normalize`, connector pulls, CSV validation | Worker | return the coverage report, not the rows |
| `analyze_trade.py` | Worker | return the printed summary |
| Reading `analysis.json`, shortlist advice, mirror-gap interpretation | Planner | |
| Freight: collecting quotes from the user | Planner (it is dialogue) | |
| Freight: connector calls, published-rate search, calibration points, `estimate_freight.py` | Worker | one subagent for all lanes; Light for a single missing port or charge |
| Tariff lookups with citations | Worker | one subagent per importer for export studies with many markets |
| Deciding which preference applies, reading rules of origin | Planner | |
| `build_financial_model.py`, rerun after changes | Worker | return the per-lane table |
| Writing `narrative.json` | Planner | never delegated |
| `build_report.py`, `build_dashboard.py`, `validate_study.py` | Worker | return `qa.json` headline |
| Final read-through of the three files | Planner | |

## Delegation prompt (template)

```
You are the <Worker|Light> for one stage of a trade study.
Study folder: <path>. Toolkit: <path>.
Read: <toolkit>/references/contracts.md section <n> (and <guide>.md).
Do: <exact commands / the lookup>.
Rules: never invent or default a number; a value you cannot source stays missing; cite URL + date for anything from the web.
Reply in at most 10 lines: files written, what is missing, anything surprising. Do not paste data.
```

## Host mapping

| Host | Planner | Worker | Light | How |
|---|---|---|---|---|
| Claude (chat, desktop, Claude Code) | session model | `sonnet` subagent | `haiku` subagent | Agent/Task tool with a `model` argument; run independent workers in parallel |
| Codex, OpenCode, Antigravity and similar | strongest reasoning model | default coding model | smallest/fastest model | the host's sub-agent or model-switch feature; if none, switch model per stage or run everything in one model |
| Orchestrated stacks (e.g. a router agent plus task agents) | the client-facing/orchestrating agent | task agents | utility agent | each agent takes the stages in its tier; the study folder is the hand-off, `validate_study.py --stage N` is the gate |

Machine-readable copy for orchestrators:

```json
{
  "tiers": {"planner": "strongest", "worker": "standard", "light": "fast"},
  "stages": [
    {"id": 0, "name": "environment", "tier": "light", "writes": ["env.json"]},
    {"id": 1, "name": "scope", "tier": "planner", "writes": ["inputs.json"], "checkpoint": "A"},
    {"id": 2, "name": "trade_data", "tier": "worker", "writes": ["query_plan.json", "flows.csv", "flows_meta.json"]},
    {"id": 3, "name": "analysis", "tier": "worker", "writes": ["analysis.json"]},
    {"id": 3.5, "name": "shortlist", "tier": "planner", "writes": ["selection.json"], "checkpoint": "B"},
    {"id": 4, "name": "freight", "tier": "worker", "writes": ["freight_inputs.json", "freight.json"]},
    {"id": 5, "name": "tariff", "tier": "worker", "writes": ["tariff.json"]},
    {"id": 6, "name": "financial", "tier": "worker", "writes": ["financial.json", "financial_model.xlsx"], "checkpoint": "C"},
    {"id": 7, "name": "narrative", "tier": "planner", "writes": ["narrative.json"]},
    {"id": 7.5, "name": "outputs", "tier": "worker", "writes": ["study.docx", "dashboard.html", "qa.json"]}
  ]
}
```

## When not to delegate

- The host cannot spawn subagents or pick models: run everything in order in the current model.
- A step is smaller than the prompt needed to delegate it.
- Anything said to the user, and the narrative: these stay with the planner.
