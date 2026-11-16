#!/usr/bin/env python3
"""Stage 3 of the Trade Study Planner: turn flows.csv into analysis.json (contracts sections 4 and 4.1).

Reads inputs.json, flows.csv and flows_meta.json from the study folder; writes analysis.json.
Works for both perspectives: `export` (partners are destination markets) and `import` (partners are suppliers).
Missing data stays null: nothing is defaulted, and a partner that does not report is never given a zero.

Usage:
    python3 analyze_trade.py --study-dir DIR [--latest-year Y] [--base-year Y] [--core-share-pct 5]
                             [--core-top-n 3] [--min-presence-share-pct 0.25] [--material-partner-share-pct 0.25]

Exit codes: 0 ok, 2 BLOCKED (a required file or the primary world view is missing), 1 error.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SCORE_WEIGHTS = {"size": 0.35, "growth": 0.25, "price": 0.15, "foothold": 0.15, "openness": 0.10}
COMPLETE_YEAR_MIN_RATIO = 0.70   # reporters vs median of the three previous years
BASE_YEAR_LAG = 3
MAX_CORE, TOP_GROWTH, TOP_NEW = 6, 5, 5
MIRROR_LOW, MIRROR_HIGH = 0.75, 1.35
SCORE_MIN_COMPONENTS = 2
MIN_DETAIL_PARTNERS = 2   # a partner's own report needs this many counterparties besides home (else it is just a mirror row)


# =============================================================== data access

class Flows:
    """Indexed view of flows.csv: lookups by (flow, reporter, partner, year)."""

    def __init__(self, df: pd.DataFrame):
        g = df.groupby(["flow", "reporter_iso3", "partner_iso3", "year"], as_index=False)[["value_usd", "net_kg"]].sum(min_count=1)
        self.cell = {}
        for r in g.itertuples(index=False):
            self.cell[(r.flow, r.reporter_iso3, r.partner_iso3, int(r.year))] = (
                None if pd.isna(r.value_usd) else float(r.value_usd), None if pd.isna(r.net_kg) else float(r.net_kg))
        self._world, self._partners = defaultdict(dict), defaultdict(dict)
        for (flow, rep, par, year), v in self.cell.items():
            if v[0] is None:
                continue
            if par == "W00":
                if common.is_rankable(rep):
                    self._world[(flow, year)][rep] = v
            else:
                self._partners[(flow, rep, year)][par] = v
        self.names = {}
        for r in df.itertuples(index=False):
            self.names.setdefault(r.reporter_iso3, r.reporter_name)
            self.names.setdefault(r.partner_iso3, r.partner_name)
        self.years = sorted({int(y) for y in df["year"].unique()})

    def value(self, flow, rep, par, year):
        c = self.cell.get((flow, rep, par, year))
        return c[0] if c else None

    def kg(self, flow, rep, par, year):
        c = self.cell.get((flow, rep, par, year))
        return c[1] if c else None

    def world_year(self, flow, year) -> dict:
        """{reporter: (value, kg)} for rankable reporters with a World-partner row in `year`."""
        return self._world.get((flow, year), {})

    def partners_of(self, flow, rep, year) -> dict:
        """{partner: (value, kg)} for every non-World partner row of `rep`."""
        return self._partners.get((flow, rep, year), {})

    def name(self, iso):
        return common.country_name(iso) if common.is_rankable(iso) else self.names.get(iso, iso)


def unit_value(value, kg):
    return round(value / kg, 4) if value is not None and kg and kg > 0 else None


def cagr(v0, v1, y0, y1):
    """Compound annual growth in percent; None unless both ends are positive and y1 > y0."""
    if v0 is None or v1 is None or v0 <= 0 or v1 <= 0 or y1 <= y0:
        return None
    return round(((v1 / v0) ** (1.0 / (y1 - y0)) - 1.0) * 100.0, 2)


def pct_ranks(values: dict) -> dict:
    """Rank-based percentile in [0, 100] (average rank for ties). A single value has no percentile (None)."""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    out = {k: None for k in values}
    if n < 2:
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][0] == items[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            out[items[t][1]] = round((avg_rank - 1.0) / (n - 1.0) * 100.0, 1)
        i = j + 1
    return out


# =============================================================== years

def reporter_counts(fl: Flows, flow: str) -> dict:
    return {y: len(fl.world_year(flow, y)) for y in fl.years}


def detect_years(counts: dict, years: list, latest_arg, base_arg) -> tuple:
    """Return (latest_complete_year, base_year, partial_years)."""
    latest = latest_arg
    if latest is None:
        for y in sorted((y for y in years if counts.get(y)), reverse=True):
            prior = [counts[p] for p in sorted((p for p in years if p < y and counts.get(p)), reverse=True)[:3]]
            if not prior or counts[y] >= COMPLETE_YEAR_MIN_RATIO * statistics.median(prior):
                latest = y
                break
    if latest is None:
        common.blocked("no year with World-partner rows in the primary view")
    base = base_arg
    if base is None:
        base = latest - BASE_YEAR_LAG if (latest - BASE_YEAR_LAG) in years else min(years)
    return latest, base, [y for y in years if y > latest]


# =============================================================== blocks

def world_block(fl: Flows, flow: str, years: list, latest: int, base: int, partial: list):
    """annual totals + full ranking for one World view (None when the view has no rows)."""
    annual = []
    for y in years:
        w = fl.world_year(flow, y)
        if w:
            annual.append({"year": y, "value_usd": sum(v[0] for v in w.values()), "n_reporters": len(w), "partial": y in partial})
    if not annual or max(a["n_reporters"] for a in annual) < 2:
        return None   # a single reporter (usually home's own row) is not a world view
    cur = fl.world_year(flow, latest)
    total = sum(v[0] for v in cur.values())
    ranking = []
    for rank, (iso, (val, kg)) in enumerate(sorted(cur.items(), key=lambda kv: (-kv[1][0], kv[0])), 1):
        ranking.append({"iso3": iso, "name": fl.name(iso), "value_usd": val,
                        "share_pct": round(val / total * 100, 4) if total > 0 else None,
                        "cagr_pct": cagr(fl.value(flow, iso, "W00", base), val, base, latest),
                        "unit_value_usd_per_kg": unit_value(val, kg), "rank": rank})
    return {"annual": annual, "ranking": ranking}


def home_total(fl: Flows, flow: str, home: str, year: int):
    """(value, kg, derived) of the home total: its World row, else the sum of its partner rows."""
    v, k = fl.value(flow, home, "W00", year), fl.kg(flow, home, "W00", year)
    if v is not None:
        return v, k, False
    rows = fl.partners_of(flow, home, year)
    if not rows:
        return None, None, False
    kgs = [r[1] for r in rows.values() if r[1] is not None]
    return sum(r[0] for r in rows.values()), (sum(kgs) if len(kgs) == len(rows) else None), True


def home_position(fl, flow, home, years, latest, base, partial, other_block, warns) -> dict:
    annual, derived_years = [], []
    for y in years:
        v, kg, derived = home_total(fl, flow, home, y)
        if v is None:
            continue
        if derived:
            derived_years.append(y)
        annual.append({"year": y, "value_usd": v, "net_kg": kg, "unit_value_usd_per_kg": unit_value(v, kg), "partial": y in partial})
    if derived_years:
        warns.append(f"home total for {derived_years} derived as the sum of partner rows (no World row)")
    cur = next((a for a in annual if a["year"] == latest), None)
    rank = share = n_ranked = None
    if other_block:
        n_ranked = len(other_block["ranking"])
        row = next((r for r in other_block["ranking"] if r["iso3"] == home), None)
        if row:
            rank, share = row["rank"], row["share_pct"]
        else:
            warns.append("home country is absent from the world ranking in the latest complete year")
    else:
        warns.append("world rank of home not computed: the other world view was not loaded")
    return {"annual": annual, "latest_value_usd": cur["value_usd"] if cur else None,
            "cagr_pct": cagr(home_total(fl, flow, home, base)[0], cur["value_usd"] if cur else None, base, latest),
            "world_rank": rank, "world_share_pct": share, "n_ranked": n_ranked}


# =============================================================== partner detail

def build_detail(entries: list, nonrank_sum: float, world_total, home: str, basis: str, year: int) -> dict | None:
    """Detail of one partner's counterparties. Shares are of the partner's TOTAL.

    entries: [{iso3, name, value, kg}] for rankable counterparties; nonrank_sum: value of non-country areas
    (kept in the total, not listed); world_total: the partner's World row or None."""
    listed = sum(e["value"] for e in entries)
    parts_sum = listed + nonrank_sum
    if parts_sum <= 0:
        return None
    if world_total is not None and world_total >= parts_sum * (1 - 1e-9):
        total, note = world_total, "total is the partner's World row"
    else:
        total = parts_sum
        note = "total is the sum of all listed rows (no World row available)" if world_total is None else \
            "World row is smaller than the sum of its rows; total is the sum of rows"
    rows = []
    for e in sorted(entries, key=lambda e: (-e["value"], e["iso3"])):
        rows.append({"iso3": e["iso3"], "name": e["name"], "value_usd": e["value"], "share_pct": round(e["value"] / total * 100, 4),
                     "unit_value_usd_per_kg": unit_value(e["value"], e["kg"])})
    hrow = next((i for i, r in enumerate(rows, 1) if r["iso3"] == home), None)
    return {"basis": basis, "year": year, "total_usd": total, "rows": rows,
            "home_rank": hrow, "home_share_pct": rows[hrow - 1]["share_pct"] if hrow else None,
            "hhi": round(sum((r["share_pct"] / 100.0) ** 2 for r in rows), 4),
            "basis_note": note + "; shares are percentages of that total, non-country areas are in the total but not listed; "
                                 "HHI covers listed rows only"}


