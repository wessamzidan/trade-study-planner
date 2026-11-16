# Trade Study Planner

An agent skill that builds an **export or import feasibility study** for any product and any country, and hands back three files:

| File | What it is | Who reads it |
|---|---|---|
| `study.docx` | The written study: findings, shortlist, financial case, risks, next steps, sources | Decision-maker |
| `financial_model.xlsx` | Editable model with live formulas, scenarios, payback, provenance | Finance |
| `dashboard.html` | Interactive dashboard: market ranking, partner detail, live what-if sliders | Everyone; works offline |

It answers two questions from the same data:

- **Export:** *Where should we sell this product, and does it pay?* Markets are ranked into **Defend / Expand / Open**, and the margin per container is modelled for each.
- **Import:** *Where should we buy it, and what does it land at?* Suppliers are ranked into **Keep / Develop / New source**, and the landed cost and margin are modelled for each.

**Every number carries its source.** A figure is tagged `user`, `quote`, `connector`, `published`, `modelled` or `derived`, or it is `missing` and left blank. The skill never fills a gap with a guess. A lane it cannot cost is shown as **incomplete**, with the missing items listed.

---

## 1. Install

The skill is the whole folder: `SKILL.md` plus `scripts/`, `references/` and `assets/`. Install the folder, not just `SKILL.md`.

### Claude (claude.ai web and desktop)

