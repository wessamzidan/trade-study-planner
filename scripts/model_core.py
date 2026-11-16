"""Financial model core for the Trade Study Planner (schema 2.0).

Pure functions, no file I/O.  This module is the single source of truth for
contracts.md section 8.1: the Excel formulas written by build_financial_model.py
and the dashboard's JS port must reproduce it.  Standard library only (plus common.py).

Notation (per lane, per container, in model currency)
    U   units per container            r    insurance_rate_pct / 100
    OI OD OT  origin inland / docs / THC          FR  freight (scenario low|base|high)
    DT  destination THC   IC import clearance     DI  destination inland
    V   invoice value = price x U / fx_price (export) or purchase price x U / fx_cost (import)
    INS insurance         DU duty                 VATnr non-recoverable import VAT

Who pays, by incoterm T (seller side): EXW none | FCA/FOB OI OD OT | CFR + FR | CIF + INS | DAP + DT DI.
FCA is treated as FOB.  DDP is not modelled.

Invoice-based values (contracts 8.1):
    T        FOBv                          CIFv
    EXW      V + OI + OD + OT              (FOBv + FR)(1 + r)
    FOB      V                             (FOBv + FR)(1 + r)
    CFR      V - FR                        V (1 + r)
    CIF      V/(1 + r) - FR                V
    DAP      CIFv/(1 + r) - FR             V - DT - DI
    CV  = CIFv or FOBv per valuation_basis       DU = (duty + other duties)/100 x CV
    VATnr = import_vat/100 x (CIFv + DU) when VAT is not recoverable, else 0
    buyer landed = CIFv + DT + IC + DU + DI + VATnr

Export (user sells): goods = cost x U / fx_cost; FOB_cost = goods + OI + OD + OT (cost.basis EXW) or goods
(cost.basis FOB); seller_cost = goods (T=EXW) or FOB_cost, + FR (CFR CIF DAP) + INS_seller (CIF DAP)
+ DT + DI (DAP), with INS_seller = r (FOB_cost + FR).  revenue = V, margin = revenue - seller_cost.
No price: required price = seller_cost / (1 - target) / U x fx_price (price_basis "required_at_target_margin").
Import (user buys): landed = buyer landed; revenue = local price x U / fx_price; margin = revenue - landed.
No local price: breakeven / required local price are reported, margin is null.

Missing data: a missing input is None.  Nothing is ever defaulted: every result that depends on a
missing input is None and the lane is "incomplete" with the missing keys listed.
Percent inputs are in percent units (4.5 = 4.5 %) and converted once, here.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

FREIGHT_SCENARIOS = ("low", "base", "high")
PRICE_PCTS = [-15, -10, -5, 0, 5, 10, 15]
COST_PCTS = [-20, -10, 0, 10, 20]
FX_PCTS = [-20, -10, 0, 10, 20]
DUTY_PPS = [-5, 0, 5]
WF_KEYS = ("goods", "OI", "OD", "OT", "FR", "INS", "DT", "IC", "DU", "DI", "VAT")
CHARGE_KEY = {"OI": "origin_inland", "OD": "origin_docs", "OT": "origin_thc", "DT": "destination_thc",
              "IC": "import_clearance", "DI": "destination_inland"}
SELLER_FREIGHT = ("CFR", "CIF", "DAP")
SELLER_INSURANCE = ("CIF", "DAP")
SUPPORTED_INCOTERMS = ("EXW", "FOB", "CFR", "CIF", "DAP")   # FCA is mapped to FOB
KG_UNIT = "kg"      # a unit named "kg" has net_kg_per_unit of exactly 1 (a definition, not an estimate)

# keys needed to compute CIFv / FOBv, by incoterm (see the table in the module docstring)
CIF_NEEDS = {"EXW": ["origin_inland", "origin_docs", "origin_thc", "freight", "insurance_rate_pct"],
             "FOB": ["freight", "insurance_rate_pct"],
             "CFR": ["insurance_rate_pct"],
             "CIF": [],
             "DAP": ["destination_thc", "destination_inland"]}
FOB_NEEDS = {"EXW": ["origin_inland", "origin_docs", "origin_thc"],
             "FOB": [],
             "CFR": ["freight"],
             "CIF": ["insurance_rate_pct", "freight"],
             "DAP": ["destination_thc", "destination_inland", "insurance_rate_pct", "freight"]}

LABELS = {
    "units_per_container": "Units per container", "net_kg_per_unit": "Net kg per unit",
    "fx_cost_per_model": "FX: cost currency per model currency", "fx_price_per_model": "FX: price currency per model currency",
    "insurance_rate_pct": "Insurance rate", "target_margin_pct": "Target margin",
    "cost_per_unit": "Goods cost per unit", "price_per_unit": "Selling price per unit",
    "purchase_price_per_unit": "Purchase price per unit", "local_price_per_unit": "Local selling price per unit",
    "freight": "Freight per container", "origin_inland": "Origin inland haulage", "origin_docs": "Origin documentation",
    "origin_thc": "Origin terminal handling", "destination_thc": "Destination terminal handling",
    "import_clearance": "Import clearance", "destination_inland": "Destination inland haulage",
    "duty_rate_pct": "Import duty rate applied", "other_duties_pct": "Other duties and charges",
    "valuation_basis": "Customs valuation basis", "import_vat_pct": "Import VAT", "vat_recoverable": "VAT recoverable",
    "credit_days": "Supplier/buyer credit days", "advance_pct": "Advance payment", "customer_credit_days": "Customer credit days",
    "production_lead_days": "Production lead time", "origin_dwell_days": "Origin dwell", "clearance_days": "Clearance time",
    "inventory_days": "Inventory holding", "transit_days_base": "Transit time (base)",
    "planned_containers_per_year": "Planned containers per year", "fixed_annual": "Fixed costs per year",
    "fixed_one_time": "One-time costs",
}

# ------------------------------------------------------------------ small helpers


def _ok(*xs) -> bool:
    return all(x is not None for x in xs)


def _missing(v: dict, keys) -> list:
    out = []
    for k in keys:
        if v.get(k) is None and k not in out:
            out.append(k)
    return out


def _dedupe(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def round_half_up(x):
    """Round to an integer, halves away from zero (same as the spreadsheet ROUND)."""
    if x is None:
        return None
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def weakest_tier(tiers) -> str:
    """Weakest tier of a set: missing > modelled > published > connector > quote > user; 'derived' counts only if alone."""
    ts = [t for t in tiers if t]
    if not ts:
        return "missing"
    if "missing" in ts:
        return "missing"
    real = [t for t in ts if t != "derived"]
    if not real:
        return "derived"
    return max(real, key=lambda t: common.TIERS.index(t))


def _sv_clean(x, unit="", note="") -> dict:
    if isinstance(x, dict):
        d = {"value": common.sv_value(x), "tier": common.sv_tier(x), "source": x.get("source") or "",
             "url": x.get("url"), "as_of": x.get("as_of")}
        if d["value"] is None:
            d["tier"] = "missing"
        elif d["tier"] == "missing":
            d["tier"] = "missing"
            d["value"] = None
    else:
        d = common.sv_missing(note or "not provided")
    d["unit"] = unit
    return d


# ------------------------------------------------------------------ building the lane inputs


def build_lane_inputs(inputs, freight_lane, tariff_lane, analysis_partner_row, partner_iso3) -> dict:
    """Flatten inputs.json + one freight lane + one tariff lane + one analysis partner row into plain numbers.

    Returned dict (flat): numbers or None for every economic input (key names equal the keys reported in
    `missing`), text settings (perspective, incoterm, cost_basis, valuation_basis, vat_recoverable,
    currencies, unit), `_sv` (the cleaned sourced value of every key, with unit) and `_notes`.
    A lane absent from freight.json / tariff.json is passed as None: its inputs are simply missing.
    """
    c = inputs.get("commercial") or {}
    persp = inputs.get("perspective")
    cont = c.get("container") or {}
    cur = c.get("currencies") or {}
    pay = c.get("payment") or {}
    tim = c.get("timing") or {}
    fixed = c.get("fixed_costs") or {}
    vol = c.get("volume") or {}
    unit = c.get("unit") or ""
    model, ccost, cprice = cur.get("model") or "", cur.get("cost") or "", cur.get("price") or ""
    li = {"perspective": persp, "partner": partner_iso3, "unit": unit, "model_currency": model,
          "cost_currency": ccost, "price_currency": cprice, "container_type": cont.get("type"),
          "_sv": {}, "_notes": [], "input_errors": []}
    svs = li["_sv"]

    def put(key, sv_obj, units):
        s = _sv_clean(sv_obj, units)
        svs[key] = s
        li[key] = s["value"]

    put("units_per_container", cont.get("units_per_container"), f"{unit}/container")
    put("net_kg_per_unit", cont.get("net_kg_per_unit"), f"kg/{unit}")
    put("fx_cost_per_model", cur.get("fx_cost_per_model"), f"{ccost} per {model}")
    put("fx_price_per_model", cur.get("fx_price_per_model"), f"{cprice} per {model}")
    put("insurance_rate_pct", c.get("insurance_rate_pct"), "%")
    put("target_margin_pct", c.get("target_margin_pct"), "%")
    if persp == "export":
        put("cost_per_unit", (c.get("cost") or {}).get("per_unit"), f"{ccost}/{unit}")
        put("price_per_unit", common.sv_pick(c.get("price_per_unit"), partner_iso3), f"{cprice}/{unit}")
        raw_t = c.get("sell_incoterm")
        li["cost_basis"] = (c.get("cost") or {}).get("basis")
    else:
        put("purchase_price_per_unit", common.sv_pick(c.get("purchase_price_per_unit"), partner_iso3), f"{ccost}/{unit}")
        put("local_price_per_unit", c.get("local_price_per_unit"), f"{cprice}/{unit}")
        raw_t = c.get("buy_incoterm")
        li["cost_basis"] = None
    li["incoterm_raw"] = raw_t
    t = str(raw_t).strip().upper() if raw_t else None
    li["incoterm"] = "FOB" if t == "FCA" else t
    if li["incoterm"] not in SUPPORTED_INCOTERMS:
        li["incoterm"] = None

    put("credit_days", pay.get("credit_days"), "days")
    put("advance_pct", pay.get("advance_pct"), "%")
    put("customer_credit_days", pay.get("customer_credit_days"), "days")
    put("production_lead_days", tim.get("production_lead_days"), "days")
    put("origin_dwell_days", tim.get("origin_dwell_days"), "days")
    put("clearance_days", tim.get("clearance_days"), "days")
    put("inventory_days", tim.get("inventory_days"), "days")
    put("fixed_annual", fixed.get("annual"), model)
    put("fixed_one_time", fixed.get("one_time"), model)
    put("planned_containers_per_year", common.sv_pick(vol.get("planned_containers_per_year"), partner_iso3), "containers/yr")

    # freight (freight.json values are USD per container; see _notes when the model currency differs)
    fl = freight_lane or {}
    fusd = fl.get("freight_usd") or {}
    fsv = fl.get("freight") if isinstance(fl.get("freight"), dict) else {}
    usable = bool(fl) and not fl.get("coverage_gap") and (model == "USD")
    if fl and model != "USD":
        li["_notes"].append("freight.json is in USD; the model currency is not USD and no conversion rate is defined, so freight is treated as missing")
    for scn in FREIGHT_SCENARIOS:
        val = fusd.get(scn) if usable else None
        li["freight_" + scn] = None if val is None else float(val)
    base_sv = dict(fsv) if fsv else {}
    base_sv["value"] = li["freight_base"]
    if li["freight_base"] is None:
        base_sv["tier"] = "missing"
    elif not base_sv.get("tier") or base_sv.get("tier") == "missing":
        base_sv["tier"] = "modelled"
    svs["freight"] = _sv_clean(base_sv, "USD/container", "no freight for this lane")
    li["freight"] = li["freight_base"]
    lc = fl.get("local_charges") or {}
    for key in common.CHARGE_KEYS:
        put(key, lc.get(key), f"{model}/container")
    td = fl.get("transit_days") or {}
    tsv = {"value": td.get("base"), "tier": td.get("tier") or "missing", "source": "freight.json transit time (base)",
           "url": None, "as_of": None}
    if td.get("base") is None:
        tsv = None
    put("transit_days_base", tsv, "days")

    # tariff
    tl = tariff_lane or {}
    put("duty_rate_pct", tl.get("duty_rate_pct"), "%")
    put("other_duties_pct", tl.get("other_duties_pct"), "%")
    put("import_vat_pct", tl.get("import_vat_pct"), "%")
    status_tier = "published" if tl.get("status") == "sourced" else "modelled"
    vb = tl.get("valuation_basis")
    vb = str(vb).strip().upper() if vb else None
    li["valuation_basis"] = vb if vb in ("CIF", "FOB") else None
    svs["valuation_basis"] = _sv_clean({"value": 0, "tier": status_tier, "source": f"tariff.json valuation_basis ({tl.get('status')})"} if li["valuation_basis"] else None, "", "valuation basis not given")
    svs["valuation_basis"]["value"] = li["valuation_basis"]
    rec = tl.get("vat_recoverable")
    li["vat_recoverable"] = rec if isinstance(rec, bool) else None
    svs["vat_recoverable"] = _sv_clean({"value": 0, "tier": status_tier, "source": f"tariff.json vat_recoverable ({tl.get('status')})"} if li["vat_recoverable"] is not None else None, "", "VAT treatment not given")
    svs["vat_recoverable"]["value"] = li["vat_recoverable"]

    # benchmark (average unit value paid in the partner market, or by home for that supplier)
    row = analysis_partner_row or {}
    bk = "partner_unit_value_usd_per_kg" if persp == "export" else "home_unit_value_usd_per_kg"
    bv = row.get(bk)
    li["benchmark_uv_usd_per_kg"] = float(bv) if isinstance(bv, (int, float)) and not isinstance(bv, bool) else None
    return li


def input_errors(li) -> list:
    """Input combinations that cannot be modelled (reported as lane status 'error')."""
    errs = list(li.get("input_errors") or [])
    if li.get("incoterm") is None:
        errs.append(f"incoterm {li.get('incoterm_raw')!r} is not supported (use EXW FCA FOB CFR CIF DAP; DDP is not modelled)")
    if li.get("perspective") == "export":
        if li.get("cost_basis") not in ("EXW", "FOB"):
            errs.append(f"cost.basis must be EXW or FOB, got {li.get('cost_basis')!r}")
        if li.get("incoterm") == "EXW" and li.get("cost_basis") == "FOB":
            errs.append("input error: selling EXW on a FOB cost basis (the cost already includes origin charges)")
    elif li.get("perspective") != "import":
        errs.append(f"perspective must be export or import, got {li.get('perspective')!r}")
    tm = li.get("target_margin_pct")
    if tm is not None and tm >= 100:
        errs.append("target_margin_pct must be below 100")
    return errs


# ------------------------------------------------------------------ the chain


def _landed_needs(T, basis, rec) -> list:
    need = list(CIF_NEEDS[T])
    if basis == "FOB":
        need += FOB_NEEDS[T]
    need += ["destination_thc", "import_clearance", "destination_inland", "duty_rate_pct", "other_duties_pct", "valuation_basis"]
    need.append("vat_recoverable")
    if rec is False:
        need.append("import_vat_pct")
    return _dedupe(need)


def _chain(T, V, v, duty_shock_pp=0.0) -> dict:
    """FOBv, CIFv, CV, DU, VATnr, buyer landed and buyer-paid insurance from the invoice value V (contracts 8.1)."""
    out = {"FOBv": None, "CIFv": None, "CV": None, "DU": None, "VATnr": None, "landed": None, "INS_buyer": None}
    if V is None:
        return out
    OI, OD, OT, FR = v["origin_inland"], v["origin_docs"], v["origin_thc"], v["freight"]
    DT, DI, IC = v["destination_thc"], v["destination_inland"], v["import_clearance"]
    r = None if v["insurance_rate_pct"] is None else v["insurance_rate_pct"] / 100.0
    fob = cif = None
    if T == "EXW":
        if _ok(OI, OD, OT):
            fob = V + OI + OD + OT
        if _ok(fob, FR, r):
            cif = (fob + FR) * (1 + r)
    elif T == "FOB":
        fob = V
        if _ok(FR, r):
            cif = (V + FR) * (1 + r)
    elif T == "CFR":
        if FR is not None:
            fob = V - FR
        if r is not None:
            cif = V * (1 + r)
    elif T == "CIF":
        cif = V
        if _ok(r, FR):
            fob = V / (1 + r) - FR
    elif T == "DAP":
        if _ok(DT, DI):
            cif = V - DT - DI
            if _ok(r, FR):
                fob = cif / (1 + r) - FR
    out["FOBv"], out["CIFv"] = fob, cif
    basis = v["valuation_basis"]
    cv = cif if basis == "CIF" else fob if basis == "FOB" else None
    out["CV"] = cv
    duty, other = v["duty_rate_pct"], v["other_duties_pct"]
    du = None
    if _ok(cv, duty, other):
        du = (max(0.0, duty + duty_shock_pp) + other) / 100.0 * cv
    out["DU"] = du
    rec = v["vat_recoverable"]
    vat = None
    if rec is True and _ok(cif, du):
        vat = 0.0
    elif rec is False and _ok(cif, du, v["import_vat_pct"]):
        vat = v["import_vat_pct"] / 100.0 * (cif + du)
    out["VATnr"] = vat
    if _ok(cif, DT, IC, du, DI, vat):
        out["landed"] = cif + DT + IC + du + DI + vat
    # insurance paid by the buyer (embedded in the invoice for CIF / DAP)
    if T == "EXW" and _ok(r, OI, OD, OT, FR):
        out["INS_buyer"] = r * (V + OI + OD + OT + FR)
    elif T == "FOB" and _ok(r, FR):
        out["INS_buyer"] = r * (V + FR)
    elif T == "CFR" and r is not None:
        out["INS_buyer"] = r * V
    elif T in ("CIF", "DAP"):
        out["INS_buyer"] = 0.0
    return out


def _vals(li, scn) -> dict:
    """The effective numeric inputs for one freight scenario."""
    keys = ("units_per_container", "net_kg_per_unit", "fx_cost_per_model", "fx_price_per_model", "insurance_rate_pct",
            "target_margin_pct", "cost_per_unit", "price_per_unit", "purchase_price_per_unit", "local_price_per_unit",
            "origin_inland", "origin_docs", "origin_thc", "destination_thc", "import_clearance", "destination_inland",
            "duty_rate_pct", "other_duties_pct", "valuation_basis", "import_vat_pct", "vat_recoverable")
    v = {k: li.get(k) for k in keys}
    v["freight"] = li.get("freight_" + scn)
    return v


def _tier(li, *keys) -> str:
    sv = li.get("_sv") or {}
    return weakest_tier([(sv.get(k) or {}).get("tier") or "missing" for k in keys])


def _benchmark(li, per_unit_value):
    """price_gap_pct = (our per-unit figure / benchmark - 1) x 100; only when the model currency is USD."""
    uv = li.get("benchmark_uv_usd_per_kg")
    net = li.get("net_kg_per_unit")
    if net is None and str(li.get("unit") or "").strip().lower() == KG_UNIT:
        net = 1.0
    if li.get("model_currency") != "USD" or uv is None or net is None or per_unit_value is None:
        return None, None
    bench = uv * net
    if bench <= 0:
        return None, None
    return bench, (per_unit_value / bench - 1.0) * 100.0


# ------------------------------------------------------------------ compute_lane


def compute_lane(li, *, freight_scenario="base", price_shock_pct=0, cost_shock_pct=0, fx_shock_pct=0, duty_shock_pp=0) -> dict:
    """One lane, one container, one scenario.  See the module docstring for every formula.

    Shocks: price_shock_pct scales the selling price (export) / local price (import); cost_shock_pct scales the goods
    cost (export cost per unit) / purchase price (import); fx_shock_pct multiplies fx_cost_per_model (export) /
    fx_price_per_model (import) by (1 + x/100), i.e. +10 means that currency weakens by 10 % against the model
    currency; duty_shock_pp adds percentage points to the duty rate (floored at 0).
    """
    errs = input_errors(li)
    base = {"status": "incomplete", "missing": [], "buyer_side_missing": [], "errors": errs, "notes": list(li.get("_notes") or []),
            "perspective": li.get("perspective"), "incoterm": li.get("incoterm"), "valuation_basis": li.get("valuation_basis"),
            "price_per_unit": None, "price_basis": None, "required_price_per_unit": None,
            "revenue": None, "user_cost": None, "margin": None, "margin_pct": None,
            "buyer_landed": None, "buyer_landed_per_unit": None, "benchmark_unit_value": None, "price_gap_pct": None,
            "FOBv": None, "CIFv": None, "CV": None, "DU": None, "INS": None, "VATnr": None, "invoice_value": None,
            "waterfall": []}
    if errs:
        base["status"] = "error"
        return base
    if freight_scenario not in FREIGHT_SCENARIOS:
        raise ValueError(f"freight_scenario must be one of {FREIGHT_SCENARIOS}")
    v = _vals(li, freight_scenario)
    res = _export(li, v, price_shock_pct, cost_shock_pct, fx_shock_pct, duty_shock_pp, base) if li["perspective"] == "export" \
        else _import(li, v, price_shock_pct, cost_shock_pct, fx_shock_pct, duty_shock_pp, base)
    res["status"] = "modelled" if not res["missing"] else "incomplete"
    return res


def _pack_waterfall(li, T, persp, c, v) -> list:
    """Waterfall rows {key,label,amount,side,tier}.  Export: seller-side rows sum to user_cost and V + buyer-side rows
    to buyer_landed.  Import: invoice (goods) + buyer-side rows = landed cost; rows inside the invoice show 0."""
    rows = []
    basis = li.get("cost_basis")

    def add(key, label, amount, side, tier):
        rows.append({"key": key, "label": label, "amount": amount, "side": side, "tier": tier})

    if persp == "export":
        add("goods", f"Goods at {basis} cost", c.get("goods"), "seller", _tier(li, "cost_per_unit", "fx_cost_per_model", "units_per_container"))
        names = {"OI": "Origin inland", "OD": "Origin documents", "OT": "Origin THC"}
        for k in ("OI", "OD", "OT"):
            ck = CHARGE_KEY[k]
            if T == "EXW":
                add(k, names[k] + " (buyer)", v[ck], "buyer", _tier(li, ck))
            elif basis == "EXW":
                add(k, names[k], v[ck], "seller", _tier(li, ck))
            else:
                add(k, names[k] + " (included in FOB cost)", 0.0, "seller", "derived")
        add("FR", "Freight", v["freight"], "seller" if T in SELLER_FREIGHT else "buyer", _tier(li, "freight"))
        if T in SELLER_INSURANCE:
            add("INS", "Insurance (seller)", c.get("INS_seller"), "seller", _tier(li, "insurance_rate_pct"))
        else:
            add("INS", "Insurance (buyer)", c.get("INS_buyer"), "buyer", _tier(li, "insurance_rate_pct"))
        add("DT", "Destination THC", v["destination_thc"], "seller" if T == "DAP" else "buyer", _tier(li, "destination_thc"))
        add("IC", "Import clearance", v["import_clearance"], "buyer", _tier(li, "import_clearance"))
        add("DU", "Import duty", c.get("DU"), "buyer", _tier(li, "duty_rate_pct", "other_duties_pct", "valuation_basis"))
        add("DI", "Destination inland", v["destination_inland"], "seller" if T == "DAP" else "buyer", _tier(li, "destination_inland"))
        add("VAT", "Non-recoverable VAT", c.get("VATnr"), "buyer", _tier(li, "vat_recoverable", "import_vat_pct") if li.get("vat_recoverable") is False else _tier(li, "vat_recoverable"))
    else:
        add("goods", f"Supplier invoice ({T})", c.get("V"), "seller", _tier(li, "purchase_price_per_unit", "fx_cost_per_model", "units_per_container"))
        names = {"OI": "Origin inland", "OD": "Origin documents", "OT": "Origin THC"}
        for k in ("OI", "OD", "OT"):
            ck = CHARGE_KEY[k]
            if T == "EXW":
                add(k, names[k], v[ck], "buyer", _tier(li, ck))
            else:
                add(k, names[k] + " (included in invoice)", 0.0, "seller", "derived")
        if T in ("EXW", "FOB"):
            add("FR", "Freight", v["freight"], "buyer", _tier(li, "freight"))
        else:
            add("FR", "Freight (included in invoice)", 0.0, "seller", "derived")
        if T in SELLER_INSURANCE:
            add("INS", "Insurance (included in invoice)", 0.0, "seller", "derived")
        else:
            add("INS", "Insurance", c.get("INS_buyer"), "buyer", _tier(li, "insurance_rate_pct"))
        if T == "DAP":
            add("DT", "Destination THC (included in invoice)", 0.0, "seller", "derived")
        else:
            add("DT", "Destination THC", v["destination_thc"], "buyer", _tier(li, "destination_thc"))
        add("IC", "Import clearance", v["import_clearance"], "buyer", _tier(li, "import_clearance"))
        add("DU", "Import duty", c.get("DU"), "buyer", _tier(li, "duty_rate_pct", "other_duties_pct", "valuation_basis"))
        if T == "DAP":
            add("DI", "Destination inland (included in invoice)", 0.0, "seller", "derived")
        else:
            add("DI", "Destination inland", v["destination_inland"], "buyer", _tier(li, "destination_inland"))
        add("VAT", "Non-recoverable VAT", c.get("VATnr"), "buyer", _tier(li, "vat_recoverable", "import_vat_pct") if li.get("vat_recoverable") is False else _tier(li, "vat_recoverable"))
    return rows


def _export(li, v, ps, cs, fs, dpp, res) -> dict:
    T, basis = li["incoterm"], li["cost_basis"]
    U, fxc, fxp = v["units_per_container"], v["fx_cost_per_model"], v["fx_price_per_model"]
    fxc_e = fxc * (1 + fs / 100.0) if fxc is not None else None
    # --- user (seller) side completeness
    cost_need = ["units_per_container", "cost_per_unit", "fx_cost_per_model"]
    if T != "EXW" and basis == "EXW":
        cost_need += ["origin_inland", "origin_docs", "origin_thc"]
    if T in SELLER_FREIGHT:
        cost_need.append("freight")
    if T in SELLER_INSURANCE:
        cost_need.append("insurance_rate_pct")
    if T == "DAP":
        cost_need += ["destination_thc", "destination_inland"]
    price_need = ["fx_price_per_model"]
    if v["price_per_unit"] is None:
        price_need += ["price_per_unit", "target_margin_pct"] if v["target_margin_pct"] is None else []
    missing = _dedupe(_missing(v, cost_need) + _missing(v, price_need))
    res["missing"] = missing

    goods = fob_cost = None
    if _ok(v["cost_per_unit"], U, fxc_e):
        goods = v["cost_per_unit"] * (1 + cs / 100.0) * U / fxc_e
    if goods is not None:
        if basis == "FOB":
            fob_cost = goods
        elif _ok(v["origin_inland"], v["origin_docs"], v["origin_thc"]):
            fob_cost = goods + v["origin_inland"] + v["origin_docs"] + v["origin_thc"]
    FR, r = v["freight"], (None if v["insurance_rate_pct"] is None else v["insurance_rate_pct"] / 100.0)
    ins_seller = None
    if T in SELLER_INSURANCE and _ok(fob_cost, FR, r):
        ins_seller = r * (fob_cost + FR)
    seller = None
    if T == "EXW":
        seller = goods
    elif T == "FOB":
        seller = fob_cost
    elif T == "CFR":
        seller = fob_cost + FR if _ok(fob_cost, FR) else None
    elif T == "CIF":
        seller = fob_cost + FR + ins_seller if _ok(fob_cost, FR, ins_seller) else None
    elif T == "DAP":
        dt, di = v["destination_thc"], v["destination_inland"]
        seller = fob_cost + FR + ins_seller + dt + di if _ok(fob_cost, FR, ins_seller, dt, di) else None

    # --- price
    price, required, basis_p = v["price_per_unit"], None, None
    tm = v["target_margin_pct"]
    if _ok(seller, tm, fxp, U):
        required = seller / (1 - tm / 100.0) / U * fxp
    if price is not None:
        used, basis_p = price, "user"
    elif required is not None:
        used, basis_p = required, "required_at_target_margin"
    else:
        used = None
        if tm is not None and price is None:
            basis_p = "required_at_target_margin"
    price_eff = used * (1 + ps / 100.0) if used is not None else None
    V = price_eff * U / fxp if _ok(price_eff, U, fxp) else None
    res.update({"price_per_unit": price_eff, "price_basis": basis_p, "required_price_per_unit": required, "invoice_value": V})

    # --- buyer side (own inputs only; gaps never block the seller margin)
    ch = _chain(T, V, v, dpp)
    rec = v["vat_recoverable"]
    buyer_need = _landed_needs(T, v["valuation_basis"], rec)
    bmiss = _missing(v, buyer_need)
    res["buyer_side_missing"] = [k for k in bmiss if k not in missing]
    res.update({"FOBv": ch["FOBv"], "CIFv": ch["CIFv"], "CV": ch["CV"], "DU": ch["DU"], "VATnr": ch["VATnr"],
                "buyer_landed": ch["landed"]})
    if ch["landed"] is not None and U:
        res["buyer_landed_per_unit"] = ch["landed"] / U
        bench, gap = _benchmark(li, res["buyer_landed_per_unit"])
        res["benchmark_unit_value"], res["price_gap_pct"] = bench, gap
    # --- user result
    if not missing:
        res["INS"] = ins_seller
        res["user_cost"] = seller
        res["revenue"] = V
        if V is not None and seller is not None:
            res["margin"] = V - seller
            res["margin_pct"] = res["margin"] / V * 100.0 if V > 0 else None
    c = {"goods": goods, "INS_seller": ins_seller, "INS_buyer": ch["INS_buyer"], "DU": ch["DU"], "VATnr": ch["VATnr"],
         "FOB_cost": fob_cost}
    res["FOB_cost"] = fob_cost
    res["goods"] = goods
    res["waterfall"] = _pack_waterfall(li, T, "export", c, v)
    return res


def _import(li, v, ps, cs, fs, dpp, res) -> dict:
    T = li["incoterm"]
    U, fxc, fxp = v["units_per_container"], v["fx_cost_per_model"], v["fx_price_per_model"]
    fxp_e = fxp * (1 + fs / 100.0) if fxp is not None else None
    need = ["units_per_container", "purchase_price_per_unit", "fx_cost_per_model", "fx_price_per_model"]
    need += _landed_needs(T, v["valuation_basis"], v["vat_recoverable"])
    missing = _missing(v, need)
    res["missing"] = missing
    pp = v["purchase_price_per_unit"]
    V = pp * (1 + cs / 100.0) * U / fxc if _ok(pp, U, fxc) else None
    ch = _chain(T, V, v, dpp)
    landed = ch["landed"]
    res.update({"invoice_value": V, "FOBv": ch["FOBv"], "CIFv": ch["CIFv"], "CV": ch["CV"], "DU": ch["DU"],
                "VATnr": ch["VATnr"], "INS": ch["INS_buyer"]})
    if not missing and landed is not None:
        res["user_cost"] = landed
        res["buyer_landed"] = landed
        res["buyer_landed_per_unit"] = landed / U
        if ch["CIFv"] is not None:
            bench, gap = _benchmark(li, ch["CIFv"] / U)
            res["benchmark_unit_value"], res["price_gap_pct"] = bench, gap
        res["breakeven_local_price_per_unit"] = landed / U * fxp_e
        tm = v["target_margin_pct"]
        res["required_local_price_per_unit"] = landed / (1 - tm / 100.0) / U * fxp_e if tm is not None else None
        lp = v["local_price_per_unit"]
        if lp is not None:
            lp_eff = lp * (1 + ps / 100.0)
            rev = lp_eff * U / fxp_e
            res.update({"price_per_unit": lp_eff, "price_basis": "user", "revenue": rev, "margin": rev - landed,
                        "margin_pct": (rev - landed) / rev * 100.0 if rev > 0 else None})
        else:
            res["price_basis"] = "required_at_target_margin"
            res["notes"].append("no local selling price: break-even and required local price are reported, margin is not")
    else:
        res.setdefault("breakeven_local_price_per_unit", None)
        res.setdefault("required_local_price_per_unit", None)
    res.setdefault("breakeven_local_price_per_unit", None)
    res.setdefault("required_local_price_per_unit", None)
    c = {"V": V, "INS_buyer": ch["INS_buyer"], "DU": ch["DU"], "VATnr": ch["VATnr"]}
    res["waterfall"] = _pack_waterfall(li, T, "import", c, v)
    return res


# ------------------------------------------------------------------ break-even, cash cycle, summary


def break_even(margin, fixed_annual):
    """Annual fixed cost / margin per container; None if margin <= 0 or either is missing."""
    if margin is None or fixed_annual is None or margin <= 0:
        return None
    return fixed_annual / margin


def cash_cycle(li):
    """(days, missing_keys).  Export: (production_lead + origin_dwell + credit_days) x (1 - advance).
    Import: transit_base + clearance + inventory + customer_credit - credit_days x (1 - advance) + production_lead x advance."""
    if li["perspective"] == "export":
        need = ["production_lead_days", "origin_dwell_days", "credit_days", "advance_pct"]
    else:
        need = ["transit_days_base", "clearance_days", "inventory_days", "customer_credit_days", "credit_days", "advance_pct",
                "production_lead_days"]
    miss = _missing(li, need)
    if miss:
        return None, miss
    adv = li["advance_pct"] / 100.0
    if li["perspective"] == "export":
        return (li["production_lead_days"] + li["origin_dwell_days"] + li["credit_days"]) * (1 - adv), []
    return (li["transit_days_base"] + li["clearance_days"] + li["inventory_days"] + li["customer_credit_days"]
            - li["credit_days"] * (1 - adv) + li["production_lead_days"] * adv), []


def _fix_price(li, res):
    """For grids on a required-price lane: hold the base required price fixed so price/cost shocks move the margin."""
    if li["perspective"] == "export" and li.get("price_per_unit") is None and res.get("required_price_per_unit") is not None:
        li2 = dict(li)
        li2["price_per_unit"] = res["required_price_per_unit"]
        return li2
    return li


def lane_summary(li) -> dict:
    """compute_lane at base + scenarios, sensitivity grids, break-even, cash cycle and working capital."""
    res = compute_lane(li)
    res["freight_scenarios"] = {}
    for scn in FREIGHT_SCENARIOS:
        r2 = compute_lane(li, freight_scenario=scn)
        res["freight_scenarios"][scn] = {"margin": r2["margin"], "margin_pct": r2["margin_pct"],
                                         "buyer_landed": r2["buyer_landed"], "user_cost": r2["user_cost"]}
    li_g = _fix_price(li, res)
    grid = []
    for p in PRICE_PCTS:
        grid.append([compute_lane(li_g, price_shock_pct=p, cost_shock_pct=c)["margin"] for c in COST_PCTS])
    res["sensitivity"] = {"price_pct": list(PRICE_PCTS), "cost_pct": list(COST_PCTS), "margin": grid,
                          "layout": "margin[i][j] = price_pct[i] x cost_pct[j], margin per container"}
    res["fx_sensitivity"] = [{"shock_pct": s, "margin": compute_lane(li_g, fx_shock_pct=s)["margin"]} for s in FX_PCTS]
    metric = "buyer_landed" if li["perspective"] == "export" else "margin"
    res["duty_sensitivity_metric"] = metric
    res["duty_sensitivity"] = [{"shock_pp": s, "value": compute_lane(li_g, duty_shock_pp=s)[metric]} for s in DUTY_PPS]
    res["break_even_containers_per_year"] = break_even(res["margin"], li.get("fixed_annual"))
    cyc, cmiss = cash_cycle(li)
    res["cash_cycle_days"] = cyc
    res["cash_cycle_missing"] = cmiss
    res["cash_tied_per_container"] = res["user_cost"]
    plan = li.get("planned_containers_per_year")
    res["planned_containers_per_year"] = plan
    res["transit_days_base"] = li.get("transit_days_base")
    pw = None
    if cyc is not None and plan is not None and res["user_cost"] is not None:
        pw = plan * cyc / 365.0 * res["user_cost"]
    res["peak_working_capital"] = pw
    return res


# ------------------------------------------------------------------ portfolio


def portfolio(lanes, inputs) -> dict:
    """Plan-weighted margin and the 24-month cash table (contracts 8.1 'Payback').

    lanes: {iso3: lane_summary dict}.  Lanes with a margin and planned containers > 0 are included.
    Monthly containers = commercial.volume.monthly_ramp (24 numbers) else planned total / 12 (derived).
    Cash of month m = containers(m - lag) x weighted margin - annual fixed / 12 - one-time (month 1), with
    lag = max(0, round(weighted cash cycle / 30)) months.  payback_month = first month with cumulative >= 0.
    """
    c = inputs.get("commercial") or {}
    fixed = c.get("fixed_costs") or {}
    ann, one = common.sv_value(fixed.get("annual")), common.sv_value(fixed.get("one_time"))
    ramp = (c.get("volume") or {}).get("monthly_ramp")
    if ramp is not None:
        if not isinstance(ramp, list) or len(ramp) != 24 or any(not isinstance(x, (int, float)) or isinstance(x, bool) for x in ramp):
            raise ValueError("volume.monthly_ramp must be null or a list of 24 numbers")
    inc = [(iso, L) for iso, L in lanes.items()
           if L.get("margin") is not None and L.get("planned_containers_per_year") is not None and L["planned_containers_per_year"] > 0]
    out = {"planned_containers_per_year": None, "weighted_margin": None, "annual_margin": None, "annual_fixed": ann,
           "one_time": one, "payback_month": None, "payback_note": "", "monthly": [], "lanes_included": [i for i, _ in inc],
           "weighted_cash_cycle_days": None, "cash_lag_months": None, "monthly_basis": None, "notes": []}
    excluded = [iso for iso, L in lanes.items() if iso not in out["lanes_included"]]
    if excluded:
        out["notes"].append("excluded from the plan (no margin or no planned containers): " + ", ".join(excluded))
    notes = []
    total = sum(L["planned_containers_per_year"] for _, L in inc)
    if inc:
        out["planned_containers_per_year"] = total
        out["annual_margin"] = sum(L["planned_containers_per_year"] * L["margin"] for _, L in inc)
        out["weighted_margin"] = out["annual_margin"] / total
        cycs = [L.get("cash_cycle_days") for _, L in inc]
        if all(x is not None for x in cycs):
            out["weighted_cash_cycle_days"] = sum(L["planned_containers_per_year"] * L["cash_cycle_days"] for _, L in inc) / total
            out["cash_lag_months"] = max(0, round_half_up(out["weighted_cash_cycle_days"] / 30.0))
        else:
            notes.append("cash cycle incomplete for: " + ", ".join(i for i, L in inc if L.get("cash_cycle_days") is None))
    else:
        notes.append("no lane has both a margin and planned containers")
    if ramp is not None:
        cont = [float(x) for x in ramp]
        out["monthly_basis"] = "monthly_ramp"
    elif inc:
        cont = [total / 12.0] * 24
        out["monthly_basis"] = "derived: planned containers per year / 12"
    else:
        cont = [None] * 24
    if ann is None:
        notes.append("annual fixed cost missing")
    if one is None:
        notes.append("one-time cost missing")
    lag = out["cash_lag_months"]
    wm = out["weighted_margin"]
    can = wm is not None and ann is not None and one is not None and lag is not None and all(x is not None for x in cont)
    cum = 0.0
    payback = None
    for m in range(1, 25):
        row = {"month": m, "containers": cont[m - 1], "cash": None, "cumulative": None}
        if can:
            recv = cont[m - lag - 1] if m - lag >= 1 else 0.0
            cash = recv * wm - ann / 12.0 - (one if m == 1 else 0.0)
            cum += cash
            row["cash"], row["cumulative"] = cash, cum
            if payback is None and cum >= 0:
                payback = m
        out["monthly"].append(row)
    if can:
        out["payback_month"] = payback
        if payback is None:
            notes.append("cumulative cash stays negative over the 24-month table")
    out["payback_note"] = "; ".join(notes)
    return out


# ------------------------------------------------------------------ provenance echo


def echo_rows(lane_inputs_by_iso: dict) -> list:
    """inputs_echo: every sourced value used, global ones once (lane null), lane-specific ones per lane."""
    lane_keys = {"price_per_unit", "purchase_price_per_unit", "planned_containers_per_year", "freight", "transit_days_base",
                 "duty_rate_pct", "other_duties_pct", "import_vat_pct", "valuation_basis", "vat_recoverable", *common.CHARGE_KEYS}
    rows, seen = [], set()
    for iso, li in lane_inputs_by_iso.items():
        for key, s in li["_sv"].items():
            lane = iso if key in lane_keys else None
            if lane is None:
                if key in seen:
                    continue
                seen.add(key)
            rows.append({"key": key, "lane": lane, "value": s["value"], "unit": s.get("unit", ""), "tier": s["tier"],
                         "source": s.get("source", ""), "url": s.get("url"), "as_of": s.get("as_of")})
    return rows