def partner_detail_for(fl: Flows, p: str, ctx: dict, latest: int, views: dict) -> dict | None:
    """Prefer the partner's own report; fall back to counterparties' reports from the matrix (export only)."""
    lab, home = ctx["lab"], ctx["home"]
    own = fl.partners_of(lab["mirror_flow_code"], p, latest)
    if len([k for k in own if k != home]) >= MIN_DETAIL_PARTNERS:
        entries = [{"iso3": k, "name": fl.name(k), "value": v[0], "kg": v[1]} for k, v in own.items() if common.is_rankable(k)]
        other = sum(v[0] for k, v in own.items() if not common.is_rankable(k))
        return build_detail(entries, other, fl.value(lab["mirror_flow_code"], p, "W00", latest), home, "partner_reported", latest)
    if ctx["perspective"] == "export" and views.get("matrix"):
        rows = {k[1]: v for k, v in fl.cell.items() if k[0] == "X" and k[2] == p and k[3] == latest and v[0] is not None}
        entries = [{"iso3": k, "name": fl.name(k), "value": v[0], "kg": v[1]} for k, v in rows.items() if common.is_rankable(k)]
        return build_detail(entries, 0.0, None, home, "counterparty_reported", latest)
    return None


# =============================================================== partners

def candidate_partners(fl: Flows, ctx: dict, warns: list) -> list:
    lab, home = ctx["lab"], ctx["home"]
    cands = {k[1] for k in fl.cell if k[0] == ctx["primary_flow"] and k[2] == "W00" and common.is_rankable(k[1])}
    hf_partners = {k[2] for k in fl.cell if k[0] == lab["home_flow_code"] and k[1] == home and k[2] != "W00"}
    skipped = sorted(p for p in hf_partners if not common.is_rankable(p))
    if skipped:
        warns.append(f"home flows to non-country areas {skipped} are in totals but not ranked")
    cands |= {p for p in hf_partners if common.is_rankable(p)}
    return sorted(cands - {home} - set(ctx["inputs"].get("partners_excluded") or []))


