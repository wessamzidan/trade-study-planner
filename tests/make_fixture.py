#!/usr/bin/env python3
"""Write clearly synthetic, Comtrade-shaped raw files for a study folder (test data only).

Usage:  python3 tests/make_fixture.py --study-dir DIR

Reads DIR/inputs.json (HS code, years, perspective, home) and DIR/selection.json (written with the demo
partners when absent), builds ONE global synthetic world (fixed random seed; every number is invented)
and writes one file per planned query as DIR/raw/SYNTHETIC_<query id>.<ext>, plus raw/_synthetic_truth.json
(ignored by normalize) that records what the quirks should produce.

Deliberate quirks: the last year is partial (about 30 % of reporters, lower values); one importer stops
reporting in the last two complete years; a hub market reports far less than the home country exports;
one market reports more; an apostrophe name and a comma name; EU aggregate rows; re-export rows;
transport-mode breakdown rows; a large market with no home presence; mixed file formats (Latin-1 UI CSV
with the trailing-comma quirk, UTF-8 UI CSV, API JSON with null *Desc/*ISO fields, generic JSON/CSV).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import trade_data  # noqa: E402

SEED = 7
DEMO_SELECTION = {"export": ["SAU", "ARE", "NLD", "GBR", "DEU", "KWT"], "import": ["EGY", "CHN", "BEL", "ESP", "IND"]}

# Comtrade-style reporter names, ordered roughly by market size (the order drives the synthetic size weights).
IMPORTER_NAMES = [
    "USA", "Germany", "United Kingdom", "France", "Netherlands", "Japan", "Canada", "Italy", "Spain", "Belgium",
    "Russian Federation", "Poland", "Saudi Arabia", "United Arab Emirates", "Rep. of Korea", "Switzerland",
    "Sweden", "Austria", "Australia", "Singapore", "Czechia", "Denmark", "Ireland", "Norway", "Kuwait", "Qatar",
    "Romania", "Greece", "Jordan", "Oman", "Bahrain", "Libya", "Sudan", "Côte d'Ivoire", "Kenya", "Lebanon",
    "Iraq", "Hungary", "Portugal", "Finland", "China, Hong Kong SAR", "Türkiye", "Malaysia", "Egypt", "India",
    "China", "Mexico", "Brazil"]
# exporter -> (synthetic annual scale in USD, synthetic unit value USD/kg)
EXPORTERS = {"NLD": (420e6, 1.45), "ESP": (380e6, 1.20), "BEL": (310e6, 1.30), "CHN": (300e6, 0.85),
             "POL": (160e6, 0.95), "FRA": (150e6, 1.35), "IND": (130e6, 0.80), "DEU": (110e6, 1.40),
             "MEX": (90e6, 1.00), "USA": (80e6, 1.50), "TUR": (70e6, 0.90), "ITA": (60e6, 1.35),
             "SAU": (4e6, 1.20), "EGY": (None, 1.05)}
# Home-specific destination values (USD in the latest complete year) for the EGY exporter.
EGY_DESTINATIONS = {"Saudi Arabia": 9.5e6, "United Arab Emirates": 6.1e6, "Netherlands": 4.2e6,
                    "United Kingdom": 3.3e6, "Russian Federation": 2.8e6, "Kuwait": 1.9e6, "Germany": 0.6e6,
                    "Qatar": 0.5e6, "Côte d'Ivoire": 0.9e6, "Libya": 0.8e6, "Sudan": 0.7e6, "Jordan": 0.45e6,
                    "China, Hong Kong SAR": 0.35e6, "Oman": 0.3e6, "Lebanon": 0.25e6, "France": 0.2e6,
                    "Iraq": 0.12e6, "Bahrain": 0.1e6, "Italy": 0.09e6, "Kenya": 0.06e6}
PROTECTED = ("SAU", "ARE", "NLD", "GBR", "DEU", "KWT", "EGY")   # never skipped as destinations


class World:
    """Global synthetic report table: rows of (year, reporter, partner, flow, value, kg)."""

    def __init__(self, years):
        self.years, self.rng = years, random.Random(SEED)
        self.iso, self.name_of = [], {}
        for nm in IMPORTER_NAMES:
            rec = common.resolve_country(nm)
            if not rec:
                raise SystemExit(f"fixture: name {nm!r} does not resolve in countries.json; add an alias")
            self.iso.append(rec["iso3"])
            self.name_of[rec["iso3"]] = nm
        self.rows = []
        self.truth = {}
        self._build()

    def _flow_series(self, e, c, base, g):
        n = len(self.years)
        return [base * (1 + g) ** (k - (n - 2)) * (self.partial_factor if k == n - 1 else 1) for k in range(n)]

    def _build(self):
        rng, yrs = self.rng, self.years
        self.partial_factor = rng.uniform(0.3, 0.5)
        pick = lambda pool, k: set(rng.sample(sorted(pool), k))
        self.part_imp = pick(set(self.iso) - {"EGY", "SAU"}, round(0.3 * len(self.iso)) - 2) | {"EGY", "SAU"}
        self.part_exp = pick(set(EXPORTERS) - {"EGY", "SAU"}, 2) | {"EGY", "SAU"}
        w = {c: 1 / (i + 1) ** 1.1 for i, c in enumerate(self.iso)}
        wsum, price = sum(w.values()), {c: rng.uniform(0.9, 1.3) for c in self.iso}
        gap = set(yrs[-3:-1])
        flows = {}
        for e, (scale, uv) in EXPORTERS.items():
            for c in self.iso:
                if c == e:
                    continue
                if e == "EGY":
                    base = next((v for nm, v in EGY_DESTINATIONS.items() if common.resolve_country(nm)["iso3"] == c), None)
                    if base is None:
                        continue
                    g = rng.uniform(0.0, 0.15)
                else:
                    if c not in PROTECTED and rng.random() < 0.12:
                        continue
                    base = scale * w[c] / wsum * 0.85 * rng.lognormvariate(0, 0.4)
                    g = rng.uniform(-0.05, 0.2)
                flows[(e, c)] = (self._flow_series(e, c, base, g), uv * price[c])
        ratio = {k: rng.uniform(0.95, 1.2) for k in flows}
        ratio[("EGY", "NLD")] = rng.uniform(0.22, 0.28)      # hub: reports far less than Egypt exports
        ratio[("EGY", "KWT")] = rng.uniform(1.65, 1.8)       # reports more than Egypt exports
        self._emit(flows, ratio, gap)

    def _emit(self, flows, ratio, gap):
        rng, yrs = self.rng, self.years
        last = yrs[-1]
        for side in ("X", "M"):
            parties = {}
            for (e, c), (series, uvp) in flows.items():
                owner, other = (e, c) if side == "X" else (c, e)
                if side == "M" and (e, c) == ("EGY", "SDN"):
                    continue                                   # Sudan shows nothing from Egypt
                for k, y in enumerate(yrs):
                    if y == last and owner not in (self.part_exp if side == "X" else self.part_imp):
                        continue
                    if side == "M" and owner == "ARE" and y in gap:
                        continue
                    fac = rng.uniform(0.98, 1.02) if side == "X" else ratio[(e, c)] * rng.uniform(0.97, 1.03)
                    val = max(1, round(series[k] * fac))
                    kg = max(1, round(series[k] / uvp * rng.uniform(0.99, 1.01)))
                    parties.setdefault((owner, y), []).append((other, val, kg))
            for (owner, y), items in sorted(parties.items()):
                tv, tk = sum(i[1] for i in items), sum(i[2] for i in items)
                rest = rng.uniform(0.15, 0.4)
                for other, val, kg in items:
                    self.rows.append({"year": y, "rep": owner, "par": other, "flow": side, "value": val, "kg": kg})
                self.rows.append({"year": y, "rep": owner, "par": "_899", "flow": side,
                                  "value": round(tv * rest), "kg": round(tk * rest)})
                self.rows.append({"year": y, "rep": owner, "par": "W00", "flow": side,
                                  "value": tv + round(tv * rest), "kg": tk + round(tk * rest)})
        self.truth["uae_gap_years"] = sorted(gap)


# ---------------------------------------------------------------- identities

def area_info(world: World, iso: str):
    """(numeric code, display name, ISO text) as Comtrade would show it."""
    special = {"W00": (0, "World", "W00"), "_899": (899, "Areas, nes", "_X"), "_EU": (97, "European Union", "EU")}
    if iso in special:
        return special[iso]
    rec = common.resolve_country(iso)
    return int(rec["comtrade_code"]), world.name_of.get(iso, rec["name"]), iso


# ---------------------------------------------------------------- quirk injection

def select_rows(world: World, q: dict) -> list:
    p = q["params"]
    return [dict(r) for r in world.rows
            if r["flow"] == p["flow"] and (p["reporter"] == "all" or r["rep"] == p["reporter"])
            and (p["partner"] == "all" or r["par"] == p["partner"])]


def inject_quirks(world: World, q: dict, rows: list, truth: dict, home: str) -> list:
    """Add EU aggregate rows, re-export rows and transport-mode breakdown rows where the query allows."""
    p, extra = q["params"], []
    if p["reporter"] == "all":
        for y in world.years:
            vals = [r["value"] for r in rows if r["year"] == y]
            if vals:
                extra.append({"year": y, "rep": "_EU", "par": rows[0]["par"] if p["partner"] != "all" else "W00",
                              "flow": p["flow"], "value": 2 * max(vals), "kg": 2 * max(vals)})
                truth["aggregate_rows"] += 1
    if q["id"] == "home_flows":
        dests = sorted({r["par"] for r in rows if r["par"] not in ("W00", "_899")})[:2]
        for y in world.years[:2]:
            for d in dests:
                extra.append({"year": y, "rep": home, "par": d, "flow": "R" + p["flow"], "value": 12345, "kg": 6789})
                truth["reexport_rows"] += 1
    if q["id"] == "world_exports":
        for y in world.years[:3]:
            extra.append({"year": y, "rep": "NLD", "par": "W00", "flow": "RX", "value": 99_999_999, "kg": 50_000_000})
            truth["reexport_rows"] += 1
    if q["id"] == "world_imports":
        y1 = world.years[1]
        for r in list(rows):
            if r["year"] == y1 and r["par"] == "W00" and r["rep"] == "GBR":
                extra += [{**r, "mot": 1000, "value": round(r["value"] * 0.7)}, {**r, "mot": 2000, "value": round(r["value"] * 0.3)}]
                truth["breakdown_with_total"] = {"reporter": "GBR", "year": y1, "value": r["value"], "flow": "M"}
            if r["year"] == y1 and r["par"] == "W00" and r["rep"] == "POL":
                a = round(r["value"] * 0.6)
                extra += [{**r, "mot": 1000, "value": a}, {**r, "mot": 2000, "value": r["value"] - a}]
                rows.remove(r)
                truth["breakdown_without_total"] = {"reporter": "POL", "year": y1, "value": r["value"], "flow": "M"}
    return rows + extra


# ---------------------------------------------------------------- serialisers

UI_HEADER = ("typeCode,freqCode,refPeriodId,refYear,refMonth,period,reporterCode,reporterISO,reporterDesc,flowCode,"
             "flowDesc,partnerCode,partnerISO,partnerDesc,partner2Code,partner2ISO,partner2Desc,classificationCode,"
             "classificationSearchCode,isOriginalClassification,cmdCode,cmdDesc,aggrLevel,isLeaf,customsCode,"
             "customsDesc,mosCode,motCode,motDesc,qtyUnitCode,qtyUnitAbbr,qty,isQtyEstimated,altQtyUnitCode,"
             "altQtyUnitAbbr,altQty,isAltQtyEstimated,netWgt,isNetWgtEstimated,grossWgt,isGrossWgtEstimated,"
             "cifvalue,fobvalue,primaryValue,legacyEstimationFlag,isReported,isAggregate").split(",")
FLOW_DESC = {"X": "Export", "M": "Import", "RX": "Re-export", "RM": "Re-import"}


def comtrade_record(world, r, hs, hs_desc, api: bool) -> dict:
    rc, rn, ri = area_info(world, r["rep"])
    pc, pn, pi = area_info(world, r["par"])
    mot = r.get("mot", 0)
    val = r["value"]
    rec = dict.fromkeys(UI_HEADER, "")
    rec.update({"typeCode": "C", "freqCode": "A", "refPeriodId": f"{r['year']}0101", "refYear": r["year"],
                "refMonth": 52, "period": r["year"], "reporterCode": rc, "reporterISO": ri, "reporterDesc": rn,
                "flowCode": r["flow"], "flowDesc": FLOW_DESC.get(r["flow"], r["flow"]), "partnerCode": pc,
                "partnerISO": pi, "partnerDesc": pn, "partner2Code": 0, "partner2ISO": "W00", "partner2Desc": "World",
                "classificationCode": "H6", "classificationSearchCode": "HS", "isOriginalClassification": "False",
                "cmdCode": hs, "cmdDesc": hs_desc, "aggrLevel": 6, "isLeaf": "True", "customsCode": "C00",
                "customsDesc": "TOTAL CPC", "mosCode": 0, "motCode": mot,
                "motDesc": "TOTAL MOT" if mot == 0 else f"SYNTHETIC mode {mot}", "qtyUnitCode": -1,
                "qtyUnitAbbr": "N/A", "netWgt": r["kg"], "isAggregate": "True" if r["rep"] == "_EU" else "False"})
    side = "cifvalue" if r["flow"] in ("M", "RM") else "fobvalue"
    rec[side] = val
    rec["primaryValue"] = "" if r.get("blank_primary") else val
    if api:
        for k in ("reporterISO", "reporterDesc", "partnerISO", "partnerDesc", "flowDesc", "cmdDesc", "qtyUnitAbbr"):
            rec[k] = None
        rec = {k: (None if v == "" else v) for k, v in rec.items()}
    return rec


def write_ui_csv(path, recs, encoding):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(UI_HEADER)
    text = buf.getvalue()
    lines = []
    for rec in recs:
        b = io.StringIO()
        csv.writer(b, lineterminator="").writerow([rec[h] for h in UI_HEADER])
        lines.append(b.getvalue() + ("," if encoding == "latin-1" else ""))   # trailing-comma quirk
    path.write_bytes((text + "\n".join(lines) + "\n").encode(encoding))


def generic_records(world, rows, hs, style):
    out = []
    for r in rows:
        def who(iso):
            code, name, _ = area_info(world, iso)
            return {"names": name, "iso3": ("World" if iso == "W00" else iso), "codes": str(code)}[style]
        out.append({"year": r["year"], "reporter": who(r["rep"]), "partner": who(r["par"]),
                    "flow": ("Export" if r["flow"] == "X" else "Import") if style == "names" else r["flow"],
                    "value_usd": r["value"], "net_kg": r["kg"], "qty": None, "qty_unit": None, "hs_code": hs})
    return out


KINDS_BASE = {"home_flows": ("ui_latin1", "csv"), "world_imports": ("ui_latin1", "csv"),
              "world_exports": ("api", "json"), "mirror": ("api", "json"), "matrix": ("ui_latin1", "csv")}
DETAIL_KINDS = [("generic_names", "json"), ("generic_codes", "csv"), ("ui_utf8", "csv"), ("generic_iso3", "csv")]


def kind_for(qid, n_detail):
    if qid in KINDS_BASE:
        return KINDS_BASE[qid]
    return DETAIL_KINDS[min(n_detail, len(DETAIL_KINDS) - 1)] if n_detail < 3 else ("generic_iso3", "csv")


def write_query_file(world, q, rows, kind, ext, hs, hs_desc, path):
    if kind in ("ui_latin1", "ui_utf8"):
        recs = [comtrade_record(world, r, hs, hs_desc, False) for r in rows]
        write_ui_csv(path, recs, "latin-1" if kind == "ui_latin1" else "utf-8")
    elif kind == "api":
        recs = [comtrade_record(world, r, hs, hs_desc, True) for r in rows]
        path.write_text(json.dumps({"elapsedTime": "synthetic", "count": len(recs), "data": recs, "error": ""},
                                   ensure_ascii=False), encoding="utf-8")
    else:
        style = kind.split("_")[1]
        recs = generic_records(world, rows, hs, style)
        if ext == "json":
            path.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
        else:
            cols = ["year", "reporter", "partner", "flow", "value_usd", "net_kg", "qty", "qty_unit", "hs_code"]
            with open(path, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                w.writerows([[("" if r[c] is None else r[c]) for c in cols] for r in recs])


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write synthetic Comtrade-shaped raw files into a study folder.")
    ap.add_argument("--study-dir", required=True)
    args = ap.parse_args(argv)
    sd = Path(args.study_dir)
    ctx = trade_data.load_inputs(sd)
    sel_path = common.study_file(sd, "selection")
    if not sel_path.exists():
        common.write_json(sel_path, {"schema_version": common.SCHEMA_VERSION, "partners": DEMO_SELECTION[ctx["perspective"]],
                                     "decided_by": "demo", "notes": {}})
    partners = trade_data.selected_partners(sd, None, ctx["home"])
    plan = trade_data.build_plan(ctx, partners, matrix=False)
    world = World(ctx["years"])
    hs_desc = (ctx["inputs"].get("product") or {}).get("hs_description") or "SYNTHETIC commodity"
    raw = sd / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    truth = {"synthetic": True, "seed": SEED, "years": ctx["years"], "latest_complete_year": ctx["years"][-2],
             "partial_year": ctx["years"][-1], "aggregate_rows": 0, "reexport_rows": 0, "files": {}, **world.truth}
    n_detail = 0
    for q in plan["queries"]:
        rows = select_rows(world, q)
        if not rows:
            print(f"note: no synthetic rows for {q['id']}; file not written")
            continue
        rows = inject_quirks(world, q, rows, truth, ctx["home"])
        if q["id"] == "world_imports":
            for r in [r for r in rows if r["par"] == "W00" and "mot" not in r][:3]:
                r["blank_primary"] = True
            truth["blank_primary_rows"] = 3
        kind, ext = kind_for(q["id"], n_detail)
        n_detail += q["id"].startswith("partner_detail")
        path = raw / f"SYNTHETIC_{trade_data.id_stem(q['id'])}.{ext}"
        write_query_file(world, q, rows, kind, ext, ctx["hs"], hs_desc, path)
        truth["files"][path.name] = {"kind": kind, "rows": len(rows)}
    truth["partial_importers"] = sorted(world.part_imp)
    truth["partial_exporters"] = sorted(world.part_exp)
    common.write_json(raw / "_synthetic_truth.json", truth)
    print(f"wrote {len(truth['files'])} synthetic files to {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
