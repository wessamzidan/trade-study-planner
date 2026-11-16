#!/usr/bin/env python3
"""Tests for trade_data.py, analyze_trade.py and make_fixture.py. Run: python3 tests/test_trade.py

Plain asserts (no pytest). Works on temporary copies of the two demo folders; prints PASS/FAIL per check
and exits non-zero when any check fails.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import analyze_trade  # noqa: E402
import common  # noqa: E402
import make_fixture  # noqa: E402
import pandas as pd  # noqa: E402
import trade_data  # noqa: E402

RESULTS = []
SECRET = "SECRET-KEY-0123456789abcdef"


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <-- {detail}"))


def run(mod, argv):
    """Run mod.main(argv) in-process; returns (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = mod.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def fresh(tmp: Path, name: str, tag: str = "") -> Path:
    d = tmp / (tag or name)
    shutil.copytree(ROOT / "tests" / f"demo_{name}", d, ignore=shutil.ignore_patterns("raw", "flows*", "analysis.json", "query_plan.json", "freight*", "tariff*", "financial*"))
    return d


def pipeline(d: Path):
    assert make_fixture.main(["--study-dir", str(d)]) == 0
    assert run(trade_data, ["plan", "--study-dir", str(d)])[0] == 0
    return run(trade_data, ["normalize", "--study-dir", str(d)])


# ------------------------------------------------------------------ plan

def test_plan(tmp):
    for name, flows in (("export", {"home_flows": "X", "world_imports": "M", "world_exports": "X", "mirror": "M"}),
                        ("import", {"home_flows": "M", "world_imports": "M", "world_exports": "X", "mirror": "X"})):
        d = fresh(tmp, name, "plan_" + name)
        pd_ids = ["DEU", "SAU"] if name == "export" else ["DEU", "EGY"]
        code, out, _ = run(trade_data, ["plan", "--study-dir", str(d), "--partners", ",".join(pd_ids)])
        plan = json.loads((d / "query_plan.json").read_text())
        q = {x["id"]: x for x in plan["queries"]}
        check(f"plan[{name}]: exit 0 and the 4 base queries exist", code == 0 and set(flows) <= set(q))
        check(f"plan[{name}]: flows per perspective", all(q[i]["params"]["flow"] == f for i, f in flows.items()),
              str({i: q[i]["params"]["flow"] for i in flows}))
        check(f"plan[{name}]: partner_detail queries from --partners", {f"partner_detail:{i}" for i in pd_ids} <= set(q))
        years = plan["years"]
        check(f"plan[{name}]: years from inputs", years == list(range(2021, 2027)) and q["home_flows"]["comtrade"]["period"] == ",".join(map(str, years)))
        hc = common.resolve_country(("EGY" if name == "export" else "SAU"))["comtrade_code"]
        check(f"plan[{name}]: numeric home code; reporter omitted for 'all'; World = 0",
              q["home_flows"]["comtrade"]["reporterCode"] == hc and "reporterCode" not in q["world_imports"]["comtrade"]
              and q["world_imports"]["comtrade"]["partnerCode"] == 0 and "partnerCode" not in q["home_flows"]["comtrade"])
        check(f"plan[{name}]: fixed Comtrade settings", all(q[i]["comtrade"][k] == v for i in q for k, v in
              (("partner2Code", 0), ("customsCode", "C00"), ("motCode", 0))))
        check(f"plan[{name}]: renderings present", all(q[i]["ui_steps"] and q[i]["connector_hint"] and q[i]["api_path"]
              and q[i]["preview_path"] and q[i]["save_as"] for i in q))
        check(f"plan[{name}]: UI steps cover the required settings",
              all(any(t in " ".join(q["mirror"]["ui_steps"]) for t in (s,)) for s in ("Goods", "Annual", "HS", "2nd partner: World", "TOTAL", "CSV")))
    d = fresh(tmp, "export", "plan_matrix")
    code, out, _ = run(trade_data, ["plan", "--study-dir", str(d), "--matrix", "--format", "json"])
    check("plan: --matrix adds the matrix query; json format parses", code == 0 and "matrix" in {x["id"] for x in json.loads(out)["queries"]})
    (d / "inputs.json").write_text(json.dumps({**json.loads((d / "inputs.json").read_text()), "years": None}))
    check("plan: missing years -> exit 2", run(trade_data, ["plan", "--study-dir", str(d)])[0] == 2)


# ------------------------------------------------------------------ fetch

class FakeHttp:
    def __init__(self, rows_per_call=3, script=None):
        self.calls, self.rows, self.script = [], rows_per_call, list(script or [])

    def __call__(self, url, headers=None, timeout=60):
        self.calls.append((url, dict(headers or {})))
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return 200, {"count": self.rows, "data": [{"refYear": 2021, "primaryValue": 1}] * self.rows, "error": ""}


def all_text(d: Path) -> str:
    return "\n".join(p.read_text(errors="ignore") for p in d.rglob("*") if p.is_file())


def test_fetch(tmp):
    d = fresh(tmp, "export", "fetch")
    run(trade_data, ["plan", "--study-dir", str(d), "--partners", "SAU"])
    sleeps = []
    orig_get, orig_pause = trade_data.http_get_json, trade_data.pause
    trade_data.pause = sleeps.append
    try:
        os.environ["TEST_COMTRADE_KEY"] = SECRET
        fake = FakeHttp()
        trade_data.http_get_json = fake
        code, out, err = run(trade_data, ["fetch", "--study-dir", str(d), "--key-env", "TEST_COMTRADE_KEY"])
        plan = json.loads((d / "query_plan.json").read_text())
        check("fetch keyed: exit 0, one call per query (<=12 periods)", code == 0 and len(fake.calls) == len(plan["queries"]), f"{code} {len(fake.calls)} {err[-200:]}")
        check("fetch keyed: key in header only, uses keyed endpoint", all(h.get(trade_data.KEY_HEADER) == SECRET and SECRET not in u and "/data/v1/get/" in u for u, h in fake.calls))
        check("fetch keyed: raw files and log written", (d / "raw" / "home_flows.json").exists() and (d / "raw" / "_fetch_log.json").exists())
        check("fetch keyed: pacing >= 1.1 s between calls", len(sleeps) == len(fake.calls) - 1 and all(s >= 1.1 for s in sleeps))
        check("fetch keyed: key never written to any file or output", SECRET not in all_text(d) and SECRET not in out + err)
        code, out, err = run(trade_data, ["fetch", "--study-dir", str(d), "--key-env", "TEST_COMTRADE_KEY", "--dry-run"])
        check("fetch --dry-run: redacts the key and makes no calls", code == 0 and SECRET not in out + err and "<redacted>" in out and len(fake.calls) == len(plan["queries"]))
        # keyless: one call per year, truncation flag at exactly 500 rows, retry on 429
        shutil.rmtree(d / "raw")
        os.environ.pop("TEST_COMTRADE_KEY")
        fake = FakeHttp(script=[(429, {"error": "slow down"}), (200, {"data": [{}] * 500}), (200, {"data": [{}] * 5})])
        fake.rows = 4
        trade_data.http_get_json = fake
        code, out, err = run(trade_data, ["fetch", "--study-dir", str(d), "--only", "home_flows"])
        log = json.loads((d / "raw" / "_fetch_log.json").read_text())
        e = log["queries"][0]
        check("fetch keyless: preview endpoint, one call per year (+1 retry)", code == 0 and len(fake.calls) == 6 + 1 and "/public/v1/preview/" in fake.calls[0][0]
              and not any(h for _, h in fake.calls), f"{len(fake.calls)}")
        check("fetch keyless: 500-row response flagged truncated with a loud warning", e["truncated"] is True and "TRUNCATED" in err)
        check("fetch keyless: per-year files raw/<id>_<year>.json", (d / "raw" / "home_flows_2021.json").exists() and (d / "raw" / "home_flows_2026.json").exists())
        check("fetch keyless: retry backoff slept", any(s >= 2 for s in sleeps))
        # unreachable host
        shutil.rmtree(d / "raw")
        trade_data.http_get_json = FakeHttp(script=[urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))])
        code, out, err = run(trade_data, ["fetch", "--study-dir", str(d)])
        check("fetch: unreachable host -> exit 2 BLOCKED naming alternatives", code == 2 and "BLOCKED" in err and "connector" in err and "CSV" in err, err)
    finally:
        trade_data.http_get_json, trade_data.pause = orig_get, orig_pause
        os.environ.pop("TEST_COMTRADE_KEY", None)


