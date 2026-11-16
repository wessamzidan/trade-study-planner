# Trade data guide (stage 2 and 3) — for the agent running the skill

Goal: get annual UN Comtrade data for ONE agreed HS code into `flows.csv` / `flows_meta.json`, then build `analysis.json`.
Never fill a gap with an estimate. A missing number stays missing and is reported to the user.

## 1. Pick the acquisition route (read `env.json` from `check_env.py`)

Use the first route that applies. Mixing routes is fine: `normalize` reads every file in `raw/`.

| Order | Route | Applies when | Result |
|---|---|---|---|
| 1 | **Direct API with the user's key** | `env.json` says a Comtrade key is present in the environment variable (default `COMTRADE_SUBSCRIPTION_KEY`) and the host is reachable | `fetch` writes `raw/<id>.json` |
| 2 | **Comtrade connector / MCP** in the host | a Comtrade tool is listed in your tools (or the user connects one) | you save the rows as generic-rows JSON to `raw/<id>.json` |
| 3 | **Manual CSV upload** | no key, no connector, or the host is blocked | the user downloads CSVs from the Comtrade web UI; you save them to `raw/` |

The keyless preview endpoint (`fetch` without a key) is a last resort for tiny queries only: 500 rows per call and one period per call, so any large market is silently cut at 500 rows. `fetch` flags this (`truncated: true` in `raw/_fetch_log.json`); when you see it, switch to route 1, 2 or 3 for that query.

## 2. Commands

```
python3 scripts/trade_data.py plan --study-dir D                       # writes query_plan.json, prints it
python3 scripts/trade_data.py plan --study-dir D --partners SAU,DEU    # add partner_detail queries (after checkpoint B)
python3 scripts/trade_data.py plan --study-dir D --matrix              # optional all-reporters x all-partners query
python3 scripts/trade_data.py fetch --study-dir D --dry-run            # show calls, key redacted
python3 scripts/trade_data.py fetch --study-dir D [--only id,id]       # route 1 (or keyless preview)
python3 scripts/trade_data.py normalize --study-dir D [--allow-hs CODE]
python3 scripts/analyze_trade.py --study-dir D                         # writes analysis.json
```

Exit codes: `0` ok, `2` BLOCKED (read the message; it names what to do), `1` error.
`fetch` exits 2 when `comtradeapi.un.org` cannot be reached (sandbox or proxy). Do not retry in a loop: move to route 2 or 3.

## 3. The queries (what each one answers)

`home_flows` (home to all partners), `world_imports` (all reporters, partner World, flow M), `world_exports` (same, flow X), `mirror` (all reporters reporting the home country as partner), and optionally `partner_detail:<ISO3>` and `matrix`. The perspective decides the flow codes; the plan already encodes this. The 4 base queries come first; `partner_detail` waits for the shortlist checkpoint.

Years come from `inputs.years`. Everything is annual, HS, the agreed code, totals only (no transport-mode, customs or second-partner breakdown).

## 4. Route 1: direct API

1. `plan`, then `fetch --dry-run` and show the user the number of calls.
2. Free key limits: 500 calls per day, up to 100,000 rows per call, up to 12 periods per call. A base plan needs about 4 calls; each `partner_detail` adds 1.
3. `fetch` paces calls (at least 1.1 s apart), retries 429/5xx with backoff (max 4) and never prints or stores the key. Raw responses go to `raw/<id>.json`; the log to `raw/_fetch_log.json`.
4. Never paste the key into a file, a chat message or a command line; it is read from the environment variable only.

## 5. Route 2: connector / MCP

For each query in `query_plan.json`, read `connector_hint` and ask the connector for exactly that. Ask for totals only. Save every result as **generic rows** JSON, a list of objects:

```
[{"year": <year>, "reporter": "<ISO3>", "partner": "W00", "flow": "X", "value_usd": 123456, "net_kg": 98000, "qty": null, "qty_unit": null, "hs_code": "071080"}]
```

