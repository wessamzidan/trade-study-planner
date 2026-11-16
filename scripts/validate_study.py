#!/usr/bin/env python3
"""QA gate for a study folder (contracts section 10): writes qa.json, exit 0 if passed else 2.

    python3 validate_study.py --study-dir D [--stage N] [--no-recalc]

Stages: 1 inputs . 2 trade data . 3 analysis+selection . 4 freight . 5 tariff . 6 financial (+xlsx) . 7 narrative+outputs.
Default: every stage whose primary file is present. With --stage N: stages 1..N, and a missing primary file is an error
(freight/tariff/financial only for a full study with commercial inputs; outputs only when N >= 7).
Also provides collect_svs() / verify_rows() / verify_list() which build_report.py reuses for Appendix A.
"""
from __future__ import annotations

import argparse
import bisect
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

NARRATIVE_REQUIRED_FULL = ["executive_summary", "scope_method", "home_position", "global_landscape", "competition",
                           "logistics_access", "shortlist", "financial_case", "risks", "next_steps"]
NARRATIVE_REQUIRED_QUICK = ["executive_summary", "scope_method", "home_position", "global_landscape", "shortlist", "next_steps"]
XLSX_ERRORS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!", "#NULL!", "Err:")
LOCAL_CHARGE_KEYS = common.CHARGE_KEYS


# ------------------------------------------------------------------ helpers shared with build_report

def is_sv(x) -> bool:
    return isinstance(x, dict) and "tier" in x and "value" in x


def narrative_gaps(narr: dict, depth: str, commercial_present: bool) -> list:
    """Names of required narrative sections that are empty. Single definition used by report and QA."""
    req = list(NARRATIVE_REQUIRED_QUICK if depth == "quick_scan" else NARRATIVE_REQUIRED_FULL)
    if depth != "quick_scan" and not commercial_present:
        req.remove("financial_case")
    secs = (narr or {}).get("sections") or {}
    gaps = []

    def txt(v):
        return isinstance(v, str) and v.strip() != ""

    def lst(v):
        return isinstance(v, list) and any((txt(i) if isinstance(i, str) else bool(i)) for i in v)

    for name in req:
        if name == "executive_summary":
            e = (narr or {}).get("executive_summary") or {}
            if not (txt(e.get("headline")) and lst(e.get("paragraphs")) and lst(e.get("recommendations")) and txt(e.get("key_caveat"))):
                gaps.append(name)
            continue
        s = secs.get(name) or {}
        if name == "risks":
            ok = lst(s.get("diagnostic")) and lst(s.get("data_limits"))
        elif name == "next_steps":
            ok = lst(s.get("two_weeks")) and lst(s.get("thirty_days")) and lst(s.get("ninety_days"))
        else:
            ok = lst(s.get("paragraphs")) or lst(s.get("bullets"))
        if not ok:
            gaps.append(name)
    return gaps


def _lane_label(iso, analysis):
    for p in (analysis or {}).get("partners", []):
        if p.get("iso3") == iso:
            return p.get("name") or iso
    return common.country_name(iso)


