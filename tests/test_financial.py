#!/usr/bin/env python3
"""Tests for the financial model stage: python3 tests/test_financial.py

Plain asserts, PASS/FAIL per check, non-zero exit on failure. Builds its own small study folders in a temp
directory (no dependency on other stages' outputs). The parity section recalculates the generated workbooks with
LibreOffice (soffice --headless) and compares the hidden _check sheet with financial.json.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import build_financial_model as bfm  # noqa: E402
import common  # noqa: E402
import make_demo_tariff  # noqa: E402
import model_core as mc  # noqa: E402

PASS, FAIL = 0, 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"FAIL  {name}  {detail}")


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ------------------------------------------------------------------ fixtures


def S(v, tier="user"):
    return common.sv(v, tier, "TEST fixture", None, "2026-11-13")


def M():
    return common.sv_missing("")


def make_inputs(persp, T, basis="EXW", price=0.30, purchase=0.20, local=1.5, tm=20, by_partner=None, plan_by=None, ramp=None):
    exp = persp == "export"
    c = {"unit": "kg",
         "container": {"type": "40RF", "units_per_container": S(1000), "net_kg_per_unit": S(1, "derived")},
         "currencies": ({"model": "USD", "cost": "EGP", "price": "USD", "fx_cost_per_model": S(50), "fx_price_per_model": S(1, "derived")} if exp else
                        {"model": "USD", "cost": "USD", "price": "SAR", "fx_cost_per_model": S(1, "derived"), "fx_price_per_model": S(3.75)}),
         "insurance_rate_pct": S(0.5), "target_margin_pct": S(tm) if tm is not None else M(),
         "payment": {"terms": "T/T", "credit_days": S(30), "advance_pct": S(20), "customer_credit_days": S(45)},
         "timing": {"production_lead_days": S(20), "origin_dwell_days": S(5), "clearance_days": S(4), "inventory_days": S(25)},
         "fixed_costs": {"one_time": S(1000), "annual": S(5000)},
         "volume": {"capacity_units_per_year": S(100000), "planned_containers_per_year": {"default": S(12), "by_partner": plan_by or {}},
                    "monthly_ramp": ramp}}
    if exp:
        c["cost"] = {"basis": basis, "per_unit": S(10)}
        c["sell_incoterm"] = T
        c["price_per_unit"] = {"default": S(price) if price is not None else M(), "by_partner": by_partner or {}}
    else:
        c["buy_incoterm"] = T
        c["purchase_price_per_unit"] = {"default": S(purchase) if purchase is not None else M(), "by_partner": by_partner or {}}
        c["local_price_per_unit"] = S(local) if local is not None else M()
    return {"schema_version": "2.0", "study_id": "TEST", "prepared_for": "test", "prepared_by": "test", "perspective": persp,
            "home_country": "EGY" if exp else "SAU", "product": {"name": "Test product", "hs_code": "071080"}, "commercial": c}


def make_freight(fr=(35.0, 40.0, 55.0), charges=None):
    ch = {"origin_inland": 10, "origin_docs": 5, "origin_thc": 15, "destination_thc": 12, "import_clearance": 8, "destination_inland": 6}
    ch.update(charges or {})
    return {"partner": {"iso3": "SAU"}, "freight_usd": {"low": fr[0], "base": fr[1], "high": fr[2]}, "freight": S(fr[1], "quote"),
            "transit_days": {"low": 4, "base": 6, "high": 9, "tier": "quote"}, "coverage_gap": False,
            "local_charges": {k: S(v, "quote") for k, v in ch.items()}}


def make_tariff(valb="CIF", rec=True, duty=10.0, other=0.0, vat=15.0):
    return {"importer": "SAU", "exporter": "EGY", "duty_rate_pct": S(duty, "published"), "other_duties_pct": S(other, "published"),
            "import_vat_pct": S(vat, "published"), "vat_recoverable": rec, "valuation_basis": valb, "status": "sourced"}


def lane(inputs, fl=None, tl=None, row=None, iso="SAU"):
    return mc.build_lane_inputs(inputs, make_freight() if fl is None else fl, make_tariff() if tl is None else tl, row, iso)


# ------------------------------------------------------------------ 1. hand-computed cases


def test_hand_cases():
    r = 0.005
    # A: export, FOB sale, EXW cost basis
    inp = make_inputs("export", "FOB", "EXW", price=0.30)
    res = mc.compute_lane(lane(inp, tl=make_tariff("CIF", True, 10.0)))
    goods = 10 * 1000 / 50
    fob_cost = goods + 10 + 5 + 15
    V = 0.30 * 1000 / 1
    cifv = (V + 40) * (1 + r)
    du = 0.10 * cifv
    landed = cifv + 12 + 8 + du + 6
    check("A export FOB/EXW: status modelled", res["status"] == "modelled" and res["missing"] == [])
    check("A export FOB/EXW: revenue", close(res["revenue"], V))
    check("A export FOB/EXW: seller cost", close(res["user_cost"], fob_cost), res["user_cost"])
    check("A export FOB/EXW: margin", close(res["margin"], V - fob_cost))
    check("A export FOB/EXW: margin %", close(res["margin_pct"], (V - fob_cost) / V * 100))
    check("A export FOB/EXW: CIFv, DU, buyer landed", close(res["CIFv"], cifv) and close(res["DU"], du) and close(res["buyer_landed"], landed),
          f"{res['buyer_landed']} vs {landed}")
    check("A export FOB/EXW: landed per unit", close(res["buyer_landed_per_unit"], landed / 1000))
    # B: export, CIF sale, EXW cost, FOB valuation, non-recoverable VAT
    inp = make_inputs("export", "CIF", "EXW", price=0.35)
    res = mc.compute_lane(lane(inp, tl=make_tariff("FOB", False, 10.0, 0.0, 15.0)))
    V = 350.0
    ins_s = r * (fob_cost + 40)
    seller = fob_cost + 40 + ins_s
    fobv = V / (1 + r) - 40
    du = 0.10 * fobv
    vat = 0.15 * (V + du)
    landed = V + 12 + 8 + du + 6 + vat
    check("B export CIF: seller cost incl. insurance", close(res["user_cost"], seller), res["user_cost"])
    check("B export CIF: margin", close(res["margin"], V - seller))
    check("B export CIF: FOBv and FOB customs value", close(res["FOBv"], fobv) and close(res["CV"], fobv))
    check("B export CIF: duty, VAT, buyer landed", close(res["DU"], du) and close(res["VATnr"], vat) and close(res["buyer_landed"], landed))
    # C: import, FOB purchase, CIF valuation, recoverable VAT
    inp = make_inputs("import", "FOB", purchase=0.20, local=1.5)
    res = mc.compute_lane(lane(inp, tl=make_tariff("CIF", True, 10.0)))
    V = 0.20 * 1000 / 1
    cifv = (V + 40) * (1 + r)
    du = 0.10 * cifv
    landed = cifv + 12 + 8 + du + 6
    rev = 1.5 * 1000 / 3.75
    check("C import FOB/CIF: landed", close(res["user_cost"], landed) and close(res["buyer_landed"], landed), res["user_cost"])
    check("C import FOB/CIF: revenue and margin", close(res["revenue"], rev) and close(res["margin"], rev - landed))
    check("C import FOB/CIF: margin %", close(res["margin_pct"], (rev - landed) / rev * 100))
    check("C import FOB/CIF: break-even local price", close(res["breakeven_local_price_per_unit"], landed / 1000 * 3.75))
    check("C import FOB/CIF: required local price at 20 %", close(res["required_local_price_per_unit"], landed / 0.8 / 1000 * 3.75))
    # D: import, CIF purchase, FOB valuation, non-recoverable VAT, other duties 2 %
    inp = make_inputs("import", "CIF", purchase=0.25, local=1.5)
    res = mc.compute_lane(lane(inp, tl=make_tariff("FOB", False, 10.0, 2.0, 15.0)))
    V = 250.0
    fobv = V / (1 + r) - 40
    du = (0.10 + 0.02) * fobv
    vat = 0.15 * (V + du)
    landed = V + 12 + 8 + du + 6 + vat
    check("D import CIF/FOB valuation: FOBv and duty", close(res["FOBv"], fobv) and close(res["DU"], du), f"{res['DU']} vs {du}")
    check("D import CIF/FOB valuation: non-recoverable VAT and landed", close(res["VATnr"], vat) and close(res["user_cost"], landed))
    check("D import CIF/FOB valuation: margin", close(res["margin"], 400.0 - landed))
    # E: import DAP, EXW and CFR spot checks
    res = mc.compute_lane(lane(make_inputs("import", "DAP", purchase=0.30), tl=make_tariff("CIF", True, 10.0)))
    V = 300.0
    cifv = V - 12 - 6
    check("E import DAP: CIFv = V - DT - DI and landed = V + IC + DU", close(res["CIFv"], cifv) and close(res["user_cost"], V + 8 + 0.10 * cifv),
          res["user_cost"])
    res = mc.compute_lane(lane(make_inputs("import", "EXW", purchase=0.15), tl=make_tariff("CIF", True, 10.0)))
    V = 150.0
    cifv = (V + 30 + 40) * (1 + r)
    check("E import EXW: CIFv = (V+OI+OD+OT+FR)(1+r)", close(res["CIFv"], cifv) and close(res["user_cost"], cifv + 12 + 8 + 0.1 * cifv + 6))
    res = mc.compute_lane(lane(make_inputs("export", "CFR", price=0.30)))
    check("E export CFR: FOBv = V - FR, CIFv = V(1+r)", close(res["FOBv"], 300 - 40) and close(res["CIFv"], 300 * 1.005))
    # FCA is treated as FOB
    a = mc.compute_lane(lane(make_inputs("export", "FCA", price=0.30)))
    b = mc.compute_lane(lane(make_inputs("export", "FOB", price=0.30)))
    check("FCA equals FOB", a["margin"] == b["margin"] and a["buyer_landed"] == b["buyer_landed"] and a["incoterm"] == "FOB")
    # FX convention: +10 % moves the cost-currency rate by x1.10 (export) and the price-currency rate (import)
    base = mc.compute_lane(lane(make_inputs("export", "FOB", price=0.30)))
    fx = mc.compute_lane(lane(make_inputs("export", "FOB", price=0.30)), fx_shock_pct=10)
    check("FX shock export: goods cost / 1.10", close(fx["margin"], base["margin"] + 200 - 200 / 1.10))
    fxi = mc.compute_lane(lane(make_inputs("import", "FOB")), fx_shock_pct=10)
    bi = mc.compute_lane(lane(make_inputs("import", "FOB")))
    check("FX shock import: revenue / 1.10", close(fxi["revenue"], bi["revenue"] / 1.10))
    # input error
    res = mc.compute_lane(lane(make_inputs("export", "EXW", basis="FOB")))
    check("EXW sale on FOB cost basis is an input error", res["status"] == "error" and res["margin"] is None and res["errors"])
    # benchmark
    row = {"partner_unit_value_usd_per_kg": 0.5}
    res = mc.compute_lane(lane(make_inputs("export", "FOB", price=0.30), row=row))
    check("export benchmark price gap", close(res["price_gap_pct"], (res["buyer_landed"] / 1000 / 0.5 - 1) * 100), str(res["price_gap_pct"]))
    row = {"home_unit_value_usd_per_kg": 0.25}
    res = mc.compute_lane(lane(make_inputs("import", "FOB"), row=row))
    check("import benchmark price gap uses CIFv per unit", close(res["price_gap_pct"], (res["CIFv"] / 1000 / 0.25 - 1) * 100))
    # cash cycle and payback
    ex = mc.lane_summary(lane(make_inputs("export", "FOB", price=0.30)))
    check("export cash cycle", close(ex["cash_cycle_days"], (20 + 5 + 30) * (1 - 0.2)))
    check("export peak working capital", close(ex["peak_working_capital"], 12 * 44 / 365 * 230))
    im = mc.lane_summary(lane(make_inputs("import", "FOB")))
    check("import cash cycle", close(im["cash_cycle_days"], 6 + 4 + 25 + 45 - 30 * 0.8 + 20 * 0.2))
    check("break-even containers", close(ex["break_even_containers_per_year"], 5000 / 70))


# ------------------------------------------------------------------ 2. identities and required price


def test_identities():
    res = mc.compute_lane(lane(make_inputs("export", "DAP", price=0.40), tl=make_tariff("CIF", False, 10.0, 0.0, 15.0)))
    check("DAP export: buyer landed = invoice + IC + DU + VAT",
          close(res["buyer_landed"], res["invoice_value"] + 8 + res["DU"] + res["VATnr"]), f"{res['buyer_landed']}")
    res = mc.compute_lane(lane(make_inputs("import", "DAP", purchase=0.40), tl=make_tariff("FOB", False, 10.0, 0.0, 15.0)))
    check("DAP import: landed = invoice + IC + DU + VAT", close(res["user_cost"], res["invoice_value"] + 8 + res["DU"] + res["VATnr"]))
    res = mc.compute_lane(lane(make_inputs("export", "CFR", price=0.33)))
    check("CFR export: margin = revenue - (FOB_cost + FR)", close(res["margin"], res["revenue"] - (res["FOB_cost"] + 40)))
    # waterfall sums
    for persp, T in (("export", "EXW"), ("export", "FOB"), ("export", "CFR"), ("export", "CIF"), ("export", "DAP")):
        r_ = mc.compute_lane(lane(make_inputs(persp, T, price=0.45), tl=make_tariff("CIF", False, 10.0)))
        sell = sum(w["amount"] for w in r_["waterfall"] if w["side"] == "seller")
        buy = sum(w["amount"] for w in r_["waterfall"] if w["side"] == "buyer")
        check(f"waterfall export {T}: seller rows = seller cost, V + buyer rows = landed",
              close(sell, r_["user_cost"]) and close(r_["invoice_value"] + buy, r_["buyer_landed"]), f"{sell} {r_['user_cost']} {buy}")
    for T in ("EXW", "FOB", "CFR", "CIF", "DAP"):
        r_ = mc.compute_lane(lane(make_inputs("import", T, purchase=0.25), tl=make_tariff("FOB", False, 10.0)))
        buy = sum(w["amount"] for w in r_["waterfall"] if w["side"] == "buyer")
        inv = [w for w in r_["waterfall"] if w["key"] == "goods"][0]["amount"]
        check(f"waterfall import {T}: invoice + buyer rows = landed", close(inv + buy, r_["user_cost"]))
    # required price
    res = mc.compute_lane(lane(make_inputs("export", "FOB", price=None, tm=20)))
    check("required price: margin % equals target", close(res["margin_pct"], 20.0), str(res["margin_pct"]))
    check("required price: flagged and equals seller cost / (1-t) / U", res["price_basis"] == "required_at_target_margin"
          and close(res["price_per_unit"], 230 / 0.8 / 1000) and res["status"] == "modelled")
    res = mc.compute_lane(lane(make_inputs("export", "CIF", price=None, tm=15)))
    check("required price at CIF: margin % equals 15", close(res["margin_pct"], 15.0))
    res = mc.compute_lane(lane(make_inputs("export", "FOB", price=None, tm=None)))
    check("no price and no target margin: incomplete, both keys listed",
          res["status"] == "incomplete" and "price_per_unit" in res["missing"] and "target_margin_pct" in res["missing"] and res["margin"] is None)
    res = mc.compute_lane(lane(make_inputs("import", "FOB", local=None)))
    check("import without local price: margin null, break-even and required price reported",
          res["margin"] is None and res["status"] == "modelled" and res["breakeven_local_price_per_unit"] is not None
          and res["required_local_price_per_unit"] is not None and res["price_basis"] == "required_at_target_margin")


# ------------------------------------------------------------------ 3. missing data


def remove(inputs, fl, tl, key):
    """Return copies with the input behind `key` made missing."""
    inputs, fl, tl = copy.deepcopy(inputs), copy.deepcopy(fl), copy.deepcopy(tl)
    c = inputs["commercial"]
    if key in ("units_per_container", "net_kg_per_unit"):
        c["container"][key] = M()
    elif key in ("fx_cost_per_model", "fx_price_per_model"):
        c["currencies"][key] = M()
    elif key in ("insurance_rate_pct", "target_margin_pct", "local_price_per_unit"):
        c[key] = M()
    elif key == "cost_per_unit":
        c["cost"]["per_unit"] = M()
    elif key == "price_per_unit":
        c["price_per_unit"] = {"default": M(), "by_partner": {}}
    elif key == "purchase_price_per_unit":
        c["purchase_price_per_unit"] = {"default": M(), "by_partner": {}}
    elif key == "freight":
        fl["freight_usd"] = {"low": None, "base": None, "high": None}
        fl["freight"] = M()
    elif key in common.CHARGE_KEYS:
        fl["local_charges"][key] = M()
    elif key in ("duty_rate_pct", "other_duties_pct", "import_vat_pct"):
        tl[key] = M()
    elif key == "valuation_basis":
        tl[key] = None
    elif key == "vat_recoverable":
        tl[key] = None
    else:
        raise KeyError(key)
    return inputs, fl, tl


CHAIN_BY_T = {"EXW": ["origin_inland", "origin_docs", "origin_thc", "freight", "insurance_rate_pct"], "FOB": ["freight", "insurance_rate_pct"],
              "CFR": ["insurance_rate_pct"], "CIF": [], "DAP": ["destination_thc", "destination_inland"]}
COMMON_CHAIN = ["destination_thc", "import_clearance", "destination_inland", "duty_rate_pct", "other_duties_pct", "valuation_basis",
                "vat_recoverable", "import_vat_pct"]


def export_user_keys(T, basis):
    k = ["units_per_container", "cost_per_unit", "fx_cost_per_model", "fx_price_per_model"]
    if T != "EXW" and basis == "EXW":
        k += ["origin_inland", "origin_docs", "origin_thc"]
    if T in ("CFR", "CIF", "DAP"):
        k.append("freight")
    if T in ("CIF", "DAP"):
        k.append("insurance_rate_pct")
    if T == "DAP":
        k += ["destination_thc", "destination_inland"]
    return k


def test_missing():
    for T in ("EXW", "FOB", "CFR", "CIF", "DAP"):
        inp = make_inputs("export", T, "EXW", price=0.45)
        fl, tl = make_freight(), make_tariff("CIF", False, 10.0)
        base = mc.compute_lane(mc.build_lane_inputs(inp, fl, tl, None, "SAU"))
        user = export_user_keys(T, "EXW")
        for key in user:
            i2, f2, t2 = remove(inp, fl, tl, key)
            r_ = mc.compute_lane(mc.build_lane_inputs(i2, f2, t2, None, "SAU"))
            ok = (r_["status"] == "incomplete" and key in r_["missing"] and r_["margin"] is None and r_["margin_pct"] is None
                  and r_["revenue"] is None and r_["user_cost"] is None)
            check(f"export {T}: removing user-side {key} -> incomplete, listed, margin null", ok, str(r_["missing"]))
        buyer_only = [k for k in dict.fromkeys(CHAIN_BY_T[T] + COMMON_CHAIN) if k not in user]
        for key in buyer_only:
            i2, f2, t2 = remove(inp, fl, tl, key)
            r_ = mc.compute_lane(mc.build_lane_inputs(i2, f2, t2, None, "SAU"))
            ok = (r_["status"] == "modelled" and close(r_["margin"], base["margin"]) and key in r_["buyer_side_missing"]
                  and r_["buyer_landed"] is None and key not in r_["missing"])
            check(f"export {T}: removing buyer-side {key} keeps the seller margin", ok, f"{r_['status']} {r_['buyer_side_missing']}")
    for T in ("EXW", "FOB", "CFR", "CIF", "DAP"):
        inp = make_inputs("import", T, purchase=0.25)
        fl, tl = make_freight(), make_tariff("FOB", False, 10.0)
        keys = ["units_per_container", "purchase_price_per_unit", "fx_cost_per_model", "fx_price_per_model"] + \
            [k for k in dict.fromkeys(CHAIN_BY_T[T] + COMMON_CHAIN)]
        if T in ("CIF", "DAP"):   # FOB valuation needs freight and insurance to back out FOBv
            keys += ["freight", "insurance_rate_pct"]
        for key in dict.fromkeys(keys):
            i2, f2, t2 = remove(inp, fl, tl, key)
            r_ = mc.compute_lane(mc.build_lane_inputs(i2, f2, t2, None, "SAU"))
            ok = (r_["status"] == "incomplete" and key in r_["missing"] and r_["margin"] is None and r_["user_cost"] is None
                  and r_["buyer_landed"] is None and r_["breakeven_local_price_per_unit"] is None)
            check(f"import {T}: removing {key} -> incomplete, listed, results null", ok, str(r_["missing"]))
    # nothing from nothing: lane absent from freight.json and tariff.json
    for persp, T in (("export", "FOB"), ("import", "FOB")):
        li = mc.build_lane_inputs(make_inputs(persp, T), None, None, None, "SAU")
        res = mc.lane_summary(li)
        nums = ["revenue", "user_cost", "margin", "margin_pct", "buyer_landed", "buyer_landed_per_unit", "price_gap_pct",
                "break_even_containers_per_year", "peak_working_capital", "cash_tied_per_container"]
        allnull = all(res[k] is None for k in nums)
        grids = all(v is None for row in res["sensitivity"]["margin"] for v in row) and \
            all(e["margin"] is None for e in res["fx_sensitivity"]) and all(e["value"] is None for e in res["duty_sensitivity"]) and \
            all(v["margin"] is None and v["buyer_landed"] is None for v in res["freight_scenarios"].values())
        wf = all(w["amount"] in (None, 0.0) or w["key"] in ("goods",) for w in res["waterfall"])
        check(f"{persp}: lane with no freight/tariff -> incomplete and every result null", res["status"] == "incomplete" and allnull and grids, str(res["missing"]))
        check(f"{persp}: waterfall of an empty lane has no invented amounts", wf)
    # a single missing planned-containers value never becomes zero
    inp = make_inputs("export", "FOB")
    inp["commercial"]["volume"]["planned_containers_per_year"] = {"default": M(), "by_partner": {}}
    res = mc.lane_summary(lane(inp))
    port = mc.portfolio({"SAU": res}, inp)
    check("no plan: payback null with a reason, weighted margin null", port["payback_month"] is None and port["weighted_margin"] is None and port["payback_note"])
    inp = make_inputs("export", "FOB")
    inp["commercial"]["fixed_costs"]["annual"] = M()
    res = mc.lane_summary(lane(inp))
    port = mc.portfolio({"SAU": res}, inp)
    check("fixed cost missing: break-even and payback null, never zero", res["break_even_containers_per_year"] is None and port["payback_month"] is None and "annual fixed cost missing" in port["payback_note"])
    inp = make_inputs("export", "FOB")
    inp["commercial"]["timing"]["origin_dwell_days"] = M()
    res = mc.lane_summary(lane(inp))
    check("missing timing: cash cycle and peak working capital null, margin unaffected",
          res["cash_cycle_days"] is None and res["peak_working_capital"] is None and res["margin"] is not None and "origin_dwell_days" in res["cash_cycle_missing"])
    # sensitivity grid sanity
    res = mc.lane_summary(lane(make_inputs("export", "FOB", price=0.30)))
    g = res["sensitivity"]
    check("sensitivity grid centre equals base margin", close(g["margin"][g["price_pct"].index(0)][g["cost_pct"].index(0)], res["margin"]))
    check("price +10 % raises margin by 10 % of revenue", close(g["margin"][g["price_pct"].index(10)][g["cost_pct"].index(0)], res["margin"] + 30.0))
    # portfolio payback by hand: monthly cash with lag
    inp = make_inputs("export", "FOB", price=0.30)
    res = mc.lane_summary(lane(inp))
    port = mc.portfolio({"SAU": res}, inp)
    lag = mc.round_half_up(44 / 30)
    cum, pb = 0.0, None
    for m in range(1, 25):
        cash = (12 / 12 * 70 if m - lag >= 1 else 0.0) - 5000 / 12 - (1000 if m == 1 else 0)
        cum += cash
        if pb is None and cum >= 0:
            pb = m
    check("portfolio payback month equals hand arithmetic", port["payback_month"] == pb and port["cash_lag_months"] == lag, f"{port['payback_month']} vs {pb}")


# ------------------------------------------------------------------ 4. study folders, CLI, parity


def write_json(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


PARTNERS = ["SAU", "CIV", "HKG", "IND"]


def write_study(d, persp, T, valb="CIF", basis="EXW", price=True, local=True):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    if persp == "export":
        by = {"SAU": S(0.30), "CIV": S(0.33), "HKG": S(0.28), "IND": S(0.31)} if price else {}
        inp = make_inputs("export", T, basis, price=None if not price else 0.30, by_partner=by, plan_by={"SAU": S(24)},
                          ramp=[1, 2, 3, 4, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6] if T == "DAP" else None)
    else:
        by = {"SAU": S(0.20), "CIV": S(0.22), "HKG": S(0.19), "IND": S(0.21)}
        inp = make_inputs("import", T, purchase=0.20, local=1.5 if local else None, by_partner=by, plan_by={"SAU": S(24)})
        inp["home_country"] = "EGY"
    write_json(d / "inputs.json", inp)
    write_json(d / "selection.json", {"schema_version": "2.0", "partners": PARTNERS, "decided_by": "test"})
    lanes_f = {"SAU": make_freight(), "CIV": make_freight((60, 70, 90), {"origin_thc": 20, "destination_thc": 18}),
               "HKG": make_freight((20, 30, 38), {"import_clearance": 11})}   # IND is deliberately absent
    write_json(d / "freight.json", {"schema_version": "2.0", "container_type": "40RF", "direction": "home_to_partner", "lanes": lanes_f})
    lanes_t = {"SAU": make_tariff(valb, True, 10.0, 0.0, 15.0), "CIV": make_tariff(valb, False, 5.0, 1.0, 12.0),
               "HKG": make_tariff(valb, True, 0.0, 1.5, 0.0), "IND": make_tariff(valb, False, 20.0, 0.0, 18.0)}
    write_json(d / "tariff.json", {"schema_version": "2.0", "hs_code": "071080", "lanes": lanes_t})
    write_json(d / "analysis.json", {"schema_version": "2.0", "partners": [
        {"iso3": "SAU", "partner_unit_value_usd_per_kg": 0.55, "home_unit_value_usd_per_kg": 0.30}]})
    return d


NAMES = {"HKG": "China, Hong Kong SAR"}


def run_cli(*args):
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_financial_model.py"), *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def test_cli(tmp):
    rc, out, err = run_cli("--help")
    check("CLI --help exits 0", rc == 0 and "--study-dir" in out)
    d = write_study(tmp / "cli", "export", "CFR")
    rc, out, err = run_cli("--study-dir", str(d))
    check("CLI builds a study (exit 0)", rc == 0 and (d / "financial.json").exists() and (d / "financial_model.xlsx").exists(), err[-400:])
    check("CLI prints a per-lane table with missing keys", "IND" in out and "MISSING" not in out and "origin_inland" in out, out[-600:])
    doc = json.loads((d / "financial.json").read_text(encoding="utf-8"))
    check("financial.json has the contract keys", all(k in doc for k in ("schema_version", "perspective", "currency", "container_type", "units_per_container",
                                                                    "unit", "incoterm", "generated_at", "lanes", "portfolio", "inputs_echo")))
    lane_keys = ("status", "missing", "buyer_side_missing", "price_per_unit", "price_basis", "revenue", "user_cost", "margin", "margin_pct",
                 "buyer_landed", "buyer_landed_per_unit", "benchmark_unit_value", "price_gap_pct", "waterfall", "freight_scenarios", "sensitivity",
                 "fx_sensitivity", "duty_sensitivity", "break_even_containers_per_year", "cash_cycle_days", "cash_tied_per_container",
                 "peak_working_capital", "planned_containers_per_year", "transit_days_base")
    check("every lane carries the contract keys", all(all(k in L for k in lane_keys) for L in doc["lanes"].values()))
    check("absent lane (IND) is incomplete, not skipped", doc["lanes"]["IND"]["status"] == "incomplete" and doc["lanes"]["IND"]["margin"] is None
          and "origin_inland" in doc["lanes"]["IND"]["missing"])
    check("inputs_echo has units and tiers", all("unit" in e and "tier" in e and "source" in e and "as_of" in e for e in doc["inputs_echo"]) and len(doc["inputs_echo"]) > 20)
    check("--no-excel writes JSON only", run_cli("--study-dir", str(d), "--no-excel")[0] == 0)
    d2 = write_study(tmp / "trade_only", "export", "FOB")
    inp = json.loads((d2 / "inputs.json").read_text())
    inp["commercial"] = None
    write_json(d2 / "inputs.json", inp)
    rc, out, err = run_cli("--study-dir", str(d2))
    check("trade-only study is blocked (exit 2)", rc == 2 and "BLOCKED" in err)
    d3 = write_study(tmp / "nofreight", "export", "FOB")
    (d3 / "freight.json").unlink()
    rc, out, err = run_cli("--study-dir", str(d3))
    check("missing freight.json is blocked (exit 2)", rc == 2 and "BLOCKED" in err)
    d4 = write_study(tmp / "exwfob", "export", "EXW", basis="FOB")
    rc, out, err = run_cli("--study-dir", str(d4))
    check("EXW sale on FOB cost basis is blocked (exit 2)", rc == 2 and "BLOCKED" in err)
    # demo tariff generator
    d5 = tmp / "demo_tariff"
    d5.mkdir()
    write_json(d5 / "inputs.json", make_inputs("export", "FOB"))
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "make_demo_tariff.py"), "--study-dir", str(d5)], capture_output=True, text=True)
    check("make_demo_tariff blocked without selection.json (exit 2)", p.returncode == 2)
    for persp in ("export", "import"):
        d6 = tmp / f"demo_tariff_{persp}"
        d6.mkdir()
        write_json(d6 / "inputs.json", make_inputs(persp, "FOB"))
        write_json(d6 / "selection.json", {"schema_version": "2.0", "partners": ["SAU", "ARE", "NLD", "USA"]})
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "make_demo_tariff.py"), "--study-dir", str(d6)], capture_output=True, text=True)
        t = json.loads((d6 / "tariff.json").read_text())
        txt = json.dumps(t)
        srcs = re.findall(r'"source": "([^"]*)"', txt)
        urls = re.findall(r'"url": "([^"]*)"', txt)
        verify = [k for k, v in t["lanes"].items() if v["status"] == "verify"]
        zero = [k for k, v in t["lanes"].items() if v["duty_rate_pct"]["value"] == 0]
        pos = [k for k, v in t["lanes"].items() if (v["duty_rate_pct"]["value"] or 0) > 0]
        rec = sum(1 for v in t["lanes"].values() if v["vat_recoverable"])
        check(f"make_demo_tariff ({persp}): synthetic sources, demo URLs", p.returncode == 0 and all(s == "" or s.startswith("DEMO synthetic") for s in srcs)
              and all(u == "https://example.com/demo" for u in urls) and srcs)
        check(f"make_demo_tariff ({persp}): zero-duty, positive-duty and one verify lane with missing duty",
              len(verify) == 1 and t["lanes"][verify[0]]["duty_rate_pct"]["value"] is None and zero and pos and rec >= 2)
        if persp == "import":
            check("make_demo_tariff (import): importer is the home country", all(v["importer"] == "SAU" for v in t["lanes"].values()))
        else:
            check("make_demo_tariff (export): FOB valuation for USA as importer", t["lanes"]["USA"]["valuation_basis"] == "FOB")
        write_json(d6 / "freight.json", {"schema_version": "2.0", "lanes": {}})
        write_json(d6 / "selection.json", {"schema_version": "2.0", "partners": ["SAU", "ARE", "NLD", "USA"]})
        rc, out, err = run_cli("--study-dir", str(d6), "--no-excel")
        check(f"model runs on the demo tariff ({persp}): lanes incomplete without freight, exit 0", rc == 0, err[-300:])


def cell_blank(v):
    return v is None or v == ""


def soffice_convert(files, outdir, profile):
    cmd = ["soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--calc", "--convert-to", "xlsx", "--outdir", str(outdir)] + [str(f) for f in files]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900)


def read_check(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    ws = wb["_check"]
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        out[(row[0], row[1])] = row[2]
    return wb, out


def scan_errors(wb):
    bad = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if c.data_type == "e" or (isinstance(v, str) and (re.match(r"^#(VALUE|REF|NAME|DIV/0|N/A|NUM|NULL)", v) or v.startswith("Err:"))):
                    bad.append(f"{ws.title}!{c.coordinate}={v}")
    return bad


def parity_cases():
    cases = []
    for persp in ("export", "import"):
        for T in ("EXW", "FOB", "CFR", "CIF", "DAP"):
            for valb in ("CIF", "FOB"):
                cases.append(dict(name=f"{persp}-{T}-{valb}", persp=persp, T=T, valb=valb))
    cases += [dict(name="export-FCA-CIF", persp="export", T="FCA", valb="CIF"),
              dict(name="export-FOB-FOBcost", persp="export", T="FOB", valb="CIF", basis="FOB"),
              dict(name="export-CIF-FOBcost-FOBval", persp="export", T="CIF", valb="FOB", basis="FOB"),
              dict(name="export-CFR-requiredprice", persp="export", T="CFR", valb="CIF", price=False),
              dict(name="export-DAP-requiredprice", persp="export", T="DAP", valb="FOB", price=False),
              dict(name="import-FOB-nolocalprice", persp="import", T="FOB", valb="CIF", local=False),
              dict(name="import-CIF-nolocalprice", persp="import", T="CIF", valb="FOB", local=False)]
    return cases


def test_parity(tmp):
    cases = parity_cases()
    prof = tmp / "lo_profile"
    ins = tmp / "recalc_in"
    outs = tmp / "recalc_out"
    ins.mkdir()
    outs.mkdir()
    docs = {}
    for cs in cases:
        d = write_study(tmp / "p" / cs["name"], cs["persp"], cs["T"], cs["valb"], cs.get("basis", "EXW"), cs.get("price", True), cs.get("local", True))
        doc = bfm.run(d, names=NAMES)
        docs[cs["name"]] = doc
        shutil.copy(d / "financial_model.xlsx", ins / f"{cs['name']}.xlsx")
    p = soffice_convert(sorted(ins.glob("*.xlsx")), outs, prof)
    check("LibreOffice recalculated every workbook", p.returncode == 0 and len(list(outs.glob("*.xlsx"))) == len(cases), p.stderr[-300:])
    n_cmp = 0
    for cs in cases:
        f = outs / f"{cs['name']}.xlsx"
        if not f.exists():
            check(f"parity {cs['name']}: workbook recalculated", False)
            continue
        wb, vals = read_check(f)
        doc = docs[cs["name"]]
        bad, nulls_ok, cmp_n = [], True, 0
        for iso, ln in doc["lanes"].items():
            for metric, key in (("revenue", "revenue"), ("user_cost", "user_cost"), ("margin", "margin"), ("buyer_landed", "buyer_landed")):
                jv, xv = ln[key], vals.get((iso, metric))
                cmp_n += 1
                if jv is None:
                    if not cell_blank(xv):
                        bad.append(f"{iso}.{metric}: json null, excel {xv!r}")
                else:
                    if cell_blank(xv) or not isinstance(xv, (int, float)) or abs(xv - jv) > 0.01:
                        bad.append(f"{iso}.{metric}: json {jv}, excel {xv!r}")
        pf = doc["portfolio"]
        for metric, jv in (("weighted_margin", pf["weighted_margin"]), ("payback_month", pf["payback_month"])):
            xv = vals.get(("PORTFOLIO", metric))
            cmp_n += 1
            if jv is None:
                if not cell_blank(xv):
                    bad.append(f"portfolio.{metric}: json null, excel {xv!r}")
            elif cell_blank(xv) or abs(xv - jv) > 0.01:
                bad.append(f"portfolio.{metric}: json {jv}, excel {xv!r}")
        n_cmp += cmp_n
        errs = scan_errors(wb)
        some_value = any(ln["margin"] is not None or ln["buyer_landed"] is not None for ln in doc["lanes"].values())
        check(f"parity {cs['name']}: _check equals financial.json ({cmp_n} values)", not bad and some_value, "; ".join(bad[:4]))
        check(f"parity {cs['name']}: no error values in any cell", not errs, "; ".join(errs[:4]))
        # blank, never zero, for the incomplete lane
        wsL = wb["Lanes"]
        hdr = {c.value: c.column for c in wsL[4] if c.value}
        mcol = [col for h, col in hdr.items() if str(h).startswith("Margin /")][0]
        irow = 5 + list(doc["lanes"]).index("IND")
        ind_null = doc["lanes"]["IND"]["margin"] is None
        xv = wsL.cell(row=irow, column=mcol).value
        check(f"parity {cs['name']}: IND lane (absent from freight.json) is {'blank, not zero' if ind_null else 'modelled without freight inputs'}",
              cell_blank(xv) if ind_null else isinstance(xv, (int, float)))
    print(f"      parity: {len(cases)} workbooks, {n_cmp} values compared")
    return docs, outs


def test_apostrophe_and_dropdown(tmp):
    from openpyxl import load_workbook
    wb = load_workbook(tmp / "p" / "export-CFR-CIF" / "financial_model.xlsx")
    names = [wb["Lanes"].cell(row=5 + i, column=1).value for i in range(4)]
    check("lane names keep apostrophe and comma", "Côte d'Ivoire" in names and "China, Hong Kong SAR" in names, str(names))
    dv = wb["Lane detail"].data_validations.dataValidation
    check("Lane detail dropdown is a range-list validation", len(dv) == 1 and dv[0].type == "list" and "Lanes!$A$5:$A$8" in dv[0].formula1)
    check("sheet names are fixed", wb.sheetnames == ["Cover", "Assumptions", "Lanes", "Lane detail", "Scenarios", "Plan & payback",
                                                   "Working capital", "Provenance", "Limitations", "_check"] and wb["_check"].sheet_state == "hidden")


def test_lane_detail_lookup(tmp, outs_prefix="lanedetail"):
    """Pick the apostrophe lane in Lane detail, recalc, and compare the waterfall to financial.json."""
    from openpyxl import load_workbook
    cases = [("export-CIF-FOB", "Côte d'Ivoire", "CIV"), ("import-FOB-CIF", "China, Hong Kong SAR", "HKG")]
    ins, outs = tmp / "ld_in", tmp / "ld_out"
    ins.mkdir()
    outs.mkdir()
    for nm, lane_name, iso in cases:
        wb = load_workbook(tmp / "p" / nm / "financial_model.xlsx")
        wb["Lane detail"]["B3"] = lane_name
        wb.save(ins / f"{nm}.xlsx")
    soffice_convert(sorted(ins.glob("*.xlsx")), outs, tmp / "lo_profile")
    for nm, lane_name, iso in cases:
        doc = json.loads((tmp / "p" / nm / "financial.json").read_text(encoding="utf-8"))
        wb = load_workbook(outs / f"{nm}.xlsx", data_only=True)
        ws = wb["Lane detail"]
        wf = doc["lanes"][iso]["waterfall"]
        ok = ws["B4"].value == 1 + list(doc["lanes"]).index(iso)
        for i, w in enumerate(wf):
            xv = ws.cell(row=7 + i, column=2).value
            if w["amount"] is None:
                ok = ok and cell_blank(xv)
            else:
                ok = ok and isinstance(xv, (int, float)) and abs(xv - w["amount"]) <= 0.01
            ok = ok and ws.cell(row=7 + i, column=4).value == w["tier"]
        check(f"Lane detail lookup survives {lane_name!r} ({nm})", ok)


def test_live_switches(tmp):
    from openpyxl import load_workbook
    cases = [("export-CIF-FOB", "export"), ("export-DAP-requiredprice", "export"), ("import-FOB-CIF", "import"), ("import-CIF-FOB", "import")]
    ins, outs = tmp / "sw_in", tmp / "sw_out"
    ins.mkdir()
    outs.mkdir()
    shocks = dict(freight_scenario="high", price_shock_pct=10, cost_shock_pct=5, fx_shock_pct=10)
    for nm, _ in cases:
        wb = load_workbook(tmp / "p" / nm / "financial_model.xlsx")

        def cell(name):
            ref = wb.defined_names[name].attr_text
            sh, addr = ref.split("!")
            return wb[sh.strip("'")][addr.replace("$", "")]
        cell("Sw_freight").value = "high"
        cell("Sw_price_pct").value = 10
        cell("Sw_cost_pct").value = 5
        cell("Sw_fx_pct").value = 10
        wb.save(ins / f"{nm}.xlsx")
    p = soffice_convert(sorted(ins.glob("*.xlsx")), outs, tmp / "lo_profile")
    check("live-switch workbooks recalculated", p.returncode == 0 and len(list(outs.glob("*.xlsx"))) == len(cases))
    for nm, persp in cases:
        d = tmp / "p" / nm
        inp = json.loads((d / "inputs.json").read_text(encoding="utf-8"))
        fr = json.loads((d / "freight.json").read_text(encoding="utf-8"))
        tf = json.loads((d / "tariff.json").read_text(encoding="utf-8"))
        wb, vals = read_check(outs / f"{nm}.xlsx")
        doc = json.loads((d / "financial.json").read_text(encoding="utf-8"))
        bad, changed = [], 0
        for iso in PARTNERS:
            li = mc.build_lane_inputs(inp, fr["lanes"].get(iso), tf["lanes"].get(iso), None, iso)
            res = mc.compute_lane(li, **shocks)
            for metric, key in (("revenue", "revenue"), ("user_cost", "user_cost"), ("margin", "margin"), ("buyer_landed", "buyer_landed")):
                ev, xv = res[key], vals.get((iso, metric))
                if ev is None:
                    if not cell_blank(xv):
                        bad.append(f"{iso}.{metric}: expected null got {xv!r}")
                elif cell_blank(xv) or abs(xv - ev) > 0.01:
                    bad.append(f"{iso}.{metric}: expected {ev} got {xv!r}")
            if res["margin"] is not None and doc["lanes"][iso]["margin"] is not None and abs(res["margin"] - doc["lanes"][iso]["margin"]) > 1:
                changed += 1
        check(f"live switches {nm}: Excel equals model_core with price +10, cost +5, FX +10, freight high", not bad and changed > 0, "; ".join(bad[:4]) + f" changed={changed}")


def test_guard():
    pat = re.compile(r"\.get\(\s*[^()]*?,\s*-?\d+(\.\d+)?\s*\)")
    for fn in ("build_financial_model.py", "model_core.py"):
        src = (ROOT / "scripts" / fn).read_text(encoding="utf-8")
        hits = [m.group(0) for m in pat.finditer(src) if not re.search(r",\s*-?0(\.0+)?\s*\)$", m.group(0))]
        check(f"{fn}: no numeric .get(key, number) fallbacks", not hits, str(hits))
        check(f"{fn}: no `or <number>` fallbacks on economic values", not re.search(r"\bor\s+-?[1-9]\d*(\.\d+)?\b", src.replace("or 0", "")), "")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="fin_test_"))
    try:
        test_hand_cases()
        test_identities()
        test_missing()
        test_guard()
        test_cli(tmp)
        test_parity(tmp)
        test_apostrophe_and_dropdown(tmp)
        test_lane_detail_lookup(tmp)
        test_live_switches(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        print("FAILED: " + "; ".join(FAILED[:20]))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
