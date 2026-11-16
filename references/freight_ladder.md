# Freight ladder — how to collect, tag and present freight (stage 4)

For the agent running the skill. Contract details: `contracts.md` section 6. All commands take `--study-dir DIR`
and live in `scripts/estimate_freight.py`. Freight is the least certain number in the model and the one clients
challenge first, so every figure carries the rung it came from.

## 1. The ladder

| Rung | Tier tag | What it is | Reliability |
|---|---|---|---|
| 1 | `quote` | The client's own forwarder or carrier quote | Firm, but check validity date |
| 2 | `connector` | A live rate tool (Freightos Rate Estimate API, a freight MCP, other tools if connected) | Market average, not bookable |
| 3 | `published` | A cited public rate for that lane (carrier page, an index covering the lane) | Indicative, dated |
| 4 | `modelled` | Sea distance x a line fitted to dated, cited benchmarks | Indicative only, wide range |
| 5 | none | `coverage_gap`, freight tier `missing` | Say so; never fill it |

`build` uses the highest rung that has a usable rate and keeps the lower rungs under `alternatives`, so the client
sees how far the figures differ. Rule that never bends: **a modelled price needs at least one dated, cited
calibration point.** No benchmark, no number. A gap is a legitimate finding; an invented figure is not.
The tier travels into the model, report appendix and dashboard, so a reader can always see "quote" versus "modelled".

## 2. Order of work

1. `init` (needs `inputs.json` and the checkpoint-B `selection.json`). It picks default ports per country from
   `scripts/data/ports.json`, shows distances and lists what each lane still needs. Landlocked countries get a gateway
   port plus a note: cost the inland leg under `origin_inland` / `destination_inland`.
2. Confirm ports with the client. A big country has several gateways (`ports --country ISO3` lists them); change with
   `set-port --lane ISO3|home --locode XXXXX`. A wrong port silently changes distance and cost.
3. Ask for quotes first, then try the connector, then search for published rates, then calibrate for what is left.
4. Collect local charges for every lane (section 5).
5. `build`, read the table and the notes, fix gaps, rebuild. Build is cheap and idempotent.

## 3. Rung 1: client quotes

Ask the client for their forwarder's or carrier's written quote per lane, and for each one record:
- price per container and **container type** (a dry quote is not a reefer quote);
- the **port pair** (a quote from a different port is a different lane);
- what it **includes**: ocean freight only, or all-in. If all-in, exactly which of `origin_inland origin_docs origin_thc
  destination_thc import_clearance destination_inland` it covers. Only listed charges are zeroed in the model;
- **validity** (`--valid-until`), transit time, carrier, quote reference. An expired quote is still used but flagged;
  ask for a refresh before the report goes out.

```
add-rate --lane NLD --tier quote --usd N --transit-days N --carrier S --source "Forwarder, ref 123" --as-of DATE --valid-until DATE [--all-in --covers origin_thc,destination_thc]
```
Several quotes on a lane: the model uses median as base, min as low, max as high. A single quote gives low = base = high.

## 4. Rungs 2 and 3: connector and published rates

**Freightos adapter (optional).** Offer it only if `check_env.py` shows the host reachable. The user can request a free
key, which allows 10 requests a day, about one study of up to 10 lanes. Tell them to set `FREIGHTOS_API_KEY` in the
environment; never ask them to paste it into chat, never print or store it. Then:
```
freightos [--lanes SAU,NLD] [--force]
```
It prints how many calls it will make, counts calls per day in `freight_inputs.json` (`meta.freightos_calls`), refuses to
pass 10 a day without `--force`, skips lanes already fetched today, and ignores non-USD prices. Reefers are priced via the
dry equivalent and stored with their real container type, so the equipment multiplier (section 7) applies. If the host is
unreachable or there is no key, the command stops with a message; carry on with the other rungs. Treat the result as a
market average, not a bookable price.

**Other connectors** (a freight MCP, a booking platform): save the rate with `add-rate --tier connector`, naming the tool in
`--source`, and the date queried in `--as-of`.

**Published rates.** Use web search for: carrier spot-rate or "rate calculator" pages, port-to-port rate pages from
forwarders and marketplaces, and public indices (Freightos Baltic Index FBX, Drewry World Container Index WCI,
Shanghai Containerized Freight Index SCFI) when an index lane matches your port pair. Record the exact page in `--url`
and the date shown on it in `--as-of`. Only use a figure that states its route, container type and date. A route that
only roughly matches belongs in calibration (section 6), not as a lane rate.
```
add-rate --lane GBR --tier published --usd N --container-type 40DC --source "Carrier X spot page" --url https://... --as-of DATE
```

