---
name: trade-study-planner
description: Builds an export or import feasibility study for any product and any country — trade-flow analysis, freight, tariffs and a financial case — delivered as a Word study, an Excel model and an interactive dashboard. Use whenever someone wants to evaluate selling a physical product abroad or sourcing one from abroad, e.g. "export study", "import study", "market entry analysis", "where should I export X", "where should I source Y", "landed cost", "sourcing analysis", "trade feasibility", "should I export X to Y" — even if they never say "study".
---

# Trade Study Planner

Produces a client-ready feasibility study for one product (an HS code) and one home country, from either side of the trade:

| Perspective | The question | Partners are | Shortlist tiers |
|---|---|---|---|
| `export` | Where should we sell, and does it pay? | destination markets | Defend · Expand · Open |
| `import` | Where should we source, and what does it land at? | supplier countries | Keep · Develop · New source |

Deliverables, all built from the same checked data: **`study.docx`** (the argument), **`financial_model.xlsx`** (the editable working model), **`dashboard.html`** (interactive: rankings, partner detail, live what-if). Two depths: `quick_scan` (market or supplier scan plus dashboard, no financials) and `full_study`.

The work runs as seven stages that hand fixed-name JSON files to each other inside one study folder. `references/contracts.md` defines every file; read it before stage 2.

## Rules that are never broken

1. **No invented numbers.** Every figure is a *sourced value* with a tier: `user`, `quote`, `connector`, `published`, `modelled`, `derived` — or it is `missing` and stays blank. Scripts stop (`BLOCKED:`) or mark a lane `incomplete` rather than fill a gap. You do the same: no "typical", no "approximately", no placeholder.
2. **The scripts are canonical.** Run them; do not redo their arithmetic in prose and do not rewrite them ad hoc. If a script fails, fix the input or the script, then rerun.
3. **Confirm the HS code with the user** against its official description before pulling data. A wrong code makes the whole study wrong.
4. **Say which year is partial.** `analysis.json` decides the latest complete year from the data; never hard-code years.
5. **Tariffs are cited or marked "verify".** Never state a duty rate from memory. Trade rules change faster than this skill.
6. **Mirror gaps are shown, not resolved by picking a side.**
7. **Modelled freight is an estimate, never a quote**, and exists only when fitted to dated, cited benchmarks.
8. **Nothing is silently dropped.** A section that cannot be filled says so and says what would fill it.
9. **Client-facing tone:** neutral, specific, no internal jargon, no generic advice ("leverage digital marketing").

## Model routing — spend the strong model only on judgement

The owner of this skill asks for three tiers so usage limits last. Treat this as the user's standing instruction to delegate wherever the host allows it.

| Tier | Does | In Claude | Elsewhere |
|---|---|---|---|
| **Planner** | interview and checkpoints, HS-code judgement, reading the analysis, shortlist advice, interpreting tariffs and mirror gaps, writing `narrative.json`, final QA sign-off | the main session model (strongest available) | strongest reasoning model |
| **Worker** | running scripts, fetching and normalising data, collecting freight and tariff figures with citations, building the three files, fixing script errors | subagent, model `sonnet` | standard coding model |
| **Light** | single lookups and mechanical checks: a port code, an FX rate with its source, unit conversions, validating a file, summarising one web page | subagent, model `haiku` | fastest model |

How to delegate: give the subagent the study folder path, the stage's section of `references/contracts.md`, the exact commands, and "reply in at most 10 lines: files written, what is missing, anything surprising". Keep raw data out of the planner's context — read the script summaries and JSON headlines, not the row dumps. Never delegate a conversation with the user or the narrative. If the host cannot spawn subagents or choose models, run the same steps in order in the current model. If a subagent stops on a usage limit, resume it once the limit resets rather than starting a new one. Stage-by-stage map and prompts: `references/model_routing.md`.

## Step 0 — Locate the toolkit and check the environment

The toolkit is the `scripts/`, `references/` and `assets/` folders that ship with this file. Some hosts sync only `SKILL.md`. If `scripts/` is not next to this file:

1. Look for an attached `trade-study-planner*.zip` and unzip it.
2. Else clone it: `git clone --depth 1 https://github.com/wessamzidan/trade-study-planner` *(toolkit source — edit this line if the repository moves)*.
3. Else ask the user to attach the toolkit zip. Do not re-implement the scripts.

Then, with `TSP` = toolkit root and `STUDY` = a new folder `studies/<study_id>/` in the working directory:

```bash
pip install -r $TSP/requirements.txt        # pandas, openpyxl, python-docx, searoute, matplotlib
python3 $TSP/scripts/check_env.py --study-dir $STUDY
```

`env.json` lists which routes work here for trade data, tariffs and freight. Use the first route in each list; do not ask the user for an API key that this environment cannot use.

## Asking the user

