#!/usr/bin/env python3
"""Build the self-contained interactive dashboard (dashboard.html) for a study folder (schema 2.0).

    python3 build_dashboard.py --study-dir DIR [--out FILE]

Reads inputs.json and analysis.json (both required: exit 2 "BLOCKED:" when missing) and, when present,
selection.json, freight.json, tariff.json, financial.json and narrative.json.  A quick_scan folder (inputs +
analysis, optionally selection) is enough.  The data are embedded as JSON at the single marker `/*__DATA__*/null`
of assets/dashboard_template.html; the template holds a JavaScript port of model_core.compute_lane, so this script
also exports the plain-number inputs of every lane (export_lane_inputs) so that JS and Python start from
identical numbers.  Standard library only.  See references/contracts.md section 10.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import model_core as mc  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "dashboard_template.html"
MARKER = "/*__DATA__*/null"

# keys of the flat dict returned by model_core.build_lane_inputs that are not plain inputs for the JS port
_INTERNAL = ("_sv", "_notes", "input_errors")


def export_lane_inputs(li: dict) -> dict:
    """Plain-number inputs of one lane, exactly as model_core.build_lane_inputs returned them.

    Pure function.  Numbers stay numbers, None stays None (never defaulted).  Extra keys for display:
    `tiers` (tier of every sourced value, used for the waterfall badges), `units` (unit of each sourced value),
    `notes` and `input_errors`.
    """
    out = {k: v for k, v in li.items() if k not in _INTERNAL}
    sv = li.get("_sv") or {}
    out["tiers"] = {k: (s.get("tier") or "missing") for k, s in sv.items()}
    out["units"] = {k: (s.get("unit") or "") for k, s in sv.items()}
    out["notes"] = list(li.get("_notes") or [])
    out["input_errors"] = list(li.get("input_errors") or [])
    return out


def lane_inputs_for_study(inputs, selection, freight, tariff, analysis):
    """({iso3: exported lane inputs}, {iso3: raw lane inputs}) for every selected partner (empty without commercial/selection)."""
    c = (inputs or {}).get("commercial")
    partners = list((selection or {}).get("partners") or [])
    if not c or not partners or inputs.get("perspective") not in common.LABELS:
        return {}, {}
    f_lanes = (freight or {}).get("lanes") or {}
    t_lanes = (tariff or {}).get("lanes") or {}
    a_rows = {r.get("iso3"): r for r in ((analysis or {}).get("partners") or [])}
    exported, raw = {}, {}
    for iso in partners:
        li = mc.build_lane_inputs(inputs, f_lanes.get(iso), t_lanes.get(iso), a_rows.get(iso), iso)
        if freight is not None and f_lanes.get(iso) is None:
            li["_notes"].append("lane absent from freight.json: freight and local charges are missing")
        if freight is None:
            li["_notes"].append("freight.json not available: freight and local charges are missing")
        if tariff is not None and t_lanes.get(iso) is None:
            li["_notes"].append("lane absent from tariff.json: duty and VAT are missing")
        if tariff is None:
            li["_notes"].append("tariff.json not available: duty and VAT are missing")
        if (freight or {}).get("container_type") and li.get("container_type") and freight["container_type"] != li["container_type"]:
            li["_notes"].append(f"freight.json container type {freight['container_type']} differs from inputs {li['container_type']}")
        raw[iso] = li
        exported[iso] = export_lane_inputs(li)
    return exported, raw


def build_data(study_dir) -> dict:
    d = Path(study_dir)
    inputs = common.read_json(common.study_file(d, "inputs"))
    analysis = common.read_json(common.study_file(d, "analysis"))
    selection = common.read_json(common.study_file(d, "selection"), required=False)
    freight = common.read_json(common.study_file(d, "freight"), required=False)
    tariff = common.read_json(common.study_file(d, "tariff"), required=False)
    financial = common.read_json(common.study_file(d, "financial"), required=False)
    narrative = common.read_json(common.study_file(d, "narrative"), required=False)
    persp = (analysis.get("meta") or {}).get("perspective") or inputs.get("perspective")
    if persp not in common.LABELS:
        common.blocked(f"perspective must be 'export' or 'import', got {persp!r}")
    lanes, raw = lane_inputs_for_study(inputs, selection, freight, tariff, analysis)
    names = {}
    for r in analysis.get("partners") or []:
        names[r["iso3"]] = r.get("name") or common.country_name(r["iso3"])
    for iso in list(lanes) + [inputs.get("home_country")]:
        if iso and iso not in names:
            names[iso] = common.country_name(iso)
    home = (analysis.get("meta") or {}).get("home") or {}
    if home.get("iso3"):
        names[home["iso3"]] = home.get("name") or names.get(home["iso3"]) or home["iso3"]
    if financial is not None:
        sources = financial.get("inputs_echo") or []
    else:
        sources = mc.echo_rows(raw) if raw else []
    c = inputs.get("commercial") or {}
    slim = {k: inputs.get(k) for k in ("study_id", "prepared_for", "prepared_by", "date_prepared", "depth", "perspective",
                                       "home_country", "product", "years", "partners_of_interest", "partners_existing",
                                       "partners_excluded")}
    slim["has_commercial"] = bool(c)
    slim["monthly_ramp"] = ((c.get("volume") or {}).get("monthly_ramp")) if c else None
    return {"build": {"date": inputs.get("date_prepared") or common.today(), "built_on": common.today(),
                      "tool": "Trade Study Planner", "schema_version": common.SCHEMA_VERSION},
            "inputs": slim, "analysis": analysis, "selection": selection, "freight": freight, "tariff": tariff,
            "financial": financial, "narrative": narrative, "lanes": lanes, "names": names, "sources": sources}


def render(data: dict, template_path=TEMPLATE) -> str:
    tpl = Path(template_path).read_text(encoding="utf-8")
    if tpl.count(MARKER) != 1:
        raise ValueError(f"template must contain the marker {MARKER!r} exactly once")
    blob = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # "<" never appears outside JSON strings, so \u003c is safe and also neutralises "</script" and "<!--"
    blob = blob.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return tpl.replace(MARKER, blob)


def build(study_dir, out=None) -> Path:
    data = build_data(study_dir)
    html = render(data)
    p = Path(out) if out else common.study_file(study_dir, "dashboard")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build dashboard.html for a study folder.")
    ap.add_argument("--study-dir", required=True)
    ap.add_argument("--out", help="output file (default: <study-dir>/dashboard.html)")
    a = ap.parse_args(argv)
    if not TEMPLATE.exists():
        print(f"error: template not found: {TEMPLATE}", file=sys.stderr)
        return 1
    p = build(a.study_dir, a.out)
    print(f"wrote {p} ({p.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