def collect_svs(study: dict) -> list:
    """Every sourced value used in the study: inputs_echo (or inputs.commercial) + freight + tariff, de-duplicated."""
    rows, seen = [], set()

    def add(key, lane, sv_, unit="", origin=""):
        if not is_sv(sv_):
            return
        k = (lane, key)
        if k in seen:
            return
        seen.add(k)
        rows.append({"key": key, "lane": lane, "value": sv_.get("value"), "unit": unit, "tier": sv_.get("tier") or "missing",
                     "source": sv_.get("source") or "", "url": sv_.get("url"), "as_of": sv_.get("as_of"), "origin": origin})

    fin = study.get("financial")
    if fin and fin.get("inputs_echo"):
        for e in fin["inputs_echo"]:
            add(e.get("key"), e.get("lane"), {"value": e.get("value"), "tier": e.get("tier"), "source": e.get("source"),
                                               "url": e.get("url"), "as_of": e.get("as_of")}, e.get("unit") or "", "financial")
    elif (study.get("inputs") or {}).get("commercial"):
        def walk(o, path, lane=None):
            if is_sv(o):
                add(path, lane, o, "", "inputs")
            elif isinstance(o, dict):
                for k, v in o.items():
                    if k == "by_partner" and isinstance(v, dict):
                        for iso, s in v.items():
                            walk(s, f"{path}", iso)
                    else:
                        walk(v, f"{path}.{k}" if path else k, lane)
        walk(study["inputs"]["commercial"], "")
    fr = study.get("freight")
    if fr:
        for iso, ln in (fr.get("lanes") or {}).items():
            add("freight", iso, ln.get("freight"), "USD/container", "freight")
            for ck in LOCAL_CHARGE_KEYS:
                if ck in (ln.get("local_charges") or {}):
                    add(ck, iso, ln["local_charges"][ck], "USD/container", "freight")
        for i, pt in enumerate((fr.get("model") or {}).get("points") or []):
            add(f"calibration: {pt.get('label', i + 1)}", None,
                {"value": pt.get("usd"), "tier": "published", "source": pt.get("source"), "url": pt.get("url"), "as_of": pt.get("as_of")},
                "USD/container", "freight")
    ta = study.get("tariff")
    if ta:
        for iso, ln in (ta.get("lanes") or {}).items():
            for k in ("duty_rate_pct", "mfn_rate_pct", "other_duties_pct", "import_vat_pct"):
                add(k, iso, ln.get(k), "%", "tariff")
            add("preference_rate_pct", iso, (ln.get("preference") or {}).get("rate_pct"), "%", "tariff")
    return rows


def verify_rows(study: dict) -> list:
    """Checklist rows: every SV whose tier is not user/quote, plus every tariff lane with status verify."""
    out = []
    for r in collect_svs(study):
        if r["tier"] not in common.FIRM_TIERS:
            out.append({"item": r["key"], "lane": r["lane"], "tier": r["tier"],
                        "source": r["source"] or ("not provided" if r["tier"] == "missing" else ""),
                        "value": r["value"], "url": r["url"], "as_of": r["as_of"]})
    for iso, ln in ((study.get("tariff") or {}).get("lanes") or {}).items():
        if ln.get("status") == "verify":
            out.append({"item": "tariff lane (all duty fields)", "lane": iso, "tier": "verify",
                        "source": "tariff status verify: confirm with customs authority or broker" + (f" ({ln['notes']})" if ln.get("notes") else ""),
                        "value": None, "url": None, "as_of": None})
    out.sort(key=lambda r: (r["lane"] or "", r["item"] or ""))
    return out


def verify_list(study: dict) -> list:
    return [{"item": r["item"], "lane": r["lane"], "tier": r["tier"], "source": r["source"]} for r in verify_rows(study)]


# ------------------------------------------------------------------ numbers in narrative

_SUFFIX = {"%": (1.0, 100.0), "pp": (1.0,), "percent": (1.0, 100.0), "per cent": (1.0, 100.0),
           "million": (1e-6,), "m": (1e-6,), "mn": (1e-6,), "thousand": (1e-3,), "k": (1e-3,),
           "billion": (1e-9,), "bn": (1e-9,), "b": (1e-9,)}
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?:\s?(%|pp\b|percent\b|per cent\b|million\b|billion\b|thousand\b|bn\b|mn\b|[MKB]\b|m\b|k\b))?")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def sig_digits(tok: str) -> int:
    t = tok.replace(",", "")
    if "." in t:
        digits = t.replace(".", "").lstrip("0")
    else:
        digits = t.lstrip("0").rstrip("0")
    return len(digits)


def _harvest(obj, out, key=None):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        if not (isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj))):
            out.add(abs(float(obj)))
    elif isinstance(obj, str):
        if key == "url":
            return
        s = obj.replace(",", "") if re.search(r"\d,\d{3}", obj) else obj
        for m in re.finditer(r"\d+(?:\.\d+)?", s):
            try:
                out.add(float(m.group(0)))
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _harvest(v, out, k)
    elif isinstance(obj, list):
        for v in obj:
            _harvest(v, out, key)