# ------------------------------------------------------------------ normalize

def test_normalize(tmp):
    d = fresh(tmp, "export")
    code, out, err = pipeline(d)
    truth = json.loads((d / "raw" / "_synthetic_truth.json").read_text())
    meta = json.loads((d / "flows_meta.json").read_text())
    df = pd.read_csv(d / "flows.csv", dtype={"hs_code": str}, keep_default_na=False)
    check("normalize: exit 0 on the synthetic export set", code == 0, err)
    check("normalize: drops re-export flows and counts them", set(df["flow"]) == {"X", "M"} and meta["dropped"]["reexports"] == truth["reexport_rows"] > 0)
    check("normalize: drops aggregate reporters (EU) and counts them", meta["dropped"]["aggregates"] == truth["aggregate_rows"] > 0
          and not df["reporter_iso3"].str.startswith("_").any())
    b = truth["breakdown_with_total"]
    row = df[(df.year == b["year"]) & (df.reporter_iso3 == b["reporter"]) & (df.partner_iso3 == "W00") & (df.flow == "M")]
    check("normalize: breakdown rows ignored when a TOTAL row exists", len(row) == 1 and row.value_usd.iloc[0] == b["value"], str(row))
    b = truth["breakdown_without_total"]
    row = df[(df.year == b["year"]) & (df.reporter_iso3 == b["reporter"]) & (df.partner_iso3 == "W00") & (df.flow == "M")]
    check("normalize: breakdown rows summed when no TOTAL row exists", len(row) == 1 and row.value_usd.iloc[0] == b["value"], str(row))
    check("normalize: one row per (year, reporter, partner, flow, hs)", not df.duplicated(["year", "reporter_iso3", "partner_iso3", "flow", "hs_code"]).any())
    civ = df[df.partner_iso3 == "CIV"]
    check("normalize: Cote d'Ivoire (apostrophe, Latin-1) survives the round trip", len(civ) > 0 and (civ.partner_name == "Côte d'Ivoire").all())
    check("normalize: 'China, Hong Kong SAR' (quoted comma) parsed into HKG rows", (df.partner_iso3 == "HKG").any() and (df.reporter_iso3 == "HKG").any())
    check("normalize: all four file formats read", {f["format"] for f in meta["files"]} >= {"comtrade_ui_csv", "comtrade_api_json", "generic_rows_json", "generic_rows_csv"},
          str({f["format"] for f in meta["files"]}))
    check("normalize: _-prefixed raw files skipped", all(not f["file"].startswith("_") for f in meta["files"]))
    check("normalize: views detected", meta["views"]["home_flows"] and meta["views"]["world_imports"] and meta["views"]["world_exports"] and meta["views"]["mirror"]
          and set(meta["views"]["partner_detail"]) == {"SAU", "ARE", "NLD", "GBR", "DEU", "KWT"} and meta["views"]["matrix"] is False, str(meta["views"]))
    check("normalize: duplicates across files collapsed (World row in a detail file equals the world file)",
          len(df[(df.reporter_iso3 == "SAU") & (df.partner_iso3 == "W00") & (df.flow == "M") & (df.year == truth["latest_complete_year"])]) == 1)
    check("normalize: blank primaryValue falls back to cif/fob value", truth["blank_primary_rows"] == 3 and not df.value_usd.isna().any())
    check("normalize: hs_codes_found is the agreed code only", meta["hs_codes_found"] == ["071080"])
    # wrong HS code -> exit 2
    bad = tmp / "badhs"
    shutil.copytree(d, bad)
    (bad / "raw" / "extra.json").write_text(json.dumps([{"year": 2021, "reporter": "EGY", "partner": "SAU", "flow": "X", "value_usd": 5, "hs_code": "070410"}]))
    code, _, err = run(trade_data, ["normalize", "--study-dir", str(bad)])
    check("normalize: another HS code in the data -> exit 2", code == 2 and "HS" in err, err)
    code, _, err = run(trade_data, ["normalize", "--study-dir", str(bad), "--allow-hs", "070410"])
    check("normalize: --allow-hs override accepted", code == 0, err)
    # missing home_flows -> exit 2
    nohome = tmp / "nohome"
    shutil.copytree(d, nohome)
    (nohome / "raw" / "SYNTHETIC_home_flows.csv").unlink()
    check("normalize: home_flows absent -> exit 2", run(trade_data, ["normalize", "--study-dir", str(nohome)])[0] == 2)
    check("normalize: no raw files -> exit 2", run(trade_data, ["normalize", "--study-dir", str(fresh(tmp, "import", "emptyimp"))])[0] == 2)