## 5. Local charges and all-in rates

Six charges per lane, each a sourced value: `origin_inland origin_docs origin_thc destination_thc import_clearance
destination_inland`. Find them from: the client (best), the forwarder quote breakdown, carrier local-charge pages for the
port and country, port authority or terminal tariffs, customs-broker fee schedules. Do not reuse one country's tariff for
another. Cite and date each.
```
set-charge --lane ISO3|all --key destination_thc --value N --tier published --source "Port tariff 2026" --url https://... --as-of DATE
```
Anything not found stays **missing**; the financial model will mark the lane incomplete rather than guess. Say which
charges are missing and who could supply them. Do not set a charge to 0 unless it genuinely is zero or an all-in rate covers it.

## 6. Rung 4: the modelled estimate

Collect at least two benchmarks, ideally on different distances, each a real, dated, cited price for a port pair:
```
add-calibration --label "Index lane name" --from-locode CNSHA --to-locode NLRTM --usd N --container-type 40DC --source "Index name" --url https://... --as-of DATE
```
`url` and `as-of` are mandatory; the command refuses anything uncited. Prefer benchmarks close to the study's trade
routes (similar region, similar distance), published within the last weeks, in the lane's container type or with a
multiplier. Two points give a line `usd = a + b x nm`; one point gives a line through the origin. If the fitted slope or
intercept is not positive, the script falls back to the through-origin fit and warns.

Limits to state plainly to the client:
- it uses **distance only**: it ignores trade imbalance (head-haul vs back-haul), port congestion, seasonality, carrier
  capacity, canal or route disruptions, and special equipment availability;
- the range is +/- `model.range_pct` (default 35%, change with `set-range --pct N`) and is a judgement, not a statistic;
- it is **never a quote**; it is a placeholder until rung 1-3 evidence exists. Lanes served by it are put on the
  verification checklist automatically (tier is not user/quote).

## 7. Reefers and the equipment multiplier

Rates and benchmarks are usually quoted for dry boxes. A rate whose container type differs from the study's is **not
applied** (it is listed under alternatives as "different equipment") until an `equipment_multiplier` exists:
```
set-multiplier --value N --tier published --source "Carrier reefer surcharge page" --url https://... --as-of DATE
```
The multiplier is lane price in the study's equipment divided by the price in the quoted equipment (dry to reefer is above
1). Source it from a carrier's reefer-vs-dry tariff, a forwarder's statement, or the client's own history. Prefer a direct
reefer quote over multiplying. State in the report when a reefer price was derived from a dry one.

## 8. How to present each tier

- `quote`: "Based on your forwarder's quote (ref, date, valid until)". Safe to plan on until it expires.
- `connector`: "Market average from a live rate tool on DATE; confirm with a forwarder before committing."
- `published`: "Published rate from SOURCE dated DATE; indicative, not bookable."
- `modelled`: "Estimate from sea distance and N benchmark(s) (names, dates); range low-high; for screening only."
- gap: "No freight evidence yet for this lane; the financial case for it is incomplete." Offer the next step (ask for a quote).
Always show the range, transit time with its tier, alternatives that disagree by more than the range, and the notes.

## 9. Command cheat-sheet

```
python3 scripts/estimate_freight.py init            --study-dir D [--container 40HC]
python3 scripts/estimate_freight.py ports           --country ISO3
python3 scripts/estimate_freight.py set-port        --study-dir D --lane ISO3|home --locode XXXXX
python3 scripts/estimate_freight.py add-rate        --study-dir D --lane ISO3 --tier quote|connector|published --usd N --source S [--container-type T] [--transit-days N] [--carrier S] [--url U] [--as-of DATE] [--valid-until DATE] [--all-in --covers k1,k2]
python3 scripts/estimate_freight.py set-charge      --study-dir D --lane ISO3|all --key KEY --value N --tier TIER --source S [--url U] [--as-of DATE]
python3 scripts/estimate_freight.py add-calibration --study-dir D --label S --from-locode X --to-locode Y --usd N --container-type T --source S --url U --as-of DATE
python3 scripts/estimate_freight.py set-multiplier  --study-dir D --value N --tier TIER --source S [--url U] [--as-of DATE]
python3 scripts/estimate_freight.py set-range       --study-dir D --pct N
python3 scripts/estimate_freight.py freightos       --study-dir D [--lanes ISO3,ISO3] [--force]
python3 scripts/estimate_freight.py build           --study-dir D
```
Exit codes: 0 ok, 2 blocked (supply the input), 1 error. `pip install searoute` for distances (without it the modelled
rung is unavailable). Re-generate the port list with
`scripts/build_ports.py --unlocode /path/to/code-list.csv`.