def partner_facts(fl, ctx, p, latest, base, world_total, htotal) -> dict:
    lab, home, pf = ctx["lab"], ctx["home"], ctx["primary_flow"]
    hf, mf = lab["home_flow_code"], lab["mirror_flow_code"]
    pt, pkg = fl.value(pf, p, "W00", latest), fl.kg(pf, p, "W00", latest)
    hv = fl.value(hf, home, p, latest)
    by_year = {str(y): fl.value(hf, home, p, y) for y in fl.years if fl.value(hf, home, p, y) is not None}
    mirror = fl.value(mf, p, home, latest)
    return {
        "iso3": p, "name": fl.name(p),
        "partner_trade_latest_usd": pt,
        "partner_trade_share_pct": round(pt / world_total * 100, 4) if pt is not None and world_total else None,
        "partner_trade_cagr_pct": cagr(fl.value(pf, p, "W00", base), pt, base, latest),
        "partner_unit_value_usd_per_kg": unit_value(pt, pkg), "partner_reports_latest": pt is not None,
        "home_flow_latest_usd": hv,
        "home_flow_share_of_home_pct": round(hv / htotal * 100, 4) if hv is not None and htotal else None,
        "home_flow_cagr_pct": cagr(fl.value(hf, home, p, base), hv, base, latest),
        "home_flow_by_year": by_year, "home_unit_value_usd_per_kg": unit_value(hv, fl.kg(hf, home, p, latest)),
        "mirror_flow_latest_usd": mirror,
        "home_share_in_partner_pct": round(mirror / pt * 100, 4) if mirror is not None and pt else None,
    }


