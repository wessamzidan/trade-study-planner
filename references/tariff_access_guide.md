# Tariff and market-access guide (stage 5)

For the worker agent that fills `tariff.json`, and for the planner who interprets it. File format: `references/contracts.md` section 7. This guide states **no rates, no agreements and no claims about current measures** on purpose: they change within months, so every one of them must come from a dated lookup made during the study.

## 1. What to record for each lane (and why)

A lane is one origin-destination pair. Export study: importer = the partner, exporter = home. Import study: importer = home, exporter = the supplier (one importer, so one schedule serves all lanes, but preferences differ by supplier).

| Item | Field | Why it matters |
|---|---|---|
| Applied duty for this pair | `duty_rate_pct` | The one figure the financial model uses. Must be the rate that actually applies to this origin, not a headline rate. |
| MFN rate | `mfn_rate_pct` | The fallback and the baseline against which any preference is judged. |
| Preferential rate, agreement, rules of origin | `preference.rate_pct`, `.agreement`, `.conditions` | A preference only counts if the client can prove origin. Record the rule (e.g. wholly obtained, tariff shift, value content) and the proof document. |
| Other duties | `other_duties_pct` | Safeguards, anti-dumping or countervailing duties, emergency surcharges, excise, seasonal or specific duties. Record 0 with a source if you checked and found none. |
| Valuation basis | `valuation_basis` | CIF or FOB changes the duty base. The tool defaults from `common.FOB_VALUATION` and flags it "confirm". |
| Import VAT/GST and recoverability | `import_vat_pct`, `vat_recoverable` | Only non-recoverable VAT is a cost to the buyer. If recoverability is unknown, say so. |
| Non-tariff requirements | `ntb` list | Registrations, import licences, certificates (health, phytosanitary, origin, halal, conformity), labelling, SPS/TBT rules, quotas and tariff-rate quotas (TRQs), pre-shipment inspection, approved-establishment lists. Often they decide feasibility more than duty does. |

## 2. Lookup order

1. **(a) Connector or WITS API** when `env.json` shows it reachable (`routes.tariff` contains `wits_api` or a connector). Fastest, and bilateral by design. Treat as tier `connector`.
2. **(b) The importer's official tariff finder** (section 3). Authoritative for legal rates. Tier `published`, cite the page and date.
3. **(c) Global free tools** (WTO, ITC Market Access Map, ITC Rules of Origin Facilitator). Good for cross-checking and for preferences. Tier `published`.
4. **(d) The client's customs broker or the importer's broker** - ask in writing. Tier `user` (client relays) or `quote` (written).
5. **(e) Leave it missing** and let the lane stay `verify`. Never fill from memory or from a "typical" figure.

When two routes disagree, record the official finder's figure, put the other in `notes`, and mention it in the study's risks.

## 3. Sources (each URL was opened or found in a search during preparation of this guide)

### Global tools

| Source | URL | Gives | Caveat |
|---|---|---|---|
| World Bank WITS | https://wits.worldbank.org/ | Bilateral applied and MFN tariffs, preferential schedules, some NTM data; web UI and API | Data lags the legal position by months; confirm recent changes at the importer. |
| WITS API documentation | https://wits.worldbank.org/witsapiintro.aspx (user guide linked there) | Request syntax for tariff (TRN) and trade data | See section 4. |
| WTO Tariff and Trade Data | https://ttd.wto.org/en (background: https://www.wto.org/english/tratop_e/tariffs_e/tariff_data_e.htm) | Bound and applied tariffs notified to the WTO, download facility | Beta interface; notified data can be old. |
| ITC Market Access Map | https://macmap.org | Applied duties by origin including preferences, NTMs, trade remedies, TRQs | Free registration may be needed for full detail; note the tool's reference year. |
| ITC Rules of Origin Facilitator | https://findrulesoforigin.org/ | Origin rules and proof requirements under trade agreements, by product | Describes rules; the customs authority decides. |
| UNCTAD TRAINS (NTM portal) | https://trains.unctad.org | Non-tariff measures by country and product | Coverage and year differ by country; not a legal text. |
| ePing (SPS and TBT) | https://epingalert.org/ | Notified SPS/TBT regulations and drafts, alerts, trade concerns | Shows notifications, not necessarily what is enforced today. |
| WTO Trade Remedies Data Portal | https://trade-remedies.wto.org/ | Anti-dumping, countervailing and safeguard investigations and measures notified by members | Notified measures only; confirm with the importer's authority. |

