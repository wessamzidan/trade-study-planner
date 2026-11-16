# Financial methodology (stage 6)

For an AI agent running the stage and for a client's finance reader. The single source of truth is `scripts/model_core.py`; the workbook formulas reproduce it and `tests/test_financial.py` proves it (LibreOffice recalculation vs `financial.json`, within 0.01). Contract: `contracts.md` section 8.

## 1. What is modelled

One **lane** = home country and one selected partner, for **one container**, in the model currency (default USD). `U` = units per container. Percent inputs are in percent units in the JSON (`insurance_rate_pct = 0.4` means 0.4 %) and converted once, `r = insurance_rate_pct / 100`.

Chain items: OI origin inland, OD origin docs, OT origin THC, FR freight, INS insurance, DT destination THC, IC import clearance, DU duty, DI destination inland.

## 2. Who pays what, by incoterm

| Incoterm | Seller pays | Buyer pays |
|---|---|---|
| EXW | nothing beyond the goods | OI OD OT FR INS DT IC DU DI VAT |
| FCA / FOB (FCA treated as FOB) | OI OD OT | FR INS DT IC DU DI VAT |
| CFR | OI OD OT FR | INS DT IC DU DI VAT |
| CIF | OI OD OT FR INS | DT IC DU DI VAT |
| DAP | OI OD OT FR INS DT DI | IC DU VAT |

DDP is not modelled; use DAP and say so in the report.

## 3. The value table

`V` = invoice value = selling price x U / fx_price (export) or purchase price x U / fx_cost (import).

| T | FOBv | CIFv |
|---|---|---|
| EXW | V + OI + OD + OT | (FOBv + FR)(1 + r) |
| FCA, FOB | V | (FOBv + FR)(1 + r) |
| CFR | V - FR | V(1 + r) |
| CIF | V/(1 + r) - FR | V |
| DAP | CIFv/(1 + r) - FR | V - DT - DI |

Customs value `CV` = CIFv or FOBv (section 5). `DU = (duty + other duties) / 100 x CV`. Non-recoverable VAT `VATnr = VAT / 100 x (CIFv + DU)` (0 when recoverable).
**Buyer landed cost = CIFv + DT + IC + DU + DI + VATnr.** For DAP this equals invoice + IC + DU + VATnr.

## 4. Margins

**Export (user sells).** goods = cost per unit x U / fx_cost. `FOB_cost` = goods + OI + OD + OT when `cost.basis = EXW`; = goods when `cost.basis = FOB` (the three charges are then shown as "included in FOB cost"). Selling EXW on a FOB cost basis is an input error (the build is blocked, exit 2).
`seller_cost` = goods (T = EXW) or FOB_cost, plus FR (CFR, CIF, DAP), plus `INS_seller = r x (FOB_cost + FR)` (CIF, DAP), plus DT and DI (DAP). `revenue = V`, `margin = revenue - seller_cost`, `margin_pct = margin / revenue x 100`.

**Import (user buys).** `user_cost` = buyer landed cost (V from the purchase price in the cost currency). `revenue` = local price x U / fx_price. `margin = revenue - landed`. In the waterfall, rows inside the supplier invoice show 0 and the landed cost is the invoice plus the buyer-side rows.

**Benchmarks** (only when the model currency is USD and both figures exist). Export: partner average import unit value (USD/kg) x net_kg_per_unit against `buyer_landed_per_unit`. Import: the unit value home pays that supplier on average (CIF) x net_kg_per_unit against CIFv per unit. `price_gap_pct = (ours / benchmark - 1) x 100`. A unit named `kg` has net_kg_per_unit 1 by definition; any other unit needs `net_kg_per_unit`.

## 5. Required price (no price given)

Export with no selling price: `required_price = seller_cost / (1 - target_margin) / U x fx_price` and that price is used (`price_basis: "required_at_target_margin"`, margin % equals the target). No price and no target: lane `incomplete`.
Import with no local price: the margin stays `null`; `breakeven_local_price_per_unit = landed / U x fx_price` and `required_local_price_per_unit = landed / (1 - target) / U x fx_price` are reported.

## 6. Customs valuation basis

Most importers assess duty on CIF value; some on FOB value. `common.FOB_VALUATION` lists the usual FOB-valuation importers (USA, CAN, AUS, NZL, ZAF, BWA, NAM, LSO, SWZ). It is a starting point only: **always confirm per study** and record it in `tariff.json` (`valuation_basis`). With FOB valuation the FOBv is backed out of the invoice by the table above, so freight and insurance must be known even when the seller pays them.

## 7. VAT

