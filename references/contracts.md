# Contracts — Trade Study Planner (schema 2.0)

Every stage reads and writes fixed-name files in one **study folder**. Any agent, on any model, can pick up a stage if it follows this file. Scripts never guess: a missing input stays missing and is reported.

## 1. Conventions

- **Study folder** (`--study-dir DIR`, required by every script). Fixed file names:

| File | Written by | Stage |
|---|---|---|
| `env.json` | `check_env.py` | 0 |
| `inputs.json` | planner (interview) | 1 |
| `query_plan.json` | `trade_data.py plan` | 2 |
| `raw/*` | `trade_data.py fetch`, connector results saved by the agent, or user CSV uploads | 2 |
| `flows.csv`, `flows_meta.json` | `trade_data.py normalize` | 2 |
| `analysis.json` | `analyze_trade.py` | 3 |
| `selection.json` | planner + user (checkpoint B) | 3 |
| `freight_inputs.json` | worker (quotes, connector results, published rates, calibration points) | 4 |
| `freight.json` | `estimate_freight.py build` | 4 |
| `tariff.json` | worker (cited lookups) | 5 |
| `financial.json`, `financial_model.xlsx` | `build_financial_model.py` | 6 |
| `narrative.json` | planner | 7 |
| `study.docx` | `build_report.py` | 7 |
| `dashboard.html` | `build_dashboard.py` | 7 |
| `qa.json` | `validate_study.py` | any |

- **Exit codes:** `0` ok · `2` blocked (a required input is missing; message starts `BLOCKED:`) · `1` error.
- **Countries** are keyed by ISO 3166-1 alpha-3 everywhere (`EGY`). Names are display only. `W00` = World. Non-country areas get `_<code>` (e.g. `_899` Areas nes) and are kept in totals but never ranked. Resolve user text with `common.resolve_country()`.
- **Perspective:** `export` (home country sells; partners are destination *markets*) or `import` (home country buys; partners are *suppliers*). Field names are perspective-neutral: `home`, `partner`, `home_flow` (home's exports to / imports from the partner), `partner_trade` (the partner's total imports of the product for `export`; its total exports for `import`).
- **Trade values** are USD (`*_usd`). **Financial amounts** are in `commercial.currencies.model` (default USD), per container unless the name says `per_unit`.
- **Sourced value (SV)** — every commercial, freight and tariff number is an object:

```json
{"value": 250.0, "tier": "quote", "source": "Forwarder X quote ref 1182", "url": null, "as_of": "2026-11-10"}
```

  `tier` ∈ `user` (client told us) · `quote` (written quote) · `connector` (live rate/tariff tool) · `published` (cited public page or index) · `modelled` (computed estimate, method stated) · `derived` (arithmetic from other SVs) · `missing`.
  `value: null` ⇔ `tier: "missing"`. A genuine zero is `{"value": 0, "tier": "user", "source": "included in all-in quote"}`.
  **No script may turn a missing SV into a number.** Use `common.sv()`, `common.sv_missing()`, `common.sv_value()`.
- All JSON files carry `"schema_version": "2.0"`.

## 2. `inputs.json` (stage 1)

```json
{
  "schema_version": "2.0",
  "study_id": "frozen-veg-egy-2026-11",
  "prepared_for": "Client or null", "prepared_by": "Team or null", "date_prepared": "2026-11-13",
  "depth": "full_study",
  "perspective": "export",
  "home_country": "EGY",
  "product": {"name": "Frozen vegetables", "description": "IQF mixed vegetables", "hs_code": "071080",
              "hs_description": "Vegetables; n.e.c. in heading 0710, frozen", "hs_confirmed_by_user": true},
  "years": {"from": 2021, "to": 2026},
  "partners_of_interest": ["DEU"], "partners_existing": ["SAU"], "partners_excluded": [],
  "commercial": { ... } 
}
```

