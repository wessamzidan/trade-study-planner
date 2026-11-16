#!/usr/bin/env python3
"""Tests for scripts/build_ports.py output and scripts/estimate_freight.py.  Run: python3 tests/test_freight.py"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import estimate_freight as ef  # noqa: E402

RESULTS = []
SEL = {"export": ["SAU", "ARE", "NLD", "GBR", "DEU", "KWT"], "import": ["EGY", "CHN", "BEL", "ESP", "IND"]}


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = ef.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def make_study(kind="export", selection=True):
    d = Path(tempfile.mkdtemp(prefix=f"frt_{kind}_"))
    shutil.copy(ROOT / "tests" / f"demo_{kind}" / "inputs.json", d / "inputs.json")
    if selection:
        (d / "selection.json").write_text(json.dumps({"schema_version": "2.0", "partners": SEL[kind], "decided_by": "test"}))
    return d


def J(d, name):
    return json.loads((Path(d) / name).read_text())


def add_rate(d, lane, tier, usd, *extra):
    return run("add-rate", "--study-dir", str(d), "--lane", lane, "--tier", tier, "--usd", str(usd),
               "--source", f"test {tier} source", "--as-of", "2026-11-01", *extra)


def fill_charges(d):
    for k in common.CHARGE_KEYS:
        run("set-charge", "--study-dir", str(d), "--lane", "all", "--key", k, "--value", "100", "--tier", "user", "--source", "test")


def numbers_in(o):
    """Any int/float anywhere in a JSON-like object (bools excluded)."""
    if isinstance(o, bool) or o is None:
        return False
    if isinstance(o, (int, float)):
        return True
    if isinstance(o, dict):
        return any(numbers_in(v) for v in o.values())
    if isinstance(o, list):
        return any(numbers_in(v) for v in o)
    return False


def two_points(d, ctype="40RF"):
    """Two synthetic benchmarks on the line usd = 300 + 0.5 * nm (distance from searoute)."""
    pts = [("SGSIN", "NLRTM"), ("CNSHA", "USLAX")]
    db = json.loads((ef.PORTS_FILE).read_text())
    for i, (f, t) in enumerate(pts):
        a, b = db["by_locode"][f], db["by_locode"][t]
        nm, _ = ef.sea_nm(a["lat"], a["lon"], b["lat"], b["lon"])
        run("add-calibration", "--study-dir", str(d), "--label", f"TEST bench {i}", "--from-locode", f, "--to-locode", t,
            "--usd", str(round(300 + 0.5 * nm, 2)), "--container-type", ctype, "--source", "TEST index", "--url", "https://example.com/t",
            "--as-of", "2026-11-05")


# ------------------------------------------------------------------ ports.json
def test_ports():
    db = json.loads(ef.PORTS_FILE.read_text())
    ok_default, ok_coords, ok_idx = True, True, True
    for iso, c in db["countries"].items():
        if c["landlocked"]:
            ok_idx &= bool(c.get("gateways")) and all(g in db["by_locode"] for g in c["gateways"])
            continue
        defaults = [p for p in c["ports"] if p.get("default")]
        ok_default &= len(defaults) == 1 and c["ports"][0].get("default")
        for p in c["ports"]:
            ok_coords &= isinstance(p.get("lat"), float) and isinstance(p.get("lon"), float) and -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180
            b = db["by_locode"].get(p["locode"])
            ok_idx &= bool(b) and b["iso3"] == iso and b["lat"] == p["lat"] and b["lon"] == p["lon"]
            iso2 = common.load_countries()["iso3"][iso]["iso2"]
            ok_idx &= p["locode"][:2] == iso2
    check("ports.json: >=140 countries", len(db["countries"]) >= 140, len(db["countries"]))
    check("ports.json: exactly one default per sea country, first", ok_default)
    check("ports.json: every port has lat/lon", ok_coords)
    check("ports.json: by_locode consistent with countries, prefix = ISO2", ok_idx)
    check("ports.json: EGY default is EGALY, CHE landlocked", db["countries"]["EGY"]["ports"][0]["locode"] == "EGALY" and db["countries"]["CHE"]["landlocked"])


# ------------------------------------------------------------------ init
def test_init():
    d = make_study("export")
    code, out, _ = run("init", "--study-dir", str(d))
    fi = J(d, "freight_inputs.json")
    check("init: exit 0, container from inputs", code == 0 and fi["container_type"] == "40RF")
    check("init: home port Alexandria EGALY", fi["home_port"]["locode"] == "EGALY")
    check("init: default partner ports", fi["lanes"]["SAU"]["partner_port"]["locode"] == "SAJED" and fi["lanes"]["NLD"]["partner_port"]["locode"] == "NLRTM"
          and fi["lanes"]["DEU"]["partner_port"]["locode"] == "DEHAM")
    check("init: all charges start missing", all(not common.sv_ok(fi["lanes"]["SAU"]["local_charges"][k]) for k in common.CHARGE_KEYS))
    check("init: prints distance", "nm" in out)
    run("set-port", "--study-dir", str(d), "--lane", "SAU", "--locode", "SADMM")
    add_rate(d, "SAU", "quote", 1234)
    run("set-charge", "--study-dir", str(d), "--lane", "SAU", "--key", "origin_thc", "--value", "77", "--tier", "quote", "--source", "x")
    sel = J(d, "selection.json"); sel["partners"].append("IND")
    (d / "selection.json").write_text(json.dumps(sel))
    code, _, _ = run("init", "--study-dir", str(d))
    fi = J(d, "freight_inputs.json")
    check("init: never overwrites port/rates/charges", fi["lanes"]["SAU"]["partner_port"]["locode"] == "SADMM" and len(fi["lanes"]["SAU"]["rates"]) == 1
          and fi["lanes"]["SAU"]["local_charges"]["origin_thc"]["value"] == 77)
    check("init: adds lane for new partner", fi["lanes"]["IND"]["partner_port"]["locode"] == "INNSA")
    d2 = make_study("export", selection=False)
    code, _, err = run("init", "--study-dir", str(d2))
    check("init: blocked (2) without selection.json", code == 2 and err.startswith("BLOCKED"))
    # container fallback
    d3 = make_study("export")
    inp = J(d3, "inputs.json"); inp["commercial"] = None
    (d3 / "inputs.json").write_text(json.dumps(inp))
    code, _, _ = run("init", "--study-dir", str(d3))
    check("init: no container anywhere -> blocked", code == 2)
    code, _, _ = run("init", "--study-dir", str(d3), "--container", "40HC")
    check("init: --container fallback", code == 0 and J(d3, "freight_inputs.json")["container_type"] == "40HC")
    # landlocked
    d4 = make_study("export")
    (d4 / "selection.json").write_text(json.dumps({"partners": ["CHE"]}))
    run("init", "--study-dir", str(d4))
    ln = J(d4, "freight_inputs.json")["lanes"]["CHE"]
    check("init: landlocked partner gets gateway + note", ln["partner_port"]["locode"] == "NLRTM" and "landlocked" in ln["port_note"])


# ------------------------------------------------------------------ ladder
def test_ladder():
    d = make_study("export")
    run("init", "--study-dir", str(d))
    # no calibration, no rates -> gap
    code, out, _ = run("build", "--study-dir", str(d))
    fr = J(d, "freight.json")
    lane = fr["lanes"]["SAU"]
    check("build: exit 0, direction home_to_partner", code == 0 and fr["direction"] == "home_to_partner")
    check("gap: coverage_gap, freight tier missing", lane["coverage_gap"] and lane["freight"]["tier"] == "missing" and lane["freight"]["value"] is None)
    check("gap: no price number anywhere", not numbers_in(lane["freight_usd"]) and not numbers_in(lane["alternatives"]) and not numbers_in(fr["model"]["a"]))
    check("gap: model block has 0 points", fr["model"]["n_points"] == 0)
    check("gap: origin is home port", lane["origin_port"]["locode"] == "EGALY" and lane["destination_port"]["locode"] == "SAJED")
    # ladder
    add_rate(d, "DEU", "published", 2000); add_rate(d, "DEU", "connector", 1800); add_rate(d, "DEU", "quote", 1500, "--transit-days", "20")
    add_rate(d, "GBR", "published", 2100); add_rate(d, "GBR", "connector", 1900)
    add_rate(d, "KWT", "published", 2300)
    two_points(d)
    run("build", "--study-dir", str(d))
    fr = J(d, "freight.json"); L = fr["lanes"]
    check("ladder: quote beats connector/published", L["DEU"]["freight"]["tier"] == "quote" and L["DEU"]["freight_usd"]["base"] == 1500)
    check("ladder: lower rungs in alternatives", {a["tier"] for a in L["DEU"]["alternatives"]} >= {"connector", "published", "modelled"})
    check("ladder: connector beats published", L["GBR"]["freight"]["tier"] == "connector" and L["GBR"]["freight_usd"]["base"] == 1900)
    check("ladder: published beats modelled", L["KWT"]["freight"]["tier"] == "published")
    check("ladder: modelled when only calibration", L["SAU"]["freight"]["tier"] == "modelled" and not L["SAU"]["coverage_gap"])
    check("transit: from quote when stated", L["DEU"]["transit_days"] == {"low": 20.0, "base": 20.0, "high": 20.0, "tier": "quote"})
    nm = L["SAU"]["distance_nm"]
    t = L["SAU"]["transit_days"]
    check("transit: modelled formula", t["tier"] == "modelled" and abs(t["low"] - (nm / 432 + 1)) < 0.06 and abs(t["base"] - (nm / 360 + 3)) < 0.06 and abs(t["high"] - (nm / 312 + 7)) < 0.06)
    # several quotes -> median / min / max
    add_rate(d, "DEU", "quote", 1700); add_rate(d, "DEU", "quote", 1600)
    run("build", "--study-dir", str(d))
    f = J(d, "freight.json")["lanes"]["DEU"]["freight_usd"]
    check("top rung: median/min/max", f == {"low": 1500.0, "base": 1600.0, "high": 1700.0}, f)
    # modelled: two points -> exact line, one point -> through origin
    m = J(d, "freight.json")["model"]
    check("modelled: 2 points least squares recovers a,b", m["n_points"] == 2 and m["method"] == "least_squares" and abs(m["a"] - 300) < 1 and abs(m["b"] - 0.5) < 0.001, m)
    base = J(d, "freight.json")["lanes"]["SAU"]["freight_usd"]
    nm = J(d, "freight.json")["lanes"]["SAU"]["distance_nm"]
    check("modelled: base = a + b*nm, range +/-35%", abs(base["base"] - (m["a"] + m["b"] * nm)) < 1 and abs(base["low"] / base["base"] - 0.65) < 0.001
          and abs(base["high"] / base["base"] - 1.35) < 0.001, base)
    check("modelled: source lists benchmark labels and dates", "TEST bench 0" in J(d, "freight.json")["lanes"]["SAU"]["freight"]["source"] and "2026-11-05" in J(d, "freight.json")["lanes"]["SAU"]["freight"]["source"])
    d1 = make_study("export"); run("init", "--study-dir", str(d1))
    run("add-calibration", "--study-dir", str(d1), "--label", "ONE", "--from-locode", "SGSIN", "--to-locode", "NLRTM", "--usd", "3000",
        "--container-type", "40RF", "--source", "s", "--url", "https://example.com/1", "--as-of", "2026-11-05")
    run("build", "--study-dir", str(d1))
    m1 = J(d1, "freight.json")["model"]
    x = m1["points"][0]["distance_nm"]
    check("modelled: 1 point -> through origin", m1["n_points"] == 1 and m1["a"] == 0 and m1["method"] == "through_origin" and abs(m1["b"] * x - 3000) < 1, m1)
    # fallback when intercept negative: usd = 0.5*nm - 2000
    d5 = make_study("export"); run("init", "--study-dir", str(d5))
    db = json.loads(ef.PORTS_FILE.read_text())
    for i, (f_, t_) in enumerate([("SGSIN", "NLRTM"), ("CNSHA", "USLAX")]):
        a_, b_ = db["by_locode"][f_], db["by_locode"][t_]
        n_, _ = ef.sea_nm(a_["lat"], a_["lon"], b_["lat"], b_["lon"])
        run("add-calibration", "--study-dir", str(d5), "--label", f"NEG{i}", "--from-locode", f_, "--to-locode", t_, "--usd", str(round(0.5 * n_ - 2000, 2)),
            "--container-type", "40RF", "--source", "s", "--url", "https://example.com/1", "--as-of", "2026-11-05")
    code, out, _ = run("build", "--study-dir", str(d5))
    fr5 = J(d5, "freight.json")
    check("modelled: negative intercept falls back to origin with warning", fr5["model"]["method"] == "through_origin" and any("through-origin" in w for w in fr5["warnings"]))
    # calibration without citation / date rejected
    code, _, err = run("add-calibration", "--study-dir", str(d1), "--label", "X", "--from-locode", "SGSIN", "--to-locode", "NLRTM", "--usd", "1",
                       "--container-type", "40RF", "--source", "s", "--url", "", "--as-of", "2026-11-05")
    check("add-calibration: empty url rejected", code == 2)
    code, _, _ = run("add-calibration", "--study-dir", str(d1), "--label", "X", "--from-locode", "SGSIN", "--to-locode", "NLRTM", "--usd", "1",
                     "--container-type", "40RF", "--source", "s", "--url", "https://e.com", "--as-of", "not-a-date")
    check("add-calibration: bad date rejected", code == 2)
    code, _, _ = run("add-calibration", "--study-dir", str(d1), "--label", "X", "--from-locode", "SGSIN", "--to-locode", "NLRTM", "--usd", "1",
                     "--container-type", "40RF", "--source", "s")
    check("add-calibration: url/as-of required (argparse)", code == 2)
    check("add-calibration: nothing saved when rejected", len(J(d1, "freight_inputs.json")["calibration_points"]) == 1)
    # expired validity
    d6 = make_study("export"); run("init", "--study-dir", str(d6))
    add_rate(d6, "SAU", "quote", 1450, "--valid-until", "2026-01-31")
    run("build", "--study-dir", str(d6))
    l6 = J(d6, "freight.json")["lanes"]["SAU"]
    check("expired validity: note, still used", l6["freight"]["tier"] == "quote" and any("expired" in n for n in l6["notes"]))
    check("rate: invalid inputs rejected", add_rate(d6, "SAU", "quote", -5)[0] == 2 and run("add-rate", "--study-dir", str(d6), "--lane", "SAU", "--tier", "quote", "--usd", "5", "--source", " ")[0] == 2
          and add_rate(d6, "XXX", "quote", 5)[0] == 2)


# ------------------------------------------------------------------ equipment
def test_equipment():
    d = make_study("export"); run("init", "--study-dir", str(d))      # 40RF lanes
    add_rate(d, "SAU", "quote", 1000, "--container-type", "40DC")
    run("build", "--study-dir", str(d))
    l = J(d, "freight.json")["lanes"]["SAU"]
    check("equipment: dry rate not applied without multiplier", l["coverage_gap"] and any("different equipment" in a.get("note", "") for a in l["alternatives"]))
    check("equipment: not-applied rate keeps its usd only in alternatives", l["freight"]["value"] is None)
    run("set-multiplier", "--study-dir", str(d), "--value", "1.3", "--tier", "published", "--source", "TEST reefer premium", "--url", "https://example.com/m", "--as-of", "2026-11-01")
    run("build", "--study-dir", str(d))
    l = J(d, "freight.json")["lanes"]["SAU"]
    check("equipment: applied with multiplier, tier kept", l["freight"]["tier"] == "quote" and abs(l["freight_usd"]["base"] - 1300) < 0.01 and any("equipment_multiplier" in n for n in l["notes"]), l["freight_usd"])
    # dry calibration needs multiplier too
    d2 = make_study("export"); run("init", "--study-dir", str(d2)); two_points(d2, "40DC")
    run("build", "--study-dir", str(d2))
    f2 = J(d2, "freight.json")
    check("equipment: dry benchmarks unusable without multiplier", f2["model"]["n_points"] == 0 and f2["lanes"]["SAU"]["coverage_gap"]
          and any("equipment_multiplier" in w for w in f2["warnings"]))
    run("set-multiplier", "--study-dir", str(d2), "--value", "2", "--tier", "published", "--source", "s", "--as-of", "2026-11-01")
    run("build", "--study-dir", str(d2))
    f3 = J(d2, "freight.json")
    check("equipment: dry benchmarks scaled by multiplier", f3["model"]["n_points"] == 2 and abs(f3["model"]["b"] - 1.0) < 0.002 and abs(f3["model"]["a"] - 600) < 2, f3["model"])


# ------------------------------------------------------------------ all-in
def test_all_in():
    d = make_study("export"); run("init", "--study-dir", str(d)); fill_charges(d)
    add_rate(d, "SAU", "quote", 2000, "--all-in", "--covers", "origin_thc,destination_thc")
    add_rate(d, "ARE", "quote", 2000)
    add_rate(d, "NLD", "quote", 2000, "--all-in")
    run("build", "--study-dir", str(d))
    L = J(d, "freight.json")["lanes"]
    s = L["SAU"]["local_charges"]
    check("all-in: listed charges zeroed with rate tier", L["SAU"]["all_in"] and all(s[k]["value"] == 0 and s[k]["tier"] == "quote" and s[k]["source"] == "included in all-in rate" for k in ("origin_thc", "destination_thc")))
    check("all-in: unlisted charges untouched", all(s[k]["value"] == 100 and s[k]["tier"] == "user" for k in common.CHARGE_KEYS if k not in ("origin_thc", "destination_thc")))
    check("not all-in: charges pass through", not L["ARE"]["all_in"] and all(v["value"] == 100 for v in L["ARE"]["local_charges"].values()))
    check("all-in without covers: nothing zeroed + note", L["NLD"]["all_in"] and all(v["value"] == 100 for v in L["NLD"]["local_charges"].values()) and any("no covered" in n or "lists no" in n for n in L["NLD"]["notes"]))
    check("missing charges stay missing", all(L["GBR"]["local_charges"][k]["tier"] == "user" for k in common.CHARGE_KEYS))
    d2 = make_study("export"); run("init", "--study-dir", str(d2)); run("build", "--study-dir", str(d2))
    check("missing charge passes through as missing", J(d2, "freight.json")["lanes"]["SAU"]["local_charges"]["origin_thc"] == common.sv_missing("not yet collected"))
    check("add-rate --covers needs --all-in", run("add-rate", "--study-dir", str(d), "--lane", "SAU", "--tier", "quote", "--usd", "5", "--source", "s", "--covers", "origin_thc")[0] == 2)
    check("add-rate unknown cover key rejected", run("add-rate", "--study-dir", str(d), "--lane", "SAU", "--tier", "quote", "--usd", "5", "--source", "s", "--all-in", "--covers", "bogus")[0] == 2)


# ------------------------------------------------------------------ import
def test_import():
    d = make_study("import"); run("init", "--study-dir", str(d))
    two_points(d)
    run("build", "--study-dir", str(d))
    fr = J(d, "freight.json"); L = fr["lanes"]
    check("import: direction partner_to_home", fr["direction"] == "partner_to_home")
    check("import: partner port is origin", L["EGY"]["origin_port"]["locode"] == "EGALY" and L["EGY"]["destination_port"]["locode"] == "SAJED"
          and L["CHN"]["origin_port"]["locode"] == "CNSHA")
    check("import: lanes follow selection order", list(L) == SEL["import"])
    check("import: modelled prices for all lanes", all(l["freight"]["tier"] == "modelled" for l in L.values()))
    check("import: India lane has distance", L["IND"]["distance_nm"] and L["IND"]["distance_nm"] > 500)


# ------------------------------------------------------------------ freightos
class FakeHTTP:
    def __init__(self, resp=None, status=200, raise_=None):
        self.calls, self.resp, self.status, self.raise_ = [], resp, status, raise_

    def __call__(self, url, headers, body, timeout=30):
        self.calls.append((url, dict(headers), body))
        if self.raise_:
            raise self.raise_
        return self.status, self.resp


GOOD = {"requestId": "r1", "modes": [{"mode": "LCL", "averagePrice": {"value": 9, "currency": "USD"}},
                                       {"mode": "FCL", "averagePrice": {"value": 2750, "currency": "USD"}, "averageTransitTime": {"from": 10, "to": 14, "unit": "days"}}]}


def test_freightos():
    saved = ef._http_post
    key = "SECRET-TEST-KEY-12345"
    try:
        d = make_study("export"); run("init", "--study-dir", str(d))
        os.environ.pop("FREIGHTOS_API_KEY", None)
        code, _, err = run("freightos", "--study-dir", str(d))
        check("freightos: no key -> blocked, mentions free key", code == 2 and "free" in err.lower() and "10" in err)
        os.environ["FREIGHTOS_API_KEY"] = key
        fake = FakeHTTP(GOOD); ef._http_post = fake
        code, out, err = run("freightos", "--study-dir", str(d), "--lanes", "SAU,NLD")
        fi = J(d, "freight_inputs.json")
        r = fi["lanes"]["SAU"]["rates"][0]
        check("freightos: 2 calls, rates stored as connector", code == 0 and len(fake.calls) == 2 and r["tier"] == "connector" and r["usd"] == 2750 and r["as_of"] == common.today()
              and r["source"] == "Freightos Rate Estimate API (market average)")
        url, headers, body = fake.calls[0]
        check("freightos: request shape", url.endswith("/api/rate-estimate/v1/search") and headers["apiKey"] == key and headers["Content-Type"] == "application/json"
              and body["shipment"]["origin"] == {"type": "seaport", "code": "EGALY"} and body["shipment"]["destination"] == {"type": "seaport", "code": "SAJED"}
              and body["shipment"]["load"]["packages"] == [{"packagingType": "container40", "quantity": 1}])
        check("freightos: reefer stored with real type, dry equivalent requested", r["container_type"] == "40RF" and r["transit_days"] == 12.0)
        check("freightos: calls counted by date", fi["meta"]["freightos_calls"][common.today()] == 2)
        allfiles = "".join(p.read_text() for p in d.iterdir() if p.is_file()) + out + err
        check("freightos: key never on disk or in output", key not in allfiles)
        # idempotent: lanes done today are skipped
        fake.calls.clear()
        run("freightos", "--study-dir", str(d))
        check("freightos: lanes already done today skipped (4 remaining)", len(fake.calls) == 4)
        # budget
        fi = J(d, "freight_inputs.json"); fi["meta"]["freightos_calls"][common.today()] = 8
        for ln in fi["lanes"].values():
            ln["rates"] = []
        (d / "freight_inputs.json").write_text(json.dumps(fi))
        fake.calls.clear()
        code, _, err = run("freightos", "--study-dir", str(d), "--lanes", "SAU,ARE,NLD")
        check("freightos: budget of 10/day refused without --force", code == 2 and "limit" in err and not fake.calls)
        code, _, _ = run("freightos", "--study-dir", str(d), "--lanes", "SAU,ARE,NLD", "--force")
        check("freightos: --force overrides", code == 0 and len(fake.calls) == 3)
        code, _, _ = run("freightos", "--study-dir", str(d), "--lanes", "SAU,ARE")
        # non-USD, no FCL, unreachable
        d2 = make_study("export"); run("init", "--study-dir", str(d2))
        ef._http_post = FakeHTTP({"modes": [{"mode": "FCL", "averagePrice": {"value": 2000, "currency": "EUR"}}]})
        code, out, _ = run("freightos", "--study-dir", str(d2), "--lanes", "SAU")
        check("freightos: non-USD skipped with warning", code == 0 and "WARNING" in out and not J(d2, "freight_inputs.json")["lanes"]["SAU"]["rates"])
        ef._http_post = FakeHTTP({"modes": [{"mode": "LCL", "averagePrice": {"value": 5, "currency": "USD"}}]})
        code, out, _ = run("freightos", "--study-dir", str(d2), "--lanes", "ARE")
        check("freightos: no FCL mode -> nothing recorded", code == 0 and "no FCL" in out and not J(d2, "freight_inputs.json")["lanes"]["ARE"]["rates"])
        ef._http_post = FakeHTTP(raise_=ef.HostUnreachable("URLError"))
        code, _, err = run("freightos", "--study-dir", str(d2), "--lanes", "NLD")
        check("freightos: host unreachable -> blocked naming other rungs", code == 2 and "quote" in err and "modelled" in err)
        # import direction
        d3 = make_study("import"); run("init", "--study-dir", str(d3))
        fk = FakeHTTP(GOOD); ef._http_post = fk
        run("freightos", "--study-dir", str(d3), "--lanes", "EGY")
        b = fk.calls[0][2]["shipment"]
        check("freightos: import origin is the partner port", b["origin"]["code"] == "EGALY" and b["destination"]["code"] == "SAJED")
    finally:
        ef._http_post = saved
        os.environ.pop("FREIGHTOS_API_KEY", None)


# ------------------------------------------------------------------ no searoute
def test_no_searoute():
    d = make_study("export"); run("init", "--study-dir", str(d)); two_points(d)
    add_rate(d, "DEU", "quote", 1500)
    prev = sys.modules.get("searoute", "absent")
    sys.modules["searoute"] = None             # makes `import searoute` raise ImportError
    try:
        code, out, _ = run("build", "--study-dir", str(d))
        fr = J(d, "freight.json")
    finally:
        if prev == "absent":
            sys.modules.pop("searoute", None)
        else:
            sys.modules["searoute"] = prev
    L = fr["lanes"]
    check("no searoute: build still exits 0 with pip hint", code == 0 and any("pip install searoute" in w for w in fr["warnings"]))
    check("no searoute: distance null, modelled lanes become gaps", L["SAU"]["distance_nm"] is None and L["SAU"]["coverage_gap"] and L["SAU"]["transit_days"]["base"] is None)
    check("no searoute: quoted lane unaffected", L["DEU"]["freight"]["tier"] == "quote" and L["DEU"]["freight_usd"]["base"] == 1500)
    check("no searoute: no calibration points usable", fr["model"]["n_points"] == 0)


def test_misc():
    d = make_study("export"); run("init", "--study-dir", str(d))
    code, out, _ = run("ports", "--country", "EGY")
    check("ports: lists EGALY default", code == 0 and "EGALY" in out and "[default]" in out)
    code, out, _ = run("ports", "--country", "Switzerland")
    check("ports: landlocked gateways", "landlocked" in out and "NLRTM" in out)
    check("set-port: unknown locode without coords blocked", run("set-port", "--study-dir", str(d), "--lane", "SAU", "--locode", "ZZZZZ")[0] == 2)
    check("set-charge: unknown key rejected", run("set-charge", "--study-dir", str(d), "--lane", "SAU", "--key", "bogus", "--value", "1", "--tier", "user", "--source", "s")[0] == 2)
    check("build: blocked when selected partner has no lane", (lambda: (
        (d / "selection.json").write_text(json.dumps({"partners": ["SAU", "FRA"]})), run("build", "--study-dir", str(d))[0])[1])() == 2)
    ok = all(c in common.CHARGE_KEYS for c in ("origin_inland", "destination_inland"))
    check("charge keys sane", ok)


if __name__ == "__main__":
    for t in (test_ports, test_init, test_ladder, test_equipment, test_all_in, test_import, test_freightos, test_no_searoute, test_misc):
        try:
            t()
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            check(f"{t.__name__} raised {type(e).__name__}", False, str(e))
    p, f = sum(RESULTS), len(RESULTS) - sum(RESULTS)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