def _strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("schema_version", "language"):
                continue
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def unmatched_numbers(narr: dict, study_json: list) -> tuple:
    """-> (errors, warnings) for numbers with >= 3 significant digits that appear in no study JSON."""
    vals = set()
    for j in study_json:
        _harvest(j, vals)
    arr = sorted(vals)

    def has(x, d, scale):
        tol = 0.5 * 10 ** (-d) * (1 + 1e-9) + 1e-12
        lo, hi = (x - tol) / scale, (x + tol) / scale
        i = bisect.bisect_left(arr, lo)
        return i < len(arr) and arr[i] <= hi

    errs, warns = [], []
    for path, text in _strings(narr):
        for sent in re.split(r"(?<=[.!?])\s+", text):
            clean = _DATE_RE.sub(" ", sent)
            for m in _NUM_RE.finditer(clean):
                tok, suf = m.group(1), m.group(2)
                if sig_digits(tok) < 3:
                    continue
                d = len(tok.split(".")[1]) if "." in tok else 0
                x = float(tok.replace(",", ""))
                scales = _SUFFIX[suf.lower()] if suf else (1.0,)
                ok = any(has(x, d, sc) for sc in scales)
                if ok:
                    continue
                is_year = "." not in tok and "," not in tok and not suf and 1990 <= x <= 2100
                msg = f"narrative number '{m.group(0).strip()}' not found in any study JSON ({path}): \"{sent.strip()[:200]}\""
                (warns if is_year else errs).append(msg)
    return errs, warns


# ------------------------------------------------------------------ individual checks

