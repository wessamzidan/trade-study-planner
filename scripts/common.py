"""Shared helpers for the Trade Study Planner toolkit (schema 2.0).

Standard library only. Every script imports from here so that file names,
sourced-value handling and country resolution behave the same everywhere.
See references/contracts.md.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = "2.0"
SCRIPTS_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPTS_DIR / "data"

TIERS = ("user", "quote", "connector", "published", "modelled", "derived", "missing")
# Tiers a client can rely on without re-checking; everything else goes on the verification checklist.
FIRM_TIERS = ("user", "quote")

INCOTERMS = ("EXW", "FCA", "FOB", "CFR", "CIF", "DAP")
CONTAINER_TYPES = ("20DC", "40DC", "40HC", "20RF", "40RF")
CHARGE_KEYS = ("origin_inland", "origin_docs", "origin_thc", "destination_thc", "import_clearance", "destination_inland")

# Importers that commonly assess customs duty on FOB value. A starting point only: confirm per study.
FOB_VALUATION = ("USA", "CAN", "AUS", "NZL", "ZAF", "BWA", "NAM", "LSO", "SWZ")

FILES = {
    "env": "env.json",
    "inputs": "inputs.json",
    "query_plan": "query_plan.json",
    "flows": "flows.csv",
    "flows_meta": "flows_meta.json",
    "analysis": "analysis.json",
    "selection": "selection.json",
    "freight_inputs": "freight_inputs.json",
    "freight": "freight.json",
    "tariff": "tariff.json",
    "financial": "financial.json",
    "model_xlsx": "financial_model.xlsx",
    "narrative": "narrative.json",
    "report": "study.docx",
    "dashboard": "dashboard.html",
    "qa": "qa.json",
}

LABELS = {
    "export": {
        "home_role": "exporter", "partner_noun": "market", "partner_noun_plural": "markets",
        "tier_core": "Defend", "tier_growth": "Expand", "tier_new": "Open",
        "home_flow": "exports", "home_flow_code": "X", "mirror_flow_code": "M",
        "partner_trade": "imports", "primary_world_view": "world_imports",
        "study_title": "Export Feasibility Study", "direction": "home_to_partner",
    },
    "import": {
        "home_role": "importer", "partner_noun": "supplier", "partner_noun_plural": "suppliers",
        "tier_core": "Keep", "tier_growth": "Develop", "tier_new": "New source",
        "home_flow": "imports", "home_flow_code": "M", "mirror_flow_code": "X",
        "partner_trade": "exports", "primary_world_view": "world_exports",
        "study_title": "Import Sourcing Study", "direction": "partner_to_home",
    },
}


# ---------------------------------------------------------------- files

def study_file(study_dir, key: str) -> Path:
    """Path of a fixed-name file inside the study folder (see FILES)."""
    return Path(study_dir) / FILES[key]


def read_json(path, required: bool = True):
    p = Path(path)
    if not p.exists():
        if required:
            blocked(f"required file not found: {p.name} (expected at {p})")
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, allow_nan=False)
    return p


def blocked(msg: str):
    """Stop with exit code 2: a required input is missing. Never substitute a default instead."""
    print(f"BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


def today() -> str:
    return _dt.date.today().isoformat()


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- sourced values

def sv(value, tier: str, source: str = "", url=None, as_of=None) -> dict:
    """Build a sourced value. value=None is only legal with tier='missing'."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if value is None and tier != "missing":
        raise ValueError("a null value must have tier 'missing'")
    if value is not None and tier == "missing":
        raise ValueError("tier 'missing' must have a null value")
    return {"value": value, "tier": tier, "source": source, "url": url, "as_of": as_of}


def sv_missing(note: str = "") -> dict:
    return {"value": None, "tier": "missing", "source": note, "url": None, "as_of": None}


def sv_value(x):
    """Numeric value of an SV (or a bare number); None when missing/absent. Never returns a default."""
    if x is None:
        return None
    if isinstance(x, dict):
        v = x.get("value")
        if v is None or x.get("tier") == "missing":
            return None
        return float(v)
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def sv_tier(x) -> str:
    if isinstance(x, dict):
        return x.get("tier") or "missing"
    return "missing" if x is None else "user"


def sv_ok(x) -> bool:
    return sv_value(x) is not None


def sv_pick(block, partner_iso3=None):
    """Resolve {'default': SV, 'by_partner': {ISO3: SV}} to the SV for one partner (or None)."""
    if not isinstance(block, dict):
        return None
    by = block.get("by_partner") or {}
    if partner_iso3 and sv_ok(by.get(partner_iso3)):
        return by[partner_iso3]
    return block.get("default")


# ---------------------------------------------------------------- countries

def norm_name(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1)
def load_countries() -> dict:
    """Indexes over scripts/data/countries.json: by iso3, iso2, numeric code and normalised name/alias."""
    raw = json.loads((DATA_DIR / "countries.json").read_text(encoding="utf-8"))
    by_iso3, by_iso2, by_code, by_name = {}, {}, {}, {}
    for c in raw["countries"]:
        rec = {"iso3": c["iso3"], "iso2": c.get("iso2"), "name": c["name"], "comtrade_code": c.get("comtrade_code"),
               "m49": c.get("m49"), "kind": "country"}
        by_iso3[c["iso3"]] = rec
        if c.get("iso2"):
            by_iso2[c["iso2"]] = rec
        for code in {c.get("m49"), c.get("comtrade_code")}:
            if code is not None:
                by_code[int(code)] = rec
        for nm in [c["name"], c.get("official_name"), c.get("comtrade_name"), *c.get("aliases", [])]:
            if nm:
                by_name.setdefault(norm_name(nm), rec)
    for kind in ("special", "aggregates"):
        for c in raw.get(kind, []):
            rec = by_iso3.get(c["iso3"]) or {"iso3": c["iso3"], "iso2": None, "name": c["name"], "comtrade_code": c["code"],
                                             "m49": c["code"], "kind": "aggregate" if kind == "aggregates" else "area"}
            by_iso3.setdefault(c["iso3"], rec)
            by_code[int(c["code"])] = rec
            by_name.setdefault(norm_name(c["name"]), rec)
    return {"iso3": by_iso3, "iso2": by_iso2, "code": by_code, "name": by_name}


def resolve_country(x):
    """Resolve ISO3, ISO2, a numeric Comtrade/M49 code or a name/alias to a country record, else None."""
    if x is None:
        return None
    idx = load_countries()
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return idx["code"].get(int(x))
    s = str(x).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+", s):
        return idx["code"].get(int(s))
    up = s.upper()
    if up in idx["iso3"]:
        return idx["iso3"][up]
    if len(up) == 2 and up in idx["iso2"]:
        return idx["iso2"][up]
    return idx["name"].get(norm_name(s))


def country_name(iso3: str) -> str:
    rec = load_countries()["iso3"].get(iso3)
    return rec["name"] if rec else iso3


def is_rankable(iso3: str) -> bool:
    """True for real countries; False for World, aggregates and '_' areas."""
    rec = load_countries()["iso3"].get(iso3)
    return bool(rec) and rec.get("kind") == "country" and not iso3.startswith("_") and iso3 != "W00"


def labels(perspective: str) -> dict:
    if perspective not in LABELS:
        blocked(f"perspective must be 'export' or 'import', got {perspective!r}")
    return LABELS[perspective]
