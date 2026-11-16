#!/usr/bin/env python3
"""Write a clearly synthetic tariff.json for the partners in <study>/selection.json (DEMO data only).

Every source starts with "DEMO synthetic" and every URL is https://example.com/demo. The mix is deterministic:
zero-duty-under-agreement lanes, MFN lanes with a positive duty, one lane with status "verify" and a missing
duty_rate_pct (to show the gap downstream), VAT recoverable on most lanes. Works for both perspectives:
the importer is the partner for an export study and the home country for an import study.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common  # noqa: E402

DEMO_URL = "https://example.com/demo"
AS_OF = "2026-11-13"


def _sv(value, tier, what):
    return common.sv(value, tier, f"DEMO synthetic {what} - not a real schedule", DEMO_URL, AS_OF)


def build(inputs: dict, selection: dict) -> dict:
    persp = inputs.get("perspective")
    common.labels(persp)
    home = inputs.get("home_country")
    partners = list(selection.get("partners") or [])
    if not partners:
        common.blocked("selection.json lists no partners")
    verify_idx = min(2, len(partners) - 1) if len(partners) >= 2 else None
    lanes = {}
    for i, iso in enumerate(partners):
        importer, exporter = (iso, home) if persp == "export" else (home, iso)
        fob = importer in common.FOB_VALUATION
        lane = {"importer": importer, "exporter": exporter,
                "valuation_basis": "FOB" if fob else "CIF", "ntb": [], "notes": "DEMO synthetic data"}
        if i == verify_idx:
            lane.update({
                "duty_rate_pct": common.sv_missing("DEMO synthetic: rate not found, verify with the customs authority"),
                "mfn_rate_pct": common.sv_missing("DEMO synthetic: rate not found"),
                "preference": {"agreement": None, "rate_pct": common.sv_missing("DEMO synthetic: none identified"), "conditions": ""},
                "other_duties_pct": _sv(0.0, "modelled", "assumed no other duties (verify)"),
                "import_vat_pct": _sv(15.0, "modelled", "VAT rate placeholder (verify)"),
                "vat_recoverable": True, "status": "verify"})
        elif i % 3 == 0:
            lane.update({
                "duty_rate_pct": _sv(0.0, "published", "zero duty under a demo agreement"),
                "mfn_rate_pct": _sv(12.0, "published", "MFN rate"),
                "preference": {"agreement": "DEMO FTA", "rate_pct": _sv(0.0, "published", "preferential rate"),
                               "conditions": "DEMO synthetic rules of origin"},
                "other_duties_pct": _sv(0.0, "published", "no other duties"),
                "import_vat_pct": _sv(15.0, "published", "import VAT"),
                "vat_recoverable": True, "status": "sourced"})
        else:
            mfn = 8.0 + 2.0 * (i % 3)
            lane.update({
                "duty_rate_pct": _sv(mfn, "published", "MFN rate applied (no agreement)"),
                "mfn_rate_pct": _sv(mfn, "published", "MFN rate"),
                "preference": {"agreement": None, "rate_pct": common.sv_missing("DEMO synthetic: no agreement"), "conditions": ""},
                "other_duties_pct": _sv(1.0 * (i % 2), "published", "other duties and charges"),
                "import_vat_pct": _sv(5.0 + 5.0 * (i % 3), "published", "import VAT"),
                "vat_recoverable": (i % 4 != 1), "status": "sourced"})
        lanes[iso] = lane
    return {"schema_version": common.SCHEMA_VERSION, "hs_code": (inputs.get("product") or {}).get("hs_code"),
            "lanes": lanes, "notes": "DEMO synthetic tariff data. Replace with cited lookups before use."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write a synthetic DEMO tariff.json for the partners in selection.json.")
    ap.add_argument("--study-dir", required=True, help="study folder with inputs.json and selection.json")
    a = ap.parse_args(argv)
    d = Path(a.study_dir)
    inputs = common.read_json(common.study_file(d, "inputs"))
    selection = common.read_json(common.study_file(d, "selection"))
    doc = build(inputs, selection)
    p = common.write_json(common.study_file(d, "tariff"), doc)
    print(f"Wrote {p} ({len(doc['lanes'])} lanes, DEMO synthetic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
