# Study structure and writing guide (stage 7)

For the planner model that writes `narrative.json` (contracts section 9) and for anyone reviewing `study.docx`. `build_report.py` fills tables and charts from the JSON files; you write the prose. Where this guide and `contracts.md` differ, the contract wins.

## 1. Purpose, length, readers

- **Full study:** 12 to 18 pages. **Quick scan:** 3 to 5 pages (executive summary, scope and method, home position, global landscape, shortlist, next steps only).
- The study helps one client decide where to sell (export) or where to source (import), and what to do in the next 90 days. It is not a market encyclopedia.
- **Who reads what.** Executive summary: the decision-maker, who may read nothing else, so it must stand alone. Financial case and logistics: finance and operations. Appendices: whoever verifies the work. Write each section for its reader, and do not repeat a table the script already prints.

## 2. Section order

Perspective wording is shown as **export / import**. Lengths are guides for a full study.

| # | Section (narrative key) | Script supplies | You write | Must include | Length |
|---|---|---|---|---|---|
| 1 | **Executive summary** (`executive_summary`: headline, paragraphs, recommendations, key_caveat) | Key-figures strip from `analysis.json` and `financial.json` | One-sentence headline; 1 to 2 short paragraphs; exactly three prioritised recommendations; one key caveat | The decision asked for; best 2 to 3 **markets / suppliers** by name; the margin or landed-cost verdict with its tier; what is unverified | 1 page |
| 2 | **Scope and method** (`scope_method`) | Scope table from `inputs.json` (product, HS code, years, perspective, depth); year note from `analysis.json` meta | What was asked, product definition, which year is latest complete and which is partial, data sources, what was not covered | HS code and description; the confirmed-by-client flag; latest complete year, partial year; **trade-only study: say the financial case is deferred and why** | 0.5 page |
| 3 | **Home position** (`home_position`) | Annual table and chart of home's **exports / imports**; world rank and share (`analysis.json: home_position`); top partners table | What the trend means for the client; concentration; unit-value movement | Value, volume and unit value for the latest complete year; CAGR with its base year; world rank among `n_ranked` countries; share of top 3 partners | 1 page |
| 4 | **Global landscape** (`global_landscape`): **Global demand landscape / Global supply landscape** | World total trend and ranking tables (`world_imports` for export, `world_exports` for import); partner table with tier, score, size, growth | Which countries matter, which are growing, where home is absent or already strong; how the shortlist tiers were reached | Size and CAGR of the leading partners; unit-value spread; reporter coverage caveats; tier labels **Defend / Expand / Open** or **Keep / Develop / New source** | 2 pages |
| 5 | **Competition / Supplier profiles** (`competition`) | `partner_detail` tables per selected partner (supplier mix, home rank and share, concentration index) | **Export: competitor exporters** in each shortlisted market, home's position among them, price positioning. **Import: supplier profiles**: scale, trend, unit value, **their other buyers** and home's rank among them | Basis of each table (`partner_reported` or `counterparty_reported`, from `basis_note`); where detail is missing, say so. No speculation about competitor strategy | 1.5 to 2 pages |
| 6 | **Logistics and market access** (`logistics_access`) | Freight table (`freight.json`: rung tier, low/base/high, transit); tariff table (`tariff.json`: duty, other duties, VAT, valuation, status, NTB list) | Reading of both: which lanes are cheap and fast, which duty or non-tariff requirement matters most, which preference is an opportunity and what it needs | Freight tier per lane (quote, connector, published, modelled estimate); coverage gaps; the rate basis (MFN or preferential) and date; **verify** lanes named; the main non-tariff requirement per lane | 2 pages |
| 7 | **Shortlist** (`shortlist`: paragraphs, `partner_notes`, `not_prioritised`) | Shortlist table from `selection.json` and `analysis.json`; mirror-check table | Selection logic and ordering; a note per selected partner; the list of partners **not prioritised** with a reason each | Why each was selected (size, growth, foothold, price, logistics, access); where you overruled the score and why; **export: markets to Defend / Expand / Open; import: suppliers to Keep / Develop / add as New source**; mirror gaps shown both ways | 1.5 pages |
| 8 | **Financial case** (`financial_case`: paragraphs, `partner_verdicts`) | Per-lane table, waterfall charts, sensitivity, break-even, cash cycle, payback (`financial.json`) | Reading of the economics; one verdict per partner; what drives the result | **Export: margin per container and the landed cost at the buyer** against the partner's average import unit value. **Import: landed cost at home** against the local price or required price. Status (`modelled` / `incomplete`), tier of the key inputs, sensitivity to freight, price, FX and duty | 2 pages |
| 9 | **Risks and open questions** (`risks`: diagnostic, macro, data_limits) | Warnings from `analysis.json` meta and `qa.json` | `diagnostic`: what the data cannot tell us; `macro`: currency, policy, demand, sanctions or conflict only if supported; `data_limits`: coverage, partial year, mirror gaps, modelled freight, verify tariffs | Each risk with its consequence and what would reduce it | 1 page |
| 10 | **Next steps** (`next_steps`: two_weeks, thirty_days, ninety_days) | Table layout only | Verb-first actions, each naming an outcome and an owner (client, team, broker) | Closing the open `verify` items first; first contacts with the top partners; samples, registrations, quotes | 1 page |
| A | **Appendix A: sources, tiers, verification checklist** | Every SV with tier, source, date; checklist of everything not `user` or `quote` | One short paragraph on how to use the checklist | Nothing else; do not restate the table | 1 to 2 pages |
| B | **Appendix B: method** | Definitions: tiers, score weights, mirror-check thresholds, freight ladder, model chain | A few lines on anything particular to this study | Rules and thresholds come from `analysis.json: meta.rules` | 1 page |