Import VAT that the importer can reclaim does not cost anything: `vat_recoverable: true` gives VATnr = 0 (a cash-flow timing effect is not modelled). When not recoverable it is added on (CIFv + DU). `vat_recoverable` and, when false, `import_vat_pct` are inputs; if either is missing the buyer landed cost is blank.

## 8. Completeness: missing stays missing

Only the inputs on the user's side of the chain block a lane. Export: goods inputs, OI OD OT (only when cost basis is EXW and T is not EXW), FR (CFR CIF DAP), insurance (CIF DAP), DT DI (DAP), fx_price, and the price or target margin. Duty, VAT, clearance and the buyer's own freight or insurance are buyer-side: when absent the seller margin is still computed and the keys are listed in `buyer_side_missing`; `buyer_landed` is null. Import: every input of the landed cost (the user pays or must know all of it) plus both exchange rates.
A blocked lane has `status: "incomplete"`, the keys in `missing`, and null results (JSON) and blank cells (Excel). Nothing is defaulted; a genuine zero must be a sourced zero. A selected partner absent from `freight.json` or `tariff.json` is a lane with those inputs missing, never silently skipped. Freight in `freight.json` is USD: when the model currency is not USD it is treated as missing (no rate is invented).

## 9. Scenarios and sensitivity

Freight low / base / high; price -15..+15 % x goods cost -20..+20 % (margin per container; `sensitivity.margin[i][j]` = price_pct[i] x cost_pct[j]); FX -20..+20 % on the cost currency (export) or price currency (import); duty -5/0/+5 pp (buyer landed for export, margin for import; duty floored at 0). **FX convention:** +10 % means the shocked currency weakens 10 % against the model currency (rate x 1.10), so an export's EGP cost falls and an import's SAR revenue falls. On a required-price lane the grids hold the base required price fixed. These grids are written as static tables in Excel; the live switches move Lanes, Plan and Working capital.

## 10. Break-even

`break_even_containers_per_year = annual fixed cost / margin per container`; null when margin <= 0 or the fixed cost is missing.

## 11. Cash cycle and working capital (days)

- Export: (production lead + origin dwell + credit days) x (1 - advance).
- Import: transit (base) + clearance + inventory + customer credit - credit days x (1 - advance) + production lead x advance.

`cash_tied_per_container` = the user's cost per container (seller cost, or landed cost for an import). `peak_working_capital = planned containers per year x cycle / 365 x cost per container`. A negative import cycle means supplier credit finances the stock. Any missing input makes that block null (`cash_cycle_missing` lists it).

## 12. Payback (portfolio)

Lanes with a margin and planned containers > 0 are included. Plan-weighted margin = sum(planned x margin) / sum(planned). Monthly containers = `monthly_ramp` (24 numbers) else planned total / 12 (flagged derived). Month cash = containers of month (m - lag) x weighted margin, less annual fixed / 12 and, in month 1, the one-time cost. `lag = max(0, round(plan-weighted cash cycle / 30))` months, rounding halves up. `payback_month` = first month with cumulative cash >= 0, else null; null with a stated reason when there is no plan, a fixed cost is missing, or a cash cycle is missing.

## 13. What the model does not do

No demand forecast or price-elasticity; no tax or legal advice (re-verify every tariff, VAT and valuation rule); modelled freight is not a quote; DDP not modelled; simple insurance and day-count arithmetic; no interest, hedging, seasonality or non-recoverable-VAT cash timing; benchmarks ignore mix and quality.

## 14. The Excel workbook

Sheets: `Cover` (legend, cell map), `Assumptions` (switches in B5:B8; every global input with unit, tier, source, as-of; defined names `Sw_freight`, `Sw_price_pct`, `Sw_cost_pct`, `Sw_fx_pct` and `A_<input>`), `Lanes` (one row per partner from row 5: inputs, then live formulas), `Lane detail` (dropdown of lane names, waterfall by INDEX/MATCH), `Scenarios`, `Plan & payback` (24-month table; monthly override cells and the cash-lag cell are inputs), `Working capital`, `Provenance` (orange = verify, red = missing), `Limitations`, hidden `_check`.
Fills: yellow input, blue formula, green key result, gold switch. Inputs are cells, never literals inside formulas; missing inputs are blank cells and dependent formulas return "" (never 0, never an error). Status text in Lanes is fixed at build time; everything else reacts live. The incoterm, cost basis and perspective are resolved at build time: change them in `inputs.json` and rebuild.
**Using the switches:** set the freight scenario (dropdown) and the price, goods-cost and FX shocks on Assumptions; Lanes, Lane detail, Plan and Working capital recalculate. Reset to base / 0 / 0 / 0 to return to `financial.json`. The `_check` sheet (iso3, metric, formula) exposes revenue, user_cost, margin and buyer_landed per lane (plus portfolio weighted margin and payback) for the QA gate.