def assign_tiers(rows: list, rules: dict) -> None:
    """Set row['tier'] per contracts 4.1 (core / growth / new / minor)."""
    present = sorted((r for r in rows if (r["home_flow_latest_usd"] or 0) > 0),
                     key=lambda r: (-r["home_flow_latest_usd"], r["iso3"]))
    top = {r["iso3"] for r in present[:rules["core_top_n"]]}
    for r in rows:
        share, mat = r["home_flow_share_of_home_pct"], r["partner_trade_share_pct"]
        if (share is not None and share >= rules["core_share_pct"]) or r["iso3"] in top:
            r["tier"] = "core"
        elif share is not None and share >= rules["min_presence_share_pct"]:
            r["tier"] = "growth"
        elif mat is not None and mat >= rules["material_partner_share_pct"]:
            r["tier"] = "new"
        else:
            r["tier"] = "minor"


def score_partners(rows: list, details: dict, perspective: str, rules: dict) -> None:
    """Fill score_components and score (weights re-normalised over non-null components)."""
    size = pct_ranks({r["iso3"]: r["partner_trade_latest_usd"] for r in rows})
    mat = rules["material_partner_share_pct"]
    growth = pct_ranks({r["iso3"]: r["partner_trade_cagr_pct"] for r in rows
                        if r["partner_trade_share_pct"] is not None and r["partner_trade_share_pct"] >= mat})
    price = pct_ranks({r["iso3"]: r["partner_unit_value_usd_per_kg"] for r in rows})
    foot = pct_ranks({r["iso3"]: (r["home_flow_latest_usd"] or 0.0) for r in rows})
    for r in rows:
        i = r["iso3"]
        pr = price.get(i)
        if pr is not None and perspective == "import":
            pr = round(100.0 - pr, 1)
        has_foot = (r["home_flow_latest_usd"] or 0) > 0
        comp = {"size": size.get(i), "growth": growth.get(i), "price": pr,
                "foothold": (foot.get(i) if has_foot else (0.0 if foot.get(i) is not None else None)),
                "openness": round(100.0 * (1 - details[i]["hhi"]), 1) if i in details else None}
        r["score_components"] = comp
        live = {k: v for k, v in comp.items() if v is not None}
        wsum = sum(SCORE_WEIGHTS[k] for k in live)
        scored = comp["size"] is not None and len(live) >= SCORE_MIN_COMPONENTS
        r["score"] = round(sum(SCORE_WEIGHTS[k] * v for k, v in live.items()) / wsum, 1) if scored else None