# ------------------------------------------------------------------ analysis

def no_nan(path: Path) -> bool:
    def bad(c):
        raise ValueError(c)
    try:
        json.loads(path.read_text(encoding="utf-8"), parse_constant=bad)
        return True
    except ValueError:
        return False


def test_analysis_export(tmp):
    d = tmp / "export"
    code, out, err = run(analyze_trade, ["--study-dir", str(d)])
    a = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    m, truth = a["meta"], json.loads((d / "raw" / "_synthetic_truth.json").read_text())
    years = truth["years"]
    check("analysis[export]: exit 0 and a 10-line summary", code == 0 and len(out.strip().splitlines()) == 10, str(len(out.splitlines())))
    check("analysis[export]: JSON has no NaN/Infinity", no_nan(d / "analysis.json"))
    check("analysis[export]: latest complete year is the second-to-last year; last year is partial",
          m["latest_complete_year"] == years[-2] and m["partial_years"] == [years[-1]] and m["base_year"] == years[-2] - 3, str(m))
    check("analysis[export]: window label and labels", m["window_label"] == f"{m['base_year']}–{m['latest_complete_year']}"
          and m["labels"]["partner_noun"] == "market" and m["labels"]["tier_core"] == "Defend")
    check("analysis[export]: top-level keys per contract", set(a) >= {"schema_version", "meta", "home_position", "world_imports", "world_exports", "partners", "shortlist", "mirror_checks", "partner_detail"})
    check("analysis[export]: last annual entries flagged partial", a["world_imports"]["annual"][-1]["partial"] and not a["world_imports"]["annual"][-2]["partial"]
          and a["home_position"]["annual"][-1]["partial"])
    rk = a["world_imports"]["ranking"]
    check("analysis[export]: ranking holds every reporter with data (no top-N), EU/areas excluded",
          len(rk) == 47 and rk == sorted(rk, key=lambda r: -r["value_usd"]) and all(common.is_rankable(r["iso3"]) for r in rk) and rk[0]["rank"] == 1, str(len(rk)))
    check("analysis[export]: home position has rank, share, cagr", all(a["home_position"][k] is not None for k in ("world_rank", "world_share_pct", "n_ranked", "cagr_pct", "latest_value_usd")))
    P = {p["iso3"]: p for p in a["partners"]}
    bad = [p["iso3"] for p in P.values() if p["partner_reports_latest"] and p["partner_trade_latest_usd"] is None]
    zero = [p["iso3"] for p in P.values() if not p["partner_reports_latest"] and p["partner_trade_latest_usd"] is not None]
    check("analysis[export]: every reporting partner has a non-null partner_trade value; non-reporters are null, not zero", not bad and not zero and sum(p["partner_reports_latest"] for p in P.values()) > 40, f"{bad} {zero}")
    MC = {c["iso3"]: c for c in a["mirror_checks"]}
    check("analysis[export]: UAE mirror status partner_not_reporting", MC["ARE"]["status"] == "partner_not_reporting" and MC["ARE"]["partner_reported_usd"] is None)
    check("analysis[export]: NLD partner_reports_less (hub); KWT partner_reports_more; SDN only_home_reports",
          MC["NLD"]["status"] == "partner_reports_less" and MC["KWT"]["status"] == "partner_reports_more" and MC["SDN"]["status"] == "only_home_reports")
    check("analysis[export]: ordinary partners consistent", MC["SAU"]["status"] == "consistent" and 0.75 <= MC["SAU"]["ratio"] <= 1.35)
    check("analysis[export]: Cote d'Ivoire survives the JSON round trip", P["CIV"]["name"] == "Côte d'Ivoire" and any(r["iso3"] == "CIV" and r["name"] == "Côte d'Ivoire" for r in rk))
    check("analysis[export]: zero-presence large market is a candidate with null home flow", P["USA"]["home_flow_latest_usd"] is None and P["USA"]["tier"] == "new"
          and P["USA"]["score_components"]["foothold"] == 0.0)
    check("analysis[export]: tiers (SAU core, DEU growth, minor never shortlisted)", P["SAU"]["tier"] == "core" and P["DEU"]["tier"] == "growth"
          and all(P[i]["tier"] != "minor" for s in a["shortlist"].values() for i in s))
    check("analysis[export]: shortlist sizes and partners_of_interest", len(a["shortlist"]["core"]) <= 6 and len(a["shortlist"]["growth"]) >= 5 and "DEU" in a["shortlist"]["growth"] and P["DEU"]["of_interest"])
    check("analysis[export]: ARE score null with note, still in core shortlist",
          P["ARE"]["score"] is None and any(n.startswith("not scored: no imports reported by this partner for 2025") for n in P["ARE"]["notes"]) and "ARE" in a["shortlist"]["core"])
    check("analysis[export]: null size -> null score; min components rule in meta", not [i for i, p in P.items() if p["score_components"]["size"] is None and p["score"] is not None]
          and a["meta"]["rules"]["score_min_components"] == 2)
    comps = P["NLD"]["score_components"]
    w = a["meta"]["rules"]["score_weights"]
    exp = sum(w[k] * v for k, v in comps.items() if v is not None) / sum(w[k] for k, v in comps.items() if v is not None)
    check("analysis[export]: score equals weighted mean of components", abs(P["NLD"]["score"] - exp) < 0.06, f"{P['NLD']['score']} vs {exp}")
    pr = {i: p["score_components"]["price"] for i, p in P.items() if p["score_components"]["price"] is not None}
    uv = {i: P[i]["partner_unit_value_usd_per_kg"] for i in pr}
    check("analysis[export]: price component higher-is-better", pr[max(uv, key=uv.get)] == 100.0 and pr[min(uv, key=uv.get)] == 0.0)
    det = a["partner_detail"]
    check("analysis[export]: partner_detail only for partners with own reports; UAE absent (no data), never fabricated",
          set(det) == {"SAU", "NLD", "GBR", "DEU", "KWT"} and "ARE" not in det, str(set(det)))
    ok = all(sum(r["share_pct"] for r in v["rows"]) <= 100 + 1e-6 and v["basis"] == "partner_reported" and v["basis_note"] for v in det.values())
    check("analysis[export]: detail shares are of the partner's total (sum <= 100, not 100 when areas/rest exist)", ok and all(sum(r["share_pct"] for r in v["rows"]) < 100 for v in det.values()))
    s = det["SAU"]
    wrow = pd.read_csv(d / "flows.csv").query("reporter_iso3=='SAU' and partner_iso3=='W00' and flow=='M' and year==@m['latest_complete_year']")
    check("analysis[export]: detail total is the partner's World row; HHI in (0,1); home rank", s["total_usd"] == wrow.value_usd.iloc[0] and 0 < s["hhi"] < 1 and s["home_rank"] is not None and s["rows"][0]["share_pct"] >= s["rows"][-1]["share_pct"])
    check("analysis[export]: openness comes from partner_detail", P["SAU"]["score_components"]["openness"] == round(100 * (1 - s["hhi"]), 1) and P["USA"]["score_components"]["openness"] is None)
    check("analysis[export]: mirror rows only for partners with presence >= threshold", all(P[c["iso3"]]["home_flow_share_of_home_pct"] >= 0.25 for c in a["mirror_checks"]))
    check("analysis[export]: world_exports block present too", a["world_exports"] is not None and len(a["world_exports"]["ranking"]) == 14)
    # CLI overrides
    code, _, _ = run(analyze_trade, ["--study-dir", str(d), "--latest-year", str(years[-3]), "--base-year", str(years[0])])
    a2 = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    check("analysis: --latest-year/--base-year overrides", code == 0 and a2["meta"]["latest_complete_year"] == years[-3] and a2["meta"]["partial_years"] == years[-2:] and a2["meta"]["base_year"] == years[0])
    run(analyze_trade, ["--study-dir", str(d)])