This skill is interactive. Use the host's structured-question tool when there is one (multiple choice with a recommended option first); otherwise ask short numbered questions. At most four questions per round. Never ask for something you can look up (an HS description, a port name, an exchange rate) — look it up, show it, and ask for confirmation. If the user is not present (scheduled or unattended run), take the most reasonable reading, record it under `notes` in the relevant JSON, and stop only at a choice that cannot be undone.

## Workflow

### Stage 1 — Scope (Planner) → `inputs.json` · **Checkpoint A**

Round 1 — establish:
- **Perspective:** selling abroad (`export`) or buying from abroad (`import`).
- **Home country** and the **product** in the user's words.
- **Depth:** `quick_scan` or `full_study`.

Then find the 6-digit HS code from the description (search if unsure), show the code with its official description, and get an explicit yes. Mention that national 8- or 10-digit lines can carry different duties.

Round 2 — commercial inputs (full study only). Explain once why they are needed: without the client's own numbers the result is a market scan, not a business case.

| Export | Import |
|---|---|
| cost per unit, and whether it is ex-works or FOB | supplier price per unit and its incoterm (per supplier if known) |
| unit, units per container, container type (dry / reefer, 20' / 40') | unit, units per container, container type |
| selling incoterm; current or target price per market (optional) | local selling price (optional) |
| target margin % | target margin % |
| cost and price currencies, FX rate | purchase and selling currencies, FX rate |
| payment terms granted, advance % | supplier terms, advance %, customer credit days, days of stock |
| production lead time, fixed export costs, planned containers per year | clearance days, fixed sourcing costs, planned containers per year |

Also ask for markets or suppliers of interest, existing ones, and any to exclude. Record every answer as a sourced value (`tier: "user"`, with the date). Anything not given is `missing` — do not fill it. If the user will not share costs, set `"commercial": null`, run a trade-only study and say the financial case is deferred.

**Checkpoint A:** play back perspective, country, product + HS code, depth and the list of missing inputs; proceed on a yes.

### Stage 2 — Trade data (Worker) → `flows.csv`

```bash
python3 $TSP/scripts/trade_data.py plan --study-dir $STUDY          # writes query_plan.json, prints every query three ways
```

Fill `raw/` by the first available route (`env.json → routes.trade_data`), details in `references/trade_data_guide.md`:

1. **`api`** — `trade_data.py fetch` with the user's UN Comtrade key in `COMTRADE_SUBSCRIPTION_KEY` (free keys exist; never print or store the key).
2. **`connector`** — if a Comtrade or other trade-data connector/MCP is connected, run each query in the plan through it and save the rows as generic-rows JSON in `raw/`.
3. **`csv_upload`** — walk the user through each query's `ui_steps`; they upload the CSVs.

```bash
python3 $TSP/scripts/trade_data.py normalize --study-dir $STUDY     # coverage report; stops on wrong HS code or missing home flows
```

Tell the user in two lines what was loaded and what is absent. Never commit or redistribute raw Comtrade extracts.

### Stage 3 — Analysis (Worker runs, Planner reads) → `analysis.json` · **Checkpoint B**

```bash
python3 $TSP/scripts/analyze_trade.py --study-dir $STUDY
```

Read the summary and `analysis.json`: home's position, the ranking, tiers and scores, mirror checks, warnings. The score is a ranking aid — apply judgement (sanctions, the client's contacts, logistics sense) and say when you overrule it. Partners that did not report in the latest complete year are left unscored.