### Importer-specific finders

| Importer | URL | Gives | Caveat |
|---|---|---|---|
| European Union | https://trade.ec.europa.eu/access-to-markets/en/home | Duties, rules of origin, product requirements, VAT and other taxes for EU imports | VAT and excise are set by each member state; pick the state of entry. |
| United Kingdom | https://www.gov.uk/trade-tariff | Commodity codes, duty, VAT, suspensions, licences | Distinguish the Great Britain and Northern Ireland tariffs where relevant. |
| United States | https://hts.usitc.gov/ | Harmonized Tariff Schedule: general and special rates, chapter notes | Additional duties and trade-remedy orders are published outside the base schedule; check them separately. |
| Canada | https://www.cbsa-asfc.gc.ca/trade-commerce/tariff-tarif/ | Customs Tariff files, classification guides, notices (including surtaxes) | Read the notices page for temporary measures. |
| Australia | https://www.abf.gov.au/importing-exporting-and-manufacturing/tariff-classification/current-tariff | Working Tariff: classifications, duty rates, preferential rules | Preferential rules are in separate schedules. |
| Japan | https://www.customs.go.jp/english/tariff/index.htm | Import tariff schedule by effective date | Pick the version in force on the study date. |
| South Africa | https://www.sars.gov.za/customs-and-excise/tariff/ | Tariff Book, tariff determinations | Schedules are amended often; check the "what's new" notices. |
| Saudi Arabia | https://zatca.gov.sa/en/RulesRegulations/Taxes/Pages/Integrated-Tarrifs.aspx | GCC integrated customs tariff, with search link | Tariff lines are national (12-digit); amendments are published by notice. |
| United Arab Emirates | https://www.dubaicustoms.gov.ae/en/eServices/Pages/HScodesearch.aspx | Dubai Customs HS search with duty rates and restrictions | Emirate-level service; confirm the emirate of entry. |
| Brazil | https://www.gov.br/siscomex/pt-br/servicos/aprendendo-a-exportar/1-classificacao-fiscal-da-mercadoria/ferramentas-uteis | Siscomex classification tool (NCM, tax and administrative treatment) | The page does not itself give duty rates; locate the current external tariff (TEC) from the customs or trade-ministry site. |
| Türkiye | https://ticaret.gov.tr/gumruk-islemleri/sikca-sorulan-sorular/english/import-regime | Ministry of Trade overview of the import regime | Overview only; search the Ministry's pages for the current customs tariff schedule. |
| China | not verified | - | Search the customs authority's tariff page and the State Council Tariff Commission announcements. |
| India | not verified | - | Search the customs authority's tariff page (Customs Tariff Act schedules and duty notifications). |
| Mexico | not verified | - | Search the customs authority's tariff page and the economy ministry's trade information system. |
| Egypt | not verified | - | Search the customs authority's tariff page. |

A URL that fails in your environment is not evidence the source does not exist: try the web search, then fall back to (d) or (e).

## 4. WITS API: bilateral tariff request at HS 6-digit

Taken from the API documentation page and its linked user guide. The guide says calls work over HTTPS, and a single request cannot pull the whole database.

```
https://wits.worldbank.org/API/V1/SDMX/V21/datasource/TRN/reporter/{reporter}/partner/{partner}/product/{hs6}/year/{year}/datatype/{reported|aveestimated}
```