`depth`: `quick_scan` (stages 1–3 + dashboard) or `full_study`. `commercial: null` ⇒ trade-only study; stage 6 is skipped and the report says the financial case is deferred.

`commercial` (all numbers are SVs):

```json
{
  "unit": "kg",
  "container": {"type": "40RF", "units_per_container": SV, "net_kg_per_unit": SV},
  "currencies": {"model": "USD", "cost": "EGP", "price": "USD", "fx_cost_per_model": SV, "fx_price_per_model": SV},
  "insurance_rate_pct": SV,
  "target_margin_pct": SV,

  "cost": {"basis": "EXW", "per_unit": SV},
  "sell_incoterm": "FOB",
  "price_per_unit": {"default": SV, "by_partner": {"SAU": SV}},

  "buy_incoterm": "FOB",
  "purchase_price_per_unit": {"default": SV, "by_partner": {"CHN": SV}},
  "local_price_per_unit": SV,

  "payment": {"terms": "T/T 30 days after B/L", "credit_days": SV, "advance_pct": SV, "customer_credit_days": SV},
  "timing": {"production_lead_days": SV, "origin_dwell_days": SV, "clearance_days": SV, "inventory_days": SV},
  "fixed_costs": {"one_time": SV, "annual": SV},
  "volume": {"capacity_units_per_year": SV, "planned_containers_per_year": {"default": SV, "by_partner": {}}, "monthly_ramp": null}
}
```

- Export uses `cost`, `sell_incoterm`, `price_per_unit`. Import uses `buy_incoterm`, `purchase_price_per_unit`, `local_price_per_unit`. Unused keys may be absent.
- `container.type` ∈ `20DC 40DC 40HC 20RF 40RF`. `cost.basis` ∈ `EXW FOB`. Incoterms supported: `EXW FCA FOB CFR CIF DAP` (FCA is treated as FOB; DDP is not modelled — use DAP and say so).
- `fx_*_per_model` = units of that currency per 1 unit of model currency (1 if same currency, tier `derived`).
- `price_per_unit.by_partner[ISO3]` overrides `default`. A missing price is allowed: the model then reports the *required* price at `target_margin_pct`.
- `monthly_ramp`: list of 24 container counts, or `null`.

## 3. Trade data (stage 2)

### 3.1 `query_plan.json`

`trade_data.py plan` derives the plan from `inputs.json`. Each query has an `id`, a Comtrade parameter set, a purpose and the views it feeds. Annual, HS, the agreed code, years `from..to`.

| id | reporter | flow | partner | export perspective answers | import perspective answers |
|---|---|---|---|---|---|
| `home_flows` | home | X (export) / M (import) | all | where home sells | who home buys from |
| `world_imports` | all | M | World | market sizes | competing buyers, home's rank |
| `world_exports` | all | X | World | competing exporters, home's rank | supplier scale |
| `mirror` | all | M (export) / X (import) | home | what partners say they bought from home | what suppliers say they sold to home |
| `partner_detail:<ISO3>` | partner | M (export) / X (import) | all | supplier mix inside that market | that supplier's other buyers |
| `matrix` (optional) | all | X | all | replaces all `partner_detail` when one big file is feasible | same |

`partner_detail` queries are added after checkpoint B (`trade_data.py plan --partners SAU,DEU`). The plan prints three renderings of every query: API parameters, connector call, and Comtrade web-UI settings for a manual CSV download.

### 3.2 `raw/` accepted formats

`trade_data.py normalize` reads every file in `raw/` (or files passed as arguments):

