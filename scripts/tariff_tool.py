#!/usr/bin/env python3
"""Record tariff findings for a study consistently (stage 5 -> tariff.json, contracts section 7).

Commands: init | set-rate | set-info | check | show | selftest
Exit codes: 0 ok, 2 blocked (required input missing / rejected input), 1 error.
Never invents a rate: every SV starts missing and is only set by an explicit command.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

RATE_FIELDS = ("duty_rate_pct", "mfn_rate_pct", "preference_rate_pct", "other_duties_pct", "import_vat_pct")
TIERS_SETTABLE = ("published", "connector", "user", "quote")
CITED_TIERS = ("published", "connector")
# A --source that names an official schedule may stand in for a URL.
OFFICIAL_RE = re.compile(r"\b(schedule|tariff|customs|gazette|HTS|TARIC|nomenclature|official|WITS|tax authority|revenue)\b", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------ helpers

def _paths(study_dir):
    return common.study_file(study_dir, "tariff")


def _load_context(study_dir):
    inputs = common.read_json(common.study_file(study_dir, "inputs"))
    sel = common.read_json(common.study_file(study_dir, "selection"))
    partners = list(sel.get("partners") or [])
    if not partners:
        common.blocked("selection.json has no partners")
    persp = inputs.get("perspective")
    if persp not in ("export", "import"):
        common.blocked(f"inputs.json perspective must be export or import, got {persp!r}")
    home = inputs.get("home_country")
    if not home:
        common.blocked("inputs.json has no home_country")
    hs = (inputs.get("product") or {}).get("hs_code")
    return inputs, partners, persp, home, hs


def _new_lane(importer, exporter):
    basis = "FOB" if importer in common.FOB_VALUATION else "CIF"
    note = "valuation basis: " + (
        f"{basis} (importer usually assesses duty on FOB value) - confirm" if basis == "FOB"
        else "CIF - default, confirm")
    return {
        "importer": importer, "exporter": exporter,
        "duty_rate_pct": common.sv_missing("not looked up"),
        "mfn_rate_pct": common.sv_missing("not looked up"),
        "preference": {"agreement": None, "rate_pct": common.sv_missing("not looked up"), "conditions": ""},
        "other_duties_pct": common.sv_missing("not looked up"),
        "valuation_basis": basis,
        "import_vat_pct": common.sv_missing("not looked up"),
        "vat_recoverable": None,
        "ntb": [], "status": "verify", "notes": note,
    }


def _lane_pair(persp, home, partner):
    return (partner, home) if persp == "export" else (home, partner)  # (importer, exporter)


def _load_tariff(study_dir):
    p = _paths(study_dir)
    if not p.exists():
        common.blocked("tariff.json not found - run `tariff_tool.py init` first")
    return common.read_json(p)


def _target_lanes(data, lane):
    if lane.lower() == "all":
        return list(data["lanes"])
    lane = lane.upper()
    if lane not in data["lanes"]:
        common.blocked(f"lane {lane} not in tariff.json (lanes: {', '.join(data['lanes'])})")
    return [lane]


def _present(x):
    return common.sv_ok(x)


def _sv_cited(x):
    return bool((x.get("url") or "").strip() or (x.get("source") or "").strip()) and bool(x.get("as_of"))


def _rate_svs(lane):
    pref = lane.get("preference") or {}
    return {"duty_rate_pct": lane.get("duty_rate_pct"), "mfn_rate_pct": lane.get("mfn_rate_pct"),
            "preference_rate_pct": pref.get("rate_pct"), "other_duties_pct": lane.get("other_duties_pct"),
            "import_vat_pct": lane.get("import_vat_pct")}


def _lane_status(lane):
    """Return (status, missing list)."""
    missing = []
    svs = _rate_svs(lane)
    if not _present(svs["duty_rate_pct"]):
        missing.append("duty_rate_pct")
    for k, x in svs.items():
        if _present(x) and not _sv_cited(x):
            missing.append(f"{k}: source/date")
    for k in ("other_duties_pct", "import_vat_pct"):
        if not _present(svs[k]):
            missing.append(k)
    if lane.get("vat_recoverable") is None and _present(svs["import_vat_pct"]):
        missing.append("vat_recoverable")
    hard = [m for m in missing if m == "duty_rate_pct" or "source/date" in m]
    return ("verify" if hard else "sourced"), missing


def _fmt(x):
    v = common.sv_value(x)
    return "-" if v is None else f"{v:g}"


def _basis(lane):
    d = lane.get("duty_rate_pct")
    if not _present(d):
        return "-"
    pref = (lane.get("preference") or {})
    dv = common.sv_value(d)
    if _present(pref.get("rate_pct")) and common.sv_value(pref["rate_pct"]) == dv and pref.get("agreement"):
        kind = f"preferential ({pref['agreement']})"
    elif _present(lane.get("mfn_rate_pct")) and common.sv_value(lane["mfn_rate_pct"]) == dv:
        kind = "MFN"
    else:
        kind = "applied"
    return f"{kind}, {d.get('tier')}"


# ------------------------------------------------------------------ commands

def cmd_init(a):
    inputs, partners, persp, home, hs = _load_context(a.study_dir)
    p = _paths(a.study_dir)
    data = common.read_json(p, required=False) or {"schema_version": common.SCHEMA_VERSION, "hs_code": hs, "lanes": {}}
    data.setdefault("lanes", {})
    data["hs_code"] = data.get("hs_code") or hs
    added = []
    for iso in partners:
        if iso in data["lanes"]:
            continue
        imp, exp = _lane_pair(persp, home, iso)
        data["lanes"][iso] = _new_lane(imp, exp)
        added.append(iso)
    common.write_json(p, data)
    print(f"tariff.json: {len(data['lanes'])} lane(s), {len(added)} added ({', '.join(added) or 'none'}); perspective={persp}, HS {data['hs_code']}")
    for iso, ln in data["lanes"].items():
        _, miss = _lane_status(ln)
        print(f"  {iso}: importer {ln['importer']} <- exporter {ln['exporter']}; valuation {ln['valuation_basis']}; needed: {', '.join(miss) or 'nothing'}")
    if persp == "import":
        print(f"  import study: one importer ({home}) - one schedule serves every lane; MFN is the same for all, preferences differ by supplier.")
    print("next: set-rate (duty/mfn/preference/other/vat, with --url and --as-of), set-info, then check.")


def cmd_set_rate(a):
    data = _load_tariff(a.study_dir)
    tier = a.tier
    source = (a.source or "").strip()
    url = (a.url or "").strip() or None
    as_of = a.as_of
    if a.field not in RATE_FIELDS:
        common.blocked(f"--field must be one of {', '.join(RATE_FIELDS)}")
    if not source:
        common.blocked("--source is required (who or what the figure comes from)")
    if as_of and not DATE_RE.match(as_of):
        common.blocked("--as-of must be YYYY-MM-DD")
    if tier in CITED_TIERS:
        if not url and not OFFICIAL_RE.search(source):
            common.blocked(f"tier {tier} needs --url, or a --source that names the official schedule/tariff")
        if not as_of:
            common.blocked(f"tier {tier} needs --as-of (the date the rate was read)")
    as_of = as_of or common.today()
    if a.value < 0:
        common.blocked("--value must be >= 0")
    for iso in _target_lanes(data, a.lane):
        ln = data["lanes"][iso]
        x = common.sv(a.value, tier, source, url, as_of)
        if a.field == "preference_rate_pct":
            ln.setdefault("preference", {"agreement": None, "conditions": ""})["rate_pct"] = x
        else:
            ln[a.field] = x
        ln["status"] = _lane_status(ln)[0]
        print(f"{iso}: {a.field} = {a.value:g} ({tier}, {as_of})")
    common.write_json(_paths(a.study_dir), data)


def cmd_set_info(a):
    data = _load_tariff(a.study_dir)
    for iso in _target_lanes(data, a.lane):
        ln = data["lanes"][iso]
        pref = ln.setdefault("preference", {"agreement": None, "rate_pct": common.sv_missing(), "conditions": ""})
        if a.agreement is not None:
            pref["agreement"] = a.agreement
        if a.conditions is not None:
            pref["conditions"] = a.conditions
        if a.valuation:
            ln["valuation_basis"] = a.valuation
        if a.vat_recoverable:
            ln["vat_recoverable"] = {"true": True, "false": False, "unknown": None}[a.vat_recoverable]
        for t in a.add_ntb or []:
            if t not in ln["ntb"]:
                ln["ntb"].append(t)
        if a.notes is not None:
            ln["notes"] = a.notes
        ln["status"] = _lane_status(ln)[0]
        print(f"{iso}: info updated")
    common.write_json(_paths(a.study_dir), data)


def cmd_check(a):
    data = _load_tariff(a.study_dir)
    rows = []
    for iso, ln in data["lanes"].items():
        status, miss = _lane_status(ln)
        ln["status"] = status
        vat = _fmt(ln.get("import_vat_pct"))
        if vat != "-":
            vat += {True: " (recov.)", False: " (cost)", None: " (recov.?)"}[ln.get("vat_recoverable")]
        rows.append((iso, _fmt(ln.get("duty_rate_pct")), _basis(ln), _fmt(ln.get("other_duties_pct")), vat,
                     ln.get("valuation_basis") or "-", status, "; ".join(miss) or "-"))
    common.write_json(_paths(a.study_dir), data)
    hdr = ("lane", "duty %", "basis", "other %", "VAT %", "valn", "status", "missing")
    widths = [max(len(str(r[i])) for r in rows + [hdr]) for i in range(len(hdr))]
    for r in [hdr] + rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    n = sum(1 for r in rows if r[6] == "sourced")
    print(f"{n}/{len(rows)} lanes sourced; the rest are 'verify'.")


def cmd_show(a):
    data = _load_tariff(a.study_dir)
    for iso in _target_lanes(data, a.lane):
        ln = data["lanes"][iso]
        print(f"== {iso}: importer {ln['importer']} <- exporter {ln['exporter']}  [{ln['status']}]")
        for k, x in _rate_svs(ln).items():
            print(f"  {k:20s} {_fmt(x):>6s}  {x.get('tier'):9s} {x.get('as_of') or '':10s} {x.get('url') or x.get('source') or ''}")
        pref = ln.get("preference") or {}
        print(f"  agreement: {pref.get('agreement') or '-'} | conditions: {pref.get('conditions') or '-'}")
        print(f"  valuation: {ln.get('valuation_basis')} | VAT recoverable: {ln.get('vat_recoverable')}")
        print(f"  ntb: {'; '.join(ln.get('ntb') or []) or '-'}")
        print(f"  notes: {ln.get('notes') or '-'}")


# ------------------------------------------------------------------ selftest

def cmd_selftest(a):
    me = str(Path(__file__).resolve())

    def run(args, expect=0):
        r = subprocess.run([sys.executable, me] + args, capture_output=True, text=True)
        assert r.returncode == expect, f"{args}: exit {r.returncode} (expected {expect})\n{r.stdout}\n{r.stderr}"
        return r.stdout

    for persp, home, partners in (("export", "EGY", ["SAU", "USA"]), ("import", "SAU", ["CHN", "IND"])):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            run(["init", "--study-dir", td], expect=2)  # blocked: no inputs
            common.write_json(d / "inputs.json", {"schema_version": "2.0", "perspective": persp, "home_country": home,
                                                  "product": {"hs_code": "071080"}})
            common.write_json(d / "selection.json", {"schema_version": "2.0", "partners": partners[:1]})
            run(["init", "--study-dir", td])
            t = common.read_json(d / "tariff.json")
            ln = t["lanes"][partners[0]]
            assert ln["status"] == "verify" and ln["duty_rate_pct"]["tier"] == "missing"
            if persp == "export":
                assert ln["importer"] == partners[0] and ln["exporter"] == home
            else:
                assert ln["importer"] == home and ln["exporter"] == partners[0]
            assert ln["valuation_basis"] == ("FOB" if ln["importer"] in common.FOB_VALUATION else "CIF")
            # rejection without URL / date
            base = ["set-rate", "--study-dir", td, "--lane", partners[0], "--field", "duty_rate_pct", "--value", "5",
                    "--tier", "published"]
            run(base + ["--source", "a website"], expect=2)
            run(base + ["--source", "a website", "--url", "https://example.org/t"], expect=2)  # no as-of
            assert common.read_json(d / "tariff.json")["lanes"][partners[0]]["duty_rate_pct"]["tier"] == "missing"
            # user-tier figure, no URL needed -> check: sourced
            run(["set-rate", "--study-dir", td, "--lane", partners[0], "--field", "duty_rate_pct", "--value", "5",
                 "--tier", "user", "--source", "client's customs broker email"])
            out = run(["check", "--study-dir", td])
            assert "sourced" in out
            # add a second lane via init: no overwrite of the first
            common.write_json(d / "selection.json", {"schema_version": "2.0", "partners": partners})
            run(["init", "--study-dir", td])
            t = common.read_json(d / "tariff.json")
            assert t["lanes"][partners[0]]["duty_rate_pct"]["value"] == 5.0 and partners[1] in t["lanes"]
            assert t["lanes"][partners[1]]["status"] == "verify"
            # preference + all lanes + cited published rate
            run(["set-rate", "--study-dir", td, "--lane", "all", "--field", "mfn_rate_pct", "--value", "8",
                 "--tier", "published", "--source", "official tariff schedule", "--as-of", "2026-11-01"])
            run(["set-rate", "--study-dir", td, "--lane", partners[1], "--field", "preference_rate_pct", "--value", "0",
                 "--tier", "published", "--source", "x", "--url", "https://example.org/p", "--as-of", "2026-11-01"])
            run(["set-info", "--study-dir", td, "--lane", partners[1], "--agreement", "Test FTA",
                 "--vat-recoverable", "true", "--add-ntb", "registration", "--add-ntb", "labelling"])
            t = common.read_json(d / "tariff.json")
            assert t["lanes"][partners[1]]["preference"]["rate_pct"]["value"] == 0.0
            assert t["lanes"][partners[1]]["ntb"] == ["registration", "labelling"]
            assert t["lanes"][partners[1]]["duty_rate_pct"]["tier"] == "missing"  # lane 2 duty still missing
            run(["check", "--study-dir", td])
            t = common.read_json(d / "tariff.json")
            assert t["lanes"][partners[1]]["status"] == "verify" and t["lanes"][partners[0]]["status"] == "sourced"
            # a present rate without a date makes the lane verify
            ln0 = t["lanes"][partners[0]]
            ln0["mfn_rate_pct"]["as_of"] = None
            common.write_json(d / "tariff.json", t)
            run(["check", "--study-dir", td])
            assert common.read_json(d / "tariff.json")["lanes"][partners[0]]["status"] == "verify"
            run(["show", "--study-dir", td, "--lane", "all"])
    print("selftest OK")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, study=True):
        p = sub.add_parser(name)
        if study:
            p.add_argument("--study-dir", required=True)
        p.set_defaults(fn=fn)
        return p

    add("init", cmd_init)
    p = add("set-rate", cmd_set_rate)
    p.add_argument("--lane", required=True)
    p.add_argument("--field", required=True, choices=RATE_FIELDS)
    p.add_argument("--value", required=True, type=float)
    p.add_argument("--tier", required=True, choices=TIERS_SETTABLE)
    p.add_argument("--source", required=True)
    p.add_argument("--url")
    p.add_argument("--as-of", dest="as_of")
    p = add("set-info", cmd_set_info)
    p.add_argument("--lane", required=True)
    p.add_argument("--agreement")
    p.add_argument("--conditions")
    p.add_argument("--valuation", choices=("CIF", "FOB"))
    p.add_argument("--vat-recoverable", dest="vat_recoverable", choices=("true", "false", "unknown"))
    p.add_argument("--add-ntb", dest="add_ntb", action="append")
    p.add_argument("--notes")
    add("check", cmd_check)
    p = add("show", cmd_show)
    p.add_argument("--lane", default="all")
    add("selftest", cmd_selftest, study=False)

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