The quick scan keeps rows 1, 2, 3, 4, 7 and 10 only.

### Wording differences

| Topic | Export | Import |
|---|---|---|
| Partners are | destination markets | supplier countries |
| Section 4 | Global demand landscape | Global supply landscape |
| Section 5 | Competitor exporters in each market | Supplier profiles and their other buyers |
| Tiers | Defend, Expand, Open | Keep, Develop, New source |
| Home flow | exports to the market | imports from the supplier |
| Price view | price the market pays versus our price | supplier export unit value versus our purchase price |
| Landed cost | at the buyer (what the importer pays, compared with local prices) | at home (what the client pays to bring the goods in) |
| Margin | on the sale price per container | on the local selling price, or required price |
| Duty and VAT | the partner's | home's, one schedule for all lanes |

## 3. Writing rules

1. **Every number exists in a JSON file.** Quote `analysis.json`, `freight.json`, `tariff.json`, `financial.json` or `selection.json`; where it matters, say which ("per the financial model"). Do not calculate new figures in prose; ask for a script change.
2. **State the tier when it is weaker than `user` or `quote`:** "modelled estimate", "published rate", "to be verified". Do not call a modelled freight cost a quote.
3. **Hedge in proportion to the evidence.** Strong (client figure, written quote, official duty): plain statement. Medium (published index, partner-reported data): "suggests". Weak (modelled, one source, missing mirror): "indicative only" and name what would settle it.
4. **Name the latest complete year and the partial year** whenever you quote a trend ("2025 is the latest complete year; 2026 is partial and excluded from growth rates").
5. **Show mirror gaps both ways:** "Home reports X; the partner reports Y." Offer causes (re-export hubs, classification, timing, indirect shipping) as possible, never as the answer. Do not pick a side.
6. **Recommendations are specific, prioritised and tied to evidence:** the partner, the reason, the number behind it.
7. **Next steps start with a verb and name an outcome:** "Request written freight quotes for two lanes to replace the modelled estimates", not "Look into logistics".
8. **Banned:** generic marketing advice ("leverage digital channels"), predictions without a source, speculation on competitors' strategy, internal jargon (file names, tier codes in client prose beyond the plain-language labels).
9. **Plain client-facing language.** Short sentences. Define a term once. Money with currency and unit; percentages with their base.
10. **Country names exactly as in `analysis.json`** (the `name` fields). Do not alternate between short and formal forms.
11. **A rate, a price or a margin quoted with its date and basis** (MFN or preferential; FOB or CIF).
12. **Say what is missing** and what would fill it, in the section where it matters, not only in risks.

## 4. Handling gaps (model sentences)

- **Incomplete lane (financial):** "The model could not compute a margin for [partner] because [input] has not been supplied. Once it is provided, the lane can be completed; until then it is excluded from the ranking by margin."
- **Required price shown instead of a margin:** "No selling price was given for [partner]. At the target margin of [x]%, the price would have to be [y] per [unit]; the market's average import unit value is [z] ([source year])."
- **Freight coverage gap:** "No freight benchmark covers the [origin] to [destination] lane, so no freight figure is shown. A forwarder quote would close this."
- **Modelled freight:** "Freight for [lane] is a modelled estimate, fitted to [n] dated benchmarks, with a range of [low] to [high]. It is not a quote."
- **Tariff `verify`:** "The duty for [lane] could not be confirmed from an official source and is left blank. [Customs broker or authority] should confirm before any commitment."
- **Preference not applied:** "A preference exists under [agreement] but is not applied because [reason]. If the client can document origin, the duty would fall from [MFN] to [preferential] (per [source, date])."
- **Trade-only study:** "No commercial inputs were provided, so the financial case is deferred. The rankings and shortlist rest on trade data alone and say nothing about profitability."
- **Missing partner detail:** "The partner's own import data were not available, so the supplier mix is not shown for [partner] rather than estimated."
- **Partial year:** "[Year] covers only the reporting countries that have published so far, so it is shown but not used for growth rates."
- **Mirror gap:** "For [partner] in [year], home reports [x] and the partner reports [y] ([ratio]). The difference may reflect [possible causes]; we use [source] for the ranking and show both."

## 5. Checklist before building the report

1. `analysis.json`, `selection.json`, and (full study) `freight.json`, `tariff.json`, `financial.json` exist and are from the same run; no stale file.
2. Every quoted number found in a JSON file; spot-check at least ten, especially in the executive summary.
3. Headline, three recommendations and key caveat agree with `financial.json` (status, margin or landed cost) and with the shortlist.
4. Each selected partner has a `partner_notes` entry; each has a `partner_verdicts` entry (full study); each omitted candidate in `not_prioritised` has a reason.
5. Latest complete and partial years named; mirror gaps shown both ways.
6. Every lane with status `incomplete`, `verify`, a freight coverage gap or a modelled input is mentioned in the text with its model sentence.
7. Country names match `analysis.json`; wording matches the perspective (section 2 table).
8. Next steps are verb-first, grouped as two weeks, thirty days and ninety days; the first group clears the largest open items.
9. No banned content (rule 8); no unsupported prediction.
10. Run `build_report.py`, then `validate_study.py`; read `qa.json`; reconcile the verification checklist with what the text says.