1. **Comtrade UI CSV** (Latin-1 or UTF-8, trailing-comma quirk, columns `refYear, reporterCode, reporterISO, reporterDesc, flowCode, flowDesc, partnerCode, partnerISO, partnerDesc, cmdCode, netWgt, qty, qtyUnitAbbr, cifvalue, fobvalue, primaryValue, …`).
2. **Comtrade API JSON** (`{"data": [ {same keys} ]}`; `*Desc`/`*ISO` may be null — resolve from the numeric code).
3. **Generic rows** JSON list or CSV with columns `year, reporter, partner, flow, value_usd` and optional `net_kg, qty, qty_unit, hs_code` — for results saved from a connector. `reporter`/`partner` may be a name, ISO3, ISO2 or numeric code; `flow` may be `X/M/Export/Import`.

### 3.3 `flows.csv` (normalized long table)

Columns: `year, reporter_iso3, reporter_name, partner_iso3, partner_name, flow, hs_code, value_usd, net_kg, qty, qty_unit, source_file`.
Rules: `flow` ∈ `X`, `M` only (re-exports/re-imports are dropped and counted in meta). One row per (year, reporter, partner, flow, hs_code): duplicates across files are collapsed (identical values kept once; conflicting values keep the row from the most specific file and add a warning). Rows that are breakdowns by second partner, customs procedure or transport mode are aggregated to the total. Aggregate reporters (EU, ASEAN…) are dropped.

`flows_meta.json`: `{schema_version, hs_codes_found, years, files: [{file, format, rows_in, rows_kept}], views: {home_flows: bool, world_imports: bool, world_exports: bool, mirror: bool, partner_detail: [ISO3…], matrix: bool}, dropped: {reexports, aggregates, other_hs}, warnings: []}`.
`normalize` exits `2` if `home_flows` is absent or the HS code found differs from `inputs.product.hs_code`.

## 4. `analysis.json` (stage 3)

```json
{
  "schema_version": "2.0",
  "meta": {
    "perspective": "export", "home": {"iso3": "EGY", "name": "Egypt"},
    "hs_code": "071080", "product_name": "Frozen vegetables",
    "years_available": [2021, 2022, 2023, 2024, 2025, 2026],
    "latest_complete_year": 2025, "base_year": 2022, "partial_years": [2026], "window_label": "2022–2025",
    "views": { ...copied from flows_meta... },
    "labels": {"partner_noun": "market", "tier_core": "Defend", "tier_growth": "Expand", "tier_new": "Open"},
    "rules": {"core_share_pct": 5.0, "core_top_n": 3, "min_presence_share_pct": 0.25, "material_partner_share_pct": 0.25,
              "score_weights": {"size": 0.35, "growth": 0.25, "price": 0.15, "foothold": 0.15, "openness": 0.10}},
    "warnings": [], "generated_at": "2026-11-13T12:00:00"
  },
  "home_position": {
    "annual": [{"year": 2021, "value_usd": 0.0, "net_kg": 0.0, "unit_value_usd_per_kg": 0.0, "partial": false}],
    "latest_value_usd": 0.0, "cagr_pct": 0.0,
    "world_rank": 12, "world_share_pct": 1.0, "n_ranked": 85
  },
  "world_imports": {"annual": [{"year": 2021, "value_usd": 0.0, "n_reporters": 150, "partial": false}],
                    "ranking": [RankRow]},
  "world_exports": {"annual": [ ... ], "ranking": [RankRow]},
  "partners": [PartnerRow],
  "shortlist": {"core": ["SAU"], "growth": ["DEU"], "new": ["POL"]},
  "mirror_checks": [MirrorRow],
  "partner_detail": {"SAU": PartnerDetail}
}
```

`RankRow` (every reporter with data in the latest complete year, sorted by value): `{iso3, name, value_usd, share_pct, cagr_pct|null, unit_value_usd_per_kg|null, rank}`.

`PartnerRow` (one per candidate partner; candidates = every country with `partner_trade` data ∪ every partner in `home_flows`, minus `partners_excluded`):