def mirror_status(row: dict, mirror_loaded: bool) -> tuple:
    ratio = None
    h, m = row["home_flow_latest_usd"], row["mirror_flow_latest_usd"]
    if not mirror_loaded:
        return "no_mirror_data", None, "mirror view not loaded"
    if not row["partner_reports_latest"]:
        return "partner_not_reporting", None, "partner has no World-partner row in the latest complete year"
    if m is None:
        return "only_home_reports", None, "partner reports the product but shows nothing from/to home"
    ratio = round(m / h, 3) if h else None
    if ratio is None:
        return "only_home_reports", None, "home-reported value is zero"
    if ratio < MIRROR_LOW:
        return "partner_reports_less", ratio, "possible hub routing or re-export, classification difference or timing"
    if ratio > MIRROR_HIGH:
        return "partner_reports_more", ratio, "possible indirect shipments attributed by origin"
    return "consistent", ratio, "within the expected CIF/FOB band"


def mirror_checks(rows: list, rules: dict, latest: int, mirror_loaded: bool) -> list:
    out = []
    for r in rows:
        share = r["home_flow_share_of_home_pct"]
        if share is None or share < rules["min_presence_share_pct"]:
            continue
        status, ratio, note = mirror_status(r, mirror_loaded)
        out.append({"iso3": r["iso3"], "name": r["name"], "year": latest, "home_reported_usd": r["home_flow_latest_usd"],
                    "partner_reported_usd": r["mirror_flow_latest_usd"], "ratio": ratio, "status": status, "note": note})
    return out


def build_shortlist(rows: list, interest: list) -> dict:
    by_score = lambda tier: [r["iso3"] for r in sorted((r for r in rows if r["tier"] == tier and r["score"] is not None),
                                                       key=lambda r: (-r["score"], r["iso3"]))][:TOP_NEW]
    core = [r["iso3"] for r in sorted((r for r in rows if r["tier"] == "core"),
                                      key=lambda r: (-(r["home_flow_latest_usd"] or 0), r["iso3"]))][:MAX_CORE]
    sl = {"core": core, "growth": by_score("growth")[:TOP_GROWTH], "new": by_score("new")[:TOP_NEW]}
    tiers = {r["iso3"]: r["tier"] for r in rows}
    for p in interest:
        tier = tiers.get(p)
        if tier is None:
            continue
        key = "new" if tier == "minor" else tier
        if p not in sl[key]:
            sl[key].append(p)
    return sl


# =============================================================== main analysis

def load_context(args) -> dict:
    sd = args.study_dir
    inputs = common.read_json(common.study_file(sd, "inputs"))
    meta = common.read_json(common.study_file(sd, "flows_meta"))
    fpath = common.study_file(sd, "flows")
    if not fpath.exists():
        common.blocked("flows.csv not found: run 'trade_data.py normalize' first")
    lab = common.labels(inputs.get("perspective"))
    home = common.resolve_country(inputs.get("home_country"))
    if not home:
        common.blocked(f"home_country {inputs.get('home_country')!r} unknown")
    df = pd.read_csv(fpath, dtype={"hs_code": str, "reporter_iso3": str, "partner_iso3": str}, keep_default_na=False,
                     na_values=[""])
    if df.empty:
        common.blocked("flows.csv has no rows")
    primary_flow = "M" if lab["primary_world_view"] == "world_imports" else "X"
    return {"inputs": inputs, "meta": meta, "lab": lab, "perspective": inputs["perspective"], "home": home["iso3"],
            "df": df, "primary_flow": primary_flow, "other_flow": "X" if primary_flow == "M" else "M"}


