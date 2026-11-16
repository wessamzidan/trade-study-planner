# Maintainer guide — Trade Study Planner

For Wessam / the AlRawaabit team: how to publish the skill, keep GitHub and Forgejo in step, release new versions, and where to take it next.

---

## 1. First push to GitHub

The repository `github.com/wessamzidan/trade-study-planner` already exists and is empty. On your machine:

```bash
unzip trade-study-planner-v2.zip
cd trade-study-planner
git init -b main
git add .
git status                      # check: no studies/, no env.json, no client files
git commit -m "Trade Study Planner v2.0.0: global export/import studies, freight ladder, dashboard"
git remote add origin https://github.com/wessamzidan/trade-study-planner.git
git push -u origin main
```

GitHub no longer accepts your password for `git push`. Use one of:
- **GitHub CLI** (simplest): install `gh`, run `gh auth login` once, then push normally.
- **Personal access token**: GitHub → Settings → Developer settings → Fine-grained tokens. Give it *Contents: read and write* on this repository only, and paste it when Git asks for a password.
- **SSH key**: `git remote set-url origin git@github.com:wessamzidan/trade-study-planner.git`.

The skill clones from this URL when only `SKILL.md` is installed, so keep the repository **public** and keep the name. If you rename it, update the URL in `SKILL.md` (Step 0) and in `README.md`.

## 2. Forgejo: two options

### Option A — Forgejo mirrors GitHub (recommended)

GitHub stays the place you push to. Forgejo pulls a copy on a schedule, so the two can never disagree.

1. In Forgejo: **+ → New migration → GitHub**.
2. URL: `https://github.com/wessamzidan/trade-study-planner`. Tick **This repository will be a mirror**. A token is only needed for private repositories.
3. Set the mirror interval (for example 8h). **Synchronise now** forces an update.

A mirror is read-only in Forgejo. Do your work on GitHub, or locally and push to GitHub.

### Option B — push to both with one command

Use this when Forgejo is your main home, or GitHub might be unreachable:

```bash
git remote set-url --add --push origin https://github.com/wessamzidan/trade-study-planner.git
git remote set-url --add --push origin https://<your-forgejo-host>/<user>/trade-study-planner.git
git remote -v        # origin now has one fetch URL and two push URLs
git push             # pushes to both
```