```json
{"iso3": "SAU", "name": "Saudi Arabia", "tier": "core", "score": 71.4,
 "score_components": {"size": 80.0, "growth": 55.0, "price": 62.0, "foothold": 95.0, "openness": null},
 "partner_trade_latest_usd": 0.0, "partner_trade_share_pct": 0.0, "partner_trade_cagr_pct": 0.0,
 "partner_unit_value_usd_per_kg": 0.0, "partner_reports_latest": true,
 "home_flow_latest_usd": 0.0, "home_flow_share_of_home_pct": 0.0, "home_flow_cagr_pct": null,
 "home_flow_by_year": {"2021": 0.0}, "home_unit_value_usd_per_kg": 0.0,
 "mirror_flow_latest_usd": null, "home_share_in_partner_pct": null,
 "of_interest": false, "notes": []}
```

`MirrorRow`: `{iso3, name, year, home_reported_usd, partner_reported_usd|null, ratio|null, status, note}`.
`PartnerDetail`: `{basis: "partner_reported"|"counterparty_reported", basis_note, year, total_usd, rows: [{iso3, name, value_usd, share_pct, unit_value_usd_per_kg|null}], home_rank|null, home_share_pct|null, hhi}` — shares are of the partner's **total** (its World row when available, else the sum of all rows — `basis_note` says which).

### 4.1 Rules the analysis must follow

- **Years.** `latest_complete_year` = newest year whose reporter count in the primary world view is ≥ 70 % of the median reporter count of the three years before it; later years are `partial_years`. `base_year` = `latest_complete_year − 3` if present, else the earliest available year. CAGR uses base→latest complete; `null` if either end is missing or zero. CLI overrides: `--latest-year`, `--base-year`. No year literals in code.
- **Primary world view:** `world_imports` for export, `world_exports` for import. `partner_trade_*` comes from it. If that view is missing, exit `2`.
- **World totals** = sum over reporting countries (aggregates and `_` areas excluded).
- **Unit value** = value_usd / net_kg where net_kg > 0, else `null`.
- **Tier** (thresholds in `meta.rules`, CLI-overridable):
  - `core`: home_flow_latest ≥ `core_share_pct` % of home's total, or the partner is among home's top `core_top_n` partners.
  - `growth`: not core and home_flow_latest ≥ `min_presence_share_pct` % of home's total.
  - `new`: everything else whose `partner_trade_share_pct` ≥ `material_partner_share_pct`.
  - `minor`: the rest (kept in `partners`, never shortlisted automatically).
- **Score** (0–100, a ranking aid, not a verdict) = weighted mean of the non-null components, weights re-normalised. It needs evidence of the partner's own trade: when `size` is null (the partner did not report in the latest complete year) or fewer than `score_min_components` (2) components exist, `score` is `null` with a note; null scores sort last and are left out of the `growth` and `new` shortlists unless the partner is in `partners_of_interest`. Components:
  - `size`: percentile of `partner_trade_latest_usd` among candidates.
  - `growth`: percentile of `partner_trade_cagr_pct` among candidates with material size.
  - `price`: percentile of `partner_unit_value_usd_per_kg` — higher is better for export, **lower is better for import**.
  - `foothold`: percentile of `home_flow_latest_usd` (0 when none).
  - `openness`: `100 × (1 − hhi)` from `partner_detail` when available, else `null`.
- **Shortlist:** `core` = all core partners by home flow (max 6); `growth` = top 5 by score; `new` = top 5 by score. `partners_of_interest` are always added to their tier's list. The planner may change it — the final choice lives in `selection.json`.
- **Mirror check** for every partner with home_flow_latest ≥ `min_presence_share_pct` % of home's total, comparing home-reported with partner-reported (`mirror` view), same year:
  - `partner_not_reporting` — partner absent from the primary world view that year.
  - `consistent` — ratio partner/home in 0.75–1.35 (import values are CIF, exports FOB).
  - `partner_reports_less` (< 0.75) — possible hub routing or re-export, classification difference, timing.
  - `partner_reports_more` (> 1.35) — possible indirect shipments attributed by origin.
  - `only_home_reports` — partner reports but shows nothing from home.
  - `no_mirror_data` — mirror view not loaded.