**Checkpoint B:** show the shortlist as a compact table (tier, partner, size, growth, home's current flow, score, one-line reason) and ask the user which partners to take forward (suggest 5–8; they may add or drop). Write `selection.json`. For `quick_scan`, skip to stage 7.

Then pull the detail for the chosen partners and re-run:

```bash
python3 $TSP/scripts/trade_data.py plan --study-dir $STUDY --partners SAU,DEU,…   # adds partner_detail queries
# fetch / connector / CSV for the new queries, then normalize and analyze again
```

### Stage 4 — Freight ladder (Worker) → `freight.json`

```bash
python3 $TSP/scripts/estimate_freight.py init --study-dir $STUDY    # lanes, default ports, distances, what is needed
```

Collect rates top-down, per `references/freight_ladder.md`. Every figure keeps its rung:

1. **`quote`** — ask whether the client has forwarder or carrier quotes for any lane; these always win.
2. **`connector`** — a live rate tool if one is connected. If `env.json` shows the Freightos host reachable: with a key, run `estimate_freight.py freightos`; without one, tell the user a free Freightos Rate Estimate key covers 10 requests a day — enough for one study of up to 10 lanes — and let them decide.
3. **`published`** — a cited public rate for that lane.
4. **`modelled`** — distance-based estimate fitted to at least one dated, cited benchmark (`add-calibration`), with a range.
5. Otherwise the lane is a **coverage gap** and the study says so.

Local charges (origin/destination THC, documents, inland, clearance) follow the same rule: quote, cited tariff page, user — or missing.

```bash
python3 $TSP/scripts/estimate_freight.py build --study-dir $STUDY
```

### Stage 5 — Tariffs and market access (Worker collects, Planner interprets) → `tariff.json`

```bash
python3 $TSP/scripts/tariff_tool.py init --study-dir $STUDY         # one lane per selected partner, everything missing
python3 $TSP/scripts/tariff_tool.py set-rate --study-dir $STUDY --lane SAU --field duty_rate_pct --value … --tier published --source "…" --url … --as-of …
python3 $TSP/scripts/tariff_tool.py check --study-dir $STUDY        # sourced vs verify, and what is still missing
```

For each lane record the duty that applies to *this* origin–destination pair, the agreement behind any preference and its rules of origin, other duties, the customs valuation basis, import VAT, and non-tariff requirements. Sources and lookup order: `references/tariff_access_guide.md`. Every rate carries a URL or a named official schedule and a date; otherwise the rate is `missing` and the lane's `status` is `verify`. Finish each lane with a dated search for recent changes to that importer's duties. For an import study there is one importer (home), so one schedule serves every lane.

### Stage 6 — Financial model (Worker) → `financial.json`, `financial_model.xlsx` · **Checkpoint C**

```bash
python3 $TSP/scripts/build_financial_model.py --study-dir $STUDY
```

Method: `references/financial_methodology.md`. The model is incoterm-aware, handles both perspectives, and marks a lane `incomplete` when one of the user's own cost items is missing.

**Checkpoint C:** show the per-lane table (status, price basis, margin %, break-even, what is missing) and ask the user to fill the gaps that matter or accept them as open. Rerun after any change.

### Stage 7 — Narrative, files, QA (Planner writes; Worker builds)

1. **Planner writes `narrative.json`** from the JSON files, following `references/study_structure.md`: a headline, three prioritised recommendations, a financial verdict per lane, the partners *not* prioritised and why (ISO3 codes), risks, and dated next steps. Every number quoted must exist in the JSON files. Hedge where the tier is weak.
2. Build and check:

```bash
python3 $TSP/scripts/build_report.py --study-dir $STUDY
python3 $TSP/scripts/build_dashboard.py --study-dir $STUDY
python3 $TSP/scripts/validate_study.py --study-dir $STUDY --stage 7   # must pass; read qa.json
```

3. **Planner QA:** open the Word file and the dashboard, read the executive summary against `financial.json`, and check the verification checklist is honest.
4. **Deliver** `study.docx`, `financial_model.xlsx` and `dashboard.html`. Where the host can publish an HTML page (for example as an artifact), publish the dashboard; otherwise send the file. In the message: the headline, the three files, and how many items are on the verification checklist. Nothing else.

## Stage map

| # | Stage | Tier | Reads | Writes |
|---|---|---|---|---|
| 0 | Toolkit + environment | Light | — | `env.json` |
| 1 | Scope | Planner | user | `inputs.json` |
| 2 | Trade data | Worker | `inputs.json` | `query_plan.json`, `raw/`, `flows.csv` |
| 3 | Analysis + shortlist | Worker / Planner | `flows.csv` | `analysis.json`, `selection.json` |
| 4 | Freight ladder | Worker (Light for lookups) | `selection.json` | `freight_inputs.json`, `freight.json` |
| 5 | Tariffs | Worker / Planner | `selection.json` | `tariff.json` |
| 6 | Financial model | Worker | all above | `financial.json`, `financial_model.xlsx` |
| 7 | Narrative + outputs + QA | Planner / Worker | all above | `narrative.json`, `study.docx`, `dashboard.html`, `qa.json` |

Each stage can be resumed or handed to another agent: the folder is the state.

## References

- `references/contracts.md` — every file, field and rule. The source of truth.
- `references/model_routing.md` — tier map, delegation prompts, mapping for other agent stacks.
- `references/trade_data_guide.md` — API, connector and CSV routes; limits; data traps.
- `references/freight_ladder.md` — collecting each rung; local charges; reefers; the modelled estimate.
- `references/tariff_access_guide.md` — where to look up duties and requirements, by importer.
- `references/financial_methodology.md` — the chain, incoterms, margins, sensitivity, cash cycle.
- `references/study_structure.md` — section-by-section guide for the narrative, both perspectives.

## Common failure modes

- **Wrong HS code** → confirm at checkpoint A; `normalize` stops if the files disagree with it.
- **Treating the score as the answer** → it ranks; the planner decides and explains.
- **Filling a gap to make the model run** → leave it missing; an honest `incomplete` lane beats a confident wrong margin.
- **Quoting a duty from memory** → cite or mark `verify`.
- **Asking for a key the sandbox cannot use** → read `env.json` first.
- **Dumping data into the planner's context** → delegate, and read summaries.
- **A long study nobody reads** → 12–18 pages; the dashboard and the workbook carry the detail.