def test_detail_math():
    ent = [{"iso3": "AAA", "name": "A", "value": 60.0, "kg": 30.0}, {"iso3": "BBB", "name": "B", "value": 40.0, "kg": None}]
    full = analyze_trade.build_detail(ent, 0.0, None, "AAA", "partner_reported", 1)
    check("detail math: all suppliers listed and no World row -> shares sum to exactly 100", abs(sum(r["share_pct"] for r in full["rows"]) - 100) < 1e-9 and "sum of" in full["basis_note"])
    part = analyze_trade.build_detail(ent, 0.0, 200.0, "AAA", "partner_reported", 1)
    check("detail math: World row larger than the listed rows -> shares sum below 100", sum(r["share_pct"] for r in part["rows"]) == 50.0 and part["total_usd"] == 200.0)
    check("detail math: HHI and null unit value", abs(full["hhi"] - (0.36 + 0.16)) < 1e-9 and full["rows"][1]["unit_value_usd_per_kg"] is None and full["rows"][0]["unit_value_usd_per_kg"] == 2.0)
    pr = analyze_trade.pct_ranks({"a": 1, "b": 2, "c": 2, "d": 3, "e": None})
    check("percentiles: rank based in [0,100], ties averaged, nulls stay null", pr == {"a": 0.0, "b": 50.0, "c": 50.0, "d": 100.0, "e": None})
    check("cagr: null when an end is missing or zero", analyze_trade.cagr(None, 5, 1, 3) is None and analyze_trade.cagr(0, 5, 1, 3) is None and analyze_trade.cagr(1, 4, 1, 3) == 100.0)