- **Partner detail:** prefer the partner's own report (`partner_detail:<ISO3>`); fall back to counterparties' reports from `matrix`; otherwise absent (never fabricate).

## 5. `selection.json` (checkpoint B)

```json
{"schema_version": "2.0", "partners": ["SAU", "ARE", "NLD"], "decided_by": "user", "notes": {"NLD": "client has a buyer contact"}}
```

Order = priority. Stages 4–7 work on exactly these partners (a **lane** = home ↔ partner).

## 6. Freight (stage 4)

### 6.1 `freight_inputs.json` (what the worker collects)

```json
{
  "schema_version": "2.0",
  "container_type": "40RF",
  "home_port": {"name": "Alexandria", "locode": "EGALY"},
  "lanes": {
    "SAU": {
      "partner_port": {"name": "Jeddah", "locode": "SAJED"},
      "rates": [
        {"tier": "quote", "usd": 1450, "all_in": false, "transit_days": 6, "carrier": "…", "source": "…", "url": null, "as_of": "2026-11-10", "valid_until": "2026-11-30"}
      ],
      "local_charges": {"origin_inland": SV, "origin_docs": SV, "origin_thc": SV, "destination_thc": SV, "import_clearance": SV, "destination_inland": SV}
    }
  },
  "calibration_points": [
    {"label": "FBX11 China/East Asia → North Europe", "from_locode": "CNSHA", "to_locode": "NLRTM", "usd": 2700, "container_type": "40DC", "source": "Freightos Baltic Index", "url": "https://…", "as_of": "2026-11-07"}
  ],
  "equipment_multiplier": {"value": 1.0, "tier": "missing", "source": "", "url": null, "as_of": null},
  "model": {"range_pct": 35}
}
```

Ports may be omitted: the script picks each country's default port from `scripts/data/ports.json`.

### 6.2 The ladder (`estimate_freight.py build`)

For each lane the freight figure comes from the **highest available rung**:

1. `quote` — the client's own forwarder or carrier quote.
2. `connector` — a live rate tool (Freightos Rate Estimate API, a freight MCP, Nowlun if connected…).
3. `published` — a cited public rate for that lane (carrier page, index covering the lane).
4. `modelled` — distance-based estimate: sea distance (offline `searoute`) × a line fitted to ≥ 2 dated, cited `calibration_points` (`usd = a + b × nm`; with one point, a line through the origin), times `equipment_multiplier` when the lane's container type differs from the calibration type. Range = base ± `range_pct`. **With no calibration point there is no modelled price** — the lane is a coverage gap.
5. none → `coverage_gap: true`, freight `missing`.

When several rates share the top rung: `base` = median, `low` = min, `high` = max. With a single quote: low = base = high. Lower rungs are kept in `alternatives` for transparency. Transit time: from the chosen rate if it states one; else modelled from distance — `low` = nm/(18 kn×24)+1, `base` = nm/(15×24)+3, `high` = nm/(13×24)+7 days, tier `modelled`.

### 6.3 `freight.json`

```json
{
  "schema_version": "2.0", "container_type": "40RF", "generated_at": "…",
  "direction": "home_to_partner",
  "lanes": {
    "SAU": {
      "partner": {"iso3": "SAU", "name": "Saudi Arabia"},
      "origin_port": {"name": "Alexandria", "locode": "EGALY", "lat": 31.18, "lon": 29.87},
      "destination_port": {"name": "Jeddah", "locode": "SAJED", "lat": 21.48, "lon": 39.17},
      "distance_nm": 881,
      "freight_usd": {"low": 1450, "base": 1450, "high": 1450},
      "freight": SV,
      "transit_days": {"low": 4, "base": 6, "high": 9, "tier": "quote"},
      "all_in": false,
      "local_charges": {"origin_inland": SV, "origin_docs": SV, "origin_thc": SV, "destination_thc": SV, "import_clearance": SV, "destination_inland": SV},
      "alternatives": [{"tier": "modelled", "usd": 1210, "note": "…"}],
      "coverage_gap": false, "notes": []
    }
  }
}
```