`reporter` / `partner` may be ISO3, ISO2, a numeric Comtrade code or a name; `flow` may be `X`, `M`, `Export`, `Import`. Write the file to `raw/<id>.json` (id with `:` replaced by `_`). Keep the rows exactly as returned: do not sum, convert or fill. If the connector returns fewer rows than expected, say so; do not pad.

## 6. Route 3: manual CSV upload (walk the user through it)

One query at a time, in plan order. For each, read the user the plan's `ui_steps` verbatim; they are the exact settings for https://comtradeplus.un.org/TradeFlow:
Type Goods; Frequency Annual; Classification HS; Periods (all years); Reporters (a country, or All); Partners (World, a country, or All; when All, also tick World); 2nd partner World; Trade flows (Export or Import); HS code; Modes of transport TOTAL; Customs code TOTAL; Download CSV.
Ask them to save the file as `raw/<id>.csv` (name in `save_as_csv`) and not to open and re-save it in a spreadsheet (this drops leading zeros and re-encodes names). Upload size limits in the UI may force one file per year; that is fine, `normalize` merges them.
After each upload run `normalize`; its coverage report shows which views are now present.

## 7. Validation and the coverage report

`normalize` prints, and stores in `flows_meta.json`:
- `views`: which of `home_flows`, `world_imports`, `world_exports`, `mirror`, `partner_detail`, `matrix` are present. Exit 2 if `home_flows` is missing.
- `hs_codes_found`: must be exactly the agreed code. Another code in any file exits 2 (re-extract; use `--allow-hs` only when the user explicitly wants a basket of codes, and say so in the report).
- `dropped`: re-exports (RX/RM), aggregate areas (EU, ASEAN...), breakdown rows ignored or summed.
- `coverage`: number of reporters per year in each world view. A last year with far fewer reporters is a partial year (the analysis flags it; never use it for growth).
- `warnings`: read every one; conflicting values between files keep the more specific file and are listed.

Then run `analyze_trade.py` and read `meta.warnings` and `mirror_checks` before showing anything to the user. Report: latest complete year, which partners do not report, which views are missing.

## 8. After the shortlist checkpoint (partner detail)

When `selection.json` exists (or the user names partners): `plan --partners <their ISO3 list>`, fetch or collect only the new `partner_detail:*` queries (`fetch --only partner_detail:SAU,...`), `normalize`, `analyze_trade.py` again. `partner_detail` shows the supplier mix inside a market (export) or a supplier's other buyers (import); it also feeds the openness score. If a partner does not report, its detail is simply absent; do not substitute.
One big `matrix` query can replace all `partner_detail` queries when the key allows it (it is split by year; not possible keyless).

## 9. Known data traps (say them out loud in the report)

- **Re-exports and hubs.** Hubs (large ports and trading centres) import and re-export; a hub may report far less (or more) than the origin says it shipped. The mirror check flags it; treat hub volumes as routing, not consumption.
- **Partial latest year.** The newest year is often incomplete. The analysis picks the latest complete year from reporter counts; never compare it with a partial year.
- **Non-reporting countries.** Some countries do not report in some years. They have no value, not zero. Use the mirror view (what others say they sent them) as a hint only.
- **CIF vs FOB.** Imports are usually valued CIF, exports FOB; mirror ratios of roughly 0.75 to 1.35 are normal.
- **HS revisions.** A code can be split, merged or redefined between HS revisions; check the code still means the same product across all years. Six-digit codes are comparable across countries; national lines are not.
- **Confidential and estimated flows.** Some reporters suppress partners or product detail ("areas nes", missing partner rows). Partner rows may not add up to the World total; shares are computed on the World row when present.
- **Net weight gaps.** Unit values need net weight; where it is missing the unit value is null, not estimated.
- **Quantity units** differ by reporter; this toolkit uses value and net kg only.

## 10. Licence and citation

UN Comtrade data may not be redistributed without UN permission. Do not commit `raw/` or `flows.csv` to a public repository, and do not publish extracts. In reports cite: "UN Comtrade (https://comtradeplus.un.org), extracted on <date>" with the extraction date from `raw/_fetch_log.json` or from the files the user saved. Say which perspective's reports were used (importer-reported or exporter-reported).