def test_analysis_import(tmp):
    d = fresh(tmp, "import")
    code, out, err = pipeline(d)
    check("import: normalize exit 0", code == 0, err)
    code, out, err = run(analyze_trade, ["--study-dir", str(d)])
    a = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
    m = a["meta"]
    P = {p["iso3"]: p for p in a["partners"]}
    check("analysis[import]: exit 0, no NaN", code == 0 and no_nan(d / "analysis.json"), err)
    check("analysis[import]: supplier labels", m["perspective"] == "import" and m["labels"] == {"partner_noun": "supplier", "tier_core": "Keep", "tier_growth": "Develop", "tier_new": "New source"})
    check("analysis[import]: partner_trade comes from world_exports (primary), ranking by supplier scale", all(p["partner_trade_latest_usd"] is not None for p in P.values() if p["partner_reports_latest"]) and a["world_exports"] is not None)
    pr = {i: p["score_components"]["price"] for i, p in P.items() if p["score_components"]["price"] is not None}
    uv = {i: P[i]["partner_unit_value_usd_per_kg"] for i in pr}
    check("analysis[import]: price component inverted (lowest unit value scores highest)", pr[min(uv, key=uv.get)] == 100.0 and pr[max(uv, key=uv.get)] == 0.0)
    check("analysis[import]: home is SAU, home flow = imports from suppliers", m["home"]["iso3"] == "SAU" and P["EGY"]["home_flow_latest_usd"] > 0 and P["EGY"]["tier"] == "core")
    check("analysis[import]: mirror compares supplier-reported exports with home-reported imports", all(c["status"] in {"consistent", "partner_not_reporting", "only_home_reports", "partner_reports_less", "partner_reports_more"} for c in a["mirror_checks"]) and
          all(c["status"] == "consistent" for c in a["mirror_checks"] if c["iso3"] in ("CHN", "BEL")), str([(c["iso3"], c["status"], c["ratio"]) for c in a["mirror_checks"]]))
    check("analysis[import]: partner_detail = suppliers' own exports to their buyers", set(a["partner_detail"]) >= {"EGY", "CHN", "BEL", "ESP", "IND"} and a["partner_detail"]["EGY"]["basis"] == "partner_reported")
    check("analysis[import]: latest complete year is second-to-last", m["latest_complete_year"] == m["years_available"][-2] and m["partial_years"] == [m["years_available"][-1]])