`direction`: `home_to_partner` for export, `partner_to_home` for import (origin = partner port). `freight` is the SV for the base figure. An `all_in` rate sets the charges it covers to `{"value": 0, "tier": <rate tier>, "source": "included in all-in rate"}` only for the charges the rate explicitly lists in `covers` (list of charge keys); others stay as supplied.

## 7. `tariff.json` (stage 5)

```json
{
  "schema_version": "2.0", "hs_code": "071080",
  "lanes": {
    "SAU": {
      "importer": "SAU", "exporter": "EGY",
      "duty_rate_pct": SV, "mfn_rate_pct": SV,
      "preference": {"agreement": "GAFTA", "rate_pct": SV, "conditions": "Arab rules of origin"},
      "other_duties_pct": SV,
      "valuation_basis": "CIF",
      "import_vat_pct": SV, "vat_recoverable": true,
      "ntb": ["SFDA registration"], "status": "sourced", "notes": ""
    }
  }
}
```

`duty_rate_pct` is the rate the model applies. `status`: `sourced` (every SV has a URL or named official schedule) or `verify`. `valuation_basis`: `CIF` by default; `FOB` for importers that assess duty on FOB value (`common.FOB_VALUATION` lists the usual ones — confirm per study). For import studies `importer` is always the home country and lanes are keyed by supplier.

## 8. Financial model (stage 6)

### 8.1 Math (`model_core.py` is the single source of truth; Excel formulas and the dashboard must reproduce it)

Per lane, per container, in model currency. `U` = units per container.

Chain items: `OI` origin inland · `OD` origin docs · `OT` origin THC · `FR` freight (scenario low/base/high) · `INS` insurance · `DT` destination THC · `IC` import clearance · `DU` duty · `DI` destination inland.

**Who pays** by incoterm `T` (seller side): EXW none · FCA/FOB `OI OD OT` · CFR `+FR` · CIF `+INS` · DAP `+DT +DI`. Everything else is the buyer's.

**Invoice-based values** (invoice `V` = selling price × U ÷ fx_price for export; purchase price × U ÷ fx_cost for import; `r` = insurance rate):

| T | FOBv | CIFv |
|---|---|---|
| EXW | V + OI + OD + OT | (FOBv + FR)(1 + r) |
| FCA, FOB | V | (FOBv + FR)(1 + r) |
| CFR | V − FR | V(1 + r) |
| CIF | V/(1 + r) − FR | V |
| DAP | CIFv/(1 + r) − FR | V − DT − DI |

Customs value `CV` = CIFv or FOBv per `valuation_basis`. `DU = (duty_rate + other_duties) × CV`. Non-recoverable VAT = `vat × (CIFv + DU)`, else 0.
**Buyer landed cost** = `CIFv + DT + IC + DU + DI + VATnr`.

**Export (user is the seller):**
- `FOB_cost` = goods + OI + OD + OT when `cost.basis = EXW`; = goods when `cost.basis = FOB` (those three are then shown as "included in FOB cost"). goods = cost per unit × U ÷ fx_cost. Selling EXW with a FOB cost basis is an input error.
- `seller_cost` = goods (if T = EXW) or FOB_cost, + FR (CFR, CIF, DAP) + `INS_seller` (CIF, DAP) + DT + DI (DAP). `INS_seller = r × (FOB_cost + FR)`.
- `revenue` = V. `margin = revenue − seller_cost`; `margin_pct = margin / revenue`.
- No price given → `required_price_per_unit = seller_cost ÷ (1 − target_margin) ÷ U × fx_price`, and that price is used (`price_basis: "required_at_target_margin"`). No price and no target margin → lane `incomplete`.
- `buyer_landed_per_unit` from the table above; when `unit = "kg"` (or `net_kg_per_unit` is given) compare with the partner's average import unit value from `analysis.json` → `price_gap_pct`.