- `reporter` = the importer; `partner` = the origin (the guide's example uses partner `000` for World); `product` = a 6-digit HS code; `year` = a year or `all`. Several values are separated by `;`. Add `?format=JSON` for JSON.
- `datatype`: `reported` = reported rates only; `aveestimated` = reported plus specific duties converted to ad valorem equivalents. Only one per request.
- Country and group codes come from the country-list call `https://wits.worldbank.org/API/V1/wits/datasource/trn/country/ALL`.
- Allowed shapes: all products for one reporter, partner and year; one reporter, partner and product over a range of years; several reporters, partners and products for one year.
- The result is the rate WITS holds for that reporter-partner-year: check that the year is recent, that the partner rate reflects any preference, and cross-check one lane against the importer's finder. Record `url` as the exact request URL and `as_of` as the date you ran it.

## 5. Which rate to apply

- Apply the **preferential rate** only if (1) an agreement is in force between the two parties today, (2) the product at national-line level is covered, and (3) the client can meet the rules of origin and issue the proof. Otherwise apply **MFN** and describe the preference as an opportunity (what it would save, what the client must do to qualify).
- Write the reason in `preference.conditions` and `notes`. If the preference is applied, `duty_rate_pct` equals the preferential rate; if not, it equals MFN.
- Add `other_duties_pct` on top; the model adds it to the duty rate. State the legal basis of each other duty in `notes`.

### Phrasing for the client

1. **Firm, cited:** "The duty on [product] entering [importer] from [origin] is [x]% under [basis], per [source], read on [date]."
2. **Conditional:** "A preferential rate of [x]% is available under [agreement] if the shipment meets [rule] and carries [proof]; until origin is confirmed the study uses the MFN rate of [y]%."
3. **Unknown:** "We could not confirm the duty for this lane from an official source. It is left blank in the model and listed for verification with [customs broker or authority]."

## 6. Volatile regimes: dated search before closing any lane

Rates and their legal basis can change within months. Before marking a lane done, run a web search dated to the current month for recent changes affecting that importer's duties on that product or that origin: emergency or temporary surcharges, safeguards, anti-dumping or countervailing duties, retaliation measures, court rulings on the legal basis, quota changes. Record in `notes`: what was searched, the date, what was found (with its URL), or "no change found as of [date]". If a change is found but its application to this product is unclear, set the rate `missing` or use the cited figure and add the doubt to `notes`; the lane stays `verify`. The planner repeats the same check before delivery.

## 7. tariff_tool.py cheat-sheet

```
S=--study-dir STUDY       (needs inputs.json and selection.json; exit 2 if absent)
tariff_tool.py init $S
tariff_tool.py set-rate $S --lane ISO3|all --field duty_rate_pct --value N --tier published \
    --source "official tariff finder, HS 6-digit line" --url URL --as-of YYYY-MM-DD
    fields: duty_rate_pct mfn_rate_pct preference_rate_pct other_duties_pct import_vat_pct
    tiers: published connector user quote   (published/connector need --url or a source naming
    the official schedule, and --as-of; all tiers need --source)
tariff_tool.py set-info $S --lane ISO3|all [--agreement NAME] [--conditions TEXT] \
    [--valuation CIF|FOB] [--vat-recoverable true|false|unknown] [--add-ntb TEXT ...] [--notes TEXT]
tariff_tool.py check $S        # recomputes status, prints the table, always exit 0
tariff_tool.py show $S [--lane ISO3|all]
tariff_tool.py selftest
```

`init` never overwrites existing lane data; it adds lanes newly in `selection.json`. A rate you set is written as is, never defaulted.

**Status.** `sourced` = `duty_rate_pct` is present AND every rate that is present (duty, MFN, preference, other duties, VAT) has a URL or a non-empty source, plus an as-of date. Anything else = `verify`. `check` also lists what is still blank (other duties, VAT, VAT recoverability) so the planner can see the gap; record 0 with a source when a duty is checked and absent.

## 8. HS caveats

- HS is harmonised to 6 digits. Duty is set at national tariff-line level (8 to 12 digits), and the rate can differ between lines under the same 6-digit code. Look up the exact national line; if several lines fit, record the range in `notes`, use the line matching the client's specification, and say which.
- HS is revised periodically (about every five years). Match the revision used for the trade data with the importer's current schedule, and check the concordance when codes were split or merged.
- Classification is the importer's customs authority's decision, not ours or the client's. For a high-value lane, recommend a binding or advance ruling where the importer offers one.
- Rates for a product can differ by form (fresh, chilled, frozen, processed), packing size or end use; check that the line wording matches the product description in `inputs.json`.