def analyze(args) -> dict:
    ctx = load_context(args)
    inputs, lab, home, persp = ctx["inputs"], ctx["lab"], ctx["home"], ctx["perspective"]
    views = ctx["meta"].get("views", {})
    fl, warns = Flows(ctx["df"]), []
    pv_name = lab["primary_world_view"]
    if not any(k[0] == ctx["primary_flow"] and k[2] == "W00" and common.is_rankable(k[1]) for k in fl.cell):
        common.blocked(f"primary world view '{pv_name}' is absent: load that query (see query_plan.json) and re-run normalize")
    latest, base, partial = detect_years(reporter_counts(fl, ctx["primary_flow"]), fl.years, args.latest_year, args.base_year)
    if base not in fl.years:
        warns.append(f"base year {base} has no data; growth rates will be null")
    rules = {"core_share_pct": args.core_share_pct, "core_top_n": args.core_top_n,
             "min_presence_share_pct": args.min_presence_share_pct,
             "material_partner_share_pct": args.material_partner_share_pct, "score_weights": dict(SCORE_WEIGHTS),
             "score_min_components": SCORE_MIN_COMPONENTS}
    blocks = {name: world_block(fl, f, fl.years, latest, base, partial)
              for name, f in (("world_imports", "M"), ("world_exports", "X"))}
    for name, b in blocks.items():
        if b is None:
            warns.append(f"{name} view not loaded: its block is null")
    other_name = "world_exports" if pv_name == "world_imports" else "world_imports"
    hflow = lab["home_flow_code"]
    pos = home_position(fl, hflow, home, fl.years, latest, base, partial, blocks[other_name], warns)
    primary_total = sum(v[0] for v in fl.world_year(ctx["primary_flow"], latest).values())
    htotal = home_total(fl, hflow, home, latest)[0]
    if htotal is None:
        warns.append("home total in the latest complete year is missing: home shares and tiers by share are null")
    partners = [partner_facts(fl, ctx, p, latest, base, primary_total, htotal) for p in candidate_partners(fl, ctx, warns)]
    details = {}
    for r in partners:
        d = partner_detail_for(fl, r["iso3"], ctx, latest, views)
        if d:
            details[r["iso3"]] = d
    assign_tiers(partners, rules)
    score_partners(partners, details, persp, rules)
    for r in partners:
        r["of_interest"] = r["iso3"] in (inputs.get("partners_of_interest") or [])
        r["notes"] = partner_notes(r, latest, lab)
    partners = [order_partner_row(r) for r in partners]
    for p in inputs.get("partners_of_interest") or []:
        if p not in {r["iso3"] for r in partners}:
            warns.append(f"partner of interest {p} has no data in the loaded views; not scored")
    return {
        "schema_version": common.SCHEMA_VERSION,
        "meta": {"perspective": persp, "home": {"iso3": home, "name": common.country_name(home)},
                 "hs_code": inputs["product"]["hs_code"], "product_name": inputs["product"].get("name"),
                 "years_available": fl.years, "latest_complete_year": latest, "base_year": base, "partial_years": partial,
                 "window_label": f"{base}–{latest}", "views": views,
                 "labels": {k: lab[k] for k in ("partner_noun", "tier_core", "tier_growth", "tier_new")},
                 "rules": rules, "warnings": warns, "generated_at": common.now_iso()},
        "home_position": pos, "world_imports": blocks["world_imports"], "world_exports": blocks["world_exports"],
        "partners": partners, "shortlist": build_shortlist(partners, inputs.get("partners_of_interest") or []),
        "mirror_checks": mirror_checks(partners, rules, latest, bool(views.get("mirror"))),
        "partner_detail": details,
    }