**Import (user is the buyer):**
- `landed_cost` = buyer landed cost above (V from the purchase price). `revenue` = local price × U ÷ fx_price. `margin = revenue − landed_cost`.
- No local price → report `breakeven_local_price_per_unit` and `required_local_price_per_unit` (at target margin); margin `null`.

**Completeness.** A lane is `modelled` only if every SV on the user's side of the chain is present. Otherwise `status: "incomplete"` with `missing: [keys]`; margin fields are `null`. Buyer-side figures in export studies are computed only when their own inputs are present (`buyer_side_missing`).

**Scenarios and sensitivity** (computed by `model_core`, written to JSON and as static grids in Excel): freight low/base/high; price ±5/10/15 % × goods cost ±10/20 % → margin per container; FX ±10/20 % on the cost currency (export) or the price currency (import); duty ±5 pp (buyer landed for export, margin for import).

**Break-even** = annual fixed cost ÷ margin per container (null if margin ≤ 0 or fixed cost missing).

**Cash cycle (days):** export = (production_lead + origin_dwell + credit_days) × (1 − advance). Import = transit_base + clearance + inventory + customer_credit − credit_days × (1 − advance) + production_lead × advance. `cash_tied_per_container` = user's cost per container; `peak_working_capital` = planned containers per year × cycle ÷ 365 × cost per container. Any required SV missing → that block is `incomplete`.

**Payback** (portfolio): monthly containers = `monthly_ramp`, else planned containers per year ÷ 12 (flagged `derived`); monthly cash = containers × plan-weighted margin, received `round(cycle ÷ 30)` months later, − annual fixed ÷ 12, − one-time cost in month 1. `payback_month` = first month with cumulative cash ≥ 0, else `null`. No plan → `null` with reason.

### 8.2 `financial.json`

```json
{
  "schema_version": "2.0", "perspective": "export", "currency": "USD", "container_type": "40RF",
  "units_per_container": 24000, "unit": "kg", "incoterm": "FOB", "generated_at": "…",
  "lanes": {
    "SAU": {
      "status": "modelled", "missing": [], "buyer_side_missing": [],
      "price_per_unit": 1.15, "price_basis": "user",
      "revenue": 27600.0, "user_cost": 21300.0, "margin": 6300.0, "margin_pct": 22.8,
      "buyer_landed": 31250.0, "buyer_landed_per_unit": 1.30, "benchmark_unit_value": 1.42, "price_gap_pct": -8.5,
      "waterfall": [{"key": "goods", "label": "Goods at EXW", "amount": 18240.0, "side": "seller", "tier": "user"}],
      "freight_scenarios": {"low": {"margin": 0, "buyer_landed": 0}, "base": {…}, "high": {…}},
      "sensitivity": {"price_pct": [-15, -10, -5, 0, 5, 10, 15], "cost_pct": [-20, -10, 0, 10, 20], "margin": [[…]]},
      "fx_sensitivity": [{"shock_pct": -20, "margin": 0.0}],
      "duty_sensitivity": [{"shock_pp": -5, "value": 0.0}],
      "break_even_containers_per_year": 2.1,
      "cash_cycle_days": 75, "cash_tied_per_container": 21300.0, "peak_working_capital": 0.0,
      "planned_containers_per_year": 24, "transit_days_base": 6
    }
  },
  "portfolio": {"planned_containers_per_year": 60, "weighted_margin": 0.0, "annual_margin": 0.0, "annual_fixed": 0.0,
                "one_time": 0.0, "payback_month": 9, "payback_note": "", "monthly": [{"month": 1, "containers": 0, "cash": 0.0, "cumulative": 0.0}]},
  "inputs_echo": [{"key": "cost.per_unit", "lane": null, "value": 38.0, "unit": "EGP/kg", "tier": "user", "source": "…", "as_of": "…"}]
}
```