def walk_json(obj, path, errs, warns):
    """SV integrity + NaN everywhere."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        errs.append(f"{path}: NaN/Inf value")
    elif is_sv(obj):
        tier, val = obj.get("tier"), obj.get("value")
        if tier not in common.TIERS:
            errs.append(f"{path}: SV tier {tier!r} not in {list(common.TIERS)}")
        elif val is None and tier != "missing":
            errs.append(f"{path}: SV value null but tier is {tier!r} (null value requires tier 'missing')")
        elif val is not None and tier == "missing":
            errs.append(f"{path}: SV has value {val!r} but tier is 'missing'")
        elif tier in ("published", "connector"):
            if not (obj.get("url") or obj.get("source")):
                errs.append(f"{path}: {tier} SV has neither url nor source")
            if not obj.get("as_of"):
                (warns if "inputs_echo[" in path else errs).append(f"{path}: {tier} SV has no as_of")
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            errs.append(f"{path}: NaN/Inf SV value")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            walk_json(v, f"{path}.{k}", errs, warns)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_json(v, f"{path}[{i}]", errs, warns)


def is_iso(x, rankable=True) -> bool:
    if not isinstance(x, str):
        return False
    rec = common.resolve_country(x)
    return bool(rec) and rec["iso3"] == x and (not rankable or common.is_rankable(x))


def need(obj, keys, where, errs):
    if not isinstance(obj, dict):
        errs.append(f"{where}: expected an object")
        return False
    miss = [k for k in keys if k not in obj]
    if miss:
        errs.append(f"{where}: missing required keys {miss}")
    return not miss


def check_inputs(inp, errs, warns):
    if not need(inp, ["depth", "perspective", "home_country", "product", "years", "commercial"], "inputs.json", errs):
        return
    if inp["perspective"] not in common.LABELS:
        errs.append(f"inputs.json: perspective {inp['perspective']!r} must be export or import")
    if inp["depth"] not in ("quick_scan", "full_study"):
        errs.append(f"inputs.json: depth {inp['depth']!r} must be quick_scan or full_study")
    if not is_iso(inp["home_country"]):
        errs.append(f"inputs.json: home_country {inp['home_country']!r} is not a valid ISO3 country")
    need(inp["product"], ["name", "hs_code"], "inputs.json product", errs)
    need(inp["years"], ["from", "to"], "inputs.json years", errs)
    for k in ("partners_of_interest", "partners_existing", "partners_excluded"):
        for iso in inp.get(k) or []:
            if not is_iso(iso):
                errs.append(f"inputs.json {k}: {iso!r} is not a valid ISO3 country")


def check_stage2(study, errs, warns):
    fm = study.get("flows_meta")
    if fm is not None:
        need(fm, ["views", "hs_codes_found"], "flows_meta.json", errs)
        if isinstance(fm.get("views"), dict) and not fm["views"].get("home_flows"):
            errs.append("flows_meta.json: home_flows view missing")
        hs = (study.get("inputs") or {}).get("product", {}).get("hs_code")
        if hs and fm.get("hs_codes_found") and [str(h) for h in fm["hs_codes_found"]] != [str(hs)]:
            errs.append(f"flows_meta.json: HS codes found {fm['hs_codes_found']} differ from inputs {hs}")


def check_analysis(study, errs, warns):
    a, inp = study["analysis"], study.get("inputs") or {}
    if not need(a, ["meta", "home_position", "partners", "shortlist", "mirror_checks", "partner_detail"], "analysis.json", errs):
        return
    persp = a["meta"].get("perspective")
    if inp.get("perspective") and persp != inp["perspective"]:
        errs.append(f"analysis.json: perspective {persp!r} differs from inputs.json {inp['perspective']!r}")
    view = common.LABELS.get(persp, {}).get("primary_world_view")
    if view and view not in a:
        errs.append(f"analysis.json: primary world view {view!r} missing")
    elif view:
        need(a[view], ["annual", "ranking"], f"analysis.json {view}", errs)
    need(a["meta"], ["latest_complete_year", "base_year", "partial_years", "rules"], "analysis.json meta", errs)
    if a["meta"].get("latest_complete_year") not in (a["meta"].get("years_available") or []):
        errs.append("analysis.json: latest_complete_year not in years_available")
    isos = set()
    for p in a["partners"]:
        if not is_iso(p.get("iso3")):
            errs.append(f"analysis.json partners: invalid ISO3 {p.get('iso3')!r}")
        isos.add(p.get("iso3"))
        if p.get("tier") not in ("core", "growth", "new", "minor"):
            errs.append(f"analysis.json partner {p.get('iso3')}: unknown tier {p.get('tier')!r}")
        sc = p.get("score")
        if sc is not None and not (0 <= sc <= 100):
            errs.append(f"analysis.json partner {p.get('iso3')}: score {sc} outside 0..100")
    for tier, lst in (a["shortlist"] or {}).items():
        for iso in lst:
            if iso not in isos:
                errs.append(f"analysis.json shortlist.{tier}: {iso} is not in partners")
    for iso in a["partner_detail"]:
        if not is_iso(iso):
            errs.append(f"analysis.json partner_detail: invalid ISO3 key {iso!r}")
    if view in a:
        vals = [r.get("value_usd") or 0 for r in a[view].get("ranking", [])]
        if vals != sorted(vals, reverse=True):
            errs.append(f"analysis.json {view}.ranking is not sorted by value")
    sel = study.get("selection")
    if sel is not None:
        if not isinstance(sel.get("partners"), list):
            errs.append("selection.json: partners must be a list")
        for iso in sel.get("partners") or []:
            if iso not in isos:
                errs.append(f"selection.json: partner {iso} is not in analysis.json partners")


def lane_set_check(name, lanes, sel, errs, warns):
    for iso in lanes:
        if not is_iso(iso):
            errs.append(f"{name}: lane key {iso!r} is not a valid ISO3 country")
        elif sel is not None and iso not in sel:
            warns.append(f"{name}: lane {iso} is not in selection.json")
    if sel:
        miss = [i for i in sel if i not in lanes]
        if miss:
            warns.append(f"{name}: selected partners without a lane: {miss}")


def check_freight(study, errs, warns):
    fr = study["freight"]
    if not need(fr, ["container_type", "direction", "lanes"], "freight.json", errs):
        return
    if fr["container_type"] not in common.CONTAINER_TYPES:
        errs.append(f"freight.json: container_type {fr['container_type']!r} unknown")
    persp = (study.get("inputs") or {}).get("perspective")
    if persp and fr["direction"] != common.LABELS[persp]["direction"]:
        errs.append(f"freight.json: direction {fr['direction']!r} does not fit a {persp} study")
    sel = (study.get("selection") or {}).get("partners")
    lane_set_check("freight.json", fr["lanes"], sel, errs, warns)
    model = fr.get("model") or {}
    counts = [c for c in (model.get("n_points"), len(model["points"]) if isinstance(model.get("points"), list) else None) if c is not None]
    fi = study.get("freight_inputs")
    if not counts and fi is not None:
        counts = [len(fi.get("calibration_points") or [])]
    n_cal = min(counts) if counts else 0
    for iso, ln in fr["lanes"].items():
        w = f"freight.json lane {iso}"
        if not need(ln, ["freight", "freight_usd", "coverage_gap", "local_charges"], w, errs):
            continue
        missing = common.sv_value(ln["freight"]) is None
        if bool(ln["coverage_gap"]) != missing:
            errs.append(f"{w}: coverage_gap={ln['coverage_gap']} but freight is {'missing' if missing else 'present'} (must agree)")
        if missing and (ln["freight_usd"] or {}).get("base") is not None:
            errs.append(f"{w}: coverage gap but freight_usd.base is set")
        if ln["freight"].get("tier") == "modelled" and n_cal < 1:
            errs.append(f"{w}: modelled freight with no calibration point (a modelled price needs at least one dated, cited point)")
        fu = ln["freight_usd"] or {}
        if all(fu.get(k) is not None for k in ("low", "base", "high")) and not (fu["low"] <= fu["base"] <= fu["high"]):
            errs.append(f"{w}: freight_usd low/base/high not ordered")
    if fi:
        for iso, ln in (fi.get("lanes") or {}).items():
            for i, r in enumerate(ln.get("rates") or []):
                if r.get("tier") not in common.TIERS or r.get("tier") in ("missing", "modelled", "derived", "user"):
                    errs.append(f"freight_inputs.json lane {iso} rate {i}: tier {r.get('tier')!r} not allowed for a rate")


def check_tariff(study, errs, warns):
    t = study["tariff"]
    if not need(t, ["hs_code", "lanes"], "tariff.json", errs):
        return
    inp = study.get("inputs") or {}
    persp, home = inp.get("perspective"), inp.get("home_country")
    sel = (study.get("selection") or {}).get("partners")
    lane_set_check("tariff.json", t["lanes"], sel, errs, warns)
    if inp.get("product", {}).get("hs_code") and str(t["hs_code"]) != str(inp["product"]["hs_code"]):
        errs.append(f"tariff.json: hs_code {t['hs_code']} differs from inputs.json {inp['product']['hs_code']}")
    for iso, ln in t["lanes"].items():
        w = f"tariff.json lane {iso}"
        if not need(ln, ["importer", "exporter", "duty_rate_pct", "status", "valuation_basis"], w, errs):
            continue
        if ln["status"] not in ("sourced", "verify"):
            errs.append(f"{w}: status {ln['status']!r} must be sourced or verify")
        if ln["valuation_basis"] not in ("CIF", "FOB"):
            errs.append(f"{w}: valuation_basis {ln['valuation_basis']!r} must be CIF or FOB")
        for k in ("importer", "exporter"):
            if not is_iso(ln[k]):
                errs.append(f"{w}: {k} {ln[k]!r} is not a valid ISO3 country")
        if persp == "export" and (ln["exporter"] != home or ln["importer"] != iso):
            errs.append(f"{w}: export study needs exporter={home} and importer={iso}")
        if persp == "import" and (ln["importer"] != home or ln["exporter"] != iso):
            errs.append(f"{w}: import study needs importer={home} and exporter={iso}")
        if ln["status"] == "sourced":
            if common.sv_value(ln["duty_rate_pct"]) is None:
                errs.append(f"{w}: status sourced but duty_rate_pct is missing")
            for k in ("duty_rate_pct", "mfn_rate_pct", "other_duties_pct", "import_vat_pct"):
                s = ln.get(k)
                if is_sv(s) and s.get("value") is not None and not (s.get("url") or s.get("source")):
                    errs.append(f"{w}: status sourced but {k} has no url or source")
                if is_sv(s) and s.get("value") is not None and s.get("tier") in ("modelled", "derived"):
                    errs.append(f"{w}: status sourced but {k} is tier {s['tier']}")
        pref = ln.get("preference") or {}
        if pref.get("agreement") and common.sv_value(pref.get("rate_pct")) is None:
            warns.append(f"{w}: preference {pref['agreement']} named but no preferential rate")


def check_financial(study, errs, warns):
    f = study["financial"]
    if not need(f, ["perspective", "currency", "lanes", "portfolio", "inputs_echo"], "financial.json", errs):
        return
    inp = study.get("inputs") or {}
    if inp.get("perspective") and f["perspective"] != inp["perspective"]:
        errs.append("financial.json: perspective differs from inputs.json")
    if inp and inp.get("commercial") is None:
        warns.append("financial.json present although inputs.json has no commercial block")
    sel = (study.get("selection") or {}).get("partners")
    lane_set_check("financial.json", f["lanes"], sel, errs, warns)
    for iso, ln in f["lanes"].items():
        w = f"financial.json lane {iso}"
        if ln.get("status") not in ("modelled", "incomplete"):
            errs.append(f"{w}: status {ln.get('status')!r} must be modelled or incomplete")
            continue
        if ln["status"] == "incomplete":
            if not ln.get("missing"):
                errs.append(f"{w}: incomplete lane with empty missing list")
            bad = [k for k in ("margin", "margin_pct", "break_even_containers_per_year") if ln.get(k) is not None]
            for sc, v in (ln.get("freight_scenarios") or {}).items():
                if isinstance(v, dict) and v.get("margin") is not None:
                    bad.append(f"freight_scenarios.{sc}.margin")
            grid = (ln.get("sensitivity") or {}).get("margin") or []
            if any(c is not None for row in grid for c in row):
                bad.append("sensitivity.margin")
            if any(r.get("margin") is not None for r in ln.get("fx_sensitivity") or []):
                bad.append("fx_sensitivity")
            if bad:
                errs.append(f"{w}: incomplete lane has results that must be null: {bad}")
        else:
            if ln.get("missing"):
                errs.append(f"{w}: modelled lane has non-empty missing {ln['missing']}")
            m, rev, uc, mp = ln.get("margin"), ln.get("revenue"), ln.get("user_cost"), ln.get("margin_pct")
            if None in (m, rev, uc):
                if m is None and not (ln.get("required_price_per_unit") or ln.get("required_local_price_per_unit")
                                      or ln.get("breakeven_local_price_per_unit")):
                    warns.append(f"{w}: modelled lane without a margin or a required price")
            else:
                if abs(m - (rev - uc)) > 0.02:
                    errs.append(f"{w}: margin {m} != revenue - user_cost ({rev - uc:.2f})")
                if mp is not None and rev and abs(mp - 100 * m / rev) > 0.02:
                    errs.append(f"{w}: margin_pct {mp} inconsistent with margin/revenue")
    pf = f.get("portfolio") or {}
    pm = pf.get("payback_month")
    if pm is not None and (isinstance(pm, bool) or not isinstance(pm, int) or pm < 1):
        errs.append(f"financial.json portfolio.payback_month {pm!r} must be a positive integer or null")


def check_narrative(study, errs, warns, depth, commercial):
    n = study["narrative"]
    if not need(n, ["executive_summary", "sections"], "narrative.json", errs):
        return
    gaps = narrative_gaps(n, depth, commercial)
    for g in gaps:
        errs.append(f"narrative.json: required section '{g}' is empty")
    rec = (n["executive_summary"] or {}).get("recommendations") or []
    if rec and len(rec) != 3:
        warns.append(f"narrative.json: executive summary has {len(rec)} recommendations (the guide asks for exactly three)")
    secs = n["sections"] or {}
    sel = (study.get("selection") or {}).get("partners") or []
    notes = (secs.get("shortlist") or {}).get("partner_notes") or {}
    for iso in sel:
        if iso not in notes and "shortlist" not in gaps:
            warns.append(f"narrative.json: no shortlist.partner_notes entry for selected partner {iso}")
    if depth == "full_study" and commercial and "financial_case" not in gaps:
        verd = (secs.get("financial_case") or {}).get("partner_verdicts") or {}
        for iso in sel:
            if iso not in verd:
                warns.append(f"narrative.json: no financial_case.partner_verdicts entry for {iso}")
        fin_txt = " ".join(t for _, t in _strings(secs.get("financial_case") or {}))
        for iso, ln in ((study.get("financial") or {}).get("lanes") or {}).items():
            if ln.get("status") == "incomplete" and iso not in fin_txt and (ln.get("name") or "") not in fin_txt:
                warns.append(f"narrative.json: incomplete lane {iso} is not mentioned in financial_case")
    for iso in list(notes) + list(((secs.get("financial_case") or {}).get("partner_verdicts") or {})):
        if not is_iso(iso, rankable=False):
            errs.append(f"narrative.json: partner key {iso!r} is not a valid ISO3 country")
    for r in (secs.get("shortlist") or {}).get("not_prioritised") or []:
        if not common.resolve_country(r.get("partner")):
            errs.append(f"narrative.json: not_prioritised partner {r.get('partner')!r} is not a known country (ISO3 or name)")
    srcs = [study[k] for k in ("inputs", "analysis", "selection", "freight", "tariff", "financial", "freight_inputs", "flows_meta") if study.get(k)]
    e2, w2 = unmatched_numbers(n, srcs)
    errs.extend(e2)
    warns.extend(w2)


def xlsx_error_cells(xlsx: Path) -> tuple:
    """Recalculate with LibreOffice and list error cells. -> (errors, warnings)."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return [], ["xlsx not recalculated: LibreOffice (soffice) not found"]
    try:
        import openpyxl
    except ImportError:
        return [], ["xlsx not checked: openpyxl missing"]
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in" / "model.xlsx"
        src.parent.mkdir()
        shutil.copy(xlsx, src)
        out = Path(td) / "out"
        try:
            subprocess.run([soffice, f"-env:UserInstallation=file://{td}/profile", "--headless", "--convert-to", "xlsx", "--outdir",
                            str(out), str(src)], capture_output=True, timeout=180, check=False)
        except subprocess.TimeoutExpired:
            return [], ["xlsx recalculation timed out"]
        res = out / "model.xlsx"
        if not res.exists():
            return ["financial_model.xlsx could not be recalculated with LibreOffice"], []
        wb = openpyxl.load_workbook(res, data_only=True)
        errs = []
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, str) and c.value.startswith(XLSX_ERRORS):
                        errs.append(f"financial_model.xlsx {ws.title}!{c.coordinate}: {c.value}")
        return errs, []