Create the empty repository in Forgejo first (no README, so histories don't clash). Forgejo accepts an access token as the password: **User settings → Applications → Generate token**, with repository write scope.

### Private material

Client studies, real cost figures and Comtrade extracts never go into this repository. `.gitignore` already excludes `studies/`, `raw/` folders and `env.json`. Keep client work in a separate **private** Forgejo repository (for example `alrawaabit/trade-studies`), with one folder per study.

## 3. Releasing a version

```bash
python3 tests/run_all.py                         # everything must pass
python3 tools/package_skill.py                   # builds dist/trade-study-planner-skill.zip
git add -A && git commit -m "v2.1.0: <what changed>"
git tag v2.1.0 && git push && git push --tags
gh release create v2.1.0 dist/trade-study-planner-skill.zip --title "v2.1.0" --notes "<what changed>"
```

- Version numbers: **major** for an incompatible change to `references/contracts.md` (old study folders no longer load); **minor** for new features; **patch** for fixes.
- After a release, re-upload the zip in Claude (**Customize → Skills**) so the chat version matches. Agents that cloned the repository update with `git pull`.
- `dist/` is git-ignored; the zip lives on the release page.

## 4. How the project is built (read before changing anything)

- `references/contracts.md` is the source of truth. Change the contract first, then the scripts, then the tests.
- `scripts/model_core.py` is the only place the financial math lives. The Excel formulas and the dashboard's JavaScript copy are tested against it (`test_financial.py` and `test_dashboard.py` parity checks). If you change a formula, all three must change, and the tests show you where they disagree.
- **No defaults rule.** Never add a fallback number such as `.get("freight", 1500)`. A missing input stays missing.
- **No year literals, no country literals in logic.** Countries are ISO3 codes from `scripts/data/countries.json`; ports come from `scripts/data/ports.json` (rebuild with `scripts/build_ports.py`).
- Tests use synthetic data only (`tests/make_fixture.py`, files named `SYNTHETIC_*`).

To improve it with Claude: open a Claude Code session on the repository and say, for example, "Read references/contracts.md, then add monthly seasonality to analyze_trade.py; keep tests passing and add tests for it." Keep the model split: the strong model plans and reviews, Sonnet implements, Haiku does lookups.

## 5. Status: what has and has not been tested (November 2026)

| Area | Tested | Not yet tested |
|---|---|---|
| Trade data | Normalising Comtrade CSV, API JSON and connector rows; years; mirror checks; scoring — 93 checks on synthetic data | A **live** Comtrade API call with a real key (the build sandbox blocked the API) |
| Freight | Ladder logic, ports, modelled estimate, Freightos call budget — 81 checks with a mocked API | A **live** Freightos call |
| Tariffs | Recording, citation rules, status | WITS API calls (documented, not automated) |
| Financial model | 333 checks: hand-computed cases for every incoterm, missing-data behaviour, Excel matches Python in 27 workbooks | — |
| Dashboard | 198 checks: JavaScript matches Python in 998 cases, screenshots at desktop and phone widths | — |
| Report and QA | 59 checks: both demos build; QA catches invented numbers | — |
| End to end | Two demo studies (export from Egypt, import into Saudi Arabia) on synthetic data | **A real study with real data**: the most important next test |

## 6. Roadmap — improvements in priority order

### Now (before using it with a client)

1. **Run one real study and compare.** Re-run a study you already know well (Il Mondo citrus or the frozen broccoli study) with real Comtrade data and real costs. Compare the shortlist and margins with v1 and with what actually happened. Record what the skill got wrong in GitHub Issues.
2. **Live API smoke test** from your own computer: one Comtrade call with your key, one Freightos call, one WITS lookup. Fix whatever the real responses break.
3. **Shorten the Word study** (currently about 21 pages on the demo): move Appendix A (all sources) into the Excel model and keep only the verification checklist in Word.
4. **Turn on CI**: `.github/workflows/tests.yml` runs the tests on every push. Forgejo Actions can run the same file if a runner is installed.

### Next (makes the decision better)

5. **Seasonality.** Pull monthly Comtrade data (frequency `M`) to show when each market imports and when competitors ship. This matters most for fresh produce: the best market is often the one whose gap matches your harvest.
6. **Better price evidence.** Comtrade unit values are averages across qualities. Add an optional `price_benchmarks` input (wholesale market reports, buyer quotes) and show both.
7. **Score you can tune.** Let the user set the weights (size, growth, price, foothold, openness) at checkpoint B. Add optional components for distance or freight cost per kg, tariff disadvantage versus competitors, and payment risk.
8. **Product baskets.** Several HS codes in one study (for example a frozen vegetables range), with a combined shortlist.
9. **National tariff lines.** Record 8- and 10-digit lines and their duties where they differ from the 6-digit average.
10. **Arabic and bilingual output.** Right-to-left Word study and dashboard, reusing the approach of the bilingual proposal builder.

### Later (turns it into a product)

11. **Buyer discovery hand-off.** The study ends at "which markets". Add an export of the shortlist to the lead-generation agent: target market, product, buyer profile and the price window.
12. **An MCP server for the toolkit**, so n8n, Hermes or ERPNext can trigger a study stage and read results without a chat.
13. **Benchmark library.** A dated, cited file of freight benchmarks and local charges that the team updates monthly. It improves the modelled freight rung, and the skill refuses entries older than a set age.
14. **Caching.** Store Comtrade query results by query, so a second study on the same HS code costs no API calls.
15. **Client portal view.** Publish the dashboard per client with comments, while the workbook stays with the team.

### Ideas to test before building

- A one-page "go / no-go" summary for the busy-CEO buyer persona, placed in front of the full study.
- A scheduled monthly refresh of a client's study, with an alert when a market's imports or a tariff change materially.