def partner_notes(r: dict, latest: int, lab: dict) -> list:
    notes = []
    if not r["partner_reports_latest"]:
        notes.append(f"no {lab['partner_trade']} report from this partner for {latest}; its size, growth and price are unknown")
    if (r["home_flow_latest_usd"] or 0) == 0:
        notes.append(f"no home {lab['home_flow']} recorded in {latest}")
    missing = [k for k, v in r["score_components"].items() if v is None]
    if r["score"] is None:
        if r["score_components"]["size"] is None:
            notes.append(f"not scored: no {lab['partner_trade']} reported by this partner for {latest}")
        else:
            notes.append(f"not scored: fewer than {SCORE_MIN_COMPONENTS} score components available")
    elif missing:
        notes.append(f"score uses only the available components (no data for: {', '.join(missing)})")
    return notes


PARTNER_KEYS = ["iso3", "name", "tier", "score", "score_components", "partner_trade_latest_usd", "partner_trade_share_pct",
                "partner_trade_cagr_pct", "partner_unit_value_usd_per_kg", "partner_reports_latest", "home_flow_latest_usd",
                "home_flow_share_of_home_pct", "home_flow_cagr_pct", "home_flow_by_year", "home_unit_value_usd_per_kg",
                "mirror_flow_latest_usd", "home_share_in_partner_pct", "of_interest", "notes"]


def order_partner_row(r: dict) -> dict:
    return {k: r[k] for k in PARTNER_KEYS}


def summary_lines(a: dict) -> list:
    m, h = a["meta"], a["home_position"]
    tiers = defaultdict(int)
    for p in a["partners"]:
        tiers[p["tier"]] += 1
    flags = defaultdict(list)
    for c in a["mirror_checks"]:
        flags[c["status"]].append(c["iso3"])
    sl = a["shortlist"]
    fmt = lambda x: "n/a" if x is None else f"{x:,.0f}"
    return [
        f"Analysis: {m['perspective']} | home {m['home']['iso3']} {m['home']['name']} | HS {m['hs_code']}",
        f"Years {m['years_available'][0]}-{m['years_available'][-1]}; latest complete {m['latest_complete_year']}; "
        f"partial {m['partial_years'] or 'none'}; window {m['window_label']}",
        f"Home latest: USD {fmt(h['latest_value_usd'])}, CAGR {h['cagr_pct']}%, world rank {h['world_rank']}/{h['n_ranked']}, "
        f"share {h['world_share_pct']}%",
        f"{len(a['partners'])} {m['labels']['partner_noun']} candidates; tiers: " +
        ", ".join(f"{t} {tiers[t]}" for t in ("core", "growth", "new", "minor")),
        f"Shortlist core: {sl['core']}", f"Shortlist growth: {sl['growth']}", f"Shortlist new: {sl['new']}",
        "Mirror: " + (", ".join(f"{k} {len(v)}" for k, v in sorted(flags.items())) or "no checks"),
        "Mirror flags: " + (", ".join(f"{s}={v}" for s, v in sorted(flags.items()) if s != "consistent") or "none"),
        f"Partner detail for {len(a['partner_detail'])} partners; {len(m['warnings'])} warnings",
    ]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Build analysis.json from flows.csv (both perspectives).")
    ap.add_argument("--study-dir", required=True)
    ap.add_argument("--latest-year", type=int, help="override the detected latest complete year")
    ap.add_argument("--base-year", type=int, help="override the growth base year")
    ap.add_argument("--core-share-pct", type=float, default=5.0)
    ap.add_argument("--core-top-n", type=int, default=3)
    ap.add_argument("--min-presence-share-pct", type=float, default=0.25)
    ap.add_argument("--material-partner-share-pct", type=float, default=0.25)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        a = analyze(args)
        common.write_json(common.study_file(args.study_dir, "analysis"), a)
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001 - last-resort reporting, exit code 1
        print(f"ERROR: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    print("\n".join(summary_lines(a)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
