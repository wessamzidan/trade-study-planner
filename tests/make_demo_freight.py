#!/usr/bin/env python3
"""Write a clearly synthetic freight_inputs.json (+ freight.json) for a demo study folder.

Every source string starts with "DEMO synthetic"; URLs are example.com. Lane roles by selection order:
1 client quote, all-in (covers origin_thc, destination_thc) | 2 client quote | 3 connector rate (dry, needs multiplier)
| 4 published rate | the rest modelled from two calibration points + equipment multiplier.
The LAST lane leaves destination_inland missing (an "incomplete" lane downstream).
Usage: python3 tests/make_demo_freight.py --study-dir tests/demo_export
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import estimate_freight as ef  # noqa: E402

DEFAULT_SELECTION = {"export": ["SAU", "ARE", "NLD", "GBR", "DEU", "KWT"], "import": ["EGY", "CHN", "BEL", "ESP", "IND"]}
BASE_CHARGES = {"origin_inland": 350, "origin_docs": 120, "origin_thc": 180, "destination_thc": 220,
                "import_clearance": 260, "destination_inland": 400}


def demo_sv(v, tier, what, as_of):
    return common.sv(v, tier, f"DEMO synthetic {what}", "https://example.com/demo", as_of)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create synthetic demo freight inputs and build freight.json for a demo study folder.")
    ap.add_argument("--study-dir", required=True, help="demo folder containing inputs.json")
    a = ap.parse_args(argv)
    d = Path(a.study_dir)
    inputs = common.read_json(d / "inputs.json")
    persp = inputs["perspective"]
    sel_path = d / "selection.json"
    if not sel_path.exists():
        common.write_json(sel_path, {"schema_version": common.SCHEMA_VERSION, "partners": DEFAULT_SELECTION[persp],
                                     "decided_by": "demo", "notes": {}})
    partners = common.read_json(sel_path)["partners"]
    fi_path = d / common.FILES["freight_inputs"]
    if fi_path.exists():
        fi_path.unlink()
    if ef.main(["init", "--study-dir", str(d)]) != 0:
        return 1
    fi = common.read_json(fi_path)
    ctype = fi["container_type"]
    day = common.today()
    valid = "2099-12-31"
    for i, iso in enumerate(partners):
        lane = fi["lanes"][iso]
        factor = 1 + 0.06 * i
        for k, v in BASE_CHARGES.items():
            tier = "quote" if i < 2 else ("published" if k.endswith("thc") else "user")
            lane["local_charges"][k] = demo_sv(round(v * factor), tier, f"{k.replace('_', ' ')} tariff, lane {iso}", day)
        rate = {"container_type": ctype, "all_in": False, "covers": [], "carrier": "DEMO synthetic carrier",
                "as_of": day, "url": "https://example.com/demo", "valid_until": None}
        if i == 0:
            lane["rates"].append({**rate, "tier": "quote", "usd": 1450, "all_in": True, "covers": ["origin_thc", "destination_thc"],
                                  "transit_days": 6, "valid_until": valid, "source": "DEMO synthetic forwarder quote ref D-001 (all-in)"})
        elif i == 1:
            lane["rates"].append({**rate, "tier": "quote", "usd": 1680, "transit_days": 9, "valid_until": valid,
                                  "source": "DEMO synthetic forwarder quote ref D-002"})
        elif i == 2:
            lane["rates"].append({**rate, "tier": "connector", "usd": 2350, "container_type": "40DC" if ctype == "40RF" else ctype,
                                  "transit_days": 22, "source": "DEMO synthetic rate tool market average"})
        elif i == 3:
            lane["rates"].append({**rate, "tier": "published", "usd": 2890, "transit_days": None,
                                  "source": "DEMO synthetic published carrier spot rate page"})
    last = fi["lanes"][partners[-1]]
    last["local_charges"]["destination_inland"] = common.sv_missing("DEMO synthetic: not yet obtained, left missing on purpose")
    fi["equipment_multiplier"] = demo_sv(1.25, "published", "reefer-over-dry premium (illustrative)", day)
    fi["calibration_points"] = [
        {"label": "DEMO synthetic index: Far East to North Europe", "from_locode": "SGSIN", "to_locode": "NLRTM", "usd": 2150,
         "container_type": "40DC", "source": "DEMO synthetic index", "url": "https://example.com/demo", "as_of": day},
        {"label": "DEMO synthetic index: Far East to US West Coast", "from_locode": "CNSHA", "to_locode": "USLAX", "usd": 1900,
         "container_type": "40DC", "source": "DEMO synthetic index", "url": "https://example.com/demo", "as_of": day}]
    fi["model"] = {"range_pct": 35}
    common.write_json(fi_path, fi)
    print(f"Wrote synthetic {fi_path.name} for {persp} demo ({', '.join(partners)})")
    return ef.main(["build", "--study-dir", str(d)])


if __name__ == "__main__":
    sys.exit(main())