1. Download the skill zip: `dist/trade-study-planner-skill.zip` from the latest release, or build it with `python3 tools/package_skill.py`.
2. In Claude go to **Customize → Skills → + → Create skill → Upload a skill** and upload the zip ([Claude help: Use skills](https://support.claude.com/en/articles/12512180-use-skills)).
3. Turn the skill on. Code execution must be enabled for your account.

If only `SKILL.md` arrives (for example, when the skill was saved from a chat), the skill clones the scripts from this repository on first use. Nothing else is needed, provided the sandbox can reach GitHub.

### Claude Code

```bash
# personal (all projects)
git clone https://github.com/wessamzidan/trade-study-planner ~/.claude/skills/trade-study-planner
# or per project (commit it so your team gets it)
git clone https://github.com/wessamzidan/trade-study-planner .claude/skills/trade-study-planner
pip install -r ~/.claude/skills/trade-study-planner/requirements.txt
```

### Other agents

All of these read the same `SKILL.md` format. Clone the repository into the skills folder:

| Agent | User-level folder | Project-level folder |
|---|---|---|
| OpenAI Codex | `~/.agents/skills/` | `<repo>/.agents/skills/` |
| OpenCode | `~/.config/opencode/skills/` (also reads `~/.claude/skills`, `~/.agents/skills`) | `.opencode/skills/` |
| Google Antigravity | `~/.gemini/config/skills/` | `<workspace>/.agents/skills/` |
| Hermes Agent | `~/.hermes/skills/` (or `hermes skills install <source>`) | `.hermes/skills/` or `.agents/skills/` (run `hermes skills trust`) |

```bash
git clone https://github.com/wessamzidan/trade-study-planner <skills-folder>/trade-study-planner
pip install -r <skills-folder>/trade-study-planner/requirements.txt
```

Folder locations were taken from each tool's documentation in November 2026; check your tool's docs if a path has moved.

### Prompt that makes any agent install it

```
Install the agent skill at https://github.com/wessamzidan/trade-study-planner:
clone it into your skills folder as "trade-study-planner", run
`pip install -r requirements.txt` inside it, then run
`python3 tests/run_all.py` and `python3 scripts/check_env.py --study-dir /tmp/tsp-check`
and tell me which data routes are available.
```

### Requirements

- Python 3.10 or newer: `pandas`, `openpyxl`, `python-docx` (required); `searoute`, `matplotlib` (recommended).
- Optional: LibreOffice, to recalculate and check the Excel model; Node, to run the dashboard tests.

---

## 2. Use

Ask in plain language. The skill starts on requests like:

- "Export study for frozen strawberries from Egypt"
- "Where should we sell our olive oil? We're in Tunisia."
- "Import study: sourcing IQF broccoli into Saudi Arabia"
- "Quick scan of markets for HS 080510"

The skill runs as a guided conversation with three checkpoints:

| Step | What happens | What you provide |
|---|---|---|
| **A. Scope** | Agree export or import, the country, the product and its **HS code**, and the depth (quick scan or full study) | Confirm the HS code. For a full study, give your costs, prices, container, payment terms and planned volume |
| Trade data | The skill pulls UN Comtrade data by the best available route (see below) | A Comtrade key, a connector, or CSV downloads it walks you through |
| **B. Shortlist** | Ranked markets or suppliers, each with a score and a reason | Choose 5–8 partners to model |
| Freight and tariffs | Freight per lane via the ladder; duties and requirements with citations | Any forwarder quotes you have |
| **C. Financials** | Margin, break-even, cash cycle and payback per lane | Fill the gaps that matter, or accept them as open |
| Delivery | Word study, Excel model and dashboard, plus a verification checklist | Review the checklist before sharing |

**Quick scan** stops after the shortlist and gives you the dashboard. Use it to see whether a full study is worth doing.

### Where the data comes from

| Data | Routes, best first |
|---|---|
| Trade flows | Your UN Comtrade key (free tier) → a connected Comtrade connector/MCP → guided CSV download from comtradeplus.un.org |
| Freight | Your forwarder's quote → a rate tool (a Freightos Rate Estimate key is free at 10 requests/day, about one study) → a cited published rate → a distance-based estimate fitted to dated benchmarks → reported as a gap |
| Tariffs | WITS API or the importer's official tariff finder, always cited and dated; otherwise marked **verify** |

The skill checks which routes work before asking you for anything (`scripts/check_env.py`).

### Reading the results

- **Score (0–100)** ranks partners by market size, growth, price level, your current foothold and supplier concentration. It is a ranking aid, not a verdict. A partner that does not report its trade is shown as *not scored*.
- **Required price**: when you don't give a selling price for a market, the model reports the price needed to hit your target margin. Compare it with the market's average import price, shown as the *price gap*.
- **Mirror check**: compares what you report sending with what the partner reports receiving. Large gaps usually mean hub routing or re-exports.
- **Verification checklist** (Appendix A of the study and the Sources tab of the dashboard): everything not from you or a written quote. Check these before quoting a customer.

### What it does not do

It does not forecast demand, find named buyers, give tax or legal advice, or model DDP terms. Modelled freight is not a quote, and tariffs must be re-verified before a commercial offer.

---

## 3. Running it by hand (any agent, no chat)

Each stage reads and writes fixed-name files in one study folder. `references/contracts.md` defines them all.

```bash
S=studies/my-study
python3 scripts/check_env.py --study-dir $S
# write $S/inputs.json (see tests/demo_export/inputs.json)
python3 scripts/trade_data.py plan --study-dir $S
python3 scripts/trade_data.py fetch --study-dir $S        # or save connector/CSV results into $S/raw/
python3 scripts/trade_data.py normalize --study-dir $S
python3 scripts/analyze_trade.py --study-dir $S
# write $S/selection.json
python3 scripts/estimate_freight.py init --study-dir $S   # then add-rate / set-charge / add-calibration
python3 scripts/estimate_freight.py build --study-dir $S
python3 scripts/tariff_tool.py init --study-dir $S        # then set-rate / set-info
python3 scripts/tariff_tool.py check --study-dir $S
python3 scripts/build_financial_model.py --study-dir $S
# write $S/narrative.json (see tests/demo_export/narrative.json)
python3 scripts/build_report.py --study-dir $S
python3 scripts/build_dashboard.py --study-dir $S
python3 scripts/validate_study.py --study-dir $S --stage 7
```

Try it on the demos, which use synthetic data: open `tests/demo_export/dashboard.html`, or rebuild with the commands above using `--study-dir tests/demo_export`.

---

## 4. Model routing

Judgement work (interview, shortlist, narrative, final QA) runs on the strongest model. Script runs and data collection go to a standard model, and single lookups to a fast one. In Claude these are the session model, `sonnet` and `haiku` subagents. See `references/model_routing.md` for other agent stacks.

## 5. Repository layout

```
SKILL.md                 the skill (planner instructions)
references/              contracts.md (source of truth) and one guide per stage
scripts/                 the toolkit; scripts/data/ holds countries.json and ports.json
assets/                  dashboard template
tests/                   synthetic fixtures, demo_export (home EGY), demo_import (home SAU), test suites
tools/package_skill.py   builds dist/trade-study-planner-skill.zip for upload
docs/MAINTAINER_GUIDE.md releasing, mirroring to Forgejo, and the improvement roadmap
```

Run all tests: `python3 tests/run_all.py`.

## 6. Data and licences

- UN Comtrade data may not be redistributed without UN permission. Study folders and raw extracts are git-ignored; each user brings their own key. Cite "UN Comtrade" with the extraction date.
- Demo folders contain synthetic data only.
- Port coordinates come from UN/LOCODE ([datasets/un-locode](https://github.com/datasets/un-locode)) and the `searoute` package.
- Code: Apache License 2.0. © AlRawaabit / Wessam Zidan.