For import studies the same keys are used: `user_cost` is the landed cost, `buyer_landed*` equal it, and `breakeven_local_price_per_unit` / `required_local_price_per_unit` are added. `waterfall[].side` ∈ `seller`, `buyer`; `key` ∈ `goods OI OD OT FR INS DT IC DU DI VAT`. `inputs_echo` lists every input SV used, for the provenance tab and the QA gate.

### 8.3 `financial_model.xlsx`

Sheets: `Cover`, `Assumptions` (every global input: value, unit, tier, source, as-of), `Lanes` (one row per lane with its inputs and live formulas referencing `Assumptions`), `Lane detail` (waterfall for a lane picked from a dropdown), `Scenarios` (live switches on `Assumptions`: freight scenario, price %, cost %, FX %; plus the static sensitivity grids), `Plan & payback`, `Working capital`, `Provenance`, `Limitations`, and a hidden `_check` sheet (`iso3 | metric | formula`) exposing revenue, user_cost, margin and buyer_landed per lane.
Rules: inputs are cells, never literals inside formulas; missing inputs are **blank cells** flagged `MISSING` and make dependent cells show `""`, not zero; input / formula / result cells use three distinct fills; sheet names and cross-sheet references must survive apostrophes in country names. After a LibreOffice recalculation the `_check` values must equal `financial.json` within 0.01.

## 9. `narrative.json` (stage 7 — written by the planner model, never by a script)

```json
{
  "schema_version": "2.0", "language": "en",
  "executive_summary": {"headline": "One sentence.", "paragraphs": ["…"], "recommendations": ["…", "…", "…"], "key_caveat": "…"},
  "sections": {
    "scope_method": {"paragraphs": [], "bullets": []},
    "home_position": {"paragraphs": [], "bullets": []},
    "global_landscape": {"paragraphs": [], "bullets": []},
    "competition": {"paragraphs": [], "bullets": []},
    "logistics_access": {"paragraphs": [], "bullets": []},
    "shortlist": {"paragraphs": [], "partner_notes": {"SAU": "…"}, "not_prioritised": [{"partner": "CHN", "reason": "…"}]},
    "financial_case": {"paragraphs": [], "partner_verdicts": {"SAU": "…"}},
    "risks": {"diagnostic": [], "macro": [], "data_limits": []},
    "next_steps": {"two_weeks": [], "thirty_days": [], "ninety_days": []}
  }
}
```

`quick_scan` needs only `executive_summary`, `scope_method`, `home_position`, `global_landscape`, `shortlist`, `next_steps`. `build_report.py` exits `2` if a required section is empty (override: `--allow-gaps`, which prints a visible placeholder).

## 10. Outputs

- `study.docx` — sections in the order of `references/study_structure.md`; tables from the JSON files, prose from `narrative.json`; an appendix lists every SV with tier, source and date, and a **verification checklist** of everything not `user`/`quote`.
- `dashboard.html` — one self-contained file (no network requests): data embedded as JSON, tabs Overview · Markets/Suppliers · Partner detail · Economics (live what-if using a JS port of `model_core`) · Sources. Every figure shows its tier.
- `qa.json` — `{schema_version, stage, passed: bool, errors: [], warnings: [], verify_list: [{item, lane, tier, source}]}`.

## 11. Tests

`tests/make_fixture.py` writes synthetic Comtrade-shaped files (clearly named `SYNTHETIC_*`) for one HS code covering both perspectives. `tests/run_demo.py` runs every stage for `tests/demo_export` (home EGY) and `tests/demo_import` (home SAU) and asserts: exit codes, schema keys, Python↔Excel parity, Python↔dashboard-JS parity, and that removing an input yields `incomplete`/blank rather than a number.