def test_analysis_blocked(tmp):
    d = tmp / "export"
    base = pd.read_csv(d / "flows.csv", dtype={"hs_code": str}, keep_default_na=False)
    no_primary = tmp / "noprim"
    shutil.copytree(d, no_primary)
    base[~((base.flow == "M") & (base.partner_iso3 == "W00"))].to_csv(no_primary / "flows.csv", index=False)
    code, _, err = run(analyze_trade, ["--study-dir", str(no_primary)])
    check("analysis: primary world view absent -> exit 2 BLOCKED", code == 2 and "BLOCKED" in err, err)
    no_other = tmp / "noother"
    shutil.copytree(d, no_other)
    base[~((base.flow == "X") & (base.partner_iso3 == "W00") & (base.reporter_iso3 != "EGY"))].to_csv(no_other / "flows.csv", index=False)
    code, _, _ = run(analyze_trade, ["--study-dir", str(no_other)])
    a = json.loads((no_other / "analysis.json").read_text(encoding="utf-8"))
    check("analysis: other world view absent -> null block + warning, exit 0", code == 0 and a["world_exports"] is None and any("world_exports" in w for w in a["meta"]["warnings"]) and a["home_position"]["world_rank"] is None)
    check("analysis: no flows.csv -> exit 2", run(analyze_trade, ["--study-dir", str(tmp / "empty_study")])[0] == 2)


# ------------------------------------------------------------------ hygiene

def test_no_year_literals():
    for name in ("trade_data.py", "analyze_trade.py"):
        tree = ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
        doc_nodes = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                     and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        hits = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and id(n) not in doc_nodes:
                if isinstance(n.value, int) and not isinstance(n.value, bool) and 1900 <= n.value <= 2100:
                    hits.append(n.value)
                if isinstance(n.value, str) and re.search(r"(?<!\d)(19|20)\d\d(?!\d)", n.value):
                    hits.append(n.value)
        check(f"hygiene: no 4-digit year literal in {name}", not hits, str(hits))


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (tmp / "empty_study").mkdir()
        test_plan(tmp)
        test_fetch(tmp)
        test_normalize(tmp)
        test_analysis_export(tmp)
        test_detail_math()
        test_analysis_import(tmp)
        test_analysis_blocked(tmp)
        test_no_year_literals()
    ok = sum(RESULTS)
    print(f"\n{ok} PASS, {len(RESULTS) - ok} FAIL")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