# ------------------------------------------------------------------ driver

STAGE_FILES = {1: ["inputs"], 2: ["flows_meta"], 3: ["analysis", "selection"], 4: ["freight"], 5: ["tariff"], 6: ["financial"], 7: ["narrative"]}
ALL_KEYS = ["env", "inputs", "query_plan", "flows_meta", "analysis", "selection", "freight_inputs", "freight", "tariff", "financial", "narrative"]


def validate(study_dir, stage=None, recalc=True) -> dict:
    sd = Path(study_dir)
    errs, warns = [], []
    study = {}
    for k in ALL_KEYS:
        p = common.study_file(sd, k)
        if p.exists():
            try:
                study[k] = common.read_json(p)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{p.name}: cannot parse JSON ({e})")
    inp = study.get("inputs") or {}
    depth = inp.get("depth", "full_study")
    commercial = inp.get("commercial") is not None
    explicit = stage is not None
    if explicit:
        stages = list(range(1, int(stage) + 1))
    else:
        stages = [s for s in range(1, 8) if any(common.study_file(sd, k).exists() for k in STAGE_FILES[s])]
        if 1 not in stages:
            stages.insert(0, 1)
    optional_missing = {4: depth == "quick_scan" or not commercial, 5: depth == "quick_scan" or not commercial,
                        6: depth == "quick_scan" or not commercial}
    for s in stages:
        for k in STAGE_FILES[s]:
            if k not in study and (explicit or s == 1) and not optional_missing.get(s, False):
                errs.append(f"stage {s}: required file {common.FILES[k]} is missing")
            elif k not in study and explicit and optional_missing.get(s):
                warns.append(f"stage {s}: {common.FILES[k]} not present (optional for this study)")
    for k, d in study.items():
        if k == "narrative" and 7 not in stages:
            continue
        if d.get("schema_version") != common.SCHEMA_VERSION:
            errs.append(f"{common.FILES[k]}: schema_version is {d.get('schema_version')!r}, expected {common.SCHEMA_VERSION!r}")
        if k != "narrative":
            walk_json(d, common.FILES[k], errs, warns)
        else:
            _nan_only(d, "narrative.json", errs)

    if 1 in stages and inp:
        check_inputs(inp, errs, warns)
    if 2 in stages:
        check_stage2(study, errs, warns)
    if 3 in stages and "analysis" in study:
        check_analysis(study, errs, warns)
    if 4 in stages and "freight" in study:
        check_freight(study, errs, warns)
    if 5 in stages and "tariff" in study:
        check_tariff(study, errs, warns)
    if 6 in stages:
        if "financial" in study:
            check_financial(study, errs, warns)
        x = common.study_file(sd, "model_xlsx")
        if x.exists() and recalc:
            e2, w2 = xlsx_error_cells(x)
            errs.extend(e2)
            warns.extend(w2)
        elif not x.exists() and commercial and "financial" in study and explicit:
            errs.append("stage 6: financial_model.xlsx is missing")
    if 7 in stages:
        if "narrative" in study:
            check_narrative(study, errs, warns, depth, commercial)
        outs = [("report", "study.docx")]
        if depth == "full_study" and commercial:
            outs.append(("model_xlsx", "financial_model.xlsx"))
        outs.append(("dashboard", "dashboard.html"))
        for k, nm in outs:
            if not common.study_file(sd, k).exists():
                (errs if explicit else warns).append(f"stage 7: output {nm} is missing")
        rp = common.study_file(sd, "report")
        if rp.exists():
            try:
                import docx
                docx.Document(str(rp))
            except Exception as e:  # noqa: BLE001
                errs.append(f"study.docx does not open: {e}")
    vl = verify_list(study) if study else []
    qa = {"schema_version": common.SCHEMA_VERSION, "stage": int(stage) if explicit else "all", "passed": not errs,
          "errors": errs, "warnings": warns, "verify_list": vl}
    return qa


def _nan_only(obj, path, errs):
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        errs.append(f"{path}: NaN/Inf value")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _nan_only(v, f"{path}.{k}", errs)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _nan_only(v, f"{path}[{i}]", errs)


def main(argv=None):
    ap = argparse.ArgumentParser(description="QA gate: writes qa.json; exit 0 if passed, 2 if not.")
    ap.add_argument("--study-dir", required=True)
    ap.add_argument("--stage", type=int, choices=range(1, 8))
    ap.add_argument("--no-recalc", action="store_true", help="skip the LibreOffice recalculation of the xlsx")
    a = ap.parse_args(argv)
    if not Path(a.study_dir).is_dir():
        common.blocked(f"study folder not found: {a.study_dir}")
    qa = validate(a.study_dir, a.stage, recalc=not a.no_recalc)
    common.write_json(common.study_file(a.study_dir, "qa"), qa)
    print(f"QA {'PASSED' if qa['passed'] else 'FAILED'} (stage {qa['stage']}): {len(qa['errors'])} errors, "
          f"{len(qa['warnings'])} warnings, {len(qa['verify_list'])} items to verify -> {common.FILES['qa']}")
    for e in qa["errors"][:15]:
        print(f"  ERROR   {e}")
    if len(qa["errors"]) > 15:
        print(f"  ... {len(qa['errors']) - 15} more errors in qa.json")
    for w in qa["warnings"][:8]:
        print(f"  warning {w}")
    sys.exit(0 if qa["passed"] else 2)


if __name__ == "__main__":
    main()
